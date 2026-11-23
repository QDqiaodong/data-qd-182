import json
import random
import unittest

from loom_draft.batch import validate_batch, production
from loom_draft.handoff import run_handoff
from loom_draft.materials import ceil_permille

TAIL_WINDOW = 2048


# ---------------------------------------------------------------------------
# shared builders
# ---------------------------------------------------------------------------

def acceptance_batch():
    """两根实际经端、交替升综的四纬平纹，纬纱 F、G、F、G，每纬耗 3mm。"""
    return {
        "common": {"shafts": 2,
                   "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
                   "warp": [{"shaft": 1, "yarn": "W"}, {"shaft": 2, "yarn": "W"}],
                   "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
                   "takeup_permille": 100, "warp_waste_mm": 0, "float_limit": 1},
        "jobs": [{"id": "J", "value": 1, "repeat_y": 1, "phases": [0], "picks": [
            {"treadles": ["T1"], "yarn": "F"}, {"treadles": ["T2"], "yarn": "G"},
            {"treadles": ["T1"], "yarn": "F"}, {"treadles": ["T2"], "yarn": "G"}]}],
        "bridges": [],
        "stock": [{"yarn": "W", "length_mm": 10},
                  {"yarn": "F", "length_mm": 6},
                  {"yarn": "G", "length_mm": 6}],
        "max_length_mm": 4}


def acceptance_spools():
    return [{"id": "F1", "yarn": "F", "length_mm": 6},
            {"id": "F2", "yarn": "F", "length_mm": 6},
            {"id": "G1", "yarn": "G", "length_mm": 9}]


def plan_of(batch):
    """返回 (计划纬型号序列, 每纬耗)，与批次口径一致。"""
    spec = validate_batch(batch)
    _, segments = production(spec)
    yarns = []
    for segment in segments:
        yarns.extend(segment["yarns"] * segment["repeat_count"])
    common = spec["common"]
    width = len(spec["warp"]) * common["repeat_x"] * common["warp_spacing_mm"]
    return yarns, ceil_permille(width, common["takeup_permille"])


def handoff(batch, spools, checkpoint, no, events):
    return run_handoff({"batch": batch, "spools": spools, "checkpoint": checkpoint,
                        "shift": {"no": no, "events": events}})


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def roundtrip(checkpoint):
    return json.loads(compact(checkpoint))


def spool_map(report):
    return {row["spool_id"]: row for row in report["spools"]}


# ---------------------------------------------------------------------------
# independent per-pick naive reference for the handoff mechanics
# ---------------------------------------------------------------------------

class NaiveLoom:
    """逐纬独立模拟：挂筒、织入、拆纬、签封、续织边界，不走任何批量捷径。"""

    def __init__(self, plan_yarns, unit, spools):
        self.plan = list(plan_yarns)
        self.unit = unit
        self.spools = [dict(row) for row in spools]
        self.sealed = 0
        self.next_pick = 0
        self.mounted = {}              # yarn -> spool index
        self.amounts = [[0, 0, 0] for _ in self.spools]   # [sealed, tail, scrap]
        self.tail = []                 # per tail pick: (spool index, woven shift)

    def _index(self, spool_id):
        for i, spool in enumerate(self.spools):
            if spool["id"] == spool_id:
                return i
        raise ValueError(f"unknown spool: {spool_id}")

    def _remaining(self, idx):
        sealed, tail, scrap = self.amounts[idx]
        return self.spools[idx]["length_mm"] - sealed - tail - scrap

    def run_shift(self, shift_no, events):
        scrap = {}
        for event in events:
            op = event["op"]
            if op == "mount":
                idx = self._index(event["spool_id"])
                self.mounted[self.spools[idx]["yarn"]] = idx
            elif op == "weave":
                count = event["count"]
                if self.next_pick + count > len(self.plan):
                    raise ValueError("weave exceeds plan")
                if self.next_pick + count - self.sealed > TAIL_WINDOW:
                    raise ValueError("weave exceeds window")
                for _ in range(count):
                    yarn = self.plan[self.next_pick]
                    idx = self.mounted.get(yarn)
                    if idx is None:
                        raise ValueError("no mounted spool")
                    if self._remaining(idx) < self.unit:
                        raise ValueError("insufficient")
                    self.amounts[idx][1] += self.unit
                    self.tail.append((idx, shift_no))
                    self.next_pick += 1
            elif op == "unpick":
                count = event["count"]
                if count > self.next_pick - self.sealed:
                    raise ValueError("unpick crosses sealed")
                for _ in range(count):
                    idx, woven = self.tail.pop()
                    self.amounts[idx][1] -= self.unit
                    self.amounts[idx][2] += self.unit
                    scrap[(woven, idx)] = scrap.get((woven, idx), 0) + self.unit
                    self.next_pick -= 1
            elif op == "seal":
                count = event["count"]
                if count > self.next_pick - self.sealed:
                    raise ValueError("seal exceeds woven")
                for _ in range(count):
                    idx, _ = self.tail.pop(0)
                    self.amounts[idx][1] -= self.unit
                    self.amounts[idx][0] += self.unit
                    self.sealed += 1
            else:
                raise ValueError("unknown op")
        return scrap

    def resume(self):
        virtual = [self._remaining(i) for i in range(len(self.spools))]
        pick = self.next_pick
        while True:
            if pick >= len(self.plan):
                return {"count": pick - self.next_pick, "stop_pick": len(self.plan),
                        "reason": "plan_end", "yarn": None, "spool_id": None}
            if pick - self.sealed >= TAIL_WINDOW:
                return {"count": pick - self.next_pick, "stop_pick": pick,
                        "reason": "tail_full", "yarn": None, "spool_id": None}
            yarn = self.plan[pick]
            idx = self.mounted.get(yarn)
            if idx is None:
                return {"count": pick - self.next_pick, "stop_pick": pick,
                        "reason": "no_spool", "yarn": yarn, "spool_id": None}
            if virtual[idx] < self.unit:
                return {"count": pick - self.next_pick, "stop_pick": pick,
                        "reason": "insufficient", "yarn": yarn,
                        "spool_id": self.spools[idx]["id"]}
            virtual[idx] -= self.unit
            pick += 1

    def expected_report(self, shift_no, scrap):
        rows = []
        for i, spool in enumerate(self.spools):
            sealed, tail, scrap_mm = self.amounts[i]
            rows.append({"spool_id": spool["id"], "yarn": spool["yarn"],
                         "mounted": i in set(self.mounted.values()),
                         "remaining_mm": self._remaining(i),
                         "sealed_mm": sealed, "tail_mm": tail, "scrap_mm": scrap_mm})
        rows.sort(key=lambda row: row["spool_id"])
        scrap_rows = [{"woven_shift": woven, "spool_id": self.spools[idx]["id"],
                       "mm": mm}
                      for (woven, idx), mm in scrap.items()]
        scrap_rows.sort(key=lambda row: (row["woven_shift"], row["spool_id"]))
        return {"sealed": self.sealed, "next_pick": self.next_pick,
                "total_picks": len(self.plan), "spools": rows,
                "scrap_this_shift": scrap_rows, "resume": self.resume()}


# ---------------------------------------------------------------------------
# key acceptance
# ---------------------------------------------------------------------------

class AcceptanceTests(unittest.TestCase):
    def test_three_shift_acceptance(self):
        batch, spools = acceptance_batch(), acceptance_spools()
        first = handoff(batch, spools, None, 1,
                        [{"op": "mount", "spool_id": "F1"},
                         {"op": "mount", "spool_id": "G1"},
                         {"op": "weave", "count": 3},
                         {"op": "seal", "count": 1}])
        self.assertEqual((first["sealed"], first["next_pick"],
                          first["total_picks"]), (1, 3, 4))
        rows = spool_map(first)
        self.assertEqual((rows["F1"]["sealed_mm"], rows["F1"]["tail_mm"],
                          rows["F1"]["remaining_mm"]), (3, 3, 0))
        self.assertEqual((rows["G1"]["tail_mm"], rows["G1"]["remaining_mm"]), (3, 6))
        self.assertEqual(first["resume"], {"count": 1, "stop_pick": 4,
                                           "reason": "plan_end",
                                           "yarn": None, "spool_id": None})
        self.assertEqual(first["scrap_this_shift"], [])

        # 交给“新进程”：checkpoint 经紧凑 JSON 序列化再解析
        second = handoff(batch, spools, roundtrip(first["checkpoint"]), 2,
                         [{"op": "unpick", "count": 2}])
        self.assertEqual((second["sealed"], second["next_pick"]), (1, 1))
        self.assertEqual(second["scrap_this_shift"],
                         [{"woven_shift": 1, "spool_id": "F1", "mm": 3},
                          {"woven_shift": 1, "spool_id": "G1", "mm": 3}])
        self.assertEqual(second["resume"], {"count": 1, "stop_pick": 2,
                                            "reason": "insufficient",
                                            "yarn": "F", "spool_id": "F1"})

        third = handoff(batch, spools, roundtrip(second["checkpoint"]), 3,
                        [{"op": "mount", "spool_id": "F2"},
                         {"op": "weave", "count": 3},
                         {"op": "seal", "count": 3}])
        self.assertEqual((third["sealed"], third["next_pick"]), (4, 4))
        rows = spool_map(third)
        for sid, sealed, scrap, remaining in (("F1", 3, 3, 0), ("F2", 3, 0, 3),
                                              ("G1", 6, 3, 0)):
            row = rows[sid]
            self.assertEqual((row["sealed_mm"], row["scrap_mm"],
                              row["remaining_mm"], row["tail_mm"]),
                             (sealed, scrap, remaining, 0), sid)
        self.assertEqual(third["scrap_this_shift"], [])
        self.assertEqual(third["resume"]["reason"], "plan_end")

    def test_variant_attributes_scrap_to_actual_spool(self):
        """首班纬 2 改由 F2 织入、结束前换回 F1：废耗归 F2，二班续织到卷尾。"""
        batch, spools = acceptance_batch(), acceptance_spools()
        first = handoff(batch, spools, None, 1,
                        [{"op": "mount", "spool_id": "F1"},
                         {"op": "mount", "spool_id": "G1"},
                         {"op": "weave", "count": 2},
                         {"op": "mount", "spool_id": "F2"},
                         {"op": "weave", "count": 1},
                         {"op": "mount", "spool_id": "F1"},
                         {"op": "seal", "count": 1}])
        second = handoff(batch, spools, roundtrip(first["checkpoint"]), 2,
                         [{"op": "unpick", "count": 2}])
        self.assertEqual(second["scrap_this_shift"],
                         [{"woven_shift": 1, "spool_id": "F2", "mm": 3},
                          {"woven_shift": 1, "spool_id": "G1", "mm": 3}])
        self.assertEqual(second["resume"], {"count": 3, "stop_pick": 4,
                                            "reason": "plan_end",
                                            "yarn": None, "spool_id": None})
        rows = spool_map(second)
        # 废耗归属与计划纬号、最后挂筒、拆后库存无关
        self.assertEqual((rows["F1"]["scrap_mm"], rows["F1"]["remaining_mm"]), (0, 3))
        self.assertEqual((rows["F2"]["scrap_mm"], rows["F2"]["remaining_mm"]), (3, 3))

    def test_checkpoint_binds_baseline_and_is_compact(self):
        batch, spools = acceptance_batch(), acceptance_spools()
        first = handoff(batch, spools, None, 1,
                        [{"op": "mount", "spool_id": "F1"},
                         {"op": "mount", "spool_id": "G1"},
                         {"op": "weave", "count": 3},
                         {"op": "seal", "count": 1}])
        encoded = compact(first["checkpoint"]).encode("utf-8")
        self.assertLessEqual(len(encoded), 65536)
        # 重排 spools 与 batch 对象键顺序不影响接手
        reordered_spools = [spools[2], spools[0], spools[1]]
        rebuilt_batch = dict(reversed(list(batch.items())))
        again = handoff(rebuilt_batch, reordered_spools,
                        roundtrip(first["checkpoint"]), 2,
                        [{"op": "unpick", "count": 2}])
        direct = handoff(batch, spools, first["checkpoint"], 2,
                         [{"op": "unpick", "count": 2}])
        self.assertEqual(again, direct)
        # 改动基线（库存、纱筒起始量、计划纬序）都拒绝
        for mutate in (
                lambda b: b["stock"][0].__setitem__("length_mm", 11),
                lambda b: b["jobs"][0].__setitem__("value", 2),
                lambda b: b["jobs"][0]["picks"].__setitem__(1, {"treadles": ["T2"],
                                                                "yarn": "F"})):
            changed = acceptance_batch()
            mutate(changed)
            with self.assertRaises(ValueError):
                handoff(changed, spools, first["checkpoint"], 2, [])
        changed_spools = acceptance_spools()
        changed_spools[0]["length_mm"] = 7
        with self.assertRaises(ValueError):
            handoff(batch, changed_spools, first["checkpoint"], 2, [])

    def test_deterministic_and_input_not_mutated(self):
        batch, spools = acceptance_batch(), acceptance_spools()
        events = [{"op": "mount", "spool_id": "F1"},
                  {"op": "mount", "spool_id": "G1"},
                  {"op": "weave", "count": 3}, {"op": "seal", "count": 1}]
        first = handoff(batch, spools, None, 1, events)
        snapshot = compact(first["checkpoint"])
        again = handoff(batch, spools, None, 1, events)
        self.assertEqual(first, again)
        checkpoint = roundtrip(first["checkpoint"])
        before = compact(checkpoint)
        handoff(batch, spools, checkpoint, 2, [{"op": "unpick", "count": 1}])
        self.assertEqual(compact(checkpoint), before)
        self.assertEqual(compact(first["checkpoint"]), snapshot)


# ---------------------------------------------------------------------------
# compression acceptance
# ---------------------------------------------------------------------------

class CompressionTests(unittest.TestCase):
    def batch(self):
        return {
            "common": {"shafts": 2,
                       "tie_up": [{"id": "T1", "shafts": [1]},
                                  {"id": "T2", "shafts": [2]}],
                       "warp": [{"shaft": 1, "yarn": "W"}, {"shaft": 2, "yarn": "W"}],
                       "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
                       "takeup_permille": 0, "warp_waste_mm": 0, "float_limit": 1},
            "jobs": [{"id": "A", "value": 1, "repeat_y": 1000, "phases": [0],
                      "picks": [{"treadles": ["T1"], "yarn": "y"},
                                {"treadles": ["T2"], "yarn": "y"}]},
                     {"id": "B", "value": 1, "repeat_y": 1000, "phases": [0],
                      "picks": [{"treadles": ["T1"], "yarn": "y"},
                                {"treadles": ["T2"], "yarn": "y"}]}],
            "bridges": [],
            "stock": [{"yarn": "W", "length_mm": 10 ** 9},
                      {"yarn": "y", "length_mm": 10 ** 9}],
            "max_length_mm": 10 ** 9}

    def test_hundred_thousand_repicks_keep_handoff_small(self):
        batch = self.batch()
        spools = [{"id": "S1", "yarn": "y", "length_mm": 300000}]
        report = handoff(batch, spools, None, 1,
                         [{"op": "mount", "spool_id": "S1"},
                          {"op": "weave", "count": 1}])
        self.assertEqual((report["sealed"], report["next_pick"]), (0, 1))
        total_unpicked = 0
        # 49 班在其后反复织入再拆掉 2047 纬，合计 100303 纬，仅传上班 checkpoint
        for no in range(2, 51):
            carried = roundtrip(report["checkpoint"])
            self.assertLessEqual(len(compact(carried).encode("utf-8")), 65536)
            report = handoff(batch, spools, carried, no,
                             [{"op": "weave", "count": 2047},
                              {"op": "unpick", "count": 2047}])
            total_unpicked += 2047
            self.assertEqual((report["sealed"], report["next_pick"]), (0, 1))
            self.assertEqual(len(report["checkpoint"]["tail_spool"]), 1)
        # 最后一班拆掉首班留下的那纬
        report = handoff(batch, spools, roundtrip(report["checkpoint"]), 51,
                         [{"op": "unpick", "count": 1}])
        total_unpicked += 1
        self.assertGreaterEqual(total_unpicked, 100000)
        self.assertEqual(report["scrap_this_shift"],
                         [{"woven_shift": 1, "spool_id": "S1", "mm": 2}])
        row = spool_map(report)["S1"]
        self.assertEqual(row["scrap_mm"], total_unpicked * 2)
        self.assertEqual(row["remaining_mm"], 300000 - total_unpicked * 2)
        self.assertEqual((row["sealed_mm"], row["tail_mm"]), (0, 0))
        self.assertLessEqual(len(compact(report["checkpoint"]).encode("utf-8")),
                             65536)

    def test_checkpoint_format_worst_shape_fits(self):
        """2048 纬尾窗 + 32 筒 + 最大班号的格式上界仍低于 65536 字节。"""
        spool_ids = [f"S{i:037d}" for i in range(32)]
        checkpoint = {"v": 1, "base": "a" * 64, "shift": 2147483647,
                      "sealed": 0, "next": TAIL_WINDOW,
                      "mounted": spool_ids,
                      "amounts": [[10 ** 12, 10 ** 12, 10 ** 12]] * 32,
                      "tail_spool": [31] * TAIL_WINDOW,
                      "tail_shift": [2147483647] * TAIL_WINDOW}
        self.assertLessEqual(len(compact(checkpoint).encode("utf-8")), 65536)


# ---------------------------------------------------------------------------
# nonzero phase, bridges, and mid-tail boundaries
# ---------------------------------------------------------------------------

def phase_bridge_batch():
    """非零相位 + 接缝：计划纬序 u、x、z、y、v（A 取相位 1，B 为接缝）。"""
    return {
        "common": {"shafts": 2,
                   "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
                   "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
                   "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
                   "takeup_permille": 0, "warp_waste_mm": 0, "float_limit": 1},
        "jobs": [{"id": "A", "value": 1, "repeat_y": 1, "phases": [1],
                  "picks": [{"treadles": ["T1"], "yarn": "x"},
                            {"treadles": ["T2"], "yarn": "u"}]},
                 {"id": "C", "value": 1, "repeat_y": 1, "phases": [0],
                  "picks": [{"treadles": ["T1"], "yarn": "y"},
                            {"treadles": ["T2"], "yarn": "v"}]}],
        "bridges": [{"id": "B", "picks": [{"treadles": ["T2"], "yarn": "z"}]}],
        "stock": [{"yarn": "w", "length_mm": 10}, {"yarn": "u", "length_mm": 2},
                  {"yarn": "x", "length_mm": 2}, {"yarn": "z", "length_mm": 2},
                  {"yarn": "y", "length_mm": 2}, {"yarn": "v", "length_mm": 2}],
        "max_length_mm": 5}


class PhaseBridgeTests(unittest.TestCase):
    def spools(self):
        return [{"id": "U1", "yarn": "u", "length_mm": 10},
                {"id": "X1", "yarn": "x", "length_mm": 10},
                {"id": "Z1", "yarn": "z", "length_mm": 10},
                {"id": "Y1", "yarn": "y", "length_mm": 10},
                {"id": "V1", "yarn": "v", "length_mm": 10}]

    def test_plan_uses_phase_and_bridge(self):
        yarns, unit = plan_of(phase_bridge_batch())
        self.assertEqual((yarns, unit), (["u", "x", "z", "y", "v"], 2))

    def test_cross_shift_tail_middle_boundary(self):
        batch, spools = phase_bridge_batch(), self.spools()
        mounts = [{"op": "mount", "spool_id": sid}
                  for sid in ("U1", "X1", "Z1", "Y1", "V1")]
        first = handoff(batch, spools, None, 1, mounts + [{"op": "weave", "count": 2}])
        self.assertEqual((first["sealed"], first["next_pick"]), (0, 2))
        # 第二班再织 2 纬（跨班尾料 4 纬），拆尾 1 纬、签封头 1 纬，中间剩 2 纬
        second = handoff(batch, spools, roundtrip(first["checkpoint"]), 2,
                         [{"op": "weave", "count": 2},
                          {"op": "unpick", "count": 1},
                          {"op": "seal", "count": 1}])
        self.assertEqual((second["sealed"], second["next_pick"]), (1, 3))
        self.assertEqual(second["scrap_this_shift"],
                         [{"woven_shift": 2, "spool_id": "Y1", "mm": 2}])
        rows = spool_map(second)
        self.assertEqual((rows["U1"]["sealed_mm"], rows["U1"]["tail_mm"]), (2, 0))
        self.assertEqual((rows["X1"]["tail_mm"], rows["Z1"]["tail_mm"]), (2, 2))
        self.assertEqual(rows["Y1"]["scrap_mm"], 2)
        self.assertEqual(second["resume"], {"count": 2, "stop_pick": 5,
                                            "reason": "plan_end",
                                            "yarn": None, "spool_id": None})
        # 第三班拆掉中间两纬：归属分别回到首班的 X1 与二班的 Z1
        third = handoff(batch, spools, roundtrip(second["checkpoint"]), 3,
                        [{"op": "unpick", "count": 2}])
        self.assertEqual(third["scrap_this_shift"],
                         [{"woven_shift": 1, "spool_id": "X1", "mm": 2},
                          {"woven_shift": 2, "spool_id": "Z1", "mm": 2}])
        rows = spool_map(third)
        self.assertEqual((rows["X1"]["scrap_mm"], rows["Z1"]["scrap_mm"]), (2, 2))
        self.assertEqual((third["sealed"], third["next_pick"]), (1, 1))


# ---------------------------------------------------------------------------
# naive cross-check over random multi-shift chains
# ---------------------------------------------------------------------------

class NaiveCrossCheckTests(unittest.TestCase):
    def random_batch(self, rng):
        shafts = 2
        tie_up = [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}]
        warp = [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}]
        yarns = ["f", "g"]
        jobs = []
        for i in range(rng.randint(1, 3)):
            picks = [{"treadles": [rng.choice(["T1", "T2"])],
                      "yarn": rng.choice(yarns)} for _ in range(rng.randint(1, 3))]
            jobs.append({"id": f"J{i}", "value": rng.randint(1, 3), "picks": picks,
                         "repeat_y": rng.randint(1, 2),
                         "phases": sorted(rng.sample(range(len(picks)),
                                                     rng.randint(1, min(2, len(picks)))))})
        bridges = [{"id": "S", "picks": [{"treadles": ["T1"],
                                          "yarn": rng.choice(yarns)}]}]
        stock = [{"yarn": "w", "length_mm": 10 ** 6},
                 {"yarn": "f", "length_mm": 10 ** 6},
                 {"yarn": "g", "length_mm": 10 ** 6}]
        return {"common": {"shafts": shafts, "tie_up": tie_up, "warp": warp,
                           "repeat_x": 1, "warp_spacing_mm": rng.randint(1, 2),
                           "pick_spacing_mm": 1,
                           "takeup_permille": rng.choice([0, 100]),
                           "warp_waste_mm": 0,
                           "float_limit": rng.randint(1, 4)},
                "jobs": jobs, "bridges": bridges, "stock": stock,
                "max_length_mm": 10 ** 6}

    def random_chain(self, rng, batch, plan_yarns, unit):
        """生成一批合法事件的多班序列，返回 [(events)]；镜像只用于生成。"""
        spool_ids = ["S1", "S2", "S3"]
        yarns_in_plan = sorted(set(plan_yarns)) or ["f"]
        spools = []
        for i, sid in enumerate(spool_ids[:rng.randint(1, 3)]):
            spools.append({"id": sid, "yarn": rng.choice(yarns_in_plan),
                           "length_mm": rng.randint(0, 30)})
        total = len(plan_yarns)
        sealed = next_pick = 0
        mounted = {}
        remaining = {s["id"]: s["length_mm"] for s in spools}
        yarn_of = {s["id"]: s["yarn"] for s in spools}
        shifts = []
        for _ in range(rng.randint(1, 5)):
            events = []
            for _ in range(rng.randint(0, 10)):
                choices = ["mount"]
                if next_pick < total and next_pick - sealed < TAIL_WINDOW:
                    choices += ["weave"] * 3
                if next_pick - sealed > 0:
                    choices += ["unpick", "seal"]
                op = rng.choice(choices)
                if op == "mount":
                    sid = rng.choice([s["id"] for s in spools])
                    mounted[yarn_of[sid]] = sid
                    events.append({"op": "mount", "spool_id": sid})
                elif op == "weave":
                    feasible = 0
                    needed = {}
                    for pick in range(next_pick, total):
                        yarn = plan_yarns[pick]
                        sid = mounted.get(yarn)
                        if sid is None:
                            break
                        needed[sid] = needed.get(sid, 0) + 1
                        if needed[sid] * unit > remaining[sid]:
                            break
                        feasible += 1
                        if feasible >= 6 or next_pick + feasible - sealed >= TAIL_WINDOW:
                            break
                    if feasible == 0:
                        continue
                    count = rng.randint(1, feasible)
                    for pick in range(next_pick, next_pick + count):
                        sid = mounted[plan_yarns[pick]]
                        remaining[sid] -= unit
                    next_pick += count
                    events.append({"op": "weave", "count": count})
                elif op == "unpick":
                    count = rng.randint(1, min(6, next_pick - sealed))
                    next_pick -= count
                    events.append({"op": "unpick", "count": count})
                else:
                    count = rng.randint(1, min(6, next_pick - sealed))
                    sealed += count
                    events.append({"op": "seal", "count": count})
            shifts.append(events)
        return spools, shifts

    def test_random_chains_against_naive(self):
        rng = random.Random(20261123)
        for _ in range(40):
            batch = self.random_batch(rng)
            plan_yarns, unit = plan_of(batch)
            spools, shifts = self.random_chain(rng, batch, plan_yarns, unit)
            naive = NaiveLoom(plan_yarns, unit, spools)
            checkpoint = None
            for no, events in enumerate(shifts, start=1):
                scrap = naive.run_shift(no, events)
                carried = None if checkpoint is None else roundtrip(checkpoint)
                report = handoff(batch, spools, carried, no, events)
                expected = naive.expected_report(no, scrap)
                self.assertEqual(report["schema_version"], 1)
                self.assertEqual(report["shift_no"], no)
                for key in ("sealed", "next_pick", "total_picks", "spools",
                            "scrap_this_shift", "resume"):
                    self.assertEqual(report[key], expected[key], key)
                for row in report["spools"]:
                    source = next(s for s in spools if s["id"] == row["spool_id"])
                    self.assertEqual(row["remaining_mm"] + row["sealed_mm"]
                                     + row["tail_mm"] + row["scrap_mm"],
                                     source["length_mm"])
                self.assertLessEqual(
                    len(compact(report["checkpoint"]).encode("utf-8")), 65536)
                checkpoint = report["checkpoint"]

    def test_json_handoff_equals_object_handoff(self):
        rng = random.Random(7402)
        batch = self.random_batch(rng)
        plan_yarns, unit = plan_of(batch)
        spools, shifts = self.random_chain(rng, batch, plan_yarns, unit)
        checkpoint = None
        for no, events in enumerate(shifts, start=1):
            direct = handoff(batch, spools, checkpoint, no, events)
            carried = None if checkpoint is None else roundtrip(checkpoint)
            via_json = handoff(batch, spools, carried, no, events)
            self.assertEqual(direct, via_json)
            checkpoint = direct["checkpoint"]


# ---------------------------------------------------------------------------
# validation and failure conventions
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.batch = acceptance_batch()
        self.spools = acceptance_spools()
        self.first = handoff(self.batch, self.spools, None, 1,
                             [{"op": "mount", "spool_id": "F1"},
                              {"op": "mount", "spool_id": "G1"},
                              {"op": "weave", "count": 3},
                              {"op": "seal", "count": 1}])
        self.checkpoint = self.first["checkpoint"]

    def assert_invalid(self, mutate=None, checkpoint=..., events=None, no=2):
        batch = acceptance_batch()
        spools = acceptance_spools()
        cp = self.checkpoint if checkpoint is ... else checkpoint
        if mutate:
            mutate(batch, spools)
        with self.assertRaises(ValueError):
            handoff(batch, spools, cp, no,
                    [{"op": "unpick", "count": 1}] if events is None else events)

    def test_top_level_shape(self):
        with self.assertRaises(ValueError):
            run_handoff([])
        for key in ("batch", "spools", "checkpoint", "shift"):
            data = {"batch": self.batch, "spools": self.spools,
                    "checkpoint": None, "shift": {"no": 1, "events": []}}
            del data[key]
            with self.assertRaises(ValueError):
                run_handoff(data)
        data = {"batch": self.batch, "spools": self.spools, "checkpoint": None,
                "shift": {"no": 1, "events": []}, "extra": 1}
        with self.assertRaises(ValueError):
            run_handoff(data)

    def test_spool_validation(self):
        def run(spools):
            handoff(self.batch, spools, None, 1, [])
        with self.assertRaises(ValueError):
            run([])
        with self.assertRaises(ValueError):
            run([{"id": f"S{i}", "yarn": "F", "length_mm": 1} for i in range(33)])
        with self.assertRaises(ValueError):
            run([{"id": "F1", "yarn": "F", "length_mm": 1},
                 {"id": "F1", "yarn": "G", "length_mm": 1}])
        for bad in ({"id": "bad id!", "yarn": "F", "length_mm": 1},
                    {"id": "F1", "yarn": "", "length_mm": 1},
                    {"id": "F1", "yarn": "F", "length_mm": -1},
                    {"id": "F1", "yarn": "F", "length_mm": True},
                    {"id": "F1", "yarn": "F"},
                    {"id": "F1", "yarn": "F", "length_mm": 1, "x": 1}):
            with self.assertRaises(ValueError):
                run([bad])

    def test_shift_number_rules(self):
        events = [{"op": "mount", "spool_id": "F1"}]
        for no in (0, -1, True, 2, 2147483648):
            with self.assertRaises(ValueError):
                handoff(self.batch, self.spools, None, no, events)
        # 与 checkpoint 班号不衔接
        for no in (1, 3, True, 2147483648):
            with self.assertRaises(ValueError):
                handoff(self.batch, self.spools, self.checkpoint, no, [])

    def test_event_shape_and_references(self):
        bad_events = [
            ["not-an-object"],
            [{"op": "poke"}],
            [{"op": True}],
            [{"op": "mount"}],
            [{"op": "mount", "spool_id": "ghost"}],
            [{"op": "mount", "spool_id": "F1", "count": 1}],
            [{"op": "weave"}],
            [{"op": "weave", "count": 0}],
            [{"op": "weave", "count": -1}],
            [{"op": "weave", "count": True}],
            [{"op": "weave", "count": "3"}],
            [{"op": "weave", "count": 1, "spool_id": "F1"}],
            [{"op": "unpick", "count": 1.5}],
            [{"op": "seal", "count": None}],
        ]
        for events in bad_events:
            with self.assertRaises(ValueError, msg=str(events)):
                handoff(self.batch, self.spools, None, 1, events)
        with self.assertRaises(ValueError):
            handoff(self.batch, self.spools, None, 1,
                    [{"op": "mount", "spool_id": "F1"}] * 120001)

    def test_inconsistent_weave_rejects_whole_shift(self):
        # 越出 P
        self.assert_invalid(checkpoint=None, no=1,
                            events=[{"op": "mount", "spool_id": "F1"},
                                    {"op": "mount", "spool_id": "G1"},
                                    {"op": "weave", "count": 5}])
        # 缺少在用筒（纬 1 的 G 无在用筒）
        self.assert_invalid(checkpoint=None, no=1,
                            events=[{"op": "mount", "spool_id": "F1"},
                                    {"op": "weave", "count": 2}])
        # 纱筒不足（F1 只有 6mm，第三纬需要时 F 在用筒仍是 F1）
        self.assert_invalid(checkpoint=None, no=1,
                            events=[{"op": "mount", "spool_id": "F1"},
                                    {"op": "mount", "spool_id": "G1"},
                                    {"op": "weave", "count": 3},
                                    {"op": "unpick", "count": 2},
                                    {"op": "weave", "count": 2}])
        # 越过 2048 纬尾窗（P 足够大时由窗口拦截）
        batch = self.batch
        big = {"common": dict(batch["common"]), "jobs": [
            {"id": "J", "value": 1, "repeat_y": 1000, "phases": [0],
             "picks": [{"treadles": ["T1"], "yarn": "F"},
                       {"treadles": ["T2"], "yarn": "G"},
                       {"treadles": ["T1"], "yarn": "F"}]}],
            "bridges": [],
            "stock": [{"yarn": "W", "length_mm": 10 ** 9},
                      {"yarn": "F", "length_mm": 10 ** 9},
                      {"yarn": "G", "length_mm": 10 ** 9}],
            "max_length_mm": 10 ** 9}
        big["common"]["float_limit"] = 2
        spools = [{"id": "F1", "yarn": "F", "length_mm": 10 ** 6},
                  {"id": "G1", "yarn": "G", "length_mm": 10 ** 6}]
        with self.assertRaises(ValueError):
            handoff(big, spools, None, 1,
                    [{"op": "mount", "spool_id": "F1"},
                     {"op": "mount", "spool_id": "G1"},
                     {"op": "weave", "count": 2049}])

    def test_unpick_and_seal_bounds(self):
        # 越过签封线：sealed=1 后只能再拆 2 纬
        self.assert_invalid(events=[{"op": "unpick", "count": 3}])
        # 不能签封未织部分
        self.assert_invalid(events=[{"op": "seal", "count": 3}])
        self.assert_invalid(checkpoint=None, no=1,
                            events=[{"op": "unpick", "count": 1}])
        self.assert_invalid(checkpoint=None, no=1,
                            events=[{"op": "seal", "count": 1}])

    def test_empty_plan(self):
        batch = acceptance_batch()
        for row in batch["stock"]:
            row["length_mm"] = 0
        report = handoff(batch, self.spools, None, 1, [])
        self.assertEqual((report["total_picks"], report["sealed"],
                          report["next_pick"]), (0, 0, 0))
        self.assertEqual(report["resume"], {"count": 0, "stop_pick": 0,
                                            "reason": "plan_end",
                                            "yarn": None, "spool_id": None})
        self.assertEqual(report["scrap_this_shift"], [])
        for row in report["spools"]:
            self.assertEqual((row["sealed_mm"], row["tail_mm"], row["scrap_mm"]),
                             (0, 0, 0))
        with self.assertRaises(ValueError):
            handoff(batch, self.spools, None, 1,
                    [{"op": "mount", "spool_id": "F1"},
                     {"op": "weave", "count": 1}])

    def test_mount_empty_and_remount_allowed(self):
        spools = [{"id": "F0", "yarn": "F", "length_mm": 0}] + acceptance_spools()
        report = handoff(self.batch, spools, None, 1,
                         [{"op": "mount", "spool_id": "F0"},
                          {"op": "mount", "spool_id": "F1"},
                          {"op": "mount", "spool_id": "F1"},
                          {"op": "mount", "spool_id": "G1"},
                          {"op": "weave", "count": 1}])
        rows = spool_map(report)
        self.assertFalse(rows["F0"]["mounted"])
        self.assertTrue(rows["F1"]["mounted"])
        # 装空筒后织该型号：扣料不足，整班拒绝
        with self.assertRaises(ValueError):
            handoff(self.batch, spools, None, 1,
                    [{"op": "mount", "spool_id": "F0"},
                     {"op": "weave", "count": 1}])

    def test_checkpoint_shape_rejected(self):
        for bad in ([], "x", 1, True,
                      {"v": 1},                                   # 缺字段
                      dict(self.checkpoint, extra=1),             # 多字段
                      dict(self.checkpoint, v=2),
                      dict(self.checkpoint, v=True),
                      dict(self.checkpoint, base="0" * 64),
                      dict(self.checkpoint, shift=0),
                      dict(self.checkpoint, shift=True),
                      dict(self.checkpoint, sealed=3),            # sealed > next
                      dict(self.checkpoint, next=5),              # next > P
                      dict(self.checkpoint, sealed=0, next=2049), # 尾窗超限
                      dict(self.checkpoint, mounted=["ghost"]),
                      dict(self.checkpoint, mounted=["F1", "F2"]),# 同型号两只
                      dict(self.checkpoint, mounted=[1]),
                      dict(self.checkpoint, amounts=[[0, 0, 0]]),
                      dict(self.checkpoint, amounts=[[0, 0, 0], [0, 0], [0, 0, 0]]),
                      dict(self.checkpoint, amounts=[[-1, 3, 0], [0, 0, 0],
                                                     [3, 3, 0]]),
                      dict(self.checkpoint, amounts=[[1, 3, 0], [0, 0, 0],
                                                     [3, 3, 0]]),  # 非整纬倍数
                      dict(self.checkpoint, amounts=[[4, 3, 0], [0, 0, 0],
                                                     [3, 3, 0]]),  # 超开卷长度
                      dict(self.checkpoint, amounts=[[0, 3, 0], [0, 0, 0],
                                                     [3, 3, 0]]),  # 签封总量不符
                      dict(self.checkpoint, tail_spool=[0]),
                      dict(self.checkpoint, tail_shift=[1]),
                      dict(self.checkpoint, tail_spool=[0, 3], tail_shift=[1, 1]),
                      dict(self.checkpoint, tail_spool=[0, True],
                           tail_shift=[1, 1]),
                      dict(self.checkpoint, tail_shift=[0, 1]),
                      dict(self.checkpoint, tail_shift=[2, 1]),    # 晚于本班
                      ):
            with self.assertRaises(ValueError, msg=str(bad)[:80]):
                handoff(self.batch, self.spools, bad, 2,
                        [{"op": "unpick", "count": 1}])

    def test_checkpoint_attribution_rejected(self):
        # 真实首班尾料为 [G1(班1), F1(班1)]；篡改归属与尾料量都拒绝
        yarn_mismatch = dict(self.checkpoint, tail_spool=[0, 2])
        with self.assertRaises(ValueError):
            handoff(self.batch, self.spools, yarn_mismatch, 2, [])
        tail_amount = dict(self.checkpoint,
                           amounts=[[3, 3, 0], [0, 0, 0], [0, 6, 0]])
        with self.assertRaises(ValueError):
            handoff(self.batch, self.spools, tail_amount, 2, [])

    def test_oversized_checkpoint_rejected(self):
        big = dict(self.checkpoint)
        big["tail_shift"] = [10 ** 30] * 2048
        big["tail_spool"] = [0] * 2048
        big["next"] = 2048
        big["sealed"] = 0
        self.assertGreater(len(compact(big).encode("utf-8")), 65536)
        with self.assertRaises(ValueError):
            handoff(self.batch, self.spools, big, 2, [])

    def test_batch_itself_invalid(self):
        def mutate(batch, _spools):
            batch["jobs"][0]["picks"] = [{"treadles": ["ghost"], "yarn": "F"}]
        self.assert_invalid(mutate=mutate, checkpoint=None, no=1, events=[])


if __name__ == "__main__":
    unittest.main()
