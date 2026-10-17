"""Counterfactual SQLite index experiments on a private, disposable snapshot.

The unit of work is a progress-handler callback with N=1.  SQLite documents this
as an approximate work proxy, not as an exact SQLITE_STMTSTATUS_VM_STEP count;
on recent SQLite versions preparation can also invoke the handler.
"""

from __future__ import annotations

import hashlib
import math
import platform
import re
import sqlite3
import struct
import time
from pathlib import Path
from typing import Any

from .workload import validate_workload


class AnalysisError(ValueError):
    """The analysis could not produce a complete, trustworthy recommendation."""


class _DeadlineExceeded(AnalysisError):
    pass


class _ResourceBudgetExceeded(AnalysisError):
    pass


# Deliberately explicit: connection state, extension loading, random values and
# the date/time family are excluded.  Unknown extension/UDF functions fail closed.
_SAFE_FUNCTIONS = frozenset(
    """abs acos acosh asin asinh atan atan2 atanh avg ceil ceiling char coalesce
    concat concat_ws cos cosh count degrees exp floor format glob group_concat
    hex if ifnull iif instr json json_array json_array_length json_error_position
    json_extract json_group_array json_group_object json_insert json_object
    json_patch json_pretty json_quote json_remove json_replace json_set json_type
    json_valid jsonb jsonb_array jsonb_extract jsonb_group_array jsonb_group_object
    jsonb_insert jsonb_object jsonb_patch jsonb_remove jsonb_replace jsonb_set
    length like likelihood likely ln log log10 log2 lower ltrim max min mod nullif
    octet_length pi pow power printf quote radians replace round rtrim sign sin
    sinh soundex sqrt string_agg substr substring sum tan tanh total trim trunc
    typeof unhex unicode unlikely upper zeroblob -> ->>""".split()
)
_ALLOWED_AUTHORIZER_ACTIONS = frozenset(
    (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE)
)
_MAX_VALUE_BYTES = 16 * 1024 * 1024
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_SCHEMA_GAP = r"(?:\s|/\*[\s\S]*?\*/|--[^\r\n]*(?:\r?\n|$))*"
_VIRTUAL_TABLE = re.compile(
    rf"^{_SCHEMA_GAP}CREATE{_SCHEMA_GAP}VIRTUAL{_SCHEMA_GAP}TABLE\b", re.IGNORECASE
)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _DeadlineExceeded(
            "Overall deadline exceeded; exhaustive search is incomplete and no "
            "recommendation was produced."
        )


def _connection() -> sqlite3.Connection:
    # Statement caching would otherwise permit a stale prepared plan after DDL.
    conn = sqlite3.connect(":memory:", isolation_level=None, cached_statements=0)
    try:
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_VALUE_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
        conn.execute("PRAGMA trusted_schema=OFF")
        if hasattr(conn, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        return conn
    except BaseException:
        conn.close()
        raise


def _copy_database(
    source: sqlite3.Connection, target: sqlite3.Connection, deadline: float
) -> None:
    page_size = source.execute("PRAGMA page_size").fetchone()[0]
    page_count = source.execute("PRAGMA page_count").fetchone()[0]
    if page_size * page_count > _MAX_SNAPSHOT_BYTES:
        raise AnalysisError("The SQLite snapshot exceeds the 256 MiB snapshot budget.")

    def progress(_status: int, _remaining: int, _total: int) -> None:
        _check_deadline(deadline)
        if page_size * _total > _MAX_SNAPSHOT_BYTES:
            raise AnalysisError("The SQLite snapshot exceeds the 256 MiB snapshot budget.")

    _check_deadline(deadline)
    source.backup(target, pages=128, progress=progress, sleep=0.01)
    _check_deadline(deadline)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _catalog(conn: sqlite3.Connection, deadline: float) -> tuple[list[dict], list[dict]]:
    # Existing virtual tables can invoke module code while merely being read.
    # Reject them before workload preparation, even if the workload names another
    # table.  Only inspect the declaration prefix; EQP detail is always opaque.
    for (sql,) in conn.execute("SELECT sql FROM main.sqlite_schema WHERE type='table'"):
        _check_deadline(deadline)
        if sql is not None and _VIRTUAL_TABLE.match(sql):
            raise AnalysisError("Databases containing virtual tables are not supported.")
    rows = []
    for row in conn.execute(
            "SELECT name, tbl_name, sql FROM main.sqlite_schema "
            "WHERE type='index' ORDER BY name COLLATE BINARY"):
        _check_deadline(deadline)
        rows.append(row)
    unique: dict[str, bool] = {}
    for table in sorted({row[1] for row in rows}):
        for info in conn.execute(f"PRAGMA main.index_list({_quoted(table)})"):
            _check_deadline(deadline)
            unique[info[1]] = bool(info[2])
    eligible, pinned = [], []
    for name, table, sql in rows:
        if sql is None:
            pinned.append({"name": name, "table": table, "reason": "automatic constraint index"})
        elif unique.get(name, True):
            pinned.append({"name": name, "table": table, "reason": "unique index"})
        else:
            # Partial-index literals can contain sensitive data.  The declaration
            # is needed for eligibility only and is never included in the report.
            eligible.append({"name": name, "table": table})
    return eligible, pinned


class _Guard:
    def __init__(self) -> None:
        self.problem: str | None = None

    def authorize(self, action: int, first: str | None, second: str | None,
                  _database: str | None, _trigger: str | None) -> int:
        if action == sqlite3.SQLITE_FUNCTION:
            name = (second or first or "").lower()
            if name in _SAFE_FUNCTIONS:
                return sqlite3.SQLITE_OK
            self.problem = (
                f"Function {name[:80]!r} is not on the deterministic function allowlist."
            )
            return sqlite3.SQLITE_DENY
        if action in _ALLOWED_AUTHORIZER_ACTIONS:
            # Table-valued PRAGMA functions can trigger privileged introspection.
            if action == sqlite3.SQLITE_READ and (first or "").lower().startswith("pragma_"):
                self.problem = "Table-valued PRAGMA functions are not permitted."
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.problem = "Only read-only SELECT statements are permitted (no PRAGMA, DDL, or writes)."
        return sqlite3.SQLITE_DENY


class _Budget:
    def __init__(self, limit: int, deadline: float) -> None:
        self.limit = limit
        self.deadline = deadline
        self.count = 0
        self.reason: str | None = None

    def progress(self) -> int:
        self.count += 1
        if time.monotonic() >= self.deadline:
            self.reason = "deadline"
            return 1
        if self.count >= self.limit:
            self.reason = "callbacks"
            return 1
        return 0


def _cell_bytes(value: Any) -> tuple[bytes, bytes]:
    if value is None:
        return b"n", b""
    if isinstance(value, int):
        return b"i", str(value).encode("ascii")
    if isinstance(value, float):
        # float.hex canonically represents finite values, signed zero and +/-inf.
        # SQLite normally converts NaN to NULL; a returned NaN is still explicit.
        return b"r", value.hex().encode("ascii")
    if isinstance(value, str):
        return b"t", value.encode("utf-8")
    if isinstance(value, bytes):
        return b"b", value
    raise AnalysisError("SQLite returned an unsupported result value type.")


def _row_digest(row: tuple) -> tuple[bytes, int]:
    digest = hashlib.sha256(b"indexwitness-row-v1\0")
    total = 0
    for value in row:
        kind, payload = _cell_bytes(value)
        total += len(payload) + 9
        if total > _MAX_VALUE_BYTES:
            raise _ResourceBudgetExceeded("A result row exceeded the 16 MiB encoded-byte budget.")
        digest.update(kind)
        digest.update(struct.pack(">Q", len(payload)))
        digest.update(payload)
    return digest.digest(), total


def _columns_digest(description: tuple) -> str:
    digest = hashlib.sha256(b"indexwitness-columns-v1\0")
    for column in description:
        encoded = column[0].encode("utf-8")
        digest.update(struct.pack(">Q", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


def _error_message(exc: BaseException, guard: _Guard) -> str:
    if guard.problem:
        return guard.problem
    if isinstance(exc, sqlite3.ProgrammingError):
        return "SQLite rejected the statement or its parameter bindings."
    if isinstance(exc, sqlite3.DataError):
        return "SQLite rejected a value exceeding its resource limits."
    if isinstance(exc, (AnalysisError, UnicodeError)):
        return str(exc) if isinstance(exc, AnalysisError) else "A result contained invalid UTF-8 text."
    # SQLite diagnostics can echo user-supplied bound JSON paths or other data.
    # Do not place their raw message, SQL text, or bindings in a persisted report.
    return f"SQLite could not execute the query ({type(exc).__name__}); raw diagnostics withheld."


def _query(conn: sqlite3.Connection, query: dict, max_callbacks: int,
           max_rows: int, deadline: float) -> dict:
    _check_deadline(deadline)
    guard = _Guard()
    budget = _Budget(max_callbacks, deadline)
    plan: list[list] = []
    result: dict[str, Any] = {
        "name": query["name"], "status": "error", "progress_callbacks": 0,
        "callback_ratio": None, "row_count": 0, "result_match": False, "plan": plan,
    }
    cursor: sqlite3.Cursor | None = None
    conn.set_authorizer(guard.authorize)
    try:
        # Collect the planner's opaque rows; never parse unstable detail strings.
        plan_budget = _Budget(max_callbacks, deadline)
        conn.set_progress_handler(plan_budget.progress, 1)
        try:
            plan.extend(
                list(row) for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + query["sql"], query["params"]
                ).fetchall()
            )
        except sqlite3.Error:
            if plan_budget.reason == "deadline":
                _check_deadline(deadline)
            if plan_budget.reason == "callbacks":
                result.update(status="budget_exceeded", error="Query-plan callback budget exceeded.")
                return result
            raise
        conn.set_progress_handler(budget.progress, 1)
        cursor = conn.execute(query["sql"], query["params"])
        if cursor.description is None:
            raise AnalysisError("The statement did not return a SELECT result.")
        # SQLite labels an unaliased literal with its SQL text.  Hash column
        # labels too, so SELECT 'private' cannot leak that literal in a report.
        result["column_count"] = len(cursor.description)
        result["columns_digest"] = _columns_digest(cursor.description)
        row_hashes: list[bytes] = []
        result_bytes = 0
        digest = hashlib.sha256(
            b"indexwitness-ordered-v1\0" if query["compare"] == "ordered"
            else b"indexwitness-bag-v1\0"
        )
        while True:
            _check_deadline(deadline)
            row = cursor.fetchone()
            if row is None:
                break
            result["row_count"] += 1
            if result["row_count"] > max_rows:
                result.update(status="budget_exceeded", error="Result row budget exceeded.")
                return result
            row_hash, row_bytes = _row_digest(row)
            result_bytes += row_bytes
            if result_bytes > _MAX_RESULT_BYTES:
                raise _ResourceBudgetExceeded("Result exceeded the 64 MiB encoded-byte budget.")
            if query["compare"] == "ordered":
                digest.update(row_hash)
            else:
                row_hashes.append(row_hash)
        if query["compare"] == "bag":
            for row_hash in sorted(row_hashes):
                digest.update(row_hash)
        _check_deadline(deadline)
        result.update(status="ok", result_digest=digest.hexdigest())
    except _DeadlineExceeded:
        raise
    except (sqlite3.Error, AnalysisError, UnicodeError, OverflowError) as exc:
        if budget.reason == "deadline":
            _check_deadline(deadline)
        if isinstance(exc, _ResourceBudgetExceeded):
            result.update(status="budget_exceeded", error=str(exc))
        elif budget.reason == "callbacks":
            result.update(status="budget_exceeded", error="Progress callback budget exceeded.")
        else:
            result["error"] = _error_message(exc, guard)
    finally:
        if cursor is not None:
            cursor.close()
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
        result["progress_callbacks"] = budget.count
    return result


def _ratio(current: int, baseline: int) -> float | None:
    return current / baseline if baseline else (1.0 if not current else None)


def _experiment(snapshot: sqlite3.Connection, retained: list[str], removed: list[str],
                queries: list[dict], baseline: list[dict] | None, max_callbacks: int,
                max_rows: int, deadline: float) -> dict:
    conn = _connection()
    try:
        _copy_database(snapshot, conn, deadline)
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        for index in removed:
            _check_deadline(deadline)
            conn.execute(f"DROP INDEX main.{_quoted(index)}")
            _check_deadline(deadline)
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(None, 0)
        results = []
        feasible = True
        for position, query in enumerate(queries):
            metric = _query(conn, query, max_callbacks, max_rows, deadline)
            if baseline is None:
                if metric["status"] != "ok":
                    raise AnalysisError(
                        f"Baseline query {query['name']!r} failed: {metric.get('error', 'unknown failure')}"
                    )
                metric.update(callback_ratio=1.0, result_match=True)
            else:
                reference = baseline[position]
                metric["callback_ratio"] = _ratio(
                    metric["progress_callbacks"], reference["progress_callbacks"]
                )
                metric["result_match"] = (
                    metric["status"] == "ok"
                    and metric.get("column_count") == reference["column_count"]
                    and metric.get("columns_digest") == reference["columns_digest"]
                    and metric["row_count"] == reference["row_count"]
                    and metric.get("result_digest") == reference["result_digest"]
                )
                within_budget = (
                    metric["progress_callbacks"] <=
                    reference["progress_callbacks"] * query["max_step_ratio"]
                )
                if not (metric["result_match"] and within_budget):
                    feasible = False
            results.append(metric)
        ratios = [metric["callback_ratio"] for metric in results]
        return {
            "retained": retained, "removed": removed, "index_count": len(retained),
            "queries": results, "feasible": feasible,
            "weighted_callbacks": sum(
                query["weight"] * metric["progress_callbacks"]
                for query, metric in zip(queries, results)
            ) if all(metric["status"] == "ok" for metric in results) else None,
            "worst_callback_ratio": max(ratios) if all(ratio is not None for ratio in ratios) else None,
        }
    finally:
        conn.close()


def run_analysis(database: str | Path, workload: dict, *, max_indexes: int = 8,
                 max_vm_steps: int = 2_000_000, max_rows: int = 100_000,
                 timeout: float = 30.0) -> dict:
    """Exhaust all retained candidate-index subsets and recommend a feasible one.

    Only nonunique, explicitly declared indexes are candidates.  Every subset
    starts from a fresh in-memory backup; existing statistics are retained and
    ANALYZE is never run.  ``max_vm_steps`` bounds progress callbacks at N=1,
    an approximate work proxy.  Any incomplete search raises ``AnalysisError``.
    """
    if isinstance(max_indexes, bool) or not isinstance(max_indexes, int) or not 0 <= max_indexes <= 12:
        raise AnalysisError("max_indexes must be an integer between 0 and 12.")
    for label, value in (("max_vm_steps", max_vm_steps), ("max_rows", max_rows)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AnalysisError(f"{label} must be a positive integer.")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or timeout <= 0 or timeout > 86400 or not math.isfinite(timeout)):
        raise AnalysisError("timeout must be a positive finite number of seconds, at most 86400.")
    normalized = validate_workload(workload)
    deadline = time.monotonic() + timeout
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise AnalysisError("The source must be an existing SQLite database file.")
    source_size = path.stat().st_size
    snapshot = _connection()
    try:
        try:
            source = sqlite3.connect(
                path.as_uri() + "?mode=ro", uri=True, isolation_level=None,
                cached_statements=0, timeout=min(timeout, 1.0),
            )
            try:
                source.execute("PRAGMA query_only=ON")
                _copy_database(source, snapshot, deadline)
            finally:
                source.close()
            snapshot.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            eligible, pinned = _catalog(snapshot, deadline)
            snapshot.set_progress_handler(None, 0)
        except _DeadlineExceeded:
            raise
        except sqlite3.Error as exc:
            _check_deadline(deadline)
            raise AnalysisError("Could not read a consistent SQLite snapshot of the source.") from exc
        by_name = {item["name"]: item for item in eligible}
        requested = normalized.get("candidate_indexes")
        names = sorted(by_name) if requested is None else sorted(requested)
        if any(name not in by_name for name in names):
            raise AnalysisError("Every candidate must name an existing explicit, nonunique index.")
        if len(names) > max_indexes:
            raise AnalysisError(
                f"Found {len(names)} candidate indexes, exceeding max_indexes={max_indexes}; "
                "supply a smaller candidate_indexes list."
            )
        for item in eligible:
            if item["name"] not in names:
                pinned.append({"name": item["name"], "table": item["table"], "reason": "outside candidate set"})
        pinned.sort(key=lambda item: item["name"])
        queries = normalized["queries"]
        baseline = _experiment(
            snapshot, names, [], queries, None, max_vm_steps, max_rows, deadline
        )
        experiments = []
        full_mask = (1 << len(names)) - 1
        for mask in range(full_mask + 1):
            _check_deadline(deadline)
            retained = [name for bit, name in enumerate(names) if mask & (1 << bit)]
            removed = [name for bit, name in enumerate(names) if not mask & (1 << bit)]
            experiment = baseline if mask == full_mask else _experiment(
                snapshot, retained, removed, queries, baseline["queries"],
                max_vm_steps, max_rows, deadline,
            )
            experiments.append(experiment)
        feasible = [experiment for experiment in experiments if experiment["feasible"]]
        frontier = [
            candidate for candidate in feasible
            if not any(
                other["index_count"] <= candidate["index_count"]
                and other["weighted_callbacks"] <= candidate["weighted_callbacks"]
                and (other["index_count"] < candidate["index_count"]
                     or other["weighted_callbacks"] < candidate["weighted_callbacks"])
                for other in feasible
            )
        ]
        best = min(feasible, key=lambda item: (
            item["index_count"], item["weighted_callbacks"], item["retained"]
        ))
        _check_deadline(deadline)
        return {
            "schema_version": 1, "complete": True,
            "runtime": {
                "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "sqlite_source_id": snapshot.execute("SELECT sqlite_source_id()").fetchone()[0],
                "sqlite_compile_options": sorted(row[0] for row in snapshot.execute("PRAGMA compile_options")),
                "automatic_index": bool(snapshot.execute("PRAGMA automatic_index").fetchone()[0]),
            },
            "limits": {
                "max_indexes": max_indexes, "max_callbacks": max_vm_steps,
                "max_rows": max_rows, "timeout_seconds": timeout,
                "max_snapshot_bytes": _MAX_SNAPSHOT_BYTES,
                "max_result_bytes": _MAX_RESULT_BYTES, "max_row_bytes": _MAX_VALUE_BYTES,
            },
            "source": {"name": path.name, "size_bytes": source_size},
            "candidates": [by_name[name] for name in names], "pinned_indexes": pinned,
            "workload": {"queries": [
                {key: query[key] for key in ("name", "weight", "compare", "max_step_ratio")}
                for query in queries
            ]},
            "baseline": {key: baseline[key] for key in (
                "retained", "index_count", "queries", "weighted_callbacks"
            )},
            "experiments": experiments,
            "frontier": [item["retained"] for item in frontier],
            "recommendation": {key: best[key] for key in (
                "retained", "removed", "index_count", "weighted_callbacks", "worst_callback_ratio"
            )},
        }
    except _DeadlineExceeded:
        raise
    except sqlite3.Error as exc:
        _check_deadline(deadline)
        raise AnalysisError("SQLite could not complete the isolated experiment.") from exc
    finally:
        snapshot.close()
