import itertools
import json
import random
import unittest
from loom_draft.batch import run_plan
from loom_draft.service import run as run_v1


def common(**overrides):
    base = {"shafts": 2,
            "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
            "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
            "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
            "takeup_permille": 100, "warp_waste_mm": 1, "float_limit": 3}
    base.update(overrides)
    return base


def pick(treadles, yarn="w"):
    return {"treadles": list(treadles), "yarn": yarn}


def job(jid, treadle_rows, value=1, repeat_y=1, phases=(0,), yarn="w"):
    return {"id": jid, "value": value, "repeat_y": repeat_y, "phases": list(phases),
            "picks": [pick(row, yarn) for row in treadle_rows]}


def bridge(bid, treadle_rows, yarn="w"):
    return {"id": bid, "picks": [pick(row, yarn) for row in treadle_rows]}


def batch(jobs, bridges=(), stock=(("w", 10**6),), max_length_mm=10**6, **common_overrides):
    return {"common": common(**common_overrides), "jobs": list(jobs),
            "bridges": list(bridges),
            "stock": [{"yarn": yarn, "length_mm": length} for yarn, length in stock],
            "max_length_mm": max_length_mm}


def acceptance():
    return batch([job("A", [("T2",), ("T2",), ("T1",), ("T1",)]),
                  job("B", [("T1",), ("T1",), ("T2",), ("T2",)])],
                 bridges=[bridge("S", [("T2",)])],
                 stock=(("w", 49),), max_length_mm=9)


def expand(report):
    """把压缩段报告展开成完整生产串：[(drawdown_row, lifts, yarn)]，并核对段内一致。"""
    rows = []
    for segment in report["segments"]:
        period = list(zip(segment["repeat_drawdown_warp_above"],
                          segment["repeat_pick_lifts"], segment["repeat_yarns"]))
        assert segment["pick_count"] == len(period) * segment["repeat_count"]
        for _ in range(segment["repeat_count"]):
            rows.extend(period)
    return rows


def naive_max_run_ones(column):
    best = current = 0
    best_start = start = None
    for index, value in enumerate(column):
        if value:
            if current == 0:
                start = index
            current += 1
            if current > best:
                best, best_start = current, start
        else:
            current = 0
    return best, best_start


def naive_circular_zero_run(row):
    if all(value == 0 for value in row):
        return None
    best = current = 0
    for value in list(row) + list(row):
        current = current + 1 if value == 0 else 0
        best = max(best, current)
    return best


def assert_consistent(case, data, report):
    """独立核对报告与输入语义：可展开、浮线、材料、库存、汇总全部一致。"""
    common_data = data["common"]
    tie_up = {t["id"]: t["shafts"] for t in common_data["tie_up"]}
    warp = common_data["warp"]
    jobs_by_id = {j["id"]: j for j in data["jobs"]}
    bridges_by_id = {b["id"]: b for b in data["bridges"]}
    stock = {s["yarn"]: s["length_mm"] for s in data["stock"]}
    limit = common_data["float_limit"]
    plan = report["plan"]
    case.assertEqual(report["schema_version"], 1)
    case.assertEqual(report["unselected_job_ids"],
                     sorted(j["id"] for j in data["jobs"]
                            if j["id"] not in {step["job_id"] for step in plan}))
    case.assertEqual(len({step["job_id"] for step in plan}), len(plan))
    if plan:
        case.assertEqual(plan[0]["bridge_before"], "")
    for step in plan:
        case.assertIn(step["job_id"], jobs_by_id)
        case.assertIn(step["phase"], jobs_by_id[step["job_id"]]["phases"])
        if step["bridge_before"]:
            case.assertIn(step["bridge_before"], bridges_by_id)
    # 段与 plan 对齐：start_pick 连续覆盖全串；每个 plan 步对应一个 job 段
    segments = report["segments"]
    cursor = 0
    for segment in segments:
        case.assertEqual(segment["start_pick"], cursor)
        cursor += segment["pick_count"]
    case.assertEqual(cursor, report["material_requirements"]["total_picks"])
    job_positions = [i for i, s in enumerate(segments) if s["kind"] == "job"]
    case.assertEqual(len(job_positions), len(plan))
    for step, index in zip(plan, job_positions):
        segment = segments[index]
        case.assertEqual((segment["id"], segment["phase"]),
                         (step["job_id"], step["phase"]))
        case.assertEqual(segment["repeat_count"], jobs_by_id[step["job_id"]]["repeat_y"])
        if step["bridge_before"]:
            case.assertGreater(index, 0)
            previous = segments[index - 1]
            case.assertEqual((previous["kind"], previous["id"]),
                             ("bridge", step["bridge_before"]))
    if segments:
        case.assertEqual(segments[0]["kind"], "job")
        case.assertEqual(segments[-1]["kind"], "job")
        for left, right in zip(segments, segments[1:]):
            case.assertFalse(left["kind"] == right["kind"] == "bridge")
    # 段内容必须与输入、踏板并集一致
    for segment in segments:
        if segment["kind"] == "job":
            source = jobs_by_id[segment["id"]]
            period = source["picks"][segment["phase"]:] + source["picks"][:segment["phase"]]
        else:
            case.assertEqual(segment["phase"], 0)
            case.assertEqual(segment["repeat_count"], 1)
            period = bridges_by_id[segment["id"]]["picks"]
        case.assertEqual(len(segment["repeat_drawdown_warp_above"]), len(period))
        case.assertEqual(len(segment["repeat_pick_lifts"]), len(period))
        case.assertEqual(len(segment["repeat_yarns"]), len(period))
        for row, lifts, yarn, raw in zip(segment["repeat_drawdown_warp_above"],
                                         segment["repeat_pick_lifts"],
                                         segment["repeat_yarns"], period):
            active = set()
            for treadle in raw["treadles"]:
                active.update(tie_up[treadle])
            case.assertEqual(lifts, sorted(active))
            case.assertEqual(row, [int(end["shaft"] in active) for end in warp])
            case.assertEqual(yarn, raw["yarn"])
    # 展开后独立重算浮线
    rows = expand(report)
    total_picks = len(rows)
    case.assertEqual(report["material_requirements"]["total_picks"], total_picks)
    audit = report["float_audit"]
    if total_picks == 0:
        case.assertEqual(audit["warp_floats"], [])
        case.assertEqual(audit["weft_floats"], [])
    else:
        case.assertEqual(len(audit["warp_floats"]), len(warp))
        for x in range(len(warp)):
            best, best_start = naive_max_run_ones([row[0][x] for row in rows])
            entry = audit["warp_floats"][x]
            case.assertEqual(entry["repeat_index"], x)
            case.assertEqual(entry["max_crossings"], best)
            case.assertEqual(entry["first_start_pick"], best_start if best else None)
            case.assertLessEqual(best, limit)
        expected_weft = []
        for index, segment in enumerate(segments):
            for pick_index, row in enumerate(segment["repeat_drawdown_warp_above"]):
                expected_weft.append({"segment_index": index, "repeat_pick_index": pick_index,
                                      "unbounded": naive_circular_zero_run(row) is None,
                                      "max_crossings": naive_circular_zero_run(row)})
        case.assertEqual(audit["weft_floats"], expected_weft)
        for entry in audit["weft_floats"]:
            case.assertFalse(entry["unbounded"])
            case.assertLessEqual(entry["max_crossings"], limit)
    case.assertTrue(audit["passes_float_limit"])
    # 独立重算材料
    material = report["material_requirements"]
    if total_picks == 0:
        case.assertEqual((material["width_mm"], material["length_mm"],
                          material["total_ends"], material["total_picks"]), (0, 0, 0, 0))
        case.assertEqual(material["yarn"], [])
    else:
        ends = len(warp) * common_data["repeat_x"]
        width = ends * common_data["warp_spacing_mm"]
        length = total_picks * common_data["pick_spacing_mm"]
        case.assertEqual((material["width_mm"], material["length_mm"],
                          material["total_ends"]), (width, length, ends))
        case.assertLessEqual(length, data["max_length_mm"])
        takeup = common_data["takeup_permille"]
        warp_unit = (length * (1000 + takeup) + 999) // 1000 + common_data["warp_waste_mm"]
        weft_unit = (width * (1000 + takeup) + 999) // 1000
        amounts = {}
        for end in warp:
            key = ("warp", end["yarn"])
            amounts[key] = amounts.get(key, 0) + warp_unit * common_data["repeat_x"]
        for _, _, yarn in rows:
            key = ("weft", yarn)
            amounts[key] = amounts.get(key, 0) + weft_unit
        case.assertEqual(material["yarn"],
                         [{"orientation": side, "yarn": yarn, "length_mm": amount}
                          for (side, yarn), amount in sorted(amounts.items()) if amount > 0])
    used = {}
    for row in material["yarn"]:
        used[row["yarn"]] = used.get(row["yarn"], 0) + row["length_mm"]
    case.assertEqual(material["stock_balance"],
                     [{"yarn": yarn, "available_mm": stock.get(yarn, 0),
                       "used_mm": used.get(yarn, 0),
                       "remaining_mm": stock.get(yarn, 0) - used.get(yarn, 0)}
                      for yarn in sorted(set(stock) | set(used))])
    for entry in material["stock_balance"]:
        case.assertGreaterEqual(entry["remaining_mm"], 0)
    summary = report["summary"]
    case.assertEqual(summary["delivered_value"],
                     sum(jobs_by_id[step["job_id"]]["value"] for step in plan))
    case.assertEqual(summary["delivered_jobs"], len(plan))
    case.assertEqual(summary["bridge_picks"],
                     sum(s["pick_count"] for s in segments if s["kind"] == "bridge"))
    case.assertEqual(summary["total_yarn_mm"],
                     sum(row["length_mm"] for row in material["yarn"]))


def brute_force(data):
    """独立直接展开穷举：返回最优(交付价值, 总用纱, 身份键)。"""
    common_data = data["common"]
    tie_up = {t["id"]: t["shafts"] for t in common_data["tie_up"]}
    warp = common_data["warp"]
    limit = common_data["float_limit"]
    spacing = common_data["pick_spacing_mm"]
    takeup = common_data["takeup_permille"]
    stock = {s["yarn"]: s["length_mm"] for s in data["stock"]}
    jobs = data["jobs"]
    bridges = {b["id"]: b["picks"] for b in data["bridges"]}

    def lifts_of(raw):
        active = set()
        for treadle in raw["treadles"]:
            active.update(tie_up[treadle])
        return active

    def expand_picks(picks, times):
        rows = []
        for _ in range(times):
            for raw in picks:
                active = lifts_of(raw)
                rows.append(([int(end["shaft"] in active) for end in warp], raw["yarn"]))
        return rows

    best = None
    for mask in range(1 << len(jobs)):
        selected = [i for i in range(len(jobs)) if mask >> i & 1]
        value = sum(jobs[i]["value"] for i in selected)
        for perm in itertools.permutations(selected):
            options = [sorted(set(jobs[i]["phases"])) for i in perm]
            for phases in itertools.product(*options):
                gap_slots = [[None] + sorted(bridges)] * max(len(perm) - 1, 0)
                for gap_choice in itertools.product(*gap_slots) if gap_slots else [()]:
                    sequence = []
                    identity = []
                    for position, i in enumerate(perm):
                        gap = gap_choice[position - 1] if position else None
                        if gap is not None:
                            sequence += expand_picks(bridges[gap], 1)
                        rotated = jobs[i]["picks"][phases[position]:] + jobs[i]["picks"][:phases[position]]
                        sequence += expand_picks(rotated, jobs[i]["repeat_y"])
                        identity.append((jobs[i]["id"], phases[position], gap or ""))
                    total_picks = len(sequence)
                    length = total_picks * spacing
                    if length > data["max_length_mm"]:
                        continue
                    feasible = True
                    for x in range(len(warp)):
                        run, _ = naive_max_run_ones([row[0][x] for row in sequence])
                        if run > limit:
                            feasible = False
                            break
                    if not feasible:
                        continue
                    for row, _ in sequence:
                        run = naive_circular_zero_run(row)
                        if run is None or run > limit:
                            feasible = False
                            break
                    if not feasible:
                        continue
                    usage = {}
                    if total_picks:
                        ends = len(warp) * common_data["repeat_x"]
                        width = ends * common_data["warp_spacing_mm"]
                        warp_unit = (length * (1000 + takeup) + 999) // 1000 + common_data["warp_waste_mm"]
                        weft_unit = (width * (1000 + takeup) + 999) // 1000
                        for end in warp:
                            usage[end["yarn"]] = usage.get(end["yarn"], 0) + warp_unit * common_data["repeat_x"]
                        for _, yarn in sequence:
                            usage[yarn] = usage.get(yarn, 0) + weft_unit
                    if any(amount > stock.get(yarn, 0) for yarn, amount in usage.items()):
                        continue
                    total = sum(usage.values())
                    if (best is None or value > best[0]
                            or (value == best[0] and (total < best[1]
                                or (total == best[1] and identity < best[2])))):
                        best = (value, total, identity)
    return best


def assert_optimal(case, data):
    report = run_plan(data)
    assert_consistent(case, data, report)
    identity = [(step["job_id"], step["phase"], step["bridge_before"])
                for step in report["plan"]]
    best = brute_force(data)
    case.assertIsNotNone(best)
    case.assertEqual((report["summary"]["delivered_value"],
                      report["summary"]["total_yarn_mm"], identity), best)
    return report


class AcceptanceTests(unittest.TestCase):
    def test_stock_49_delivers_a_s_b(self):
        report = run_plan(acceptance())
        self.assertEqual(report["plan"],
                         [{"job_id": "A", "phase": 0, "bridge_before": ""},
                          {"job_id": "B", "phase": 0, "bridge_before": "S"}])
        self.assertEqual(report["unselected_job_ids"], [])
        material = report["material_requirements"]
        self.assertEqual((material["total_picks"], material["length_mm"],
                          material["width_mm"], material["total_ends"]), (9, 9, 2, 2))
        self.assertEqual(material["yarn"],
                         [{"orientation": "warp", "yarn": "w", "length_mm": 22},
                          {"orientation": "weft", "yarn": "w", "length_mm": 27}])
        self.assertEqual(material["stock_balance"],
                         [{"yarn": "w", "available_mm": 49, "used_mm": 49, "remaining_mm": 0}])
        self.assertEqual(report["summary"], {"delivered_value": 2, "delivered_jobs": 2,
                                             "bridge_picks": 1, "total_yarn_mm": 49})
        self.assertEqual([s["kind"] for s in report["segments"]], ["job", "bridge", "job"])
        self.assertEqual([s["start_pick"] for s in report["segments"]], [0, 4, 5])
        self.assertEqual([s["pick_count"] for s in report["segments"]], [4, 1, 4])
        self.assertEqual(report["float_audit"]["warp_floats"],
                         [{"repeat_index": 0, "max_crossings": 2, "first_start_pick": 2},
                          {"repeat_index": 1, "max_crossings": 2, "first_start_pick": 0}])
        self.assertTrue(report["float_audit"]["passes_float_limit"])
        assert_consistent(self, acceptance(), report)

    def test_stock_48_delivers_only_a(self):
        data = acceptance()
        data["stock"][0]["length_mm"] = 48
        report = run_plan(data)
        self.assertEqual(report["plan"], [{"job_id": "A", "phase": 0, "bridge_before": ""}])
        self.assertEqual(report["unselected_job_ids"], ["B"])
        self.assertEqual(report["summary"], {"delivered_value": 1, "delivered_jobs": 1,
                                             "bridge_picks": 0, "total_yarn_mm": 24})
        self.assertEqual(report["material_requirements"]["yarn"],
                         [{"orientation": "warp", "yarn": "w", "length_mm": 12},
                          {"orientation": "weft", "yarn": "w", "length_mm": 12}])
        assert_consistent(self, data, report)

    def test_bridgeless_orders_have_cross_job_float(self):
        # 不加接缝时 A,B 与 B,A 都有跨单 4 长经浮线；B,S,A 仍不合格
        for stock in (49,):
            report = run_plan(acceptance())
            kinds_ids = [(s["kind"], s["id"]) for s in report["segments"]]
            self.assertEqual(kinds_ids, [("job", "A"), ("bridge", "S"), ("job", "B")])


class PhaseAndBoundaryTests(unittest.TestCase):
    def test_phase_decides_feasibility(self):
        data = batch([job("J", [("T1",), ("T2",)], phases=(0, 1)),
                      job("K", [("T1",), ("T1",)])],
                     takeup_permille=0, warp_waste_mm=0, float_limit=2)
        report = assert_optimal(self, data)
        self.assertEqual([(s["job_id"], s["phase"]) for s in report["plan"]],
                         [("J", 0), ("K", 0)])

    def test_repeat_junction_is_audited(self):
        # 单周期最长 1，但 repeat_y=2 的周期间接缝形成长 2 的段
        data = batch([job("M", [("T1",), ("T2",), ("T1",)], repeat_y=2),
                      job("N", [("T2",)])],
                     takeup_permille=0, warp_waste_mm=0, float_limit=1)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["N"])
        self.assertEqual(report["unselected_job_ids"], ["M"])
        # 放宽到 2 后 M 可交付，且最长段来自周期边界
        data["common"]["float_limit"] = 2
        data["jobs"][0]["value"] = 5
        report = assert_optimal(self, data)
        self.assertIn("M", [s["job_id"] for s in report["plan"]])
        m_index = [s["id"] for s in report["segments"]].index("M")
        self.assertEqual(report["segments"][m_index]["pick_count"], 6)
        warp0 = report["float_audit"]["warp_floats"][0]
        self.assertEqual((warp0["max_crossings"], warp0["first_start_pick"]), (2, 2))

    def test_suffix_run_at_period_boundary(self):
        # 周期尾部的段在有限重复中保持独立：0,1,1 | 0,1,1
        data = batch([job("E", [("T2",), ("T1",), ("T1",)], repeat_y=2)],
                     takeup_permille=0, warp_waste_mm=0, float_limit=2)
        report = assert_optimal(self, data)
        self.assertEqual(report["float_audit"]["warp_floats"][0],
                         {"repeat_index": 0, "max_crossings": 2, "first_start_pick": 1})

    def test_cross_segment_accumulation_and_earliest_start(self):
        jobs = [job("A", [("T1",), ("T2",)]), job("B", [("T1",), ("T1",)]),
                job("C", [("T1",), ("T2",)])]
        data = batch(jobs, takeup_permille=0, warp_waste_mm=0, float_limit=3)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["A", "B", "C"])
        # 1,0 | 1,1 | 1,0 跨三段拼出长 3，最早起点为纬 2
        self.assertEqual(report["float_audit"]["warp_floats"][0],
                         {"repeat_index": 0, "max_crossings": 3, "first_start_pick": 2})
        data["common"]["float_limit"] = 2
        report = assert_optimal(self, data)
        # 全选的唯一可行排列（字典序较小者）：B 不能紧跟 C 之后
        self.assertEqual([s["job_id"] for s in report["plan"]], ["A", "C", "B"])
        self.assertEqual(report["float_audit"]["warp_floats"][0]["max_crossings"], 2)

    def test_rotated_period_boundaries_connect(self):
        # 周期 1,1,0,1 在 repeat_y=2 时跨周期边界拼出长 3，最早起点为纬 3
        data = batch([job("R", [("T1",), ("T1",), ("T2",), ("T1",)], repeat_y=2)],
                     takeup_permille=0, warp_waste_mm=0, float_limit=3)
        report = assert_optimal(self, data)
        self.assertEqual(report["float_audit"]["warp_floats"][0],
                         {"repeat_index": 0, "max_crossings": 3, "first_start_pick": 3})
        # float_limit=2 时周期间接缝段 3 超限，该工单整体不可交付
        data["common"]["float_limit"] = 2
        report = assert_optimal(self, data)
        self.assertEqual(report["plan"], [])

    def test_segment_matrix_uses_rotated_phase(self):
        data = batch([job("Q", [("T2",), ("T1",)], phases=(1,))],
                     takeup_permille=0, warp_waste_mm=0)
        report = assert_optimal(self, data)
        segment = report["segments"][0]
        self.assertEqual(segment["phase"], 1)
        self.assertEqual(segment["repeat_drawdown_warp_above"], [[1, 0], [0, 1]])
        self.assertEqual(segment["repeat_pick_lifts"], [[1], [2]])
        self.assertEqual(segment["repeat_yarns"], ["w", "w"])

    def test_finite_all_ones_column_is_not_unbounded(self):
        data = batch([job("F", [("T1", "T2")], repeat_y=3)],
                     takeup_permille=0, warp_waste_mm=0, float_limit=3)
        report = assert_optimal(self, data)
        self.assertEqual(report["float_audit"]["warp_floats"],
                         [{"repeat_index": 0, "max_crossings": 3, "first_start_pick": 0},
                          {"repeat_index": 1, "max_crossings": 3, "first_start_pick": 0}])
        # 同一组织在 v1 无限平铺口径下仍是 unbounded
        v1 = {"shafts": 2, "tie_up": data["common"]["tie_up"], "warp": data["common"]["warp"],
              "picks": data["jobs"][0]["picks"], "repeat_x": 1, "repeat_y": 3,
              "warp_spacing_mm": 1, "pick_spacing_mm": 1, "takeup_permille": 0,
              "warp_waste_mm": 0, "float_limit": 3}
        result = run_v1(v1)
        self.assertTrue(all(row["unbounded"] for row in result["float_audit"]["warp_floats"]))
        self.assertFalse(result["float_audit"]["passes_float_limit"])
        # 有限串长 4 超过 float_limit=3 则整单不可交付
        data["jobs"][0]["repeat_y"] = 4
        report = run_plan(data)
        self.assertEqual(report["plan"], [])


class OptimizationTests(unittest.TestCase):
    def test_two_lower_value_jobs_beat_single_high(self):
        # H 与 X/Y 相邻都会产生超限经浮线，{X,Y} 价值 4 胜过 H 价值 3
        data = batch([job("H", [("T1",), ("T1",)], value=3),
                      job("X", [("T1",)], value=2),
                      job("Y", [("T1",)], value=2)],
                     takeup_permille=0, warp_waste_mm=0, float_limit=2)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["X", "Y"])
        self.assertEqual(report["summary"]["delivered_value"], 4)

    def test_stock_forces_smaller_delivery(self):
        # {X,Y} 需要纱 w 共 8，库存 5 时只能交付用纱 h 的 H
        data = batch([job("H", [("T1",), ("T1",)], value=3, yarn="h"),
                      job("X", [("T1",)], value=2),
                      job("Y", [("T1",)], value=2)],
                     stock=(("w", 5), ("h", 4)),
                     takeup_permille=0, warp_waste_mm=0, float_limit=2)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["H"])

    def test_phase_swap_beats_bridge_yarn(self):
        # 0 接缝的相位交错方案（纱 768）优于先被搜到的 5 接缝方案（纱 928）：
        # 身份键前缀剪枝不得盖过更优的纱量下界
        warp = [{"shaft": 1 + i % 2, "yarn": "w"} for i in range(16)]
        tie = [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}]

        def pk(t):
            return {"treadles": [t], "yarn": "w"}

        jobs = []
        for i in range(6):
            rows = [("T1",), ("T1",), ("T2",), ("T2",)] if i % 2 == 0 else [("T2",), ("T2",), ("T1",), ("T1",)]
            jobs.append(job(f"J{i}", rows, value=10 - i, phases=(0, 2)))
        data = {"common": common(shafts=2, tie_up=tie, warp=warp, takeup_permille=0,
                                 warp_waste_mm=0, float_limit=3),
                "jobs": jobs,
                "bridges": [bridge("S1", [("T1",)]), bridge("S2", [("T2",)])],
                "stock": [{"yarn": "w", "length_mm": 10**9}], "max_length_mm": 10**9}
        report = run_plan(data)
        assert_consistent(self, data, report)
        self.assertEqual(report["summary"]["total_yarn_mm"], 768)
        self.assertEqual(report["summary"]["bridge_picks"], 0)
        self.assertEqual([(s["job_id"], s["phase"]) for s in report["plan"]],
                         [("J0", 0), ("J1", 2), ("J2", 0), ("J3", 2), ("J4", 0), ("J5", 2)])

    def test_shared_stock_and_single_beam_rounding(self):
        jobs = [job("A", [("T1",)] * 3), job("B", [("T2",)] * 3)]
        data = batch(jobs, stock=(("w", 42),), warp_waste_mm=5, float_limit=10)
        report = assert_optimal(self, data)
        # 整轴一次取整一次损耗：经 unit=ceil(6*1.1)+5=12，纬 unit=ceil(2*1.1)=3
        self.assertEqual(report["material_requirements"]["yarn"],
                         [{"orientation": "warp", "yarn": "w", "length_mm": 24},
                          {"orientation": "weft", "yarn": "w", "length_mm": 18}])
        self.assertEqual(report["summary"]["total_yarn_mm"], 42)
        # 经纬同纱共享库存：41 不够 24+18，只能交付一单
        data["stock"][0]["length_mm"] = 41
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["A"])
        self.assertEqual(report["summary"]["total_yarn_mm"], 27)

    def test_identity_tiebreak_ignores_input_order(self):
        data = acceptance()
        data["common"]["float_limit"] = 4
        data["stock"][0]["length_mm"] = 10**6
        data["max_length_mm"] = 100
        report = assert_optimal(self, data)
        self.assertEqual([(s["job_id"], s["bridge_before"]) for s in report["plan"]],
                         [("A", ""), ("B", "")])
        shuffled = acceptance()
        shuffled["common"]["float_limit"] = 4
        shuffled["stock"][0]["length_mm"] = 10**6
        shuffled["max_length_mm"] = 100
        shuffled["jobs"] = list(reversed(shuffled["jobs"]))
        self.assertEqual(run_plan(shuffled), report)

    def test_identical_bridge_recipes_pick_smaller_id(self):
        data = acceptance()
        data["bridges"] = [bridge("S2", [("T2",)]), bridge("S1", [("T2",)])]
        report = assert_optimal(self, data)
        self.assertEqual([s["bridge_before"] for s in report["plan"]], ["", "S1"])

    def test_empty_plan_when_nothing_affordable(self):
        data = acceptance()
        data["stock"][0]["length_mm"] = 0
        report = run_plan(data)
        self.assertEqual(report["plan"], [])
        self.assertEqual(report["unselected_job_ids"], ["A", "B"])
        self.assertEqual(report["segments"], [])
        self.assertEqual(report["float_audit"],
                         {"warp_floats": [], "weft_floats": [], "passes_float_limit": True})
        material = report["material_requirements"]
        self.assertEqual((material["width_mm"], material["length_mm"],
                          material["total_ends"], material["total_picks"]), (0, 0, 0, 0))
        self.assertEqual(material["yarn"], [])
        self.assertEqual(material["stock_balance"],
                         [{"yarn": "w", "available_mm": 0, "used_mm": 0, "remaining_mm": 0}])
        self.assertEqual(report["summary"], {"delivered_value": 0, "delivered_jobs": 0,
                                             "bridge_picks": 0, "total_yarn_mm": 0})
        assert_consistent(self, data, report)

    def test_no_bridge_recipes_and_unused_stock_yarn(self):
        data = batch([job("A", [("T1",)])], bridges=[],
                     stock=(("w", 10), ("zz", 7)), takeup_permille=0, warp_waste_mm=0)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["A"])
        self.assertIn({"yarn": "zz", "available_mm": 7, "used_mm": 0, "remaining_mm": 7},
                      report["material_requirements"]["stock_balance"])

    def test_all_down_pick_never_selected(self):
        data = batch([job("D", [()]), job("G", [("T1",)])],
                     takeup_permille=0, warp_waste_mm=0)
        report = assert_optimal(self, data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["G"])


class ValidationTests(unittest.TestCase):
    def assert_invalid(self, mutate):
        data = acceptance()
        mutate(data)
        with self.assertRaises(ValueError):
            run_plan(data)

    def test_unselected_job_still_validated(self):
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("picks", [pick(["missing"])]))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("value", True))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("value", 0))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("phases", [0, 4]))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("phases", [0, 0]))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("phases", []))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("phases", [0, 1, 2]))
        self.assert_invalid(lambda d: d["jobs"][1].__setitem__("extra", 1))

    def test_structural_validation(self):
        self.assert_invalid(lambda d: d.__setitem__("jobs", d["jobs"] * 4))          # >6 工单
        self.assert_invalid(lambda d: d["jobs"].__setitem__(1, d["jobs"][0]))        # 重复工单 id
        self.assert_invalid(lambda d: d["bridges"].append(bridge("S", [("T1",)])))   # 重复接缝 id
        self.assert_invalid(lambda d: d["bridges"].append(bridge("S2", [("T1",)] * 9)))  # 接缝超 8 纬
        self.assert_invalid(lambda d: d["bridges"][0].__setitem__("picks", []))      # 接缝空纬
        self.assert_invalid(lambda d: d["stock"].append({"yarn": "w", "length_mm": 1}))  # 重复库存纱
        self.assert_invalid(lambda d: d["stock"][0].__setitem__("length_mm", -1))
        self.assert_invalid(lambda d: d.__setitem__("max_length_mm", 0))
        self.assert_invalid(lambda d: d.__setitem__("max_length_mm", True))
        self.assert_invalid(lambda d: d.__setitem__("unknown", 1))
        self.assert_invalid(lambda d: d["common"].__setitem__("repeat_y", 1))        # common 不许有 repeat_y
        self.assert_invalid(lambda d: d["common"].__setitem__("float_limit", 0))
        self.assert_invalid(lambda d: d["jobs"][0].__setitem__("id", "bad id!"))
        self.assert_invalid(lambda d: d["jobs"][0].__setitem__("id", "x" * 41))
        self.assert_invalid(lambda d: d["jobs"][0].__setitem__("repeat_y", 1001))

    def test_job_and_bridge_may_share_id(self):
        data = batch([job("X", [("T1",)])], bridges=[bridge("X", [("T2",)])],
                     takeup_permille=0, warp_waste_mm=0)
        report = run_plan(data)
        self.assertEqual([s["job_id"] for s in report["plan"]], ["X"])

    def test_top_level_must_be_object(self):
        with self.assertRaises(ValueError):
            run_plan([])


class LargeRepeatTests(unittest.TestCase):
    def test_compressed_report_for_huge_repeats(self):
        warp = [{"shaft": 1 + i % 2, "yarn": "a" if i % 2 == 0 else "b"} for i in range(128)]
        picks = [pick(("T1",), "a") if i % 2 == 0 else pick(("T2",), "b") for i in range(128)]
        data = {"common": common(warp=warp, repeat_x=1000, takeup_permille=0,
                                 warp_waste_mm=0, float_limit=1),
                "jobs": [{"id": "BIG", "value": 1, "picks": picks,
                          "repeat_y": 1000, "phases": [0]}],
                "bridges": [],
                "stock": [{"yarn": "a", "length_mm": 2 * 8_192_000_000},
                          {"yarn": "b", "length_mm": 2 * 8_192_000_000}],
                "max_length_mm": 10**12}
        report = run_plan(data)
        assert_consistent(self, data, report)
        self.assertEqual(len(report["segments"]), 1)
        segment = report["segments"][0]
        self.assertEqual((segment["pick_count"], segment["repeat_count"]), (128000, 1000))
        self.assertEqual(len(segment["repeat_drawdown_warp_above"]), 128)
        material = report["material_requirements"]
        self.assertEqual((material["total_picks"], material["total_ends"]), (128000, 128000))
        self.assertEqual(material["yarn"],
                         [{"orientation": "warp", "yarn": "a", "length_mm": 8_192_000_000},
                          {"orientation": "warp", "yarn": "b", "length_mm": 8_192_000_000},
                          {"orientation": "weft", "yarn": "a", "length_mm": 8_192_000_000},
                          {"orientation": "weft", "yarn": "b", "length_mm": 8_192_000_000}])
        self.assertEqual(report["summary"]["total_yarn_mm"], 32_768_000_000)
        self.assertEqual(report["float_audit"]["warp_floats"][0],
                         {"repeat_index": 0, "max_crossings": 1, "first_start_pick": 0})
        self.assertEqual(report["float_audit"]["warp_floats"][1],
                         {"repeat_index": 1, "max_crossings": 1, "first_start_pick": 1})
        self.assertEqual(len(report["float_audit"]["weft_floats"]), 128)

    def test_all_ones_huge_column(self):
        data = batch([job("F", [("T1", "T2")], repeat_y=1000)],
                     takeup_permille=0, warp_waste_mm=0, float_limit=1000)
        report = run_plan(data)
        self.assertEqual(report["float_audit"]["warp_floats"][0]["max_crossings"], 1000)
        self.assertEqual(report["material_requirements"]["total_picks"], 1000)


class RandomCrossCheckTests(unittest.TestCase):
    def random_data(self, rng, max_jobs, max_bridges):
        shafts = rng.choice([1, 2])
        shaft_pool = list(range(1, shafts + 1))
        treadles = [{"id": "T1", "shafts": sorted(rng.sample(shaft_pool, rng.randint(0, shafts)))}]
        if rng.random() < 0.5 and shafts == 2:
            treadles.append({"id": "T2", "shafts": sorted(rng.sample(shaft_pool, rng.randint(0, 2)))})
        yarns = ["w", "u"]
        warp = [{"shaft": rng.randint(1, shafts), "yarn": rng.choice(yarns)}
                for _ in range(rng.randint(1, 3))]

        def random_picks(maximum):
            return [{"treadles": sorted(rng.sample([t["id"] for t in treadles],
                                                   rng.randint(0, len(treadles)))),
                     "yarn": rng.choice(yarns)} for _ in range(rng.randint(1, maximum))]

        jobs = []
        for i in range(rng.randint(1, max_jobs)):
            picks = random_picks(3)
            phases = sorted(rng.sample(range(len(picks)), min(len(picks), rng.randint(1, 2))))
            jobs.append({"id": f"J{i}", "value": rng.randint(1, 5), "picks": picks,
                         "repeat_y": rng.randint(1, 2), "phases": phases})
        # 倾向产生多相位工单，覆盖“先找到带接缝解、后出现无接缝更优解”的剪枝路径
        for j in jobs:
            if len(j["picks"]) > 1 and len(j["phases"]) == 1 and rng.random() < 0.5:
                j["phases"] = sorted(set(j["phases"]) | {rng.randrange(len(j["picks"]))})
        bridges = [{"id": f"B{i}", "picks": random_picks(2)}
                   for i in range(rng.randint(0, max_bridges))]
        stock = [{"yarn": yarn, "length_mm": rng.randint(0, 40)} for yarn in yarns]
        if rng.random() < 0.3:
            stock = stock[:1]
        return {"common": {"shafts": shafts, "tie_up": treadles, "warp": warp,
                           "repeat_x": rng.randint(1, 2),
                           "warp_spacing_mm": rng.randint(1, 2),
                           "pick_spacing_mm": rng.randint(1, 2),
                           "takeup_permille": rng.randint(0, 200),
                           "warp_waste_mm": rng.randint(0, 3),
                           "float_limit": rng.randint(1, 3)},
                "jobs": jobs, "bridges": bridges, "stock": stock,
                "max_length_mm": rng.randint(3, 60)}

    def test_random_small_against_brute_force(self):
        rng = random.Random(20261122)
        for _ in range(150):
            assert_optimal(self, self.random_data(rng, max_jobs=3, max_bridges=1))

    def test_random_medium_against_brute_force(self):
        rng = random.Random(7401)
        for _ in range(12):
            assert_optimal(self, self.random_data(rng, max_jobs=4, max_bridges=2))


if __name__ == "__main__":
    unittest.main()
