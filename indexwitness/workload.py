"""Strict, bounded workload input. No SQL rewriting or parameter interpolation."""

import json
import math
from pathlib import Path


class WorkloadError(ValueError):
    """The workload is invalid or exceeds a documented input limit."""


def _utf8(value, context):
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise WorkloadError(f"{context} must contain valid Unicode text without lone surrogates") from exc


def _keys(value, allowed, context):
    if not isinstance(value, dict):
        raise WorkloadError(f"{context} must be an object")
    unknown = set(value) - set(allowed)
    if unknown:
        raise WorkloadError(f"{context}: unknown keys: {', '.join(sorted(map(str, unknown)))}")


def _number(value, context, minimum, exclusive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value > 1e9 or value < -1e9 or not math.isfinite(value)
            or (value <= minimum if exclusive else value < minimum)):
        op = ">" if exclusive else ">="
        raise WorkloadError(f"{context} must be a finite number {op} {minimum} and <= 1e9")
    return value


def validate_workload(payload):
    """Validate and normalize a version 1 workload without changing caller data."""
    _keys(payload, ("version", "queries", "candidate_indexes"), "workload")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise WorkloadError("workload.version must be 1")
    queries = payload.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 64:
        raise WorkloadError("queries must contain between 1 and 64 entries")
    normalized = []
    names = set()
    for i, query in enumerate(queries):
        context = f"queries[{i}]"
        _keys(query, ("name", "sql", "params", "weight", "compare", "max_step_ratio"), context)
        name = query.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise WorkloadError(f"{context}.name must be nonempty text (max 128 characters, no controls)")
        if name in names:
            raise WorkloadError(f"duplicate query name: {name}")
        _utf8(name, f"{context}.name")
        names.add(name)
        sql = query.get("sql")
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 65536 or "\x00" in sql:
            raise WorkloadError(f"{context}.sql must be nonempty SQL (max 65536 characters, no NUL)")
        _utf8(sql, f"{context}.sql")
        params = query.get("params", [])
        if not isinstance(params, (list, dict)) or len(params) > 1000:
            raise WorkloadError(f"{context}.params must be an array or object (max 1000 bindings)")
        if isinstance(params, dict) and any(not isinstance(k, str) or not k or len(k) > 128 for k in params):
            raise WorkloadError(f"{context}.params keys must be nonempty strings, max 128 characters")
        if isinstance(params, dict):
            for key in params:
                _utf8(key, f"{context}.params key")
        values = params.values() if isinstance(params, dict) else params
        for value in values:
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int) and -(2**63) <= value < 2**63:
                continue
            if isinstance(value, float) and math.isfinite(value):
                continue
            if isinstance(value, str) and len(value) <= 1048576:
                _utf8(value, f"{context}.params value")
                continue
            raise WorkloadError(f"{context}.params values must be JSON scalars representable by SQLite")
        compare = query.get("compare", "bag")
        if compare not in ("bag", "ordered"):
            raise WorkloadError(f"{context}.compare must be 'bag' or 'ordered'")
        normalized.append({"name": name, "sql": sql, "params": params.copy(),
                           "weight": _number(query.get("weight", 1), f"{context}.weight", 0, True),
                           "max_step_ratio": _number(query.get("max_step_ratio", 1.25), f"{context}.max_step_ratio", 1),
                           "compare": compare})
    result = {"version": 1, "queries": normalized}
    if "candidate_indexes" in payload:
        indexes = payload["candidate_indexes"]
        if (not isinstance(indexes, list) or len(indexes) > 12
                or any(not isinstance(x, str) or not x or len(x) > 1024 or "\x00" in x for x in indexes)
                or len(set(indexes)) != len(indexes)):
            raise WorkloadError("candidate_indexes must contain up to 12 distinct, nonempty index names")
        result["candidate_indexes"] = indexes.copy()
        for index in indexes:
            _utf8(index, "candidate_indexes entry")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkloadError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_workload(path):
    """Read at most 4 MiB and reject ambiguous duplicate JSON keys."""
    with Path(path).open("rb") as stream:
        data = stream.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise WorkloadError("workload exceeds 4 MiB")
    try:
        payload = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WorkloadError(f"invalid UTF-8 JSON workload: {exc}") from exc
    return validate_workload(payload)
