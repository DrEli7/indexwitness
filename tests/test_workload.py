import json
from pathlib import Path
import tempfile
import unittest

from indexwitness.workload import WorkloadError, load_workload, validate_workload


class WorkloadTests(unittest.TestCase):
    def sample(self):
        return {"version": 1, "queries": [{"name": "lookup", "sql": "SELECT ?", "params": [42]}]}

    def test_normalizes_without_mutating(self):
        original = self.sample()
        result = validate_workload(original)
        self.assertEqual(result["queries"][0]["compare"], "bag")
        self.assertEqual(result["queries"][0]["max_step_ratio"], 1.25)
        self.assertNotIn("weight", original["queries"][0])
        result["queries"][0]["params"].append(4)
        self.assertEqual(original["queries"][0]["params"], [42])

    def test_invalid_inputs(self):
        cases = [None, {}, {"version": True, "queries": []}, {"version": 1, "queries": []}]
        for key, value in [("weight", 0), ("weight", True), ("weight", float("inf")), ("weight", 10**1000),
                           ("max_step_ratio", .9), ("compare", "set"), ("params", [2**63]),
                           ("params", [[1]]), ("sql", ""), ("name", "bad\nname"), ("typo", 1)]:
            case = self.sample()
            case["queries"][0][key] = value
            cases.append(case)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(WorkloadError):
                validate_workload(case)

    def test_duplicate_names_indexes(self):
        case = self.sample()
        case["queries"] *= 2
        with self.assertRaises(WorkloadError):
            validate_workload(case)
        case = self.sample()
        case["candidate_indexes"] = ["idx", "idx"]
        with self.assertRaises(WorkloadError):
            validate_workload(case)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "w.json"
            path.write_text('{"version":1,"version":1,"queries":[]}', encoding="utf-8")
            with self.assertRaisesRegex(WorkloadError, "duplicate JSON key"):
                load_workload(path)

    def test_bom_and_named_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "w.json"
            data = self.sample()
            data["queries"][0]["params"] = {"key": "hello"}
            path.write_text(json.dumps(data), encoding="utf-8-sig")
            self.assertEqual(load_workload(path)["queries"][0]["params"], {"key": "hello"})

    def test_deeply_nested_json_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "w.json"
            path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
            with self.assertRaises(WorkloadError):
                load_workload(path)

    def test_lone_surrogates_in_workload_strings_rejected(self):
        for field, value in [("name", "\ud800"), ("sql", "SELECT '\ud800'"),
                             ("params", ["\ud800"]), ("params", {"\ud800": 1})]:
            with self.subTest(field=field):
                case = self.sample()
                case["queries"][0][field] = value
                with self.assertRaises(WorkloadError):
                    validate_workload(case)
        case = self.sample()
        case["candidate_indexes"] = ["\ud800"]
        with self.assertRaises(WorkloadError):
            validate_workload(case)


if __name__ == "__main__":
    unittest.main()
