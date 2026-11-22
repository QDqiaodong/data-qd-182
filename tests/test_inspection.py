"""Tests for loom-inspect: inspection-machine reading alignment and recheck list.

The correctness oracle is an independent brute-force enumeration of **all**
complete alignments over the expanded production string; the engine under test
never enumerates routes and works on compressed bitsets instead.
"""
import json
import os
import random
import resource
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from loom_draft.batch import run_plan
from loom_draft.inspection import run_inspection
from loom_draft.io import load

SRC = Path(__file__).resolve().parent.parent / "src"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def common(**overrides):
    base = {"shafts": 2,
            "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
            "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
            "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
            "takeup_permille": 0, "warp_waste_mm": 0, "float_limit": 3}
    base.update(overrides)
    return base


def pk(treadle, yarn="w"):
    return {"treadles": [treadle], "yarn": yarn}


def job(jid, treadles, value=1, repeat_y=1, phases=(0,)):
    return {"id": jid, "value": value, "repeat_y": repeat_y, "phases": list(phases),
            "picks": [pk(t) for t in treadles]}


def batch(jobs, bridges=(), stock=(("w", 10**6),), max_length_mm=10**6, **overrides):
    return {"common": common(**overrides), "jobs": list(jobs),
            "bridges": list(bridges),
            "stock": [{"yarn": yarn, "length_mm": length} for yarn, length in stock],
            "max_length_mm": max_length_mm}


def plain_batch(picks=5):
    """两根实际经端、交替平纹、至少 picks 纬（纬 0 经端 0 在上方）。"""
    treadles = ["T1" if i % 2 == 0 else "T2" for i in range(picks)]
    return batch([job("A", treadles)])


def cut(cid, es, ee, ps, pe):
    return {"id": cid, "end_start": es, "end_stop": ee,
            "pick_start": ps, "pick_stop": pe}


def key_case(frames, cuts=None):
    """契约关键对照：2 经端 5 纬交替平纹，probe_ends=[0]。"""
    if cuts is None:
        cuts = [cut("L13", 0, 1, 1, 3), cut("L2", 0, 1, 2, 3),
                cut("L3", 0, 1, 3, 4), cut("L4", 0, 1, 4, 5),
                cut("R2", 1, 2, 2, 3)]
    return {"batch": plain_batch(5), "probe_ends": [0], "frames": frames,
            "cuts": cuts}


KEY_FRAMES = [[0, 0, 0, 0, "1", None],
              [1, 2, 1, 2, "?", [0, 1]],
              [2, 3, 1, 1, "?", None],
              [4, 4, 1, 2, "1", None]]


def entry(report, cid):
    return next(item for item in report["cuts"] if item["id"] == cid)


class KeyScenarioTests(unittest.TestCase):
    def test_contract_key_comparison(self):
        report = run_inspection(key_case(KEY_FRAMES))
        self.assertEqual(report["schema_version"], 1)
        # 不输出全部路线或逐交织点报告
        self.assertEqual(set(report), {"schema_version", "plan", "segments",
                                       "cuts", "summary"})
        self.assertEqual([c["id"] for c in report["cuts"]],
                         ["L13", "L2", "L3", "L4", "R2"])
        # 完整路线恰为 (0,1,2,4) 与 (0,2,3,4)：确定验过纬 0、2、4
        self.assertEqual(entry(report, "L13"),
                         {"id": "L13", "status": "damaged", "certain_defect": True,
                          "possible_defect": {"pick_index": 1, "frame_index": 1,
                                              "end_start": 0, "end_stop": 1},
                          "forced_observed_picks": 1, "first_unconfirmed_pick": 1})
        self.assertEqual(entry(report, "L2"),
                         {"id": "L2", "status": "recheck", "certain_defect": False,
                          "possible_defect": {"pick_index": 2, "frame_index": 1,
                                              "end_start": 0, "end_stop": 1},
                          "forced_observed_picks": 1, "first_unconfirmed_pick": None})
        self.assertEqual(entry(report, "L3"),
                         {"id": "L3", "status": "recheck", "certain_defect": False,
                          "possible_defect": None,
                          "forced_observed_picks": 0, "first_unconfirmed_pick": 3})
        self.assertEqual(entry(report, "L4"),
                         {"id": "L4", "status": "clear", "certain_defect": False,
                          "possible_defect": None,
                          "forced_observed_picks": 1, "first_unconfirmed_pick": None})
        self.assertEqual(entry(report, "R2"),
                         {"id": "R2", "status": "clear", "certain_defect": False,
                          "possible_defect": None,
                          "forced_observed_picks": 1, "first_unconfirmed_pick": None})
        self.assertEqual(report["summary"],
                         {"cuts": 5, "clear": 2, "recheck": 2, "damaged": 1})

    def test_plan_and_segments_echo_batch_report(self):
        data = key_case(KEY_FRAMES)
        report = run_inspection(data)
        planned = run_plan(data["batch"])
        self.assertEqual(report["plan"], planned["plan"])
        self.assertEqual(report["segments"], planned["segments"])

    def test_cuts_sorted_by_ascii_id(self):
        frames = [[0, 0, 0, 0, "1", None]]
        cuts = [cut("b", 0, 1, 0, 1), cut("A", 0, 1, 0, 1), cut("10", 0, 1, 0, 1)]
        report = run_inspection(key_case(frames, cuts))
        self.assertEqual([c["id"] for c in report["cuts"]], ["10", "A", "b"])
        self.assertEqual(report["summary"],
                         {"cuts": 3, "clear": 3, "recheck": 0, "damaged": 0})

    def test_tighter_last_step_removes_route(self):
        # 末帧最小步长 1->2：路线 (0,2,3,4) 消失，左半幅 [2,3) 变为 clear
        frames = [row[:] for row in KEY_FRAMES]
        frames[3][2] = 2
        report = run_inspection(key_case(frames))
        self.assertEqual(entry(report, "L2")["status"], "clear")
        self.assertIsNone(entry(report, "L2")["possible_defect"])
        self.assertEqual(entry(report, "L13")["status"], "damaged")
        self.assertEqual(report["summary"],
                         {"cuts": 5, "clear": 3, "recheck": 1, "damaged": 1})

    def test_contradictory_last_sample_has_no_alignment(self):
        frames = [row[:] for row in KEY_FRAMES]
        frames[3][4] = "0"
        with self.assertRaises(ValueError):
            run_inspection(key_case(frames))


class ReshotTests(unittest.TestCase):
    """同一纬先干净后命中、或先命中后干净，裁区都不能通过。"""

    def frames(self, defect_first):
        first = [1, 1, 1, 1, "?", [0, 1]] if defect_first else [1, 1, 1, 1, "?", None]
        second = [1, 1, 0, 0, "?", None] if defect_first else [1, 1, 0, 0, "?", [0, 1]]
        return [[0, 0, 0, 0, "1", None], first, second, [2, 2, 1, 1, "1", None]]

    def test_clean_reshot_does_not_erase_defect(self):
        for defect_first in (True, False):
            data = {"batch": plain_batch(4), "probe_ends": [0],
                    "frames": self.frames(defect_first),
                    "cuts": [cut("C", 0, 1, 1, 2)]}
            report = run_inspection(data)
            target = entry(report, "C")
            self.assertEqual(target["status"], "damaged")
            self.assertTrue(target["certain_defect"])
            self.assertEqual(target["forced_observed_picks"], 1)
            self.assertIsNone(target["first_unconfirmed_pick"])
            # 缺陷见证来自带缺陷的那一帧（两次拍摄中先拍到的缺陷帧下标）
            expect_frame = 1 if defect_first else 2
            self.assertEqual(target["possible_defect"],
                             {"pick_index": 1, "frame_index": expect_frame,
                              "end_start": 0, "end_stop": 1})


class PhaseAndBridgeTests(unittest.TestCase):
    """探针读数必须对照实际计划（选定相位、接缝纬），不是工单最初的行。"""

    def test_probe_uses_selected_phase(self):
        # 工单原始第一行是 T2（经端0=0），相位 1 旋转后实际第一纬是 T1（经端0=1）
        data = {"batch": batch([job("A", ["T2", "T1"], phases=(1,))]),
                "probe_ends": [0],
                "frames": [[0, 0, 0, 0, "1", None], [1, 1, 1, 1, "0", None]],
                "cuts": [cut("C", 0, 2, 0, 2)]}
        report = run_inspection(data)
        self.assertEqual(report["plan"],
                         [{"job_id": "A", "phase": 1, "bridge_before": ""}])
        self.assertEqual(entry(report, "C")["status"], "clear")
        # 按工单原始第一行读数（T2 -> 0）构造的帧应无完整对齐
        data["frames"] = [[0, 0, 0, 0, "0", None], [1, 1, 1, 1, "1", None]]
        with self.assertRaises(ValueError):
            run_inspection(data)

    def test_probe_uses_bridge_picks(self):
        # 不加接缝时两单相接产生超限经浮线，唯一最优计划为 A,S,B；
        # 接缝 S 的纬是实际织出的纬（纬 2 为 T2 行，经端0=0）
        jobs = [job("A", ["T1", "T1"]), job("B", ["T1", "T1"])]
        bridges = [{"id": "S", "picks": [pk("T2")]}]
        data = {"batch": batch(jobs, bridges, float_limit=2),
                "probe_ends": [0],
                "frames": [[0, 0, 0, 0, "1", None],
                           [2, 2, 2, 2, "0", None],
                           [4, 4, 2, 2, "1", None]],
                "cuts": [cut("C", 0, 2, 2, 3)]}
        report = run_inspection(data)
        self.assertEqual([s["kind"] for s in report["segments"]],
                         ["job", "bridge", "job"])
        self.assertEqual(entry(report, "C")["status"], "clear")
        self.assertEqual(entry(report, "C")["forced_observed_picks"], 1)
        # 若把纬 2 当成工单行（T1 -> 1）则样本 "0" 无完整对齐
        data["frames"][1][4] = "1"
        with self.assertRaises(ValueError):
            run_inspection(data)
    def test_probe_maps_real_end_to_base_position(self):
        # repeat_x=2：实际经端 3 的基础位置是 3 % 2 = 1（综框 2）
        data = {"batch": batch([job("A", ["T1", "T2"])], repeat_x=2),
                "probe_ends": [3],
                "frames": [[0, 0, 0, 0, "0", None], [1, 1, 1, 1, "1", None]],
                "cuts": [cut("C", 2, 4, 0, 2)]}
        report = run_inspection(data)
        self.assertEqual(entry(report, "C")["status"], "clear")
        # 基础位置 1 的列是 0,1；若误用基础位置 0（1,0）则判定相反
        data["frames"] = [[0, 0, 0, 0, "1", None], [1, 1, 1, 1, "0", None]]
        with self.assertRaises(ValueError):
            run_inspection(data)


class LocalMatchTests(unittest.TestCase):
    """局部满足一帧的位置不一定能进入任何完整对齐。"""

    def test_unreachable_window_pick_is_not_evidence(self):
        # 帧 1 窗口 {1,2} 且局部都合法，但只有纬 2 能接到末帧；
        # 缺陷帧的可能位置只能是纬 2，纬 1 不算被验过
        data = {"batch": plain_batch(4), "probe_ends": [0],
                "frames": [[0, 0, 0, 0, "1", None],
                           [1, 2, 2, 2, "?", [0, 1]],
                           [3, 3, 1, 1, "0", None]],
                "cuts": [cut("P1", 0, 1, 1, 2), cut("P2", 0, 1, 2, 3)]}
        report = run_inspection(data)
        p1 = entry(report, "P1")
        self.assertIsNone(p1["possible_defect"])
        self.assertEqual(p1["status"], "recheck")   # 仅覆盖不确定
        self.assertEqual(p1["first_unconfirmed_pick"], 1)
        p2 = entry(report, "P2")
        self.assertEqual(p2["possible_defect"],
                         {"pick_index": 2, "frame_index": 1,
                          "end_start": 0, "end_stop": 1})
        # 唯一完整对齐 (0,2,3) 把缺陷帧放在纬 2：缺陷确定
        self.assertEqual(p2["status"], "damaged")
        self.assertEqual(p2["forced_observed_picks"], 1)


class AlternatingDefectTests(unittest.TestCase):
    def test_multiple_defect_frames_make_whole_cut_certain(self):
        # 路线 (0,1,1),(0,1,2),(0,2,2)：每条路线都有缺陷帧落在 [1,3)
        data = {"batch": plain_batch(4), "probe_ends": [0],
                "frames": [[0, 0, 0, 0, "1", None],
                           [1, 2, 1, 2, "?", [0, 1]],
                           [1, 2, 0, 1, "?", [0, 1]]],
                "cuts": [cut("W", 0, 1, 1, 3)]}
        report = run_inspection(data)
        target = entry(report, "W")
        self.assertEqual(target["status"], "damaged")
        self.assertTrue(target["certain_defect"])
        # 纬 1、2 都没有被所有路线拍到
        self.assertEqual(target["forced_observed_picks"], 0)
        self.assertEqual(target["first_unconfirmed_pick"], 1)
        self.assertEqual(target["possible_defect"],
                         {"pick_index": 1, "frame_index": 1,
                          "end_start": 0, "end_stop": 1})

    def test_defect_touching_cut_edge_is_not_a_hit(self):
        # 缺陷 [0,1) 与裁区经端区间 [1,2) 恰好相接：不算命中
        data = {"batch": plain_batch(4), "probe_ends": [0],
                "frames": [[0, 0, 0, 0, "1", None],
                           [1, 1, 1, 1, "?", [0, 1]],
                           [2, 2, 1, 1, "1", None]],
                "cuts": [cut("R", 1, 2, 1, 2)]}
        report = run_inspection(data)
        self.assertEqual(entry(report, "R")["status"], "clear")


class EmptyPlanTests(unittest.TestCase):
    def empty(self, **overrides):
        data = batch([job("A", ["T1"])], stock=(("w", 0),))
        data.update(overrides)
        return data

    def test_empty_plan_empty_scan_succeeds(self):
        report = run_inspection({"batch": self.empty(), "probe_ends": [],
                                 "frames": [], "cuts": []})
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["plan"], [])
        self.assertEqual(report["segments"], [])
        self.assertEqual(report["cuts"], [])
        self.assertEqual(report["summary"],
                         {"cuts": 0, "clear": 0, "recheck": 0, "damaged": 0})

    def test_empty_plan_rejects_any_evidence(self):
        for field, value in (("probe_ends", [0]),
                             ("frames", [[0, 0, 0, 0, "?", None]]),
                             ("cuts", [cut("C", 0, 1, 0, 1)])):
            data = {"batch": self.empty(), "probe_ends": [], "frames": [], "cuts": []}
            data[field] = value
            with self.assertRaises(ValueError, msg=field):
                run_inspection(data)


class ValidationTests(unittest.TestCase):
    def base(self):
        return key_case(KEY_FRAMES)

    def assert_invalid(self, mutate):
        data = self.base()
        mutate(data)
        with self.assertRaises(ValueError):
            run_inspection(data)

    def test_top_level_fields(self):
        self.assert_invalid(lambda d: d.__delitem__("frames"))
        self.assert_invalid(lambda d: d.__setitem__("extra", 1))
        with self.assertRaises(ValueError):
            run_inspection([])

    def test_probe_ends(self):
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", []))
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", [0, 0]))
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", [0, 2]))   # E=2
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", [True]))
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", [0] * 9))
        self.assert_invalid(lambda d: d.__setitem__("probe_ends", "0"))

    def test_frame_shape(self):
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "1"]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "1", None, 1]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, "not-a-frame"))
        self.assert_invalid(lambda d: d.__setitem__("frames", []))

    def test_frame_window(self):
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [2, 1, 1, 2, "?", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 5, 1, 2, "?", None]))  # hi>=P
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [0, 512, 1, 2, "?", None]))  # >512 纬
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [True, 2, 1, 2, "?", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [-1, 2, 1, 2, "?", None]))

    def test_frame_steps(self):
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 1, 1, "1", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 3, 2, "?", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 5, "?", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, -1, 2, "?", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, True, 2, "?", None]))

    def test_frame_sample(self):
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "11", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "x", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, "2", None]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(0, [0, 0, 0, 0, 1, None]))

    def test_frame_defect(self):
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [1, 1]]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [0, 3]]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [-1, 1]]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [0]]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [0, 1, 2]]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", "x"]))
        self.assert_invalid(lambda d: d["frames"].__setitem__(1, [1, 2, 1, 2, "?", [True, 1]]))

    def test_cuts(self):
        self.assert_invalid(lambda d: d["cuts"].append(cut("L2", 0, 1, 0, 1)))   # 重复 id
        self.assert_invalid(lambda d: d["cuts"].append(cut("bad id!", 0, 1, 0, 1)))
        self.assert_invalid(lambda d: d["cuts"].append(cut("X", 1, 1, 0, 1)))
        self.assert_invalid(lambda d: d["cuts"].append(cut("X", 0, 3, 0, 1)))    # ee>E
        self.assert_invalid(lambda d: d["cuts"].append(cut("X", 0, 1, 4, 4)))
        self.assert_invalid(lambda d: d["cuts"].append(cut("X", 0, 1, 0, 6)))    # pe>P
        self.assert_invalid(lambda d: d["cuts"].append(cut("X", True, 1, 0, 1)))
        self.assert_invalid(lambda d: d["cuts"].append(
            {"id": "X", "end_start": 0, "end_stop": 1, "pick_start": 0, "pick_stop": 1,
             "extra": 1}))
        self.assert_invalid(lambda d: d["cuts"].append(
            {"id": "X", "end_start": 0, "end_stop": 1, "pick_start": 0}))
        self.assert_invalid(lambda d: d.__setitem__(
            "cuts", [cut(f"C{i}", 0, 1, 0, 1) for i in range(33)]))

    def test_batch_still_validated(self):
        self.assert_invalid(lambda d: d["batch"]["jobs"][0].__setitem__("value", True))
        self.assert_invalid(lambda d: d["batch"].__setitem__("unknown", 1))

    def test_frame_count_bound(self):
        data = self.base()
        row = [0, 0, 0, 0, "1", None]
        data["frames"] = [row[:] for _ in range(120001)]
        with self.assertRaises(ValueError):
            run_inspection(data)


# ---------------------------------------------------------------------------
# brute-force oracle over all complete alignments
# ---------------------------------------------------------------------------

def expand_rows(report):
    """把批次报告的压缩段展开为逐纬组织行（只含基础经端列）。"""
    rows = []
    for segment in report["segments"]:
        for _ in range(segment["repeat_count"]):
            rows.extend(segment["repeat_drawdown_warp_above"])
    return rows


def brute_alignments(rows, probes, frames):
    """独立穷举全部完整对齐；每帧候选纬同时满足窗口与组织读数。"""
    base = len(rows[0])
    allowed = []
    for lo, hi, _mn, _mx, sample, _defect in frames:
        picks = []
        for pick in range(lo, hi + 1):
            if all(ch == "?" or rows[pick][probes[j] % base] == int(ch)
                   for j, ch in enumerate(sample)):
                picks.append(pick)
        allowed.append(picks)
    solutions = []
    current = []

    def recurse(i, previous):
        if i == len(allowed):
            solutions.append(tuple(current))
            return
        for pick in allowed[i]:
            if i and not frames[i][2] <= pick - previous <= frames[i][3]:
                continue
            current.append(pick)
            recurse(i + 1, pick)
            current.pop()

    recurse(0, None)
    return solutions


def brute_expected(rows, probes, frames, cuts):
    """用全部完整对齐直接按业务定义计算每块裁区的结论。"""
    solutions = brute_alignments(rows, probes, frames)
    if not solutions:
        return None
    total = len(rows)
    certainly = set()
    for pick in range(total):
        if all(pick in solution for solution in solutions):
            certainly.add(pick)
    expected = []
    for c in sorted(cuts, key=lambda item: item["id"]):
        ps, pe = c["pick_start"], c["pick_stop"]
        es, ee = c["end_start"], c["end_stop"]
        forced = sum(1 for pick in range(ps, pe) if pick in certainly)
        first_unconfirmed = next((pick for pick in range(ps, pe)
                                  if pick not in certainly), None)
        hits = []
        for solution in solutions:
            for index, pick in enumerate(solution):
                defect = frames[index][5]
                if defect is None or not ps <= pick < pe:
                    continue
                if max(defect[0], es) < min(defect[1], ee):
                    hits.append((pick, index, max(defect[0], es), min(defect[1], ee)))
        possible = None
        if hits:
            pick, index, ds, de = min(hits, key=lambda item: (item[0], item[1]))
            possible = {"pick_index": pick, "frame_index": index,
                        "end_start": ds, "end_stop": de}
        certain = bool(hits) and all(
            any(frames[index][5] is not None and ps <= pick < pe
                and max(frames[index][5][0], es) < min(frames[index][5][1], ee)
                for index, pick in enumerate(solution))
            for solution in solutions)
        if certain:
            status = "damaged"
        elif possible is not None or first_unconfirmed is not None:
            status = "recheck"
        else:
            status = "clear"
        expected.append({"id": c["id"], "status": status, "certain_defect": certain,
                         "possible_defect": possible,
                         "forced_observed_picks": forced,
                         "first_unconfirmed_pick": first_unconfirmed})
    return expected


class RandomCrossCheckTests(unittest.TestCase):
    def random_batch(self, rng):
        jobs = []
        for i in range(rng.randint(1, 2)):
            treadles = [rng.choice(["T1", "T2"]) for _ in range(rng.randint(2, 3))]
            phases = sorted(rng.sample(range(len(treadles)),
                                       rng.randint(1, min(2, len(treadles)))))
            jobs.append({"id": f"J{i}", "value": rng.randint(1, 3),
                         "repeat_y": rng.randint(1, 2), "phases": phases,
                         "picks": [pk(t) for t in treadles]})
        bridges = []
        if rng.random() < 0.4:
            bridges.append({"id": "S",
                            "picks": [pk(rng.choice(["T1", "T2"]))
                                      for _ in range(rng.randint(1, 2))]})
        return batch(jobs, bridges, repeat_x=rng.randint(1, 2),
                     float_limit=rng.randint(1, 3))

    def random_case(self, rng):
        for _ in range(100):
            data = self.random_batch(rng)
            rows = expand_rows(run_plan(data))
            if len(rows) >= 2:
                break
        else:
            raise AssertionError("could not sample a non-empty plan")
        total = len(rows)
        ends = len(data["common"]["warp"]) * data["common"]["repeat_x"]
        probes = sorted(rng.sample(range(ends), rng.randint(1, min(2, ends))))
        frames = []
        if rng.random() < 0.7:
            # 沿一条随机真实路径开窗/给步长，多数情况下存在完整对齐
            pick = rng.randint(0, min(2, total - 1))
            while True:
                lo = max(0, pick - rng.randint(0, 2))
                hi = min(total - 1, pick + rng.randint(0, 2))
                if frames:
                    step = pick - frames[-1][6]
                    mn = rng.randint(0, step)
                    mx = rng.randint(step, 4)
                else:
                    mn = mx = 0
                sample = ""
                for probe in probes:
                    r = rng.random()
                    if r < 0.5:
                        sample += "?"
                    elif r < 0.8:
                        sample += str(rows[pick][probe % len(rows[0])])
                    else:
                        sample += rng.choice("01")
                defect = None
                if rng.random() < 0.3:
                    ds = rng.randrange(ends)
                    defect = [ds, rng.randint(ds + 1, ends)]
                frames.append([lo, hi, mn, mx, sample, defect, pick])
                if len(frames) >= 6:
                    break
                nxt = pick + rng.randint(0, 3)
                if nxt >= total:
                    break
                pick = nxt
            frames = [row[:6] for row in frames]
        else:
            for i in range(rng.randint(1, 6)):
                lo = rng.randrange(total)
                hi = min(total - 1, lo + rng.randint(0, 4))
                mn, mx = (0, 0) if i == 0 else (rng.randint(0, 3), rng.randint(0, 4))
                if mn > mx:
                    mn, mx = mx, mn
                sample = "".join(rng.choice("01?") for _ in probes)
                defect = None
                if rng.random() < 0.3:
                    ds = rng.randrange(ends)
                    defect = [ds, rng.randint(ds + 1, ends)]
                frames.append([lo, hi, mn, mx, sample, defect])
        cuts = []
        for i in range(rng.randint(0, 3)):
            es = rng.randrange(ends)
            ps = rng.randrange(total)
            cuts.append(cut(f"C{i}", es, rng.randint(es + 1, ends),
                            ps, rng.randint(ps + 1, total)))
        return {"batch": data, "probe_ends": probes, "frames": frames,
                "cuts": cuts}, rows

    def test_random_against_brute_force(self):
        rng = random.Random(20261122)
        checked = 0
        for _ in range(400):
            data, rows = self.random_case(rng)
            expected = brute_expected(rows, data["probe_ends"],
                                      data["frames"], data["cuts"])
            if expected is None:
                with self.assertRaises(ValueError):
                    run_inspection(data)
                continue
            report = run_inspection(data)
            self.assertEqual(report["cuts"], expected)
            summary = {"cuts": len(expected), "clear": 0, "recheck": 0, "damaged": 0}
            for item in expected:
                summary[item["status"]] += 1
            self.assertEqual(report["summary"], summary)
            checked += 1
        self.assertGreater(checked, 100)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(source, output, module="loom_draft.inspect_cli", limit=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)

    def preexec():
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return subprocess.run([sys.executable, "-m", module, "--source", str(source),
                           "--output", str(output)],
                          capture_output=True, text=True, env=env, timeout=600,
                          preexec_fn=preexec if limit is not None else None)


class InspectCliProcessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "input.json"
        self.output = Path(self.dir.name) / "report.json"

    def write(self, data):
        self.source.write_text(json.dumps(data))

    def test_success_prints_summary_and_writes_report(self):
        data = json.loads((FIXTURES / "inspect_input.json").read_text())
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"cuts": 5, "clear": 2, "recheck": 2, "damaged": 1})
        report = json.loads(self.output.read_text())
        self.assertEqual(report, run_inspection(load(self.source)))

    def test_recheck_and_damaged_still_exit_zero(self):
        data = json.loads((FIXTURES / "inspect_input.json").read_text())
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual({c["id"]: c["status"] for c in report["cuts"]},
                         {"L13": "damaged", "L2": "recheck", "L3": "recheck",
                          "L4": "clear", "R2": "clear"})

    def test_existing_output_kept_byte_for_byte(self):
        self.write(json.loads((FIXTURES / "inspect_input.json").read_text()))
        self.output.write_bytes(b"previous report\n")
        before = self.output.read_bytes()
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_invalid_input_leaves_no_report(self):
        data = json.loads((FIXTURES / "inspect_input.json").read_text())
        data["frames"][1][2] = 9
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(Path(self.dir.name).glob(".*"))), 0)

    def test_no_alignment_is_input_error(self):
        data = json.loads((FIXTURES / "inspect_input.json").read_text())
        data["frames"][3][4] = "0"
        self.write(data)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.output.exists())

    def test_duplicate_and_nonfinite_rejected(self):
        self.source.write_text('{"batch": {}, "batch": {}, "probe_ends": [], '
                               '"frames": [], "cuts": []}')
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)
        self.assertFalse(self.output.exists())
        data = json.loads((FIXTURES / "inspect_input.json").read_text())
        data["frames"][0][0] = "NaN"
        text = json.dumps(data).replace('"NaN"', "NaN")
        self.source.write_text(text)
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())

    def test_missing_source_and_malformed_json(self):
        result = run_cli(Path(self.dir.name) / "none.json", self.output)
        self.assertEqual(result.returncode, 2)
        self.source.write_text("{not json")
        result = run_cli(self.source, self.output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.output.exists())


# ---------------------------------------------------------------------------
# big acceptance scenario (120000 frames, 192 MiB address space)
# ---------------------------------------------------------------------------

def big_input():
    """128 纬平纹周期 ×1000、横向 ×1000 的批次 + 120000 帧验收扫描。"""
    picks = [pk("T1" if i % 2 == 0 else "T2") for i in range(128)]
    data = {"common": common(repeat_x=1000, float_limit=1),
            "jobs": [{"id": "BIG", "value": 1, "repeat_y": 1000, "phases": [0],
                      "picks": picks}],
            "bridges": [],
            "stock": [{"yarn": "w", "length_mm": 512_000_000}],
            "max_length_mm": 128000}
    frames = [[0, 0, 0, 0, "?", None]]
    frames += [[i, i + 511, 1, 2, "?", None] for i in range(1, 119999)]
    frames.append([120510, 120510, 1, 2, "?", None])
    return {"batch": data, "probe_ends": [0], "frames": frames,
            "cuts": [cut("ALL", 0, 2000, 0, 120511),
                     cut("LAST", 0, 2000, 120510, 120511)]}


class BigAcceptanceTests(unittest.TestCase):
    def test_big_scenario_values(self):
        # 任一完整对齐首尾共跳过 511 纬；每条内部纬都能被某条路线跳过
        report = run_inspection(big_input())
        self.assertEqual(entry(report, "ALL"),
                         {"id": "ALL", "status": "recheck", "certain_defect": False,
                          "possible_defect": None,
                          "forced_observed_picks": 2, "first_unconfirmed_pick": 1})
        self.assertEqual(entry(report, "LAST"),
                         {"id": "LAST", "status": "clear", "certain_defect": False,
                          "possible_defect": None,
                          "forced_observed_picks": 1, "first_unconfirmed_pick": None})
        self.assertEqual(report["summary"],
                         {"cuts": 2, "clear": 1, "recheck": 1, "damaged": 0})
        self.assertEqual(report["segments"][0]["pick_count"], 128000)
        self.assertEqual(len(report["segments"][0]["repeat_drawdown_warp_above"]), 128)

    def test_big_scenario_cli_under_memory_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "big_input.json"
            output = Path(tmp) / "big_report.json"
            generate = subprocess.run(
                [sys.executable, str(EXAMPLES / "inspection_acceptance.py"),
                 str(source)], capture_output=True, text=True, timeout=300)
            self.assertEqual(generate.returncode, 0, generate.stderr)
            result = run_cli(source, output, limit=192 * 1024 * 1024)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {"cuts": 2, "clear": 1, "recheck": 1, "damaged": 0})
            report = json.loads(output.read_text())
            self.assertEqual(entry(report, "ALL")["forced_observed_picks"], 2)
            self.assertEqual(entry(report, "ALL")["first_unconfirmed_pick"], 1)
            self.assertEqual(entry(report, "LAST")["status"], "clear")


if __name__ == "__main__":
    unittest.main()
