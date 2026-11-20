import json
import unittest
from pathlib import Path
from loom_draft.service import run
from loom_draft.weave import circular_float


def fixture():
    return json.loads(Path("fixtures/input.json").read_text())


class LoomTests(unittest.TestCase):
    def test_plain_weave_drawdown_and_yarn(self):
        result = run(fixture())
        self.assertEqual(result["drawdown_warp_above"], [[1, 0, 1, 0], [0, 1, 0, 1]])
        self.assertEqual(result["summary"], {"repeat_ends": 4, "repeat_picks": 2,
                                          "float_violations": 0, "total_yarn_mm": 380})
        self.assertEqual(result["material_requirements"]["yarn"],
                         [{"orientation": "warp", "yarn": "blue", "length_mm": 136},
                          {"orientation": "warp", "yarn": "white", "length_mm": 136},
                          {"orientation": "weft", "yarn": "white", "length_mm": 108}])

    def test_circular_boundary_and_unbounded_float(self):
        self.assertEqual(circular_float([1, 1, 0, 1], 1), {"unbounded": False, "max_crossings": 3})
        self.assertEqual(circular_float([1, 1, 1], 1), {"unbounded": True, "max_crossings": None})
        self.assertEqual(circular_float([1, 1, 1], 0), {"unbounded": False, "max_crossings": 0})

    def test_union_of_multiple_treadles_drives_quality(self):
        data = fixture()
        data["picks"][0]["treadles"] = ["T1", "T2"]
        result = run(data)
        self.assertEqual(result["drawdown_warp_above"][0], [1, 1, 1, 1])
        self.assertEqual(result["summary"]["float_violations"], 2)
        self.assertTrue(all(v["unbounded"] for v in result["float_audit"]["violations"]))

    def test_all_down_is_not_silently_rejected(self):
        data = fixture()
        for pick in data["picks"]:
            pick["treadles"] = []
        result = run(data)
        self.assertEqual(result["summary"]["float_violations"], 2)
        self.assertEqual(result["drawdown_warp_above"], [[0, 0, 0, 0], [0, 0, 0, 0]])

    def test_unknown_treadle_and_duplicate_shaft_reject(self):
        data = fixture()
        data["picks"][0]["treadles"] = ["missing"]
        with self.assertRaisesRegex(ValueError, "unknown"):
            run(data)
        data = fixture()
        data["tie_up"][0]["shafts"] = [1, 1]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            run(data)
