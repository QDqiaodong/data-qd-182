import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def run_cli(module, source, output):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return subprocess.run([sys.executable, "-m", module, "--source", str(source),
                           "--output", str(output)],
                          capture_output=True, text=True, env=env, timeout=60)


class BatchCliProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "input.json"
        self.output = Path(self.dir.name) / "report.json"

    def write(self, data):
        self.source.write_text(json.dumps(data))

    def test_success_prints_summary_and_writes_report(self):
        self.write(json.loads((FIXTURES / "batch_input.json").read_text()))
        result = run_cli("loom_draft.batch_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary, {"bridge_picks": 1, "delivered_jobs": 2,
                                   "delivered_value": 2, "total_yarn_mm": 49})
        report = json.loads(self.output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual([step["job_id"] for step in report["plan"]], ["A", "B"])

    def test_empty_delivery_still_exit_zero(self):
        data = json.loads((FIXTURES / "batch_input.json").read_text())
        data["stock"][0]["length_mm"] = 0
        self.write(data)
        result = run_cli("loom_draft.batch_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["delivered_jobs"], 0)

    def test_existing_output_kept_byte_for_byte(self):
        self.write(json.loads((FIXTURES / "batch_input.json").read_text()))
        self.output.write_bytes(b"previous report\n")
        before = self.output.read_bytes()
        result = run_cli("loom_draft.batch_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_invalid_input_leaves_no_report(self):
        data = json.loads((FIXTURES / "batch_input.json").read_text())
        data["jobs"][1]["picks"] = [{"treadles": ["ghost"], "yarn": "w"}]
        self.write(data)
        result = run_cli("loom_draft.batch_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown treadle", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_missing_source_and_malformed_json(self):
        result = run_cli("loom_draft.batch_cli", Path(self.dir.name) / "none.json", self.output)
        self.assertEqual(result.returncode, 2)
        self.source.write_text("{not json")
        result = run_cli("loom_draft.batch_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_v1_cli_still_works_as_process(self):
        result = run_cli("loom_draft.cli", FIXTURES / "input.json", self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["total_yarn_mm"], 380)
        rerun = run_cli("loom_draft.cli", FIXTURES / "input.json", self.output)
        self.assertEqual(rerun.returncode, 2)


if __name__ == "__main__":
    unittest.main()
