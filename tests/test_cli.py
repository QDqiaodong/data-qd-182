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


class TraceCliProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "input.json"
        self.output = Path(self.dir.name) / "report.json"

    def write(self, data):
        self.source.write_text(json.dumps(data))

    def test_success_prints_summary_and_writes_report(self):
        self.write(json.loads((FIXTURES / "trace_input.json").read_text()))
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary, {"shipments": 3, "released": 3, "recall": 0,
                                   "ready": 0, "hold": 0,
                                   "whole_unattributed_mm": 0})
        report = json.loads(self.output.read_text())
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual([s["id"] for s in report["shipments"]], ["R", "S", "U"])

    def test_business_recall_still_exit_zero(self):
        self.write(json.loads((FIXTURES / "trace_recall.json").read_text()))
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["recall"], 2)
        self.assertEqual(summary["released"], 1)
        report = json.loads(self.output.read_text())
        self.assertEqual({s["id"]: s["disposition"] for s in report["shipments"]},
                         {"S": "recall", "R": "recall", "U": "released"})

    def test_existing_output_kept_byte_for_byte(self):
        self.write(json.loads((FIXTURES / "trace_input.json").read_text()))
        self.output.write_bytes(b"previous report\n")
        before = self.output.read_bytes()
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_invalid_input_leaves_no_report(self):
        data = json.loads((FIXTURES / "trace_input.json").read_text())
        data["allocations"][0]["payload"]["base_end"] = 9
        self.write(data)
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_duplicate_and_nonfinite_rejected(self):
        self.source.write_text('{"batch": {}, "batch": {}, "lots": [], '
                               '"allocations": [], "certificates": [], '
                               '"shipments": [], "as_of": 0}')
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        self.assertFalse(self.output.exists())
        text = (FIXTURES / "trace_input.json").read_text().replace('"as_of": 8', '"as_of": NaN')
        self.source.write_text(text)
        result = run_cli("loom_draft.trace_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())


class CardCliProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "input.json"
        self.output = Path(self.dir.name) / "report.json"

    def write(self, data):
        self.source.write_text(json.dumps(data))

    def test_success_writes_ready_report(self):
        self.write(json.loads((FIXTURES / "card_input.json").read_text()))
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary, {"changed_pedals": 0, "patterns": 1,
                                   "status": "ready"})
        report = json.loads(self.output.read_text())
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["tie_up"][0], {"id": "A", "shafts": [1]})

    def test_infeasible_exits_zero_with_empty_card(self):
        data = json.loads((FIXTURES / "card_input.json").read_text())
        # 删掉 F 与含 F 的禁配对后，该款的应急无法交付。
        data["pedals"] = [p for p in data["pedals"] if p["id"] != "F"]
        data["blocked_pairs"] = []
        self.write(data)
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "infeasible")
        report = json.loads(self.output.read_text())
        self.assertEqual(report, {"schema_version": 1, "status": "infeasible",
                                  "changed_pedals": None, "tie_up": [],
                                  "patterns": []})

    def test_existing_output_kept_byte_for_byte(self):
        self.write(json.loads((FIXTURES / "card_input.json").read_text()))
        self.output.write_bytes(b"previous report\n")
        before = self.output.read_bytes()
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_invalid_input_leaves_no_report(self):
        data = json.loads((FIXTURES / "card_input.json").read_text())
        data["patterns"][0]["faults"] = [[0, "ghost"]]
        self.write(data)
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown pedal", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_missing_source_malformed_and_duplicate_keys(self):
        result = run_cli("loom_draft.card_cli", Path(self.dir.name) / "none.json",
                         self.output)
        self.assertEqual(result.returncode, 2)
        self.source.write_text("{not json")
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())
        self.source.write_text('{"shafts": 1, "shafts": 2}')
        result = run_cli("loom_draft.card_cli", self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        self.assertFalse(self.output.exists())
