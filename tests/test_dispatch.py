"""Tests for loom-dispatch: real cut delivery ledger and recall hand-off.

The quality oracle is loom-trace itself (already independently checked against
the naive point-by-point engine in test_trace.py): every ledger decision must
equal ``trace.decide`` on the same source.  Persistence, request identity,
rectangle occupancy and notification correctness are asserted from independent
recomputation — never from the ledger's own replay output.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loom_draft import dispatch
from loom_draft.trace import Shipment, Snapshot, decide

SRC = Path(__file__).resolve().parent.parent / "src"


# ---------------------------------------------------------------------------
# synthetic cloth builders
# ---------------------------------------------------------------------------

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


def one_job_batch(picks=None, yarn="f", repeat_y=1, **common_overrides):
    if picks is None:
        picks = [pk("T1", yarn), pk("T2", yarn), pk("T1", yarn), pk("T2", yarn)]
    return {"common": common(**common_overrides),
            "jobs": [{"id": "A", "value": 1, "repeat_y": repeat_y,
                      "phases": [0], "picks": picks}],
            "bridges": [],
            "stock": [{"yarn": "w", "length_mm": 10**6},
                      {"yarn": "f", "length_mm": 10**6},
                      {"yarn": "g", "length_mm": 10**6}],
            "max_length_mm": 10**6}


def warp_alloc(rid, base_end, lot, start=0, stop=4, known_at=1):
    return {"id": rid, "revision": 1, "known_at": known_at, "payload": {
        "orientation": "warp", "base_end": base_end, "repeat_start": 0,
        "repeat_stop": 1, "pick_start": start, "pick_stop": stop, "lot_id": lot}}


def weft_alloc(rid, lot, start=0, stop=4, known_at=1, revision=1, payload_override=None):
    payload = {"orientation": "weft", "pick_start": start, "pick_stop": stop,
               "lot_id": lot}
    if payload_override is not None:
        payload = payload_override
    return {"id": rid, "revision": revision, "known_at": known_at, "payload": payload}


def cert(rid, lot, start, stop, verdict, known_at=1, revision=1):
    return {"id": rid, "revision": revision, "known_at": known_at,
            "payload": {"lot_id": lot, "pick_start": start, "pick_stop": stop,
                        "verdict": verdict}}


def source(batch=None, lots=None, allocations=None, certificates=None, as_of=3):
    return {"batch": batch or one_job_batch(),
            "lots": lots or [
                {"id": "W", "yarn": "w", "capacity_mm": 12},
                {"id": "F", "yarn": "f", "capacity_mm": 12}],
            "allocations": allocations if allocations is not None else [
                warp_alloc("aw0", 0, "W"), warp_alloc("aw1", 1, "W"),
                weft_alloc("wf", "F")],
            "certificates": certificates if certificates is not None else [
                cert("cw", "W", 0, 4, "pass"), cert("cf", "F", 0, 4, "pass")],
            "shipments": [], "as_of": as_of}


CUT_S = {"cut_id": "S", "end_start": 0, "end_stop": 1,
         "pick_start": 0, "pick_stop": 2}
CUT_R = {"cut_id": "R", "end_start": 1, "end_stop": 2,
         "pick_start": 0, "pick_stop": 2}
CUT_U = {"cut_id": "U", "end_start": 0, "end_stop": 2,
         "pick_start": 2, "pick_stop": 4}


def trace_decision(src, cut, at=None):
    """Independent decision via the trace engine's own public pieces."""
    knowledge = dispatch.Knowledge(src)
    at = src["as_of"] if at is None else at
    snapshot = Snapshot(knowledge.geometry, knowledge.lots, knowledge.allocations,
                        knowledge.certificates, at)
    shipment = Shipment(cut["cut_id"], at, cut["end_start"], cut["end_stop"],
                        cut["pick_start"], cut["pick_stop"])
    return decide(snapshot, shipment)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="loom-dispatch-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.src = source()

    def init(self, src=None, request_id="req-init"):
        return dispatch.apply(self.dir, {"op": "init", "request_id": request_id,
                                         "source": src or self.src})

    def ingest(self, src, request_id, expected):
        return dispatch.apply(self.dir, {"op": "ingest", "request_id": request_id,
                                         "expected_generation": expected, "source": src})

    def deliver(self, cuts, request_id, expected):
        return dispatch.apply(self.dir, {"op": "dispatch", "request_id": request_id,
                                         "expected_generation": expected, "cuts": cuts})

    def ack(self, event_ids, request_id):
        return dispatch.apply(self.dir, {"op": "ack", "request_id": request_id,
                                         "event_ids": event_ids})

    def view(self):
        return dispatch.read_store(self.dir)


# ---------------------------------------------------------------------------
# acceptance scenario from the contract
# ---------------------------------------------------------------------------

class AcceptanceScenarioTests(LedgerTestCase):
    def test_full_recall_clear_lifecycle_and_frozen_credentials(self):
        init = self.init()
        self.assertEqual(init["status"], "accepted")
        self.assertEqual(init["generation"], 1)
        self.assertEqual(init["result"], {"as_of": 3, "transition_event_ids": []})

        d1 = self.deliver([CUT_S, CUT_R], "D1", 1)
        self.assertEqual(d1["status"], "accepted")
        self.assertEqual([d["cut_id"] for d in d1["result"]["deliveries"]], ["R", "S"])
        by_id = {d["cut_id"]: d for d in d1["result"]["deliveries"]}
        s_lots = {x["lot_id"]: x for x in by_id["S"]["admitted_decision"]["lots"]}
        r_lots = {x["lot_id"]: x for x in by_id["R"]["admitted_decision"]["lots"]}
        # S: warp 3mm, weft 4mm; R: warp 3mm, weft 2mm — no per-cut re-rounding.
        self.assertEqual((s_lots["W"]["warp_mm"], s_lots["F"]["weft_mm"]), (3, 4))
        self.assertEqual((r_lots["W"]["warp_mm"], r_lots["F"]["weft_mm"]), (3, 2))
        for credential in d1["result"]["deliveries"]:
            self.assertEqual(credential["admitted_generation"], 1)
            self.assertEqual(credential["admitted_at"], 3)
            self.assertEqual(credential["request_id"], "D1")
            self.assertTrue(credential["admitted_decision"]["allowed"])
            self.assertEqual(credential["admitted_decision"]["knowledge_at"], 3)
        # credentials equal the trace decision on the same source.
        for cut in (CUT_S, CUT_R):
            self.assertEqual(by_id[cut["cut_id"]]["admitted_decision"],
                             trace_decision(self.src, cut, 3))
        # U was never requested: it must not appear in the real ledger.
        view = self.view()
        self.assertEqual({d["cut_id"] for d in view["deliveries"]}, {"S", "R"})
        self.assertEqual(view["summary"], {"delivered_cuts": 2, "normal": 2,
                                           "recall": 0, "events": 0,
                                           "pending_events": 0})

        # generation 2: independent fail on F pick 1 known at 5.
        src2 = copy.deepcopy(self.src)
        src2["certificates"].append(cert("ff", "F", 1, 2, "fail", known_at=5))
        src2["as_of"] = 6
        ing2 = self.ingest(src2, "ing-2", 1)
        self.assertEqual(ing2["status"], "accepted")
        self.assertEqual(ing2["generation"], 2)
        self.assertEqual(ing2["result"]["as_of"], 6)
        self.assertEqual(ing2["result"]["transition_event_ids"], [1, 2])
        view = self.view()
        self.assertEqual({d["cut_id"]: d["state"] for d in view["deliveries"]},
                         {"S": "recall", "R": "recall"})
        kinds = [(e["event_id"], e["kind"], e["cut_id"]) for e in view["events"]]
        self.assertEqual(kinds, [(1, "recall", "R"), (2, "recall", "S")])
        for event in view["events"]:
            self.assertEqual(event["generation"], 2)
            self.assertEqual(event["knowledge_at"], 6)
            self.assertFalse(event["acknowledged"])
            self.assertFalse(event["decision"]["allowed"])
        # the original D1 receipt and credentials stay byte-identical in meaning.
        replay = self.deliver([CUT_S, CUT_R], "D1", 1)
        self.assertEqual(replay, d1)
        view = self.view()
        for delivery in view["deliveries"]:
            original = by_id[delivery["cut_id"]]
            self.assertEqual(delivery["admitted_decision"],
                             original["admitted_decision"])
            self.assertEqual(delivery["admitted_generation"], 1)

        # same area, different cut id: recall never frees the area.
        overlap = self.deliver([{**CUT_S, "cut_id": "S2"}], "D2", 2)
        self.assertEqual(overlap["status"], "rejected")
        self.assertEqual(overlap["generation"], 2)
        self.assertEqual(overlap["result"]["code"], "cloth_overlap")
        self.assertEqual(overlap["result"]["rejected_cut_ids"], ["S2"])
        self.assertEqual(overlap["result"]["conflicting_cut_ids"], ["S"])
        self.assertEqual(len(self.view()["deliveries"]), 2)

        # generation 3: withdraw the fail at 7.
        src3 = copy.deepcopy(src2)
        src3["certificates"].append(cert("ff", "F", 1, 2, "fail",
                                         known_at=7, revision=2)
                                    | {"payload": None})
        src3["as_of"] = 8
        ing3 = self.ingest(src3, "ing-3", 2)
        self.assertEqual(ing3["result"]["transition_event_ids"], [3, 4])
        view = self.view()
        self.assertEqual({d["cut_id"]: d["state"] for d in view["deliveries"]},
                         {"S": "normal", "R": "normal"})
        self.assertEqual([(e["event_id"], e["kind"]) for e in view["events"]],
                         [(1, "recall"), (2, "recall"), (3, "clear"), (4, "clear")])
        # old recalls remain pending even though quality recovered.
        self.assertEqual(view["pending_event_ids"], [1, 2, 3, 4])

        # ack batch containing an unknown id: nothing gets acknowledged.
        ack_bad = self.ack([1, 2, 99], "ack-bad")
        self.assertEqual(ack_bad["status"], "rejected")
        self.assertEqual(ack_bad["result"]["code"], "unknown_event")
        self.assertEqual(ack_bad["result"]["unknown_event_ids"], [99])
        self.assertEqual(self.view()["pending_event_ids"], [1, 2, 3, 4])

        ack = self.ack([4, 3, 2, 1], "ack-ok")
        self.assertEqual(ack["status"], "accepted")
        self.assertEqual(ack["result"]["acknowledged_event_ids"], [1, 2, 3, 4])
        self.assertEqual(self.view()["pending_event_ids"], [])
        # re-ack is idempotent and creates no event.
        ack_again = self.ack([1, 2], "ack-again")
        self.assertEqual(ack_again["status"], "accepted")
        self.assertEqual(ack_again["result"]["acknowledged_event_ids"], [1, 2])
        self.assertEqual(len(self.view()["events"]), 4)

        # generation 4: a new independent fail inside the delivered area makes
        # fresh recall events rather than reusing acknowledged ones.
        src4 = copy.deepcopy(src3)
        src4["certificates"].append(cert("ff2", "F", 0, 1, "fail", known_at=9))
        src4["as_of"] = 10
        ing4 = self.ingest(src4, "ing-4", 3)
        self.assertEqual(ing4["result"]["transition_event_ids"], [5, 6])
        view = self.view()
        self.assertEqual([(e["event_id"], e["kind"], e["acknowledged"])
                          for e in view["events"][4:]],
                         [(5, "recall", False), (6, "recall", False)])
        self.assertEqual(view["pending_event_ids"], [5, 6])
        self.assertEqual(view["summary"], {"delivered_cuts": 2, "normal": 0,
                                           "recall": 2, "events": 6,
                                           "pending_events": 2})

    def test_reopen_preserves_everything(self):
        self.init()
        self.deliver([CUT_S, CUT_R], "D1", 1)
        src2 = copy.deepcopy(self.src)
        src2["certificates"].append(cert("ff", "F", 1, 2, "fail", known_at=5))
        src2["as_of"] = 6
        ing = self.ingest(src2, "ing-2", 1)
        self.ack([1, 2], "ack-1")
        before = self.view()
        reopened = dispatch.read_store(self.dir)
        self.assertEqual(reopened, before)
        self.assertEqual(ing, dispatch.apply(
            self.dir, {"op": "ingest", "request_id": "ing-2",
                       "expected_generation": 1, "source": src2}))


class D0GapScenarioTests(LedgerTestCase):
    """Independent cloth: a gap-blocked request stays blocked forever."""

    def _gap_source(self):
        # No weft allocation at all: every cut has a source gap.
        return source(allocations=[warp_alloc("aw0", 0, "W"),
                                   warp_alloc("aw1", 1, "W")],
                      certificates=[cert("cw", "W", 0, 4, "pass")], as_of=3)

    def test_d0_remains_trace_blocked_after_the_gap_is_filled(self):
        gap_src = self._gap_source()
        self.init(gap_src, request_id="init-gap")
        d0 = {"op": "dispatch", "request_id": "D0", "expected_generation": 1,
              "cuts": [CUT_S]}
        rejected = dispatch.apply(self.dir, d0)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["result"]["code"], "trace_blocked")
        self.assertEqual(rejected["result"]["rejected_cut_ids"], ["S"])
        self.assertEqual(rejected["result"]["conflicting_cut_ids"], [])
        decisions = rejected["result"]["decisions"]
        self.assertEqual([d["cut_id"] for d in decisions], ["S"])
        self.assertFalse(decisions[0]["decision"]["allowed"])
        self.assertEqual([p["kind"] for p in decisions[0]["decision"]["problems"]],
                         ["source_gap"])
        self.assertEqual(decisions[0]["decision"], trace_decision(gap_src, CUT_S, 3))
        self.assertEqual(self.view()["deliveries"], [])

        # later knowledge fills the weft coverage with genuinely new evidence...
        full = copy.deepcopy(gap_src)
        full["allocations"].append(weft_alloc("wf", "F", known_at=5))
        full["certificates"].append(cert("cf", "F", 0, 4, "pass", known_at=5))
        full["as_of"] = 6
        self.ingest(full, "ing-fill", 1)
        # ...but retrying D0 returns the original rejection unchanged.
        replay = dispatch.apply(self.dir, copy.deepcopy(d0))
        self.assertEqual(replay, rejected)
        self.assertEqual(self.view()["deliveries"], [])
        # a fresh request id can now dispatch the same rectangle.
        fresh = self.deliver([CUT_S], "D1-new", 2)
        self.assertEqual(fresh["status"], "accepted")
        self.assertEqual(len(self.view()["deliveries"]), 1)


# ---------------------------------------------------------------------------
# dispatch rejection precedence and diagnostics
# ---------------------------------------------------------------------------

class DispatchRejectionTests(LedgerTestCase):
    def test_stale_generation_precedes_other_checks(self):
        self.init()
        self.deliver([CUT_S], "D1", 1)
        # a dispatch expecting generation 1 after an ingest: stale wins even
        # though the cut also overlaps and is recalled.
        src2 = copy.deepcopy(self.src)
        src2["certificates"].append(cert("ff", "F", 0, 1, "fail", known_at=5))
        src2["as_of"] = 6
        self.ingest(src2, "ing", 1)
        reply = self.deliver([CUT_S], "D-stale", 1)
        self.assertEqual(reply["result"]["code"], "stale_generation")
        self.assertEqual(reply["generation"], 2)
        self.assertEqual(reply["result"]["rejected_cut_ids"], ["S"])
        self.assertEqual(reply["result"]["conflicting_cut_ids"], [])
        # replay stays stale even after knowledge moves on again.
        src3 = copy.deepcopy(src2)
        src3["certificates"].append(cert("ff", "F", 0, 1, "fail",
                                         known_at=7, revision=2) | {"payload": None})
        src3["as_of"] = 8
        self.ingest(src3, "ing3", 2)
        self.assertEqual(self.deliver([CUT_S], "D-stale", 1), reply)

    def test_stale_ingest_rejection_replays_unchanged(self):
        self.init()
        src2 = copy.deepcopy(self.src)
        src2["certificates"].append(cert("nz", "F", 0, 1, "pass", known_at=5))
        src2["as_of"] = 6
        first = self.ingest(src2, "ing-ok", 1)
        self.assertEqual(first["status"], "accepted")
        # a late replay targeting the old generation is a permanent rejection.
        stale_cmd = {"op": "ingest", "request_id": "ing-stale",
                     "expected_generation": 1, "source": src2}
        reply = dispatch.apply(self.dir, stale_cmd)
        self.assertEqual(reply["status"], "rejected")
        self.assertEqual(reply["generation"], 2)
        self.assertEqual(reply["result"]["code"], "stale_generation")
        self.assertEqual(dispatch.read_store(self.dir)["generation"], 2)
        # advancing knowledge again does not turn the old stale request valid.
        src3 = copy.deepcopy(src2)
        src3["certificates"].append(cert("nz2", "F", 1, 2, "pass", known_at=7))
        src3["as_of"] = 8
        self.ingest(src3, "ing3", 2)
        self.assertEqual(dispatch.apply(self.dir, copy.deepcopy(stale_cmd)), reply)
        self.assertEqual(dispatch.read_store(self.dir)["generation"], 3)

    def test_cut_limit_counts_new_blocks_and_lists_all(self):
        self._cut_limit_on_large_cloth()

    def _cut_limit_on_large_cloth(self):
        warp = [{"shaft": 1 + i % 2, "yarn": "w"} for i in range(32)]
        big_batch = one_job_batch(
            picks=[pk("T1", "f"), pk("T2", "f")], repeat_y=1,
            warp=warp, repeat_x=4, takeup_permille=0, warp_waste_mm=0)
        lots = [{"id": "W", "yarn": "w", "capacity_mm": 10**9},
                {"id": "F", "yarn": "f", "capacity_mm": 10**9}]
        allocations = []
        for base in range(32):
            for rep in range(4):
                allocations.append({
                    "id": f"w{base}-{rep}", "revision": 1, "known_at": 1,
                    "payload": {"orientation": "warp", "base_end": base,
                                "repeat_start": rep, "repeat_stop": rep + 1,
                                "pick_start": 0, "pick_stop": 2, "lot_id": "W"}})
        allocations.append(weft_alloc("wf", "F", 0, 2))
        src = source(batch=big_batch, lots=lots, allocations=allocations,
                     certificates=[cert("cw", "W", 0, 2, "pass"),
                                   cert("cf", "F", 0, 2, "pass")], as_of=3)
        store = tempfile.mkdtemp(prefix="loom-limit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(store, ignore_errors=True))
        dispatch.apply(store, {"op": "init", "request_id": "i", "source": src})
        # 128 disjoint cuts, each one end wide and one pick tall, in batches of 16.
        cut_map = {}
        for end in range(128):
            for pick in range(2):
                cut_map[(end, pick)] = f"c{len(cut_map):03d}"
        ids = list(cut_map.values())
        for start in range(0, 128, 16):
            batch_cuts = []
            for cid in ids[start:start + 16]:
                (end, pick) = next(k for k, v in cut_map.items() if v == cid)
                batch_cuts.append({"cut_id": cid, "end_start": end,
                                   "end_stop": end + 1, "pick_start": pick,
                                   "pick_stop": pick + 1})
            reply = dispatch.apply(store, {"op": "dispatch",
                                           "request_id": f"b{start}",
                                           "expected_generation": 1,
                                           "cuts": batch_cuts})
            self.assertEqual(reply["status"], "accepted", reply)
        self.assertEqual(len(dispatch.read_store(store)["deliveries"]), 128)
        extra = dispatch.apply(store, {"op": "dispatch", "request_id": "over",
                                       "expected_generation": 1,
                                       "cuts": [{"cut_id": "x1", "end_start": 0,
                                                 "end_stop": 1, "pick_start": 0,
                                                 "pick_stop": 1},
                                                {"cut_id": "x2", "end_start": 1,
                                                 "end_stop": 2, "pick_start": 1,
                                                 "pick_stop": 2}]})
        self.assertEqual(extra["status"], "rejected")
        self.assertEqual(extra["result"]["code"], "cut_limit")
        self.assertEqual(extra["result"]["rejected_cut_ids"], ["x1", "x2"])
        self.assertEqual(extra["result"]["conflicting_cut_ids"], [])
        self.assertEqual(len(dispatch.read_store(store)["deliveries"]), 128)

    def test_cut_id_used_precedes_overlap_and_quality(self):
        self.init()
        self.deliver([CUT_S, CUT_R], "D1", 1)
        reply = self.deliver([{**CUT_U, "cut_id": "S"}], "D2", 1)
        self.assertEqual(reply["result"]["code"], "cut_id_used")
        self.assertEqual(reply["result"]["rejected_cut_ids"], ["S"])
        self.assertEqual(reply["result"]["conflicting_cut_ids"], [])

    def test_cloth_overlap_lists_all_proposed_and_existing_ids_sorted(self):
        self.init()
        self.deliver([CUT_S, CUT_R], "D1", 1)
        # two new cuts: one hits S, one hits R, and they also overlap each other.
        cuts = [
            {"cut_id": "b2", "end_start": 0, "end_stop": 2,
             "pick_start": 0, "pick_stop": 2},
            {"cut_id": "b1", "end_start": 0, "end_stop": 2,
             "pick_start": 0, "pick_stop": 2}]
        reply = self.deliver(cuts, "D2", 1)
        self.assertEqual(reply["result"]["code"], "cloth_overlap")
        self.assertEqual(reply["result"]["rejected_cut_ids"], ["b1", "b2"])
        self.assertEqual(reply["result"]["conflicting_cut_ids"], ["R", "S"])

    def test_trace_blocked_lists_every_proposed_decision(self):
        self.init()
        src2 = copy.deepcopy(self.src)
        src2["certificates"].append(cert("ff", "F", 0, 1, "fail", known_at=5))
        src2["as_of"] = 6
        self.ingest(src2, "ing", 1)
        # S/R are blocked by the fail; U is still fine. None may ship.
        reply = self.deliver([CUT_U, CUT_S, CUT_R], "D-block", 2)
        self.assertEqual(reply["result"]["code"], "trace_blocked")
        self.assertEqual(reply["result"]["rejected_cut_ids"], ["R", "S"])
        self.assertEqual(reply["result"]["conflicting_cut_ids"], [])
        decisions = {d["cut_id"]: d["decision"]
                     for d in reply["result"]["decisions"]}
        self.assertEqual(set(decisions), {"R", "S", "U"})
        self.assertTrue(decisions["U"]["allowed"])
        self.assertFalse(decisions["R"]["allowed"])
        for cut in (CUT_U, CUT_S, CUT_R):
            self.assertEqual(decisions[cut["cut_id"]], trace_decision(src2, cut, 6))
        self.assertEqual(self.view()["deliveries"], [])

    def test_one_blocked_piece_blocks_the_whole_batch_with_no_occupancy(self):
        # Independent roll: at generation 2 the fail blocks picks [0,1); a
        # batch mixing one fine cut (upper cloth) and one blocked cut must be
        # wholly refused with neither rectangle occupied.
        blocked_src = copy.deepcopy(self.src)
        blocked_src["certificates"].append(
            cert("ff", "F", 0, 1, "fail", known_at=5))
        blocked_src["as_of"] = 6
        self.init(blocked_src, request_id="i-block")
        fine = {"cut_id": "V", "end_start": 0, "end_stop": 2,
                "pick_start": 2, "pick_stop": 3}
        bad = {"cut_id": "W", "end_start": 0, "end_stop": 1,
               "pick_start": 0, "pick_stop": 1}
        reply = self.deliver([fine, bad], "D-fail", 1)
        self.assertEqual(reply["result"]["code"], "trace_blocked")
        self.assertEqual(reply["result"]["rejected_cut_ids"], ["W"])
        self.assertEqual(reply["result"]["conflicting_cut_ids"], [])
        self.assertEqual(self.view()["deliveries"], [])
        # the fine piece V is still available to a later request.
        ok = self.deliver([fine], "D-ok", 1)
        self.assertEqual(ok["status"], "accepted")
        self.assertEqual([d["cut_id"] for d in self.view()["deliveries"]], ["V"])

    def test_adjacent_boundaries_are_legal(self):
        self.init()
        left = {"cut_id": "L", "end_start": 0, "end_stop": 1,
                "pick_start": 0, "pick_stop": 2}
        right = {"cut_id": "L2", "end_start": 1, "end_stop": 2,
                 "pick_start": 0, "pick_stop": 2}
        upper = {"cut_id": "U2", "end_start": 0, "end_stop": 2,
                 "pick_start": 2, "pick_stop": 4}
        reply = self.deliver([left, right, upper], "D-edge", 1)
        self.assertEqual(reply["status"], "accepted", reply)
        self.assertEqual(len(self.view()["deliveries"]), 3)

    def test_input_order_does_not_change_stable_sorted_output(self):
        self.init()
        # submit three disjoint cuts in a deliberately scrambled order.
        left = {"cut_id": "z-cut", "end_start": 0, "end_stop": 1,
                "pick_start": 0, "pick_stop": 2}
        right = {"cut_id": "a-cut", "end_start": 1, "end_stop": 2,
                 "pick_start": 0, "pick_stop": 2}
        upper = {"cut_id": "m-cut", "end_start": 0, "end_stop": 2,
                 "pick_start": 2, "pick_stop": 4}
        reply = self.deliver([upper, left, right], "D-scrambled", 1)
        self.assertEqual(reply["status"], "accepted")
        # receipt deliveries are always sorted by cut_id regardless of input.
        self.assertEqual([d["cut_id"] for d in reply["result"]["deliveries"]],
                         ["a-cut", "m-cut", "z-cut"])
        self.assertEqual([d["cut_id"] for d in self.view()["deliveries"]],
                         ["a-cut", "m-cut", "z-cut"])
        # the same request id with a reordered cuts array is a different
        # command structurally (array order is significant) and must conflict.
        with self.assertRaises(ValueError):
            self.deliver([right, upper, left], "D-scrambled", 1)
        # an identical re-submission (same order) replays the stored receipt.
        replay = self.deliver([upper, left, right], "D-scrambled", 1)
        self.assertEqual(replay, reply)


class InputValidationTests(LedgerTestCase):
    def test_source_must_have_empty_shipments(self):
        bad = copy.deepcopy(self.src)
        bad["shipments"] = [{"id": "x", "requested_at": 1, "end_start": 0,
                             "end_stop": 1, "pick_start": 0, "pick_stop": 1}]
        with self.assertRaises(ValueError):
            self.init(bad)

    def test_future_version_rejected(self):
        bad = copy.deepcopy(self.src)
        bad["certificates"].append(cert("late", "F", 0, 1, "pass", known_at=9))
        with self.assertRaises(ValueError):
            self.init(bad)

    def test_ingest_cannot_rewrite_or_delete_old_version(self):
        self.init()
        rewritten = copy.deepcopy(self.src)
        rewritten["as_of"] = 6
        rewritten["certificates"][0] = cert("cw", "W", 0, 3, "pass")
        with self.assertRaises(ValueError):
            self.ingest(rewritten, "bad", 1)
        deleted = copy.deepcopy(self.src)
        deleted["as_of"] = 6
        deleted["allocations"] = [a for a in deleted["allocations"]
                                  if a["id"] != "wf"]
        with self.assertRaises(ValueError):
            self.ingest(deleted, "bad2", 1)
        # new version with known_at <= old as_of is illegal.
        backdated = copy.deepcopy(self.src)
        backdated["as_of"] = 6
        backdated["certificates"].append(cert("nz", "F", 0, 1, "pass", known_at=3))
        with self.assertRaises(ValueError):
            self.ingest(backdated, "bad3", 1)
        # equal as_of rejected.
        same = copy.deepcopy(self.src)
        with self.assertRaises(ValueError):
            self.ingest(same, "bad4", 1)
        # none of the failed ids were consumed.
        for rid in ("bad", "bad2", "bad3", "bad4"):
            reply = self.ack([1], rid)
            self.assertEqual(reply["result"]["code"], "unknown_event")

    def test_batch_and_lots_are_fixed(self):
        self.init()
        changed_batch = copy.deepcopy(self.src)
        changed_batch["as_of"] = 6
        changed_batch["batch"]["common"]["float_limit"] = 4
        with self.assertRaises(ValueError):
            self.ingest(changed_batch, "b", 1)
        changed_lots = copy.deepcopy(self.src)
        changed_lots["as_of"] = 6
        changed_lots["lots"][0]["capacity_mm"] = 99
        with self.assertRaises(ValueError):
            self.ingest(changed_lots, "l", 1)
        # lots array order is part of the fixed identity: reordering rejected.
        reordered_lots = copy.deepcopy(self.src)
        reordered_lots["as_of"] = 6
        reordered_lots["lots"] = [reordered_lots["lots"][1],
                                  reordered_lots["lots"][0]]
        with self.assertRaises(ValueError):
            self.ingest(reordered_lots, "rl", 1)
        # object key order alone must not matter.
        key_shuffled = copy.deepcopy(self.src)
        key_shuffled["as_of"] = 6
        key_shuffled["lots"] = [
            {"capacity_mm": lot["capacity_mm"], "yarn": lot["yarn"], "id": lot["id"]}
            for lot in key_shuffled["lots"]]
        key_shuffled["certificates"].append(
            cert("nz", "F", 0, 1, "pass", known_at=5))
        reply = self.ingest(key_shuffled, "ok", 1)
        self.assertEqual(reply["status"], "accepted")

    def test_allocation_and_certificate_arrays_may_reorder_but_versions_must_match(self):
        self.init()
        moved = copy.deepcopy(self.src)
        moved["as_of"] = 6
        moved["allocations"] = list(reversed(moved["allocations"]))
        moved["certificates"] = list(reversed(moved["certificates"]))
        moved["certificates"].append(cert("nz", "F", 0, 1, "pass", known_at=5))
        reply = self.ingest(moved, "reorder", 1)
        self.assertEqual(reply["status"], "accepted")

    def test_strict_command_shapes(self):
        self.init()
        cases = [
            {"op": "ack", "request_id": "x", "event_ids": [1], "extra": 0},
            {"op": "ack", "request_id": "x"},
            {"op": "frob", "request_id": "x", "event_ids": [1]},
            {"op": "ack", "request_id": "x", "event_ids": []},
            {"op": "ack", "request_id": "x", "event_ids": [1, 1]},
            {"op": "ack", "request_id": "x", "event_ids": [True]},
            {"op": "ack", "request_id": "x", "event_ids": [0]},
            {"op": "ingest", "request_id": "x",
             "expected_generation": True, "source": self.src},
            {"op": "ingest", "request_id": "x",
             "expected_generation": 0, "source": self.src},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": []},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [CUT_S, CUT_S]},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [{"cut_id": "S", "end_start": 1, "end_stop": 1,
                       "pick_start": 0, "pick_stop": 2}]},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [{"cut_id": "S", "end_start": 0, "end_stop": 9,
                       "pick_start": 0, "pick_stop": 2}]},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [{"cut_id": "S", "end_start": 0, "end_stop": 1,
                       "pick_start": 0, "pick_stop": 9}]},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [{"cut_id": "bad id", "end_start": 0, "end_stop": 1,
                       "pick_start": 0, "pick_stop": 1}]},
            {"op": "dispatch", "request_id": "x" * 41, "expected_generation": 1,
             "cuts": [CUT_S]},
            {"op": "dispatch", "request_id": "x", "expected_generation": 1,
             "cuts": [{"cut_id": "y" * 41, "end_start": 0, "end_stop": 1,
                       "pick_start": 0, "pick_stop": 1}]},
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dispatch.apply(self.dir, copy.deepcopy(bad))

    def test_input_errors_do_not_damage_state_or_consume_ids(self):
        self.init()
        before = self.view()
        with self.assertRaises(ValueError):
            dispatch.apply(self.dir, {"op": "dispatch", "request_id": "z",
                                      "expected_generation": 1,
                                      "cuts": [{"cut_id": "S", "end_start": 5,
                                                "end_stop": 6, "pick_start": 0,
                                                "pick_stop": 1}]})
        self.assertEqual(self.view(), before)
        # id z is still usable.
        ok = self.deliver([CUT_S], "z", 1)
        self.assertEqual(ok["status"], "accepted")

    def test_same_request_id_different_command_raises_and_keeps_receipt(self):
        self.init()
        first = self.deliver([CUT_S], "same", 1)
        with self.assertRaises(ValueError):
            self.deliver([CUT_R], "same", 1)
        with self.assertRaises(ValueError):
            self.ack([1], "same")
        self.assertEqual(self.deliver([CUT_S], "same", 1), first)

    def test_empty_plan_cannot_ship(self):
        # A legal batch whose selected plan is empty (warp stock exhausted):
        # P=0, so no positive-area cut can lie inside the produced cloth.
        starved = source()
        starved["batch"]["stock"] = [{"yarn": "w", "length_mm": 0},
                                     {"yarn": "f", "length_mm": 10**6}]
        starved["allocations"] = []
        starved["certificates"] = []
        reply_init = self.init(starved, request_id="empty-init")
        self.assertEqual(reply_init["status"], "accepted")
        self.assertEqual(self.view()["production"]["plan"], [])
        with self.assertRaises(ValueError):
            self.deliver([CUT_S], "nope", 1)
        # ingest on the empty roll stays empty and accepts knowledge growth.
        still_empty = copy.deepcopy(starved)
        still_empty["as_of"] = 5
        reply = self.ingest(still_empty, "empty-ing", 1)
        self.assertEqual(reply["status"], "accepted")


class CurrentDecisionTests(LedgerTestCase):
    def test_allowed_true_but_version_refs_change_updates_current_without_event(self):
        self.init()
        self.deliver([CUT_S, CUT_R], "D1", 1)
        # Narrow the same weft allocation via a new revision (known after the
        # old as_of) and add a second allocation for the disjoint remaining
        # picks. Total coverage and every verdict stay identical, so allowed
        # stays true, but the current decision's version references change while
        # the frozen admitted decision keeps its original single reference.
        src2 = copy.deepcopy(self.src)
        src2["allocations"].append(
            {"id": "wf", "revision": 2, "known_at": 5,
             "payload": {"orientation": "weft", "pick_start": 0, "pick_stop": 2,
                         "lot_id": "F"}})
        src2["allocations"].append(weft_alloc("wf2", "F", 2, 4, known_at=5))
        src2["as_of"] = 6
        reply = self.ingest(src2, "ing", 1)
        self.assertEqual(reply["status"], "accepted")
        self.assertEqual(reply["result"]["transition_event_ids"], [])
        view = self.view()
        self.assertEqual(view["summary"]["events"], 0)
        for delivery in view["deliveries"]:
            self.assertEqual(delivery["state"], "normal")
            current_by_lot = {lot["lot_id"]: {v["id"]: v["revision"]
                                              for v in lot["allocation_versions"]}
                              for lot in delivery["current_decision"]["lots"]}
            admitted_by_lot = {lot["lot_id"]: {v["id"]: v["revision"]
                                               for v in lot["allocation_versions"]}
                               for lot in delivery["admitted_decision"]["lots"]}
            # the weft lot F is now attributed by wf revision 2 (picks [0,2)).
            self.assertEqual(current_by_lot["F"], {"wf": 2})
            self.assertEqual(admitted_by_lot["F"], {"wf": 1})
            self.assertEqual(delivery["current_decision"],
                             trace_decision(src2, delivery, 6))
            self.assertTrue(delivery["admitted_decision"]["allowed"])
            self.assertEqual(delivery["admitted_decision"]["knowledge_at"], 3)

    def test_allowed_true_but_lot_contribution_amounts_change_without_event(self):
        # Two lots F and G share yarn f. Initially all weft is attributed to F;
        # a later ingest narrows F to picks [0,2) and adds G for [2,4), both
        # fully certified. A cut spanning all four picks stays allowed, but its
        # per-lot contribution amounts change: F shrinks while G appears.
        two_lots = [{"id": "W", "yarn": "w", "capacity_mm": 12},
                    {"id": "F", "yarn": "f", "capacity_mm": 12},
                    {"id": "G", "yarn": "f", "capacity_mm": 12}]
        base = source(lots=two_lots)
        wide = {"cut_id": "WIDE", "end_start": 0, "end_stop": 2,
                "pick_start": 0, "pick_stop": 4}
        self.init(base, request_id="i-two")
        first = self.deliver([wide], "D-wide", 1)
        self.assertEqual(first["status"], "accepted")
        admitted = first["result"]["deliveries"][0]["admitted_decision"]
        admitted_lots = {x["lot_id"]: x["total_mm"] for x in admitted["lots"]}
        # whole width = 2 ends, 4 picks: H(2)=3 -> 4 weft picks x 3 = 12mm,
        # all attributed to F initially.
        self.assertEqual(admitted_lots.get("F"), 12)
        self.assertNotIn("G", admitted_lots)

        nxt = copy.deepcopy(base)
        nxt["as_of"] = 6
        nxt["allocations"].append({
            "id": "wf", "revision": 2, "known_at": 5,
            "payload": {"orientation": "weft", "pick_start": 0, "pick_stop": 2,
                        "lot_id": "F"}})
        nxt["allocations"].append(weft_alloc("wg", "G", 2, 4, known_at=5))
        nxt["certificates"].append(cert("cg", "G", 2, 4, "pass", known_at=5))
        reply = self.ingest(nxt, "ing-shift", 1)
        self.assertEqual(reply["status"], "accepted")
        self.assertEqual(reply["result"]["transition_event_ids"], [])
        view = self.view()
        self.assertEqual(view["summary"]["events"], 0)
        delivery = view["deliveries"][0]
        self.assertEqual(delivery["state"], "normal")
        current_lots = {x["lot_id"]: x["total_mm"]
                        for x in delivery["current_decision"]["lots"]}
        # each half [0,2)/[2,4) spans 2 picks x H(2)=3 -> 6mm per lot.
        self.assertEqual(current_lots.get("F"), 6)
        self.assertEqual(current_lots.get("G"), 6)
        # the frozen admission credential still shows the old 12mm on F only
        # (warp 10mm = 2 ends x F(4)=5, unchanged by the weft re-attribution).
        frozen_lots = {x["lot_id"]: x["total_mm"]
                       for x in delivery["admitted_decision"]["lots"]}
        self.assertEqual(frozen_lots, {"F": 12, "W": 10})
        self.assertEqual(delivery["current_decision"],
                         trace_decision(nxt, wide, 6))

    def test_overdrawn_other_cloth_recalls_delivered_piece(self):
        # F capacity 8. At first only picks [0,2) are attributed to F (6mm),
        # so S/R dispatch cleanly. A later ingest extends the same weft
        # allocation to [0,4): whole-plan F attribution becomes 12mm > 8.
        # The overdraw is evaluated on the whole roll's balance, not on the
        # cut's own 6mm, so both delivered pieces must be recalled.
        cap8 = source(lots=[{"id": "W", "yarn": "w", "capacity_mm": 12},
                            {"id": "F", "yarn": "f", "capacity_mm": 8}],
                      allocations=[warp_alloc("aw0", 0, "W"),
                                   warp_alloc("aw1", 1, "W"),
                                   weft_alloc("wf", "F", 0, 2)],
                      certificates=[cert("cw", "W", 0, 4, "pass"),
                                    cert("cf", "F", 0, 2, "pass")])
        self.init(cap8, request_id="i-cap")
        first = self.deliver([CUT_S, CUT_R], "D1", 1)
        self.assertEqual(first["status"], "accepted")
        grown = copy.deepcopy(cap8)
        grown["as_of"] = 6
        # keep revision 1 untouched; revision 2 widens the same allocation.
        grown["allocations"].append(weft_alloc(
            "wf", "F", 0, 4, known_at=5, revision=2,
            payload_override={"orientation": "weft", "pick_start": 0,
                              "pick_stop": 4, "lot_id": "F"}))
        # certificate covering the newly attributed picks.
        grown["certificates"].append(cert("cf2", "F", 2, 4, "pass", known_at=5))
        reply = self.ingest(grown, "ing-over", 1)
        self.assertEqual(reply["status"], "accepted")
        self.assertEqual(reply["result"]["transition_event_ids"], [1, 2])
        view = self.view()
        self.assertEqual({d["cut_id"]: d["state"] for d in view["deliveries"]},
                         {"S": "recall", "R": "recall"})
        for delivery in view["deliveries"]:
            self.assertTrue(delivery["admitted_decision"]["allowed"])
            self.assertFalse(delivery["current_decision"]["allowed"])
            kinds = {p["kind"] for p in delivery["current_decision"]["problems"]}
            self.assertIn("overdrawn", kinds)
            self.assertEqual(delivery["current_decision"],
                             trace_decision(grown, delivery, 6))
        # the admitted credentials still describe the old, healthy snapshot.
        for delivery in view["deliveries"]:
            self.assertEqual(delivery["admitted_decision"]["knowledge_at"], 3)
            self.assertTrue(delivery["admitted_decision"]["allowed"])


class ViewContractTests(LedgerTestCase):
    def test_view_schema_and_counts(self):
        self.init()
        self.deliver([CUT_S, CUT_R], "D1", 1)
        view = self.view()
        self.assertEqual(set(view), {"schema_version", "generation", "as_of",
                                     "production", "deliveries", "events",
                                     "pending_event_ids", "summary"})
        self.assertEqual(view["schema_version"], 1)
        self.assertEqual(view["generation"], 1)
        self.assertEqual(view["as_of"], 3)
        self.assertEqual(set(view["production"]),
                         {"plan", "segments", "material_requirements"})
        delivery = view["deliveries"][0]
        self.assertEqual(set(delivery), {"cut_id", "end_start", "end_stop",
                                         "pick_start", "pick_stop", "request_id",
                                         "admitted_generation", "admitted_at",
                                         "admitted_decision", "current_decision",
                                         "state"})
        self.assertEqual([d["cut_id"] for d in view["deliveries"]], ["R", "S"])
        self.assertEqual(view["events"], [])
        self.assertEqual(view["pending_event_ids"], [])

    def test_view_missing_store_raises_and_creates_nothing(self):
        missing = os.path.join(self.dir, "does-not-exist")
        with self.assertRaises(ValueError):
            dispatch.read_store(missing)
        self.assertFalse(os.path.exists(missing))

    def test_receipts_are_not_duplicated_into_view(self):
        self.init()
        self.deliver([CUT_S], "D1", 1)
        self.ack([1], "unused") if False else None
        text = json.dumps(self.view())
        self.assertNotIn("req-init", text)
        self.assertNotIn("D1", json.dumps(
            dispatch.read_store(self.dir)["summary"]))

    def test_delivery_contains_cut_id(self):
        self.init()
        reply = self.deliver([CUT_S], "D1", 1)
        for credential in reply["result"]["deliveries"]:
            self.assertIn("cut_id", credential)


class PersistenceSafetyTests(LedgerTestCase):
    def test_foreign_file_and_corruption_are_hard_errors(self):
        self.init()
        # corrupt the ledger.
        ledger = os.path.join(self.dir, dispatch.LEDGER_NAME)
        with open(ledger, "wb") as handle:
            handle.write(b"{not json")
        with self.assertRaises(dispatch.StoreError):
            dispatch.read_store(self.dir)
        with self.assertRaises(dispatch.StoreError):
            self.deliver([CUT_R], "D2", 1)
        # a fresh directory containing an unrelated file must not be reborn.
        foreign = tempfile.mkdtemp(prefix="loom-foreign-")
        self.addCleanup(lambda: __import__("shutil").rmtree(foreign, ignore_errors=True))
        with open(os.path.join(foreign, "notes.txt"), "w") as handle:
            handle.write("manual notes")
        with self.assertRaises(dispatch.StoreError):
            dispatch.apply(foreign, {"op": "init", "request_id": "i",
                                     "source": self.src})

    def test_rejected_requests_keep_permanent_receipts(self):
        self.init()
        reply = self.deliver([CUT_S], "D1", 1)
        blocked = self.deliver([CUT_S], "D2", 1)
        self.assertEqual(blocked["result"]["code"], "cut_id_used")
        ledger = json.loads(Path(os.path.join(self.dir, dispatch.LEDGER_NAME)).read_text())
        self.assertIn("D2", ledger["requests"])
        self.assertEqual(ledger["requests"]["D2"]["receipt"], blocked)
        self.assertEqual(ledger["requests"]["D1"]["receipt"], reply)


class ScaleAndCompressionTests(LedgerTestCase):
    def test_128_successful_ingests_grow_generation_and_preserve_history(self):
        self.init()
        current = copy.deepcopy(self.src)
        for index in range(128):
            nxt = copy.deepcopy(current)
            # a harmless new pass certificate on a fresh id each ingest.
            nxt["certificates"].append(cert(
                f"nz{index}", "F", 0, 4, "pass", known_at=current["as_of"] + 1))
            nxt["as_of"] = current["as_of"] + 2
            reply = self.ingest(nxt, f"ing-{index}", 1 + index)
            self.assertEqual(reply["status"], "accepted")
            self.assertEqual(reply["generation"], 2 + index)
            current = nxt
        view = self.view()
        self.assertEqual(view["generation"], 129)
        self.assertEqual(view["as_of"], 3 + 2 * 128)
        # every old version is still present and still resolvable.
        knowledge = dispatch.Knowledge(current)
        self.assertEqual(len(knowledge.certificates), 2 + 128)
        # no transition events because allowed never flipped.
        self.assertEqual(view["events"], [])
        self.assertEqual(view["summary"]["delivered_cuts"], 0)

    def test_1024_change_requests_including_rejections_are_all_replayable(self):
        self.init()
        # 512 dispatches: the first 8 tiny disjoint cuts succeed on a 2x4 cloth,
        # every further one is a permanent business rejection (overlap / id).
        unique = [
            {"cut_id": f"u{e}-{p}", "end_start": e, "end_stop": e + 1,
             "pick_start": p, "pick_stop": p + 1}
            for e in range(2) for p in range(4)]
        first = self.deliver(unique, "fill-once", 1)
        self.assertEqual(first["status"], "accepted")
        first_receipts = {}
        for number in range(1023):
            rid = f"req-{number}"
            cut = {"cut_id": f"dup-{number % 32}", "end_start": number % 2,
                   "end_stop": (number % 2) + 1, "pick_start": number % 4,
                   "pick_stop": (number % 4) + 1}
            reply = self.deliver([cut], rid, 1)
            self.assertEqual(reply["status"], "rejected")
            first_receipts[rid] = reply
        # every one of the 1024 requests replays byte-for-byte.
        for number in range(1023):
            rid = f"req-{number}"
            cut = {"cut_id": f"dup-{number % 32}", "end_start": number % 2,
                   "end_stop": (number % 2) + 1, "pick_start": number % 4,
                   "pick_stop": (number % 4) + 1}
            self.assertEqual(self.deliver([cut], rid, 1),
                             first_receipts[rid])
        ledger = json.loads(Path(os.path.join(self.dir, dispatch.LEDGER_NAME)).read_text())
        # init + one fill + 1023 rejection requests = 1025 stored requests;
        # the 1024 change requests after initialisation are all replayable.
        self.assertEqual(len(ledger["requests"]), 1025)
        # the 8 real cuts stay the only occupancy despite ~1000 attempts.
        self.assertEqual(len(self.view()["deliveries"]), 8)

    def test_large_repeat_cloth_uses_compressed_material_and_trace_contract(self):
        # 128 base ends x repeat_x=1000 (128k real ends), 2 picks repeated 1000
        # times (2000 picks): 256 million virtual intersections, never expanded.
        warp = [{"shaft": 1 + i % 2, "yarn": "w"} for i in range(128)]
        big_batch = one_job_batch(
            picks=[pk("T1", "f"), pk("T2", "f")], repeat_y=1000,
            warp=warp, repeat_x=1000, takeup_permille=0, warp_waste_mm=0)
        # 128k ends x 2000 picks of warp and 2000 full-width weft picks need
        # ~2.56e8 mm warp and ~2.56e11 mm weft; give the plan room to exist.
        big_batch["stock"] = [{"yarn": "w", "length_mm": 10**12},
                              {"yarn": "f", "length_mm": 10**12},
                              {"yarn": "g", "length_mm": 10**12}]
        big_batch["max_length_mm"] = 10**12
        lots = [{"id": "W", "yarn": "w", "capacity_mm": 10**12},
                {"id": "F", "yarn": "f", "capacity_mm": 10**12}]
        allocations = []
        for base in range(128):
            allocations.append({
                "id": f"w{base}", "revision": 1, "known_at": 1,
                "payload": {"orientation": "warp", "base_end": base,
                            "repeat_start": 0, "repeat_stop": 1000,
                            "pick_start": 0, "pick_stop": 2000, "lot_id": "W"}})
        allocations.append(weft_alloc("wf", "F", 0, 2000))
        big_src = source(
            batch=big_batch, lots=lots, allocations=allocations,
            certificates=[cert("cw", "W", 0, 2000, "pass"),
                          cert("cf", "F", 0, 2000, "pass")], as_of=3)
        import tracemalloc
        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()
        reply_init = self.init(big_src, request_id="big-init")
        self.assertEqual(reply_init["status"], "accepted")
        # one cut spanning 10 real ends and 10 real picks.
        cut = {"cut_id": "BIG", "end_start": 0, "end_stop": 10,
               "pick_start": 0, "pick_stop": 10}
        reply = self.deliver([cut], "big-d", 1)
        self.assertEqual(reply["status"], "accepted", reply)
        credential = reply["result"]["deliveries"][0]
        decision = credential["admitted_decision"]
        self.assertTrue(decision["allowed"])
        # warp: 10 ends x 10 picks, spacing 1, takeup 0 -> 100mm.
        # every one of the 10 picks is weft yarn f (T1/T2 differ only by shaft),
        # each over 10 ends -> 10 x 10mm = 100mm, no per-cut re-rounding.
        by_lot = {x["lot_id"]: x for x in decision["lots"]}
        self.assertEqual(by_lot["W"]["warp_mm"], 100)
        self.assertEqual(by_lot["F"]["weft_mm"], 100)
        # the view still matches an independent trace decision.
        view = self.view()
        self.assertEqual(view["deliveries"][0]["current_decision"],
                         trace_decision(big_src, cut, 3))
        # memory growth is bounded: nowhere near a byte per intersection.
        snap_after = tracemalloc.take_snapshot()
        growth = sum(stat.size_diff for stat in
                     snap_after.compare_to(snap_before, "filename"))
        self.assertLess(growth, 20 * 1024 * 1024)
        tracemalloc.stop()
        # compressed production report stays segment-based.
        self.assertEqual(len(view["production"]["segments"]), 1)


if __name__ == "__main__":
    unittest.main()
