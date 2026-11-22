"""Tests for loom-trace: yarn batch tracing and historical release review.

The correctness oracle is a deliberately naive, point-by-point reimplementation
in ``naive_trace`` that expands the cloth into (end, pick) points and never
imports the trace engine's decision helpers.  Large-repeat cases are checked
against closed-form totals computed directly from the compressed plan.
"""
import copy
import json
import random
import unittest
from pathlib import Path

from loom_draft.batch import run_plan
from loom_draft.trace import run_trace

try:  # allowed both under `discover -s tests` and as `tests.test_trace`
    from naive_trace import evaluate
except ImportError:  # pragma: no cover
    from .naive_trace import evaluate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fixture(name="trace_input.json"):
    return json.loads((FIXTURES / name).read_text())


def common(**overrides):
    base = {"shafts": 2,
            "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
            "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
            "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
            "takeup_permille": 100, "warp_waste_mm": 1, "float_limit": 3}
    base.update(overrides)
    return base


def pk(treadle, yarn):
    return {"treadles": [treadle], "yarn": yarn}


def batch(jobs, bridges=(), stock=(("w", 10**6), ("f", 10**6)), max_length_mm=10**6,
          **overrides):
    return {"common": common(**overrides), "jobs": list(jobs), "bridges": list(bridges),
            "stock": [{"yarn": yarn, "length_mm": length} for yarn, length in stock],
            "max_length_mm": max_length_mm}


def one_job_batch(yarn="f"):
    return batch([{"id": "A", "value": 1, "repeat_y": 1, "phases": [0],
                   "picks": [pk("T1", yarn), pk("T2", yarn), pk("T1", yarn), pk("T2", yarn)]}])


def key_case():
    """The contract's key comparison scenario: 2 ends, 4 picks, W/F/G lots."""
    return {
        "batch": one_job_batch(),
        "lots": [{"id": "W", "yarn": "w", "capacity_mm": 12},
                 {"id": "F", "yarn": "f", "capacity_mm": 12},
                 {"id": "G", "yarn": "f", "capacity_mm": 12}],
        "allocations": [
            {"id": "aw0", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0, "repeat_stop": 1,
              "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "aw1", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 1, "repeat_start": 0, "repeat_stop": 1,
              "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "wf", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}}],
        "certificates": [
            {"id": "cw", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "W", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cg", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "G", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}}],
        "shipments": [
            {"id": "S", "requested_at": 3, "end_start": 0, "end_stop": 1,
             "pick_start": 0, "pick_stop": 2},
            {"id": "R", "requested_at": 3, "end_start": 1, "end_stop": 2,
             "pick_start": 0, "pick_stop": 2},
            {"id": "U", "requested_at": 3, "end_start": 0, "end_stop": 2,
             "pick_start": 2, "pick_stop": 4}],
        "as_of": 3,
    }


def decision(report, shipment_id, which="current"):
    return next(item for item in report["shipments"] if item["id"] == shipment_id)[which]


def item(report, shipment_id, which="current"):
    return next(item for item in report["shipments"] if item["id"] == shipment_id)


def lot_entry(decision_value, lot_id):
    return next(entry for entry in decision_value["lots"] if entry["lot_id"] == lot_id)


class KeyScenarioTests(unittest.TestCase):
    def test_baseline_releases_and_piece_material(self):
        report = run_trace(key_case())
        self.assertEqual([s["id"] for s in report["shipments"]], ["R", "S", "U"])
        self.assertEqual({s["id"]: s["disposition"] for s in report["shipments"]},
                         {"S": "released", "R": "released", "U": "released"})
        self.assertEqual(report["lot_balances"],
                         [{"lot_id": "F", "yarn": "f", "capacity_mm": 12, "warp_mm": 0,
                           "weft_mm": 12, "attributed_mm": 12, "remaining_mm": 0,
                           "overdrawn": False},
                          {"lot_id": "G", "yarn": "f", "capacity_mm": 12, "warp_mm": 0,
                           "weft_mm": 0, "attributed_mm": 0, "remaining_mm": 12,
                           "overdrawn": False},
                          {"lot_id": "W", "yarn": "w", "capacity_mm": 12, "warp_mm": 12,
                           "weft_mm": 0, "attributed_mm": 12, "remaining_mm": 0,
                           "overdrawn": False}])
        self.assertEqual(report["summary"], {"shipments": 3, "released": 3, "recall": 0,
                                             "ready": 0, "hold": 0,
                                             "whole_unattributed_mm": 0})
        # S = end0 x picks 0..1: warp count 1 per pick (3mm), weft whole strip 4mm.
        s = decision(report, "S")
        self.assertEqual((lot_entry(s, "W")["warp_mm"], lot_entry(s, "W")["weft_mm"]), (3, 0))
        self.assertEqual((lot_entry(s, "F")["warp_mm"], lot_entry(s, "F")["weft_mm"]), (0, 4))
        r = decision(report, "R")
        self.assertEqual((lot_entry(r, "W")["warp_mm"], lot_entry(r, "F")["weft_mm"]), (3, 2))
        u = decision(report, "U")
        self.assertEqual((lot_entry(u, "W")["warp_mm"], lot_entry(u, "F")["weft_mm"]), (4, 6))
        self.assertNotEqual(lot_entry(decision(report, "S"), "F")["weft_mm"],
                            lot_entry(decision(report, "R"), "F")["weft_mm"])

    def test_fail_makes_recall_but_keeps_history_and_other_piece(self):
        data = key_case()
        data["certificates"].append(
            {"id": "ff", "revision": 1, "known_at": 5, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 2, "verdict": "fail"}})
        data["as_of"] = 6
        report = run_trace(data)
        for sid in ("S", "R"):
            entry = item(report, sid)
            self.assertTrue(entry["historical"]["allowed"])
            self.assertEqual(entry["disposition"], "recall")
            self.assertFalse(entry["current"]["allowed"])
            failed = [p for p in entry["current"]["problems"] if p["kind"] == "failed"]
            self.assertEqual(failed, [{"kind": "failed", "lot_id": "F", "first_pick": 1}])
            refs = [c["id"] for c in lot_entry(entry["current"], "F")["certificate_versions"]]
            self.assertIn("cf", refs)
            self.assertIn("ff", refs)
        self.assertEqual(item(report, "U")["disposition"], "released")
        self.assertEqual(report["summary"]["recall"], 2)

    def test_revision_moves_attribution_back_to_g(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 7, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "G"}})
        data["certificates"].append(
            {"id": "ff", "revision": 1, "known_at": 5, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 2, "verdict": "fail"}})
        data["as_of"] = 8
        report = run_trace(data)
        for sid in ("S", "R", "U"):
            entry = item(report, sid)
            self.assertEqual(entry["disposition"], "released")
            self.assertEqual([c["id"] for c in lot_entry(entry["historical"], "F")
                              ["allocation_versions"]], ["wf"])
            self.assertNotIn("G", [lot["lot_id"] for lot in entry["historical"]["lots"]])
            self.assertEqual([c["id"] for c in lot_entry(entry["current"], "G")
                              ["allocation_versions"]], ["wf"])
            self.assertEqual([c["revision"] for c in lot_entry(entry["current"], "G")
                              ["allocation_versions"]], [2])
        balances = {row["lot_id"]: row for row in report["lot_balances"]}
        self.assertEqual((balances["F"]["attributed_mm"], balances["F"]["remaining_mm"]),
                         (0, 12))
        self.assertEqual((balances["G"]["attributed_mm"], balances["G"]["remaining_mm"]),
                         (12, 0))

    def test_whole_batch_overdrawn_recalls_even_small_piece(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 7, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "G"}})
        data["lots"][2]["capacity_mm"] = 11
        data["as_of"] = 8
        report = run_trace(data)
        self.assertEqual({s["disposition"] for s in report["shipments"]}, {"recall"})
        for entry in report["shipments"]:
            self.assertTrue(entry["historical"]["allowed"])
            problems = entry["current"]["problems"]
            self.assertEqual([(p["kind"], p["lot_id"]) for p in problems], [("overdrawn", "G")])
            self.assertLess(lot_entry(entry["current"], "G")["total_mm"], 11)
        self.assertEqual({row["lot_id"]: row["overdrawn"] for row in report["lot_balances"]},
                         {"F": False, "G": True, "W": False})


class VersionTests(unittest.TestCase):
    def test_valid_versions_including_future_are_checked(self):
        data = key_case()
        data["allocations"] += [
            {"id": "wfx", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 1, "lot_id": "F"}},
            {"id": "wfx", "revision": 2, "known_at": 9, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 1, "lot_id": "W"}}]
        data["allocations"] = [row for row in data["allocations"]
                               if row["id"] not in ("aw0", "aw1")]
        with self.assertRaisesRegex(ValueError, "orientation"):
            run_trace(data)

    def test_revision_gap_and_duplicate_rejected(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 3, "known_at": 9, "payload": None})
        with self.assertRaisesRegex(ValueError, "revisions"):
            run_trace(data)
        data = key_case()
        data["certificates"].append(
            {"id": "cf", "revision": 1, "known_at": 2, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}})
        with self.assertRaisesRegex(ValueError, "revisions"):
            run_trace(data)

    def test_known_at_must_strictly_increase(self):
        data = key_case()
        data["certificates"].append(
            {"id": "cf", "revision": 2, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}})
        with self.assertRaisesRegex(ValueError, "known_at"):
            run_trace(data)

    def test_revision_one_cannot_withdraw(self):
        data = key_case()
        data["allocations"].append(
            {"id": "new", "revision": 1, "known_at": 1, "payload": None})
        with self.assertRaisesRegex(ValueError, "revision 1"):
            run_trace(data)

    def test_withdrawal_reports_quantified_gap(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 2, "payload": None})
        report = run_trace(data)
        expected = {"S": (2, 4), "R": (2, 2), "U": (2, 6)}
        for entry in report["shipments"]:
            self.assertEqual(entry["disposition"], "hold")
            self.assertFalse(entry["historical"]["allowed"])
            missing = entry["current"]["missing"]
            picks, material = expected[entry["id"]]
            self.assertEqual((missing["weft_picks"], missing["material_mm"]), (picks, material))
            first = {"S": 0, "R": 0, "U": 2}[entry["id"]]
            self.assertEqual(missing["first"],
                             {"orientation": "weft",
                              "end_index": 0 if entry["id"] != "R" else 1,
                              "pick_index": first})
            self.assertEqual([p["kind"] for p in entry["current"]["problems"]],
                             ["source_gap"])
            self.assertIsNone(entry["current"]["problems"][0]["lot_id"])
        self.assertEqual(report["whole_unattributed_mm"], 12)

    def test_withdrawing_one_fail_keeps_the_other(self):
        data = key_case()
        data["certificates"] += [
            {"id": "f1", "revision": 1, "known_at": 2, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 2, "verdict": "fail"}},
            {"id": "f2", "revision": 1, "known_at": 2, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 3, "verdict": "fail"}},
            {"id": "f1", "revision": 2, "known_at": 4, "payload": None}]
        data["as_of"] = 5
        report = run_trace(data)
        entry = item(report, "S")
        self.assertFalse(entry["current"]["allowed"])
        self.assertEqual([p["first_pick"] for p in entry["current"]["problems"]
                          if p["kind"] == "failed"], [1])
        refs = [c["id"] for c in lot_entry(entry["current"], "F")["certificate_versions"]]
        self.assertEqual(sorted(refs), ["cf", "f2"])

    def test_fail_and_uncertified_coexist(self):
        data = key_case()
        data["certificates"] = [
            {"id": "cw", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "W", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cf", "revision": 2, "known_at": 3, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 2, "verdict": "pass"}},
            {"id": "ff", "revision": 1, "known_at": 2, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 2, "verdict": "fail"}}]
        data["shipments"] = [{"id": "S", "requested_at": 1, "end_start": 0, "end_stop": 1,
                              "pick_start": 0, "pick_stop": 4}]
        data["as_of"] = 4
        report = run_trace(data)
        entry = item(report, "S")
        problems = [(p["kind"], p["lot_id"], p["first_pick"]) for p in entry["current"]["problems"]]
        self.assertEqual(problems, [("failed", "F", 1), ("uncertified", "F", 2)])
        self.assertTrue(entry["historical"]["allowed"])
        refs = [(c["id"], c["revision"])
                for c in lot_entry(entry["current"], "F")["certificate_versions"]]
        self.assertEqual(refs, [("cf", 2), ("ff", 1)])

    def test_as_of_shrink_preserves_historical(self):
        data = key_case()
        data["certificates"].append(
            {"id": "ff", "revision": 1, "known_at": 5, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 2, "verdict": "fail"}})
        data["as_of"] = 8
        full = run_trace(data)
        data["as_of"] = 6
        shrunk = run_trace(data)
        self.assertEqual([s["historical"] for s in full["shipments"]],
                         [s["historical"] for s in shrunk["shipments"]])
        self.assertEqual([s["disposition"] for s in full["shipments"]],
                         [s["disposition"] for s in shrunk["shipments"]])


class ContentionTests(unittest.TestCase):
    def test_same_time_versions_conflict_and_order_is_irrelevant(self):
        data = key_case()
        data["allocations"] += [
            {"id": "a1", "revision": 1, "known_at": 3, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "G"}},
            {"id": "a2", "revision": 1, "known_at": 3, "payload":
             {"orientation": "weft", "pick_start": 1, "pick_stop": 4, "lot_id": "G"}}]
        with self.assertRaisesRegex(ValueError, "contend"):
            run_trace(data)
        data["allocations"].reverse()
        with self.assertRaisesRegex(ValueError, "contend"):
            run_trace(data)

    def test_old_and_new_versions_never_simultaneous(self):
        data = key_case()
        data["allocations"] = [
            {"id": "m", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "G"}},
            {"id": "m", "revision": 2, "known_at": 3, "payload":
             {"orientation": "weft", "pick_start": 2, "pick_stop": 4, "lot_id": "G"}},
            {"id": "n", "revision": 1, "known_at": 3, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "G"}}]
        data["as_of"] = 5
        run_trace(data)

    def test_historical_overlap_that_later_disappears_still_rejected(self):
        data = key_case()
        data["allocations"] = [
            {"id": "m", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "G"}},
            {"id": "m", "revision": 2, "known_at": 4, "payload": None},
            {"id": "n", "revision": 1, "known_at": 2, "payload":
             {"orientation": "weft", "pick_start": 1, "pick_stop": 3, "lot_id": "G"}}]
        data["as_of"] = 5
        with self.assertRaisesRegex(ValueError, "contend"):
            run_trace(data)

    def test_different_yarn_numeric_overlap_is_not_conflict(self):
        data = key_case()
        data["batch"] = batch([{"id": "A", "value": 1, "repeat_y": 1, "phases": [0],
                                "picks": [pk("T1", "f"), pk("T2", "g"),
                                          pk("T1", "f"), pk("T2", "g")]}],
                              stock=(("w", 10**6), ("f", 10**6), ("g", 10**6)))
        data["lots"] = [{"id": "W", "yarn": "w", "capacity_mm": 100},
                        {"id": "F", "yarn": "f", "capacity_mm": 100},
                        {"id": "G", "yarn": "g", "capacity_mm": 100}]
        data["allocations"] = [
            {"id": "af", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}},
            {"id": "ag", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "G"}}]
        data["allocations"] += [
            {"id": "aw0", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0, "repeat_stop": 1,
              "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "aw1", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 1, "repeat_start": 0, "repeat_stop": 1,
              "pick_start": 0, "pick_stop": 4, "lot_id": "W"}}]
        data["certificates"] = [
            {"id": "cw", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "W", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cg", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "G", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}}]
        data["shipments"] = [{"id": "S", "requested_at": 1, "end_start": 0, "end_stop": 2,
                              "pick_start": 0, "pick_stop": 4}]
        data["as_of"] = 2
        report = run_trace(data)
        self.assertEqual(item(report, "S")["disposition"], "released")

    def test_weft_allocation_must_match_a_real_weft(self):
        data = key_case()
        data["allocations"].append(
            {"id": "bad", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "W"}})
        with self.assertRaisesRegex(ValueError, "at least one real weft"):
            run_trace(data)


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate):
        data = key_case()
        mutate(data)
        with self.assertRaises(ValueError):
            run_trace(data)

    def test_top_level_and_field_strictness(self):
        with self.assertRaises(ValueError):
            run_trace([])
        self.assert_invalid(lambda d: d.__setitem__("extra", 1))
        self.assert_invalid(lambda d: d.pop("lots"))
        self.assert_invalid(lambda d: d.__setitem__("as_of", True))
        self.assert_invalid(lambda d: d.__setitem__("as_of", 10**12 + 1))

    def test_ids_and_ranges(self):
        self.assert_invalid(lambda d: d["lots"][0].__setitem__("id", "bad id"))
        self.assert_invalid(lambda d: d["lots"].append(
            {"id": "W", "yarn": "w", "capacity_mm": 1}))
        self.assert_invalid(lambda d: d["lots"].append(
            {"id": "X", "yarn": "w", "capacity_mm": -1}))
        self.assert_invalid(lambda d: d["allocations"][0]["payload"].__setitem__(
            "base_end", 2))
        self.assert_invalid(lambda d: d["allocations"][0]["payload"].__setitem__(
            "pick_stop", 5))
        self.assert_invalid(lambda d: d["allocations"][0]["payload"].__setitem__(
            "lot_id", "F"))
        self.assert_invalid(lambda d: d["allocations"][0]["payload"].__setitem__(
            "extra", 1))
        self.assert_invalid(lambda d: d["certificates"][0]["payload"].__setitem__(
            "verdict", "maybe"))
        self.assert_invalid(lambda d: d["certificates"][0]["payload"].__setitem__(
            "lot_id", "ghost"))

    def test_shipments_bounds_and_overlap(self):
        self.assert_invalid(lambda d: d["shipments"][0].__setitem__("end_stop", 3))
        self.assert_invalid(lambda d: d["shipments"][0].__setitem__("pick_stop", 5))
        self.assert_invalid(lambda d: d["shipments"][0].__setitem__(
            "requested_at", d["as_of"] + 1))
        self.assert_invalid(lambda d: d["shipments"].append(
            {"id": "S2", "requested_at": 1, "end_start": 0, "end_stop": 1,
             "pick_start": 1, "pick_stop": 2}))
        # touching edges are legal
        data = key_case()
        data["shipments"] = [
            {"id": "A", "requested_at": 1, "end_start": 0, "end_stop": 1,
             "pick_start": 0, "pick_stop": 2},
            {"id": "B", "requested_at": 1, "end_start": 1, "end_stop": 2,
             "pick_start": 0, "pick_stop": 2},
            {"id": "C", "requested_at": 1, "end_start": 0, "end_stop": 2,
             "pick_start": 2, "pick_stop": 4}]
        run_trace(data)

    def test_payload_exact_fields_and_null_replacement(self):
        self.assert_invalid(lambda d: d["allocations"][2]["payload"].__setitem__(
            "base_end", 0))
        self.assert_invalid(lambda d: d["certificates"][0]["payload"].pop("verdict"))
        self.assert_invalid(lambda d: d["allocations"][0].__setitem__(
            "payload", {"orientation": None}))

    def test_empty_plan_requires_empty_evidence(self):
        data = key_case()
        data["batch"]["stock"] = [{"yarn": "w", "length_mm": 0}]
        data["allocations"] = []
        data["certificates"] = []
        data["shipments"] = []
        report = run_trace(data)
        self.assertEqual(report["production"]["material_requirements"]["total_picks"], 0)
        self.assertEqual(report["shipments"], [])
        self.assertEqual(report["summary"]["whole_unattributed_mm"], 0)
        self.assertEqual(report["whole_unattributed_mm"], 0)
        data["certificates"] = [{"id": "c", "revision": 1, "known_at": 1, "payload":
                                 {"lot_id": "W", "pick_start": 0, "pick_stop": 1,
                                  "verdict": "pass"}}]
        with self.assertRaises(ValueError):
            run_trace(data)


class CrossCheckTests(unittest.TestCase):
    def compare(self, data):
        report = run_trace(data)
        segments = report["production"]["segments"]
        for ship in data["shipments"]:
            for which, at in (("historical", ship["requested_at"]),
                              ("current", data["as_of"])):
                got = item(report, ship["id"])[which]
                allowed, problems, lots, missing, firsts = evaluate(
                    data, segments, at, ship)
                self.assertEqual(got["allowed"], allowed,
                                 (ship["id"], which, got["problems"], problems))
                self.assertEqual([(p["kind"], p["lot_id"]) for p in got["problems"]],
                                 problems, (ship["id"], which))
                self.assertEqual(got["missing"], missing, (ship["id"], which))
                for problem in got["problems"]:
                    self.assertEqual(problem["first_pick"],
                                     firsts[(problem["kind"], problem["lot_id"])])
                expected = {lot: (row["warp_mm"], row["weft_mm"],
                                  row["whole_attributed_mm"], row["overdrawn"])
                            for lot, row in lots.items()}
                actual = {row["lot_id"]: (row["warp_mm"], row["weft_mm"],
                                          row["whole_attributed_mm"], row["overdrawn"])
                          for row in got["lots"]}
                self.assertEqual(actual, expected, (ship["id"], which))
                for row in got["lots"]:
                    expected_row = lots[row["lot_id"]]
                    self.assertEqual([(x["id"], x["revision"]) for x in row["allocation_versions"]],
                                     [tuple(x) for x in expected_row["allocation_versions"]])
                    self.assertEqual([(x["id"], x["revision"]) for x in row["certificate_versions"]],
                                     [tuple(x) for x in expected_row["certificate_versions"]])

    def test_fixture_recall_matches_naive(self):
        self.compare(fixture("trace_recall.json"))
        self.compare(fixture("trace_input.json"))

    def test_random_small_against_naive(self):
        rng = random.Random(20261122)
        for _ in range(200):
            data = random_case(rng)
            if data is None:
                continue
            self.compare(data)


def random_case(rng):
    data = key_case()
    jobs = []
    for j in range(rng.randint(1, 2)):
        picks = [pk(rng.choice(["T1", "T2"]), rng.choice(["f", "g"]))
                 for _ in range(rng.randint(1, 4))]
        jobs.append({"id": f"J{j}", "value": 1, "repeat_y": rng.randint(1, 2),
                     "phases": [0], "picks": picks})
    data["batch"]["jobs"] = jobs
    data["batch"]["common"]["float_limit"] = 10
    data["batch"]["common"]["repeat_x"] = rng.randint(1, 2)
    data["batch"]["stock"] = [{"yarn": yarn, "length_mm": 10**6}
                              for yarn in ("w", "f", "g")]
    plan = run_plan(data["batch"])
    P = plan["material_requirements"]["total_picks"]
    if P == 0:
        return None
    E = len(data["batch"]["common"]["warp"]) * data["batch"]["common"]["repeat_x"]
    data["lots"] = [{"id": "W", "yarn": "w", "capacity_mm": rng.choice([10, 50])},
                    {"id": "F", "yarn": "f", "capacity_mm": rng.choice([10, 50])},
                    {"id": "G", "yarn": "g", "capacity_mm": rng.choice([10, 50])}]
    repeat_x = data["batch"]["common"]["repeat_x"]
    allocations = []
    for base in range(2):
        if rng.random() < 0.8:
            lo = rng.randint(0, P - 1)
            allocations.append(
                {"id": f"aw{base}", "revision": 1, "known_at": rng.randint(1, 3),
                 "payload": {"orientation": "warp", "base_end": base,
                             "repeat_start": 0, "repeat_stop": repeat_x,
                             "pick_start": lo, "pick_stop": rng.randint(lo + 1, P),
                             "lot_id": "W"}})
    titles = {}
    for index, yarn in enumerate(["f", "g"]):
        if rng.random() < 0.8:
            lo = rng.randint(0, P - 1)
            hi = rng.randint(lo + 1, P)
            if not any(plan_row_yarn(plan, p) == yarn for p in range(lo, hi)):
                continue
            allocations.append(
                {"id": f"ww{index}", "revision": 1, "known_at": rng.randint(1, 3),
                 "payload": {"orientation": "weft", "pick_start": lo, "pick_stop": hi,
                             "lot_id": yarn.upper()}})
    data["allocations"] = allocations
    certificates = []
    for lot_id in ("W", "F", "G"):
        if rng.random() < 0.9:
            lo = rng.randint(0, P - 1)
            certificates.append(
                {"id": f"c{lot_id}", "revision": 1, "known_at": rng.randint(1, 3),
                 "payload": {"lot_id": lot_id, "pick_start": lo,
                             "pick_stop": rng.randint(lo + 1, P),
                             "verdict": rng.choice(["pass", "fail"])}})
    data["certificates"] = certificates
    as_of = rng.randint(0, 5)
    data["as_of"] = as_of
    bands = sorted(rng.sample(range(1, P), min(rng.randint(1, 2), P - 1)))
    edges = [0] + bands + [P]
    shipments = []
    for index in range(len(edges) - 1):
        start = edges[index]
        stop = edges[index + 1]
        if start >= stop:
            continue
        begin = rng.randint(0, E - 1)
        shipments.append({"id": f"S{index}", "requested_at": rng.randint(0, as_of),
                          "end_start": begin, "end_stop": rng.randint(begin + 1, E),
                          "pick_start": start, "pick_stop": stop})
    data["shipments"] = shipments
    return data


def plan_row_yarn(plan, pick):
    for segment in plan["segments"]:
        if segment["start_pick"] <= pick < segment["start_pick"] + segment["pick_count"]:
            offset = (pick - segment["start_pick"]) % len(segment["repeat_yarns"])
            return segment["repeat_yarns"][offset]
    raise IndexError(pick)


class LargeRepeatTests(unittest.TestCase):
    def large_case(self):
        warp = [{"shaft": 1 + i % 2, "yarn": "a" if i % 2 == 0 else "b"}
                for i in range(128)]
        picks = [pk("T1" if i % 2 == 0 else "T2", "a" if i % 2 == 0 else "b")
                 for i in range(128)]
        data = {"common": common(warp=warp, repeat_x=1000, takeup_permille=0,
                                 warp_waste_mm=0, float_limit=1),
                "jobs": [{"id": "BIG", "value": 1, "picks": picks,
                          "repeat_y": 1000, "phases": [0]}],
                "bridges": [],
                "stock": [{"yarn": "a", "length_mm": 10**12},
                          {"yarn": "b", "length_mm": 10**12}],
                "max_length_mm": 10**12}
        allocations = []
        for base in range(128):
            yarn = "a" if base % 2 == 0 else "b"
            allocations.append(
                {"id": f"w{base}", "revision": 1, "known_at": 1,
                 "payload": {"orientation": "warp", "base_end": base,
                             "repeat_start": 0, "repeat_stop": 1000,
                             "pick_start": 0, "pick_stop": 128000,
                             "lot_id": yarn.upper()}})
        allocations += [
            {"id": "fa", "revision": 1, "known_at": 1,
             "payload": {"orientation": "weft", "pick_start": 0, "pick_stop": 128000,
                         "lot_id": "A"}},
            {"id": "fb", "revision": 1, "known_at": 1,
             "payload": {"orientation": "weft", "pick_start": 0, "pick_stop": 128000,
                         "lot_id": "B"}}]
        return {"batch": data,
                "lots": [{"id": "A", "yarn": "a", "capacity_mm": 10**12},
                         {"id": "B", "yarn": "b", "capacity_mm": 10**12}],
                "allocations": allocations,
                "certificates": [
                    {"id": "ca", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "A", "pick_start": 0, "pick_stop": 128000,
                                 "verdict": "pass"}},
                    {"id": "cb", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "B", "pick_start": 0, "pick_stop": 128000,
                                 "verdict": "pass"}}],
                "shipments": [{"id": "S1", "requested_at": 1, "end_start": 0,
                               "end_stop": 128000, "pick_start": 0,
                               "pick_stop": 128000}],
                "as_of": 2}

    def test_huge_repeats_are_exact_and_compressed(self):
        data = self.large_case()
        report = run_trace(data)
        self.assertEqual(report["summary"], {"shipments": 1, "released": 1,
                                             "recall": 0, "ready": 0, "hold": 0,
                                             "whole_unattributed_mm": 0})
        self.assertEqual(report["lot_balances"],
                         [{"lot_id": "A", "yarn": "a", "capacity_mm": 10**12,
                           "warp_mm": 8_192_000_000, "weft_mm": 8_192_000_000,
                           "attributed_mm": 16_384_000_000,
                           "remaining_mm": 10**12 - 16_384_000_000, "overdrawn": False},
                          {"lot_id": "B", "yarn": "b", "capacity_mm": 10**12,
                           "warp_mm": 8_192_000_000, "weft_mm": 8_192_000_000,
                           "attributed_mm": 16_384_000_000,
                           "remaining_mm": 10**12 - 16_384_000_000, "overdrawn": False}])
        entry = decision(report, "S1")
        self.assertEqual(entry["missing"], {"warp_points": 0, "weft_picks": 0,
                                            "material_mm": 0, "first": None})
        self.assertEqual([(row["lot_id"], row["warp_mm"], row["weft_mm"])
                          for row in entry["lots"]],
                         [("A", 8_192_000_000, 8_192_000_000),
                          ("B", 8_192_000_000, 8_192_000_000)])
        # No per-pick or per-end ledger is emitted.
        self.assertEqual(set(entry["lots"][0]),
                         {"lot_id", "yarn", "warp_mm", "weft_mm", "total_mm",
                          "whole_attributed_mm", "overdrawn", "allocation_versions",
                          "certificate_versions"})

    def test_large_gap_is_reported_as_counts(self):
        data = self.large_case()
        data["allocations"] = [row for row in data["allocations"]
                               if row["id"] != "w0"]
        report = run_trace(data)
        entry = decision(report, "S1")
        self.assertFalse(entry["allowed"])
        self.assertEqual(entry["missing"]["warp_points"], 128000 * 1000)
        self.assertEqual(entry["missing"]["material_mm"], 128000 * 1000)
        self.assertEqual(entry["missing"]["first"],
                         {"orientation": "warp", "end_index": 0, "pick_index": 0})
        self.assertEqual(report["whole_unattributed_mm"], 128000 * 1000)


if __name__ == "__main__":
    unittest.main()


class FragmentationTests(unittest.TestCase):
    def two_yarn_batch(self, repeat_y=1):
        picks = [pk("T1", "f"), pk("T2", "g"), pk("T1", "f"), pk("T2", "g")]
        return batch([{"id": "A", "value": 1, "repeat_y": repeat_y, "phases": [0],
                       "picks": picks}],
                     stock=(("w", 10**6), ("f", 10**6), ("g", 10**6)))

    def test_same_lot_pools_warp_and_weft(self):
        data = key_case()
        for end in data["batch"]["common"]["warp"]:
            end["yarn"] = "f"
        data["batch"]["stock"] = [{"yarn": "f", "length_mm": 10**6}]
        data["lots"] = [{"id": "F", "yarn": "f", "capacity_mm": 24}]
        data["allocations"] = [
            {"id": "w0", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 4, "lot_id": "F"}},
            {"id": "w1", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 1, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 4, "lot_id": "F"}},
            {"id": "we", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}}]
        data["certificates"] = [
            {"id": "c", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}}]
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 0,
                              "end_stop": 2, "pick_start": 0, "pick_stop": 4}]
        report = run_trace(data)
        balance = report["lot_balances"][0]
        self.assertEqual((balance["warp_mm"], balance["weft_mm"], balance["attributed_mm"]),
                         (12, 12, 24))
        entry = decision(report, "S")
        self.assertEqual((lot_entry(entry, "F")["warp_mm"],
                          lot_entry(entry, "F")["weft_mm"]), (10, 12))

    def test_unshipped_cloth_counts_toward_capacity(self):
        data = key_case()
        # Only S is requested, but capacity 11 must be measured over the whole 12mm batch.
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 0,
                              "end_stop": 1, "pick_start": 0, "pick_stop": 1}]
        data["lots"][1]["capacity_mm"] = 11
        report = run_trace(data)
        entry = decision(report, "S")
        self.assertEqual([(p["kind"], p["lot_id"]) for p in entry["problems"]],
                         [("overdrawn", "F")])
        self.assertLess(lot_entry(entry, "F")["weft_mm"], 11)

    def test_open_beam_waste_is_charged_once_per_end(self):
        data = key_case()
        # split the warp accounting over two disjoint repeat ranges
        data["batch"]["common"]["repeat_x"] = 2
        data["allocations"] = [
            {"id": "a", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "b", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 1,
              "repeat_stop": 2, "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "c", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 1, "repeat_start": 0,
              "repeat_stop": 2, "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "wf", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}}]
        data["certificates"] = [
            {"id": "cw", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "W", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}}]
        for shipment in data["shipments"]:
            shipment["end_stop"] = min(shipment["end_stop"], 4)
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 0,
                              "end_stop": 4, "pick_start": 0, "pick_stop": 4}]
        report = run_trace(data)
        # E = 2 base ends * repeat_x 2 = 4 real ends; waste 1mm each = 4mm.
        self.assertEqual(report["lot_balances"][2]["warp_mm"], 20 + 4)
        self.assertEqual(report["whole_unattributed_mm"], 0)

    def test_weft_allocation_only_claims_its_own_yarn_picks(self):
        data = {
            "batch": self.two_yarn_batch(),
            "lots": [{"id": "F", "yarn": "f", "capacity_mm": 100},
                     {"id": "G", "yarn": "g", "capacity_mm": 100}],
            "allocations": [
                {"id": "af", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}},
                {"id": "ag", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "G"}}],
            "certificates": [
                {"id": "cf", "revision": 1, "known_at": 1, "payload":
                 {"lot_id": "F", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}},
                {"id": "cg", "revision": 1, "known_at": 1, "payload":
                 {"lot_id": "G", "pick_start": 0, "pick_stop": 4, "verdict": "pass"}}],
            "shipments": [{"id": "S", "requested_at": 1, "end_start": 0, "end_stop": 2,
                           "pick_start": 0, "pick_stop": 4}],
            "as_of": 2}
        report = run_trace(data)
        entry = decision(report, "S")
        # two f strips and two g strips; each whole strip is H(2)=3mm wide.
        self.assertEqual((lot_entry(entry, "F")["weft_mm"],
                          lot_entry(entry, "G")["weft_mm"]), (6, 6))
        # No warp allocation exists, so the whole warp beam stays unattributed.
        self.assertEqual(report["whole_unattributed_mm"], 12)

    def test_period_rotation_and_seam_identity(self):
        # A and B each float 2 picks of shaft 1; without a seam the junction
        # would be 4 picks and exceed float_limit=2, so a seam is mandatory.
        jobs = [{"id": "A", "value": 1, "repeat_y": 1, "phases": [0],
                 "picks": [pk("T1", "f"), pk("T1", "f")]},
                {"id": "B", "value": 1, "repeat_y": 1, "phases": [0],
                 "picks": [pk("T1", "f"), pk("T1", "f")]}]
        bridges = [{"id": "BR", "picks": [pk("T2", "g")]}]
        stock = (("w", 10**6), ("f", 10**6), ("g", 10**6))
        plan = run_plan(batch(jobs, bridges=bridges, stock=stock,
                              max_length_mm=100, float_limit=2))
        seams = [s for s in plan["segments"] if s["kind"] == "bridge"]
        self.assertEqual([s["id"] for s in seams], ["BR"])
        P = plan["material_requirements"]["total_picks"]
        data = {
            "batch": batch(jobs, bridges=bridges, stock=stock,
                           max_length_mm=100, float_limit=2),
            "lots": [{"id": "W", "yarn": "w", "capacity_mm": 1000},
                     {"id": "F", "yarn": "f", "capacity_mm": 1000},
                     {"id": "G", "yarn": "g", "capacity_mm": 1000}],
            "allocations": [
                {"id": "w0", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "warp", "base_end": 0, "repeat_start": 0,
                  "repeat_stop": 1, "pick_start": 0, "pick_stop": P, "lot_id": "W"}},
                {"id": "w1", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "warp", "base_end": 1, "repeat_start": 0,
                  "repeat_stop": 1, "pick_start": 0, "pick_stop": P, "lot_id": "W"}},
                {"id": "af", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "weft", "pick_start": 0, "pick_stop": P, "lot_id": "F"}},
                {"id": "ag", "revision": 1, "known_at": 1, "payload":
                 {"orientation": "weft", "pick_start": 0, "pick_stop": P, "lot_id": "G"}}],
            "certificates": [
                {"id": "cw", "revision": 1, "known_at": 1, "payload":
                 {"lot_id": "W", "pick_start": 0, "pick_stop": P, "verdict": "pass"}},
                {"id": "cf", "revision": 1, "known_at": 1, "payload":
                 {"lot_id": "F", "pick_start": 0, "pick_stop": P, "verdict": "pass"}},
                {"id": "cg", "revision": 1, "known_at": 1, "payload":
                 {"lot_id": "G", "pick_start": 0, "pick_stop": P, "verdict": "pass"}}],
            "shipments": [{"id": "S", "requested_at": 1, "end_start": 0, "end_stop": 2,
                           "pick_start": 0, "pick_stop": P}],
            "as_of": 2}
        report = run_trace(data)
        counts = {}
        for segment in plan["segments"]:
            for yarn in segment["repeat_yarns"]:
                counts[yarn] = counts.get(yarn, 0) + 1
        self.assertEqual(counts, {"f": 4, "g": 1})
        entry = decision(report, "S")
        self.assertEqual(lot_entry(entry, "F")["weft_mm"], 4 * 3)
        self.assertEqual(lot_entry(entry, "G")["weft_mm"], 1 * 3)
        self.assertEqual(report["whole_unattributed_mm"], 0)

    def test_input_order_does_not_change_versions(self):
        data = fixture("trace_input.json")
        for seed in range(12):
            shuffled = copy.deepcopy(data)
            rng = random.Random(seed)
            rng.shuffle(shuffled["allocations"])
            rng.shuffle(shuffled["certificates"])
            rng.shuffle(shuffled["shipments"])
            rng.shuffle(shuffled["lots"])
            self.assertEqual(run_trace(data), run_trace(shuffled))

    def test_future_versions_do_not_leak_into_history(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 9, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "G"}})
        data["certificates"].append(
            {"id": "cf", "revision": 2, "known_at": 10, "payload": None})
        data["as_of"] = 8
        report = run_trace(data)
        shrunk = copy.deepcopy(data)
        shrunk["as_of"] = 4
        self.assertEqual([s["historical"] for s in report["shipments"]],
                         [s["historical"] for s in run_trace(shrunk)["shipments"]])
        # the future transfer never appears in the historical decisions
        for entry in report["shipments"]:
            self.assertNotIn("G", [row["lot_id"] for row in entry["historical"]["lots"]])


class ConservationTests(unittest.TestCase):
    def test_attributed_plus_unattributed_equals_plan_total(self):
        for case in (key_case(), fixture("trace_input.json"),
                     fixture("trace_recall.json")):
            report = run_trace(case)
            total = sum(row["length_mm"]
                        for row in report["production"]["material_requirements"]["yarn"])
            attributed = sum(row["attributed_mm"] for row in report["lot_balances"])
            self.assertEqual(attributed + report["whole_unattributed_mm"], total)

    def test_partial_coverage_reconciles(self):
        data = key_case()
        data["allocations"] = [row for row in data["allocations"] if row["id"] != "aw1"]
        report = run_trace(data)
        total = sum(row["length_mm"]
                    for row in report["production"]["material_requirements"]["yarn"])
        attributed = sum(row["attributed_mm"] for row in report["lot_balances"])
        self.assertEqual(attributed + report["whole_unattributed_mm"], total)
        self.assertGreater(report["whole_unattributed_mm"], 0)

    def test_python_and_cli_output_agree(self):
        import os
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "in.json"
            output = Path(folder) / "out.json"
            text = (FIXTURES / "trace_recall.json").read_text()
            source.write_text(text)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(FIXTURES.parent / "src")
            result = subprocess.run(
                [sys.executable, "-m", "loom_draft.trace_cli", "--source", str(source),
                 "--output", str(output)],
                capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            from_python = run_trace(json.loads(text))
            from_cli = json.loads(output.read_text())
            self.assertEqual(from_python, from_cli)
            self.assertEqual(json.loads(result.stdout), from_python["summary"])


class DictionaryOutputTests(unittest.TestCase):
    def test_return_value_is_plain_json(self):
        report = run_trace(key_case())
        encoded = json.dumps(report, allow_nan=False)
        self.assertEqual(json.loads(encoded), report)
        self.assertIsInstance(report, dict)
        self.assertIsInstance(report["summary"], dict)
        self.assertIsInstance(report["lot_balances"], list)

    def test_schema_and_top_level_keys(self):
        report = run_trace(key_case())
        self.assertEqual(report["schema_version"], 1)
        for key in ("production", "lot_balances", "shipments", "summary"):
            self.assertIn(key, report)
        self.assertEqual(set(report["production"]),
                         {"plan", "segments", "material_requirements"})
        entry = report["shipments"][0]
        self.assertEqual(set(entry), {"id", "disposition", "historical", "current"})
        for which in ("historical", "current"):
            self.assertEqual(set(entry[which]),
                             {"knowledge_at", "allowed", "lots", "missing", "problems"})
            self.assertEqual(set(entry[which]["missing"]),
                             {"warp_points", "weft_picks", "material_mm", "first"})
        self.assertEqual(set(report["summary"]),
                         {"shipments", "released", "recall", "ready", "hold",
                          "whole_unattributed_mm"})


class ResourceBoundTests(unittest.TestCase):
    def test_many_allocations_and_shipments_stay_compressed(self):
        warp = [{"shaft": 1 + i % 2, "yarn": "w"} for i in range(128)]
        picks = [pk("T1" if i % 2 == 0 else "T2", "f") for i in range(128)]
        base = {"common": common(warp=warp, repeat_x=1000, takeup_permille=0,
                                 warp_waste_mm=0, float_limit=1),
                "jobs": [{"id": "BIG", "value": 1, "picks": picks,
                          "repeat_y": 1000, "phases": [0]}],
                "bridges": [],
                "stock": [{"yarn": "w", "length_mm": 10**12},
                          {"yarn": "f", "length_mm": 10**12}],
                "max_length_mm": 10**12}
        P = 128000
        allocations = [{"id": "wf", "revision": 1, "known_at": 1,
                        "payload": {"orientation": "weft", "pick_start": 0,
                                    "pick_stop": P, "lot_id": "F"}}]
        for index in range(1023):
            base_end = index % 128
            start = (index * 125) % (P - 100)
            allocations.append(
                {"id": f"w{index}", "revision": 1, "known_at": 1 + index % 7,
                 "payload": {"orientation": "warp", "base_end": base_end,
                             "repeat_start": 0, "repeat_stop": 1000,
                             "pick_start": start, "pick_stop": start + 100,
                             "lot_id": "W"}})
        shipments = []
        for index in range(128):
            start = index * 1000
            shipments.append({"id": f"S{index}", "requested_at": index % 8,
                              "end_start": (index * 1000) % 128000,
                              "end_stop": (index * 1000) % 128000 + 1000,
                              "pick_start": start, "pick_stop": start + 1000})
        data = {"batch": base,
                "lots": [{"id": "W", "yarn": "w", "capacity_mm": 10**12},
                         {"id": "F", "yarn": "f", "capacity_mm": 10**12}],
                "allocations": allocations,
                "certificates": [
                    {"id": "cw", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "W", "pick_start": 0, "pick_stop": P,
                                 "verdict": "pass"}},
                    {"id": "cf", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "F", "pick_start": 0, "pick_stop": P,
                                 "verdict": "pass"}}],
                "shipments": shipments, "as_of": 8}
        report = run_trace(data)
        self.assertEqual(report["summary"]["shipments"], 128)
        total = sum(row["length_mm"]
                    for row in report["production"]["material_requirements"]["yarn"])
        attributed = sum(row["attributed_mm"] for row in report["lot_balances"])
        self.assertEqual(attributed + report["whole_unattributed_mm"], total)
        self.assertEqual(len(report["production"]["segments"]), 1)


class EvidenceScopeTests(unittest.TestCase):
    def test_certificate_outside_used_picks_is_not_referenced(self):
        data = key_case()
        data["certificates"].append(
            {"id": "late", "revision": 1, "known_at": 4, "payload":
             {"lot_id": "F", "pick_start": 2, "pick_stop": 4, "verdict": "fail"}})
        data["as_of"] = 5
        # S uses picks [0,2); the new fail covers only [2,4) and must not touch it.
        report = run_trace(data)
        self.assertEqual(item(report, "S")["disposition"], "released")
        refs = [c["id"] for c in lot_entry(decision(report, "S"), "F")["certificate_versions"]]
        self.assertNotIn("late", refs)
        # U uses [2,4), so it is affected.
        self.assertEqual(item(report, "U")["disposition"], "recall")

    def test_fail_on_foreign_yarn_pick_does_not_fail_lot(self):
        data = key_case()
        data["batch"] = batch([{"id": "A", "value": 1, "repeat_y": 1, "phases": [0],
                                "picks": [pk("T1", "f"), pk("T2", "g")]}],
                              stock=(("w", 10**6), ("f", 10**6), ("g", 10**6)))
        data["lots"] = [{"id": "W", "yarn": "w", "capacity_mm": 100},
                        {"id": "F", "yarn": "f", "capacity_mm": 100},
                        {"id": "G", "yarn": "g", "capacity_mm": 100}]
        data["allocations"] = [
            {"id": "w0", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 0, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 2, "lot_id": "W"}},
            {"id": "w1", "revision": 1, "known_at": 1, "payload":
             {"orientation": "warp", "base_end": 1, "repeat_start": 0,
              "repeat_stop": 1, "pick_start": 0, "pick_stop": 2, "lot_id": "W"}},
            {"id": "af", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "F"}},
            {"id": "ag", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 2, "lot_id": "G"}}]
        data["certificates"] = [
            {"id": "cw", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "W", "pick_start": 0, "pick_stop": 2, "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 0, "pick_stop": 2, "verdict": "pass"}},
            {"id": "cg", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "G", "pick_start": 0, "pick_stop": 2, "verdict": "pass"}},
            {"id": "bad-f", "revision": 1, "known_at": 1, "payload":
             {"lot_id": "F", "pick_start": 1, "pick_stop": 2, "verdict": "fail"}}]
        data["shipments"] = [{"id": "S", "requested_at": 1, "end_start": 0,
                              "end_stop": 2, "pick_start": 0, "pick_stop": 2}]
        data["as_of"] = 2
        report = run_trace(data)
        # pick 1 is yarn g, so F's fail on [1,2) covers no F use.
        self.assertEqual(item(report, "S")["disposition"], "released")

    def test_negative_remaining_and_unused_lot_listed(self):
        data = key_case()
        data["lots"].append({"id": "UNUSED", "yarn": "z", "capacity_mm": 5})
        data["lots"][1]["capacity_mm"] = 3
        report = run_trace(data)
        rows = {row["lot_id"]: row for row in report["lot_balances"]}
        self.assertEqual(rows["UNUSED"], {"lot_id": "UNUSED", "yarn": "z",
                                          "capacity_mm": 5, "warp_mm": 0, "weft_mm": 0,
                                          "attributed_mm": 0, "remaining_mm": 5,
                                          "overdrawn": False})
        self.assertEqual(rows["F"]["remaining_mm"], -9)
        self.assertTrue(rows["F"]["overdrawn"])

    def test_unknown_weft_end_index_uses_shipment_end_start(self):
        data = key_case()
        data["allocations"] = [row for row in data["allocations"] if row["id"] != "wf"]
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 1,
                              "end_stop": 2, "pick_start": 0, "pick_stop": 2}]
        report = run_trace(data)
        missing = decision(report, "S")["missing"]
        self.assertEqual(missing["weft_picks"], 2)
        self.assertEqual(missing["first"],
                         {"orientation": "weft", "end_index": 1, "pick_index": 0})


class DispositionTests(unittest.TestCase):
    def test_ready_when_gap_fixed_after_request(self):
        data = key_case()
        data["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 5, "payload": None})
        data["certificates"].append(
            {"id": "cf", "revision": 2, "known_at": 5, "payload": None})
        data["as_of"] = 7
        report = run_trace(data)
        # At requested_at=3 the weft allocation existed; at as_of=7 it is withdrawn.
        for entry in report["shipments"]:
            self.assertTrue(entry["historical"]["allowed"])
            self.assertFalse(entry["current"]["allowed"])
            self.assertEqual(entry["disposition"], "recall")

    def test_ready_is_not_released(self):
        data = key_case()
        # A source gap at request time (weft withdrawn at 2) then restored at 5.
        data["allocations"] = [
            row for row in data["allocations"] if row["id"] != "wf"]
        data["allocations"] += [
            {"id": "wf", "revision": 1, "known_at": 1, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}},
            {"id": "wf", "revision": 2, "known_at": 2, "payload": None},
            {"id": "wf", "revision": 3, "known_at": 5, "payload":
             {"orientation": "weft", "pick_start": 0, "pick_stop": 4, "lot_id": "F"}}]
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 0,
                              "end_stop": 1, "pick_start": 0, "pick_stop": 2}]
        data["as_of"] = 6
        report = run_trace(data)
        entry = item(report, "S")
        self.assertFalse(entry["historical"]["allowed"])
        self.assertTrue(entry["current"]["allowed"])
        self.assertEqual(entry["disposition"], "ready")
        self.assertEqual(entry["current"]["lots"][0]["allocation_versions"],
                         [{"id": "wf", "revision": 3}])
        self.assertEqual([p["kind"] for p in entry["historical"]["problems"]],
                         ["source_gap"])

    def test_hold_when_never_allowed(self):
        data = key_case()
        data["allocations"] = [row for row in data["allocations"] if row["id"] != "wf"]
        data["shipments"] = [{"id": "S", "requested_at": 3, "end_start": 0,
                              "end_stop": 1, "pick_start": 0, "pick_stop": 2}]
        report = run_trace(data)
        self.assertEqual(item(report, "S")["disposition"], "hold")
        self.assertEqual(report["summary"]["hold"], 1)
