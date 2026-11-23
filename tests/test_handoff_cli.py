import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def run_cli(source, output):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run([sys.executable, "-m", "loom_draft.handoff_cli",
                           "--source", str(source), "--output", str(output)],
                          capture_output=True, text=True, env=env, timeout=60)


class HandoffCliProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "input.json"
        self.output = Path(self.dir.name) / "report.json"

    def write(self, data):
        self.source.write_text(json.dumps(data))

    def fixture_input(self):
        return json.loads((FIXTURES / "handoff_shift1.json").read_text())

    def test_success_prints_summary_and_writes_report(self):
        self.write(self.fixture_input())
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary, {"shift_no": 1, "sealed": 1, "next_pick": 3,
                                   "scrap_this_shift_mm": 0,
                                   "resume_count": 1, "resume_reason": "plan_end"})
        report = json.loads(self.output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["total_picks"], 4)
        self.assertEqual([row["spool_id"] for row in report["spools"]],
                         ["F1", "F2", "G1"])

    def test_cli_report_matches_python_api(self):
        from loom_draft.handoff import run_handoff
        data = self.fixture_input()
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.output.read_text()), run_handoff(data))

    def test_second_shift_via_cli_chain(self):
        self.write(self.fixture_input())
        first = run_cli(self.source, self.output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(self.output.read_text())
        follow = self.fixture_input()
        follow["checkpoint"] = report["checkpoint"]
        follow["shift"] = {"no": 2, "events": [{"op": "unpick", "count": 2}]}
        self.source2 = Path(self.dir.name) / "input2.json"
        self.source2.write_text(json.dumps(follow))
        output2 = Path(self.dir.name) / "report2.json"
        second = run_cli(self.source2, output2)
        self.assertEqual(second.returncode, 0, second.stderr)
        summary = json.loads(second.stdout)
        self.assertEqual(summary["scrap_this_shift_mm"], 6)
        self.assertEqual(summary["resume_reason"], "insufficient")
        report2 = json.loads(output2.read_text())
        self.assertEqual(report2["scrap_this_shift"],
                         [{"woven_shift": 1, "spool_id": "F1", "mm": 3},
                          {"woven_shift": 1, "spool_id": "G1", "mm": 3}])

    def test_resume_hints_still_exit_zero(self):
        # no_spool：只挂 F1，纬 1 的 G 无在用筒
        data = self.fixture_input()
        data["shift"]["events"] = [{"op": "mount", "spool_id": "F1"},
                                   {"op": "weave", "count": 1}]
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resume_reason"], "no_spool")
        # insufficient：织 3 纬再拆 2 纬后，纬 2 需要 F 而 F1 余量为零
        data["shift"]["events"] = [{"op": "mount", "spool_id": "F1"},
                                   {"op": "mount", "spool_id": "G1"},
                                   {"op": "weave", "count": 3},
                                   {"op": "unpick", "count": 2}]
        self.write(data)
        output2 = Path(self.dir.name) / "report2.json"
        result = run_cli(self.source, output2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resume_reason"], "insufficient")

    def test_tail_full_still_exit_zero(self):
        data = self.fixture_input()
        data["batch"]["jobs"] = [{
            "id": "J", "value": 1, "repeat_y": 1000, "phases": [0],
            "picks": [{"treadles": ["T1"], "yarn": "F"},
                      {"treadles": ["T2"], "yarn": "G"},
                      {"treadles": ["T1"], "yarn": "F"}]}]
        data["batch"]["common"]["float_limit"] = 2
        data["batch"]["stock"] = [{"yarn": "W", "length_mm": 10 ** 9},
                                  {"yarn": "F", "length_mm": 10 ** 9},
                                  {"yarn": "G", "length_mm": 10 ** 9}]
        data["batch"]["max_length_mm"] = 10 ** 9
        data["spools"] = [{"id": "F1", "yarn": "F", "length_mm": 10 ** 6},
                          {"id": "G1", "yarn": "G", "length_mm": 10 ** 6}]
        data["shift"]["events"] = [{"op": "mount", "spool_id": "F1"},
                                   {"op": "mount", "spool_id": "G1"},
                                   {"op": "weave", "count": 2048}]
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["resume_reason"], "tail_full")

    def test_inconsistent_weave_record_exits_two(self):
        data = self.fixture_input()
        data["shift"]["events"] = [{"op": "mount", "spool_id": "F1"},
                                   {"op": "mount", "spool_id": "G1"},
                                   {"op": "weave", "count": 3},
                                   {"op": "unpick", "count": 2},
                                   {"op": "weave", "count": 2}]
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())

    def test_existing_output_kept_byte_for_byte(self):
        self.write(self.fixture_input())
        self.output.write_bytes(b"previous report\n")
        before = self.output.read_bytes()
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_invalid_input_leaves_no_report(self):
        data = self.fixture_input()
        data["shift"]["events"] = [{"op": "mount", "spool_id": "ghost"}]
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown spool", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_wrong_batch_for_checkpoint_exits_two(self):
        self.write(self.fixture_input())
        first = run_cli(self.source, self.output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(self.output.read_text())
        follow = self.fixture_input()
        follow["checkpoint"] = report["checkpoint"]
        follow["batch"]["stock"][0]["length_mm"] = 11
        follow["shift"] = {"no": 2, "events": []}
        self.write(follow)
        output2 = Path(self.dir.name) / "report2.json"
        result = run_cli(self.source, output2)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output2.exists())

    def test_missing_source_malformed_and_duplicate_keys(self):
        result = run_cli(Path(self.dir.name) / "none.json", self.output)
        self.assertEqual(result.returncode, 2)
        self.source.write_text("{not json")
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.source.write_text('{"batch": {}, "batch": {}}')
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
