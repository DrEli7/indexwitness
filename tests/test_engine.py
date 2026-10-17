"""Integration tests for actual SQLite counterfactuals, not mocked planner text."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from indexwitness.engine import AnalysisError, run_analysis


def workload(sql="SELECT count(*) FROM events WHERE tenant = ?", params=None, **query):
    return {"version": 1, "queries": [{
        "name": "lookup", "sql": sql, "params": [7] if params is None else params,
        **query,
    }]}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "sample.sqlite"
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executescript("""
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY,
                    external_id TEXT UNIQUE,
                    tenant INTEGER,
                    created INTEGER,
                    kind INTEGER,
                    payload TEXT
                );
                CREATE INDEX by_tenant_created ON events(tenant, created);
                CREATE INDEX by_tenant_kind ON events(tenant, kind);
                CREATE UNIQUE INDEX explicit_unique ON events(created, id);
            """)
            conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", [
                (i, f"ext-{i}", i % 10, 2000 - i, i % 3, f"payload-{i}")
                for i in range(1000)
            ])

    def tearDown(self):
        self.directory.cleanup()

    def test_substitutes_are_safe_singly_but_not_jointly(self):
        report = run_analysis(self.database, workload())
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["experiments"]), 4)
        experiments = {tuple(item["retained"]): item for item in report["experiments"]}
        self.assertTrue(experiments[("by_tenant_created",)]["feasible"])
        self.assertTrue(experiments[("by_tenant_kind",)]["feasible"])
        self.assertFalse(experiments[()]["feasible"])
        self.assertEqual(report["recommendation"]["index_count"], 1)
        self.assertEqual(len(report["frontier"]), 2)
        json.dumps(report, allow_nan=False)

    def test_source_bytes_schema_and_statistics_are_unchanged(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("ANALYZE")
            expected = conn.execute("SELECT * FROM sqlite_stat1 ORDER BY idx").fetchall()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        run_analysis(self.database, workload())
        after = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            self.assertEqual(expected, conn.execute("SELECT * FROM sqlite_stat1 ORDER BY idx").fetchall())
            self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 1000)
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_schema WHERE name='by_tenant_created'"
            ).fetchone())

    def test_unique_and_constraint_indexes_are_pinned(self):
        report = run_analysis(self.database, workload())
        pinned = {item["name"]: item["reason"] for item in report["pinned_indexes"]}
        self.assertEqual(pinned["explicit_unique"], "unique index")
        self.assertTrue(any(name.startswith("sqlite_autoindex_") for name in pinned))
        bad = workload()
        bad["candidate_indexes"] = ["explicit_unique"]
        with self.assertRaisesRegex(AnalysisError, "nonunique"):
            run_analysis(self.database, bad)

    def test_writes_pragmas_and_untrusted_functions_fail_closed(self):
        for sql in (
            "DELETE FROM events", "CREATE TABLE unwanted(x)", "PRAGMA user_version=9",
            "ATTACH DATABASE ':memory:' AS unwanted", "SELECT random()",
            "SELECT datetime('now')", "SELECT load_extension('unwanted')",
            "SELECT name FROM pragma_table_info('events')",
        ):
            with self.subTest(sql=sql), self.assertRaises(AnalysisError):
                run_analysis(self.database, workload(sql, []))

    def test_bag_preserves_duplicates_but_ignores_row_order(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.executescript("""
                CREATE TABLE ordered_values(tenant, value);
                INSERT INTO ordered_values VALUES (7, 3), (7, 1), (7, 2), (7, 3);
                CREATE INDEX value_order ON ordered_values(tenant, value);
            """)
        payload = workload("SELECT value FROM ordered_values WHERE tenant = ?", max_step_ratio=100)
        payload["candidate_indexes"] = ["value_order"]
        bag = run_analysis(self.database, payload)
        removed = next(item for item in bag["experiments"] if not item["retained"])
        self.assertTrue(removed["queries"][0]["result_match"])
        self.assertEqual(removed["queries"][0]["row_count"], 4)
        self.assertTrue(removed["feasible"])
        payload["queries"][0]["compare"] = "ordered"
        ordered = run_analysis(self.database, payload)
        removed = next(item for item in ordered["experiments"] if not item["retained"])
        self.assertFalse(removed["queries"][0]["result_match"])
        self.assertFalse(removed["feasible"])

    def test_baseline_budgets_raise_and_candidate_budgets_fail(self):
        with self.assertRaisesRegex(AnalysisError, "budget"):
            run_analysis(self.database, workload(), max_vm_steps=5)
        with self.assertRaisesRegex(AnalysisError, "row budget"):
            run_analysis(self.database, workload("SELECT id FROM events", []), max_rows=2)
        baseline = run_analysis(self.database, workload())
        callbacks = baseline["baseline"]["queries"][0]["progress_callbacks"]
        report = run_analysis(self.database, workload(), max_vm_steps=callbacks + 50)
        removed = next(item for item in report["experiments"] if not item["retained"])
        self.assertFalse(removed["feasible"])
        self.assertEqual(removed["queries"][0]["status"], "budget_exceeded")
        # Older Windows Python runtimes may complete within one monotonic-clock
        # tick. Advance a controlled clock instead of assuming sub-tick precision.
        with patch("indexwitness.engine.time.monotonic", side_effect=[0.0, 1.0]):
            with self.assertRaisesRegex(AnalysisError, "deadline"):
                run_analysis(self.database, workload(), timeout=0.5)

    def test_wal_commits_are_included(self):
        writer = sqlite3.connect(self.database)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO events VALUES (1001, 'wal-only', 7, 0, 0, 'secret')")
            writer.commit()
            report = run_analysis(self.database, workload("SELECT external_id FROM events WHERE id=1001", []))
            metric = report["baseline"]["queries"][0]
            self.assertEqual(metric["row_count"], 1)
            self.assertNotIn("wal-only", json.dumps(report))
            self.assertNotIn("secret", json.dumps(report))
        finally:
            writer.close()

    def test_typed_hashes_and_errors_do_not_leak_bound_values(self):
        payload = workload("SELECT ? AS value", ["secret-parameter"])
        report = run_analysis(self.database, payload)
        self.assertNotIn("secret-parameter", json.dumps(report))
        digests = set()
        for literal in ("NULL", "1", "1.0", "'1'", "x'31'", "1e999"):
            report = run_analysis(self.database, workload(f"SELECT {literal} AS value", []))
            digests.add(report["baseline"]["queries"][0]["result_digest"])
            json.dumps(report, allow_nan=False)
        self.assertEqual(len(digests), 6)
        with self.assertRaises(AnalysisError) as caught:
            run_analysis(self.database, workload("SELECT json_extract('{}', ?)", ["secret-invalid-path"]))
        self.assertNotIn("secret-invalid-path", str(caught.exception))

    def test_reports_omit_sql_literals_and_column_labels(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE INDEX private_filter ON events(payload) WHERE payload='sensitive-filter'")
        report = run_analysis(self.database, workload("SELECT 'private-result-literal'", []))
        encoded = json.dumps(report)
        self.assertNotIn("private-result-literal", encoded)
        self.assertNotIn("sensitive-filter", encoded)
        self.assertNotIn('"sql":', encoded)
        self.assertNotIn('"columns":', encoded)

    def test_snapshot_and_cumulative_result_byte_budgets(self):
        with patch("indexwitness.engine._MAX_SNAPSHOT_BYTES", 1024):
            with self.assertRaisesRegex(AnalysisError, "snapshot budget"):
                run_analysis(self.database, workload())
        with patch("indexwitness.engine._MAX_RESULT_BYTES", 128):
            with self.assertRaisesRegex(AnalysisError, "encoded-byte budget"):
                run_analysis(self.database, workload("SELECT printf('%100s', 'x') FROM events LIMIT 3", []))

    def test_existing_virtual_tables_are_rejected(self):
        with closing(sqlite3.connect(self.database)) as conn, conn:
            try:
                conn.execute("CREATE /*prefix*/ VIRTUAL /*middle*/ TABLE search USING fts5(content)")
            except sqlite3.OperationalError:
                self.skipTest("SQLite build has no FTS5 module")
        with self.assertRaisesRegex(AnalysisError, "virtual tables"):
            run_analysis(self.database, workload())


if __name__ == "__main__":
    unittest.main()
