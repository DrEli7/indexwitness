import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from indexwitness.cli import main
from indexwitness.engine import run_analysis
from indexwitness.report import interaction_witnesses, render


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "input.db"
        self.workload = {"version": 1, "queries": [{"name": "lookup", "sql": "SELECT id FROM records WHERE owner = ? ORDER BY id", "params": [42], "compare": "ordered", "max_step_ratio": 1.3}]}
        self.workload_path = self.root / "workload.json"
        self.workload_path.write_text(json.dumps(self.workload), encoding="utf-8")
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.executescript("CREATE TABLE records(id INTEGER PRIMARY KEY, owner INT); CREATE INDEX ix_a ON records(owner); CREATE INDEX ix_b ON records(owner);")
            con.executemany("INSERT INTO records VALUES (?,?)", ((i, i % 500) for i in range(4000)))
            con.execute("ANALYZE")

    def invoke(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main([str(self.db), str(self.workload_path), *map(str, arguments)])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_json_and_witness(self):
        status, stdout, stderr = self.invoke("--format", "json")
        self.assertEqual(status, 0, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["experiments"]), 4)
        self.assertEqual(len(report["recommendation"]["removed"]), 1)
        self.assertEqual(report["interaction_witnesses"][0]["removed"], ["ix_a", "ix_b"])
        self.assertNotIn('"sql":', stdout)
        self.assertNotIn('"params":', stdout)

    def test_output_refuses_input_and_existing_files(self):
        for path in (self.db, self.workload_path):
            before = path.read_bytes()
            status, _, _ = self.invoke("--output", path)
            self.assertEqual(status, 2)
            self.assertEqual(path.read_bytes(), before)
        output = self.root / "report.json"
        self.assertEqual(self.invoke("--output", output, "--format", "json")[0], 0)
        before = output.read_bytes()
        self.assertEqual(self.invoke("--output", output)[0], 2)
        self.assertEqual(output.read_bytes(), before)

    def test_findings_exit_status(self):
        self.assertEqual(self.invoke("--fail-on-removal")[0], 1)

    def test_output_cannot_create_sqlite_sidecars(self):
        for suffix in ("-wal", "-shm", "-journal"):
            output = Path(str(self.db) + suffix)
            self.assertFalse(output.exists())
            status, _, _ = self.invoke("--output", output)
            self.assertEqual(status, 2)
            self.assertFalse(output.exists())

    def test_invalid_unicode_rejected_before_creating_output(self):
        self.workload["queries"][0]["name"] = "\ud800"
        self.workload_path.write_text(json.dumps(self.workload), encoding="utf-8")
        output = self.root / "report.json"
        status, _, stderr = self.invoke("--format", "json", "--output", output)
        self.assertEqual(status, 2)
        self.assertIn("Unicode", stderr)
        self.assertFalse(output.exists())

    def test_incomplete_analysis_has_no_output(self):
        output = self.root / "report.json"
        self.assertEqual(self.invoke("--max-indexes", 1, "--output", output)[0], 2)
        self.assertFalse(output.exists())

    def test_html_escapes_untrusted_schema_identifiers(self):
        report = run_analysis(self.db, self.workload)
        report["source"]["name"] = '<img src=x onerror=alert(1)>'
        report["experiments"][0]["queries"][0]["plan"] = [[1, 0, 0, '</pre><script>alert(1)</script>']]
        report["workload"]["queries"][0]["name"] = "lookup"
        html = render(report, "html")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Content-Security-Policy", html)

    def test_all_report_formats_render(self):
        report = run_analysis(self.db, self.workload)
        for format in ("text", "markdown", "html", "json"):
            with self.subTest(format=format):
                result = render(report, format)
                self.assertIn("ix_a", result)
                self.assertIn("ix_b", result)

    def test_candidate_query_error_renders_in_all_formats(self):
        # An explicit INDEXED BY dependency fails after the named index is removed.
        self.workload["queries"][0]["sql"] = "SELECT id FROM records INDEXED BY ix_a WHERE owner = ?"
        report = run_analysis(self.db, self.workload)
        failed = [e for e in report["experiments"] if any(q["status"] == "error" for q in e["queries"])]
        self.assertTrue(failed)
        self.assertIsNone(failed[0]["weighted_callbacks"])
        for format in ("text", "markdown", "html", "json"):
            with self.subTest(format=format):
                self.assertIn("error", render(report, format).lower() if format != "text" else str(report).lower())


if __name__ == "__main__":
    unittest.main()
