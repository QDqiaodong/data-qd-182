import itertools
import json
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loom_draft.card import run_card
from card_reference import (compile_tie_up, draw_row, allowed_picks,
                            validate_normal)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def base_input():
    return {
        "shafts": 2,
        "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
        "pedals": [
            {"id": "A", "current": [1], "options": [[1]]},
            {"id": "B", "current": [1, 2], "options": [[1, 2]]},
            {"id": "C", "current": [2], "options": [[2]]},
            {"id": "E", "current": [1], "options": [[1]]},
            {"id": "F", "current": [2], "options": [[2]]},
        ],
        "max_pressed": 2,
        "blocked_pairs": [["E", "F"]],
        "patterns": [{"id": "P", "rows": ["10", "11", "01"],
                      "faults": [[1, "B"]]}],
        "rejoin_limit": 1,
    }


def make(pedals, patterns, shafts=2, warp=None, max_pressed=2,
         blocked=(), rejoin=0):
    return {
        "shafts": shafts,
        "warp": warp if warp is not None else [
            {"shaft": s, "yarn": "w"} for s in range(1, shafts + 1)],
        "pedals": pedals,
        "max_pressed": max_pressed,
        "blocked_pairs": [list(pair) for pair in blocked],
        "patterns": patterns,
        "rejoin_limit": rejoin,
    }


def pedal(pid, current, options=None):
    return {"id": pid, "current": list(current),
            "options": [list(o) for o in (options or [current])]}


def pattern(pid, row_strings, faults=()):
    return {"id": pid, "rows": list(row_strings),
            "faults": [list(f) for f in faults]}


def report_tie_up(report):
    return {row["id"]: tuple(row["shafts"]) for row in report["tie_up"]}


def check_report(case, data, report):
    """独立回查：用旧组织编译核对常态与应急串的每一纬。"""
    case.assertEqual(report["schema_version"], 1)
    case.assertIn(report["status"], ("ready", "infeasible"))
    if report["status"] == "infeasible":
        case.assertIsNone(report["changed_pedals"])
        case.assertEqual(report["tie_up"], [])
        case.assertEqual(report["patterns"], [])
        return
    tie_up = report_tie_up(report)
    ends = [end["shaft"] for end in data["warp"]]
    blocked = data["blocked_pairs"]
    source = {p["id"]: p for p in data["pedals"]}
    changed = sum(1 for pid, shafts in tie_up.items()
                  if tuple(sorted(shafts)) != tuple(sorted(source[pid]["current"])))
    case.assertEqual(report["changed_pedals"], changed)
    for pid, shafts in tie_up.items():
        case.assertIn(tuple(sorted(shafts)),
                      [tuple(sorted(option)) for option in source[pid]["options"]])
    case.assertEqual([row["id"] for row in report["tie_up"]], sorted(tie_up))
    patterns = {p["id"]: p for p in data["patterns"]}
    case.assertEqual([p["id"] for p in report["patterns"]], sorted(patterns))
    window = 1 + data["rejoin_limit"]
    for entry in report["patterns"]:
        spec = patterns[entry["id"]]
        target_rows = [tuple(int(c) for c in text) for text in spec["rows"]]
        normal = [tuple(pick) for pick in entry["picks"]]
        case.assertEqual(len(normal), len(target_rows))
        case.assertTrue(validate_normal(ends, tie_up, blocked, target_rows,
                                        normal, data["max_pressed"]))
        # 应急串必须从给定常态表展开，且跨周期尾接回正确
        playbook = {(item["row"], item["pedal_id"]): item
                    for item in entry["fault_playbook"]}
        case.assertEqual(sorted(playbook),
                         sorted((f[0], f[1]) for f in spec["faults"]))
        for row, pedal_id in spec["faults"]:
            item = playbook[(row, pedal_id)]
            picks = [tuple(pick) for pick in item["picks"]]
            case.assertTrue(1 <= len(picks) <= window)
            resume = normal[(row + len(picks)) % len(normal)]
            previous = normal[(row - 1) % len(normal)]
            for step, pick in enumerate(picks):
                target = target_rows[(row + step) % len(target_rows)]
                case.assertEqual(draw_row(ends, tie_up, pick), target)
                case.assertLessEqual(len(pick), data["max_pressed"])
                case.assertFalse(any(frozenset(pair) in {frozenset(b) for b in blocked}
                                     for pair in itertools.combinations(pick, 2)))
            case.assertNotIn(pedal_id, picks[0])
            case.assertFalse(set(previous) & set(picks[0]))
            if len(picks) > 1:
                for left, right in zip(picks, picks[1:]):
                    case.assertFalse(set(left) & set(right))
            case.assertFalse(set(picks[-1]) & set(resume))
            case.assertEqual(item["resume_row"], (row + len(picks)) % len(spec["rows"]))


class KeyExampleTests(unittest.TestCase):
    def test_rejoin_limit_one_matches_contract(self):
        report = run_card(base_input())
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["changed_pedals"], 0)
        self.assertEqual(report["patterns"][0]["picks"], [["A"], ["B"], ["C"]])
        self.assertEqual(report["patterns"][0]["fault_playbook"],
                         [{"row": 1, "pedal_id": "B",
                           "picks": [["C", "E"], ["F"]], "resume_row": 0}])

    def test_rejoin_limit_zero_changes_normal(self):
        data = base_input()
        data["rejoin_limit"] = 0
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["patterns"][0]["picks"], [["A"], ["B"], ["F"]])
        self.assertEqual(report["patterns"][0]["fault_playbook"],
                         [{"row": 1, "pedal_id": "B",
                           "picks": [["C", "E"]], "resume_row": 2}])

    def test_reduced_machine_infeasible_but_weaves_without_fault(self):
        data = base_input()
        data["pedals"] = [p for p in data["pedals"] if p["id"] != "F"]
        data["blocked_pairs"] = []
        self.assertEqual(run_card(data)["status"], "infeasible")
        data["patterns"][0]["faults"] = []
        self.assertEqual(run_card(data)["status"], "ready")

    def test_fault_at_lap_start(self):
        data = make(
            [pedal("A", [1]), pedal("B", [2]), pedal("C", [1]),
             pedal("D", [2])],
            [pattern("P", ["10", "01"], faults=[[0, "A"]])], rejoin=1)
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        item = report["patterns"][0]["fault_playbook"][0]
        self.assertEqual((item["row"], item["resume_row"]), (0, 1))
        self.assertEqual(item["picks"], [["C"]])
        check_report(self, data, report)

    def test_fault_wraps_past_last_row(self):
        data = make(
            [pedal("A", [1]), pedal("B", [2]), pedal("C", [1]),
             pedal("D", [2])],
            [pattern("P", ["10", "01"], faults=[[1, "B"]])], rejoin=1)
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        item = report["patterns"][0]["fault_playbook"][0]
        self.assertEqual(item["row"], 1)
        self.assertEqual(item["resume_row"], 0)
        check_report(self, data, report)

    def test_single_row_lap_is_adjacent_to_itself(self):
        # 一纬周期的常态踩法与自身相邻，非空踩法必然自我重叠，只有空踩可行。
        data = make([pedal("A", [1])], [pattern("P", ["00"])], max_pressed=1)
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["patterns"][0]["picks"], [[]])
        check_report(self, data, report)
        up = make([pedal("A", [1])], [pattern("P", ["10"])], max_pressed=1)
        self.assertEqual(run_card(up)["status"], "infeasible")

    def test_empty_binding_pedal_still_counts_and_occupies_slot(self):
        # 空绑综踏板仍是实际踩踏动作：占同时踩名额并参与冷却，但不升降综框。
        data = make(
            [pedal("A", [1]), pedal("Z", [])],
            [pattern("P", ["10", "10"])], max_pressed=1)
        report = run_card(data)
        # max_pressed=1：相邻两纬都要求升 {1}，需要两条踏板分别承担；空绑综 Z
        # 无法升 {1}，故无解。
        self.assertEqual(report["status"], "infeasible")
        two = make(
            [pedal("A", [1]), pedal("B", [1]), pedal("Z", [])],
            [pattern("P", ["10", "10"])], max_pressed=1)
        report = run_card(two)
        self.assertEqual(report["status"], "ready")
        check_report(self, two, report)

    def test_fault_pedal_only_barred_on_first_emergency_pick(self):
        # 常态 [A],[C],[B]；A 在 0 纬失灵。应急 [C],[A]：第 0 拍只能踩 C，第 1 拍
        # 可再用 A（只有第 0 个应急动作受失灵限制）。
        data = make(
            [pedal("A", [1]), pedal("B", [2]), pedal("C", [1]),
             pedal("D", [2])],
            [pattern("P", ["10", "10", "01"], faults=[[0, "A"]])],
            max_pressed=1, rejoin=2)
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["patterns"][0]["picks"], [["A"], ["C"], ["B"]])
        playbook = report["patterns"][0]["fault_playbook"][0]
        self.assertEqual(playbook["picks"], [["C"], ["A"]])
        self.assertEqual(playbook["resume_row"], 2)
        check_report(self, data, report)

    def test_two_patterns_need_rebinding_to_share(self):
        # 旧绑法 Z={1,2}：A 的旧常态 [X,Y] 仍能织，但 X 在 0 纬失灵时唯一能升
        # {1} 的备用踏板 Z 会升成 {1,2}（且与上一拍 Y 冲突），应急不通过；把 Z
        # 改绑 {1} 后 A 的应急成立，共用绑综下两款都交出合法卡。
        data = make(
            [pedal("X", [1]), pedal("Y", [2]),
             pedal("Z", [1, 2], [[1, 2], [1]])],
            [pattern("A", ["10", "01"], faults=[[0, "X"]]),
             pattern("B", ["01", "10"])],
            max_pressed=2, rejoin=0)
        self.assertEqual(run_card(data)["status"], "ready")
        report = run_card(data)
        self.assertEqual(report["changed_pedals"], 1)
        self.assertEqual({row["id"]: row["shafts"] for row in report["tie_up"]},
                         {"X": [1], "Y": [2], "Z": [1]})
        self.assertEqual(report["patterns"][0]["fault_playbook"][0]["picks"],
                         [["Z"]])
        check_report(self, data, report)
        # 无故障时旧绑法本身也能织（证明失败只来自应急要求）。
        no_fault = json.loads(json.dumps(data))
        no_fault["patterns"][0]["faults"] = []
        no_fault_report = run_card(no_fault)
        self.assertEqual(no_fault_report["changed_pedals"], 0)
        self.assertEqual({row["id"]: row["shafts"]
                          for row in no_fault_report["tie_up"]},
                         {"X": [1], "Y": [2], "Z": [1, 2]})

    def test_two_patterns_independently_feasible_but_not_shareable(self):
        # A 需要 {1},{2} 交替；B 的相邻两纬都要求 {1}，在 max_pressed=1 与冷却
        # 约束下必须由两条都绑 {1} 的踏板分别承担。两款各自有可用绑法，却没有
        # 一套共用绑法能同时满足。
        data = make(
            [pedal("X", [1], [[1], [2]]), pedal("Y", [1], [[1], [2]])],
            [pattern("A", ["10", "01"]), pattern("B", ["10", "10"])],
            max_pressed=1, rejoin=0)
        self.assertEqual(run_card(data)["status"], "infeasible")
        for pattern_spec, binding in (
                (data["patterns"][0], {"X": 1, "Y": 2}),
                (data["patterns"][1], {"X": 1, "Y": 1})):
            solo = make(
                [{"id": pid, "current": [binding[pid]],
                  "options": [[1], [2]]} for pid in ("X", "Y")],
                [pattern_spec], max_pressed=1, rejoin=0)
            self.assertEqual(run_card(solo)["status"], "ready")


class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_field(self):
        data = base_input()
        data["extra"] = 1
        with self.assertRaises(ValueError):
            run_card(data)

    def test_rejects_bool_integer_positions(self):
        data = base_input()
        data["max_pressed"] = True
        with self.assertRaises(ValueError):
            run_card(data)
        data = make([pedal("A", [True])], [pattern("P", ["1"])])
        with self.assertRaises(ValueError):
            run_card(data)

    def test_rejects_bad_pedal_references(self):
        data = base_input()
        data["patterns"][0]["faults"] = [[0, "Z"]]
        with self.assertRaises(ValueError):
            run_card(data)
        data = base_input()
        data["blocked_pairs"] = [["A", "A"]]
        with self.assertRaises(ValueError):
            run_card(data)
        data = base_input()
        data["blocked_pairs"] = [["E", "F"], ["F", "E"]]
        with self.assertRaises(ValueError):
            run_card(data)

    def test_rejects_duplicate_options_and_current_mismatch(self):
        data = make([{"id": "A", "current": [1], "options": [[1], [1]]}],
                    [pattern("P", ["1"])])
        with self.assertRaises(ValueError):
            run_card(data)
        data = make([{"id": "A", "current": [1], "options": [[2]]}],
                    [pattern("P", ["1"])])
        with self.assertRaises(ValueError):
            run_card(data)

    def test_rejects_duplicate_shafts_and_row_length(self):
        data = make([pedal("A", [1, 2])], [pattern("P", ["1"])], shafts=2,
                    warp=[{"shaft": 1, "yarn": "w"}])
        data["pedals"][0]["options"] = [[1, 1]]
        with self.assertRaises(ValueError):
            run_card(data)
        data = make([{"id": "A", "current": [1],
                      "options": [[1], [1, 1]]}], [pattern("P", ["1"])],
                    shafts=2, warp=[{"shaft": 1, "yarn": "w"}])
        with self.assertRaises(ValueError):
            run_card(data)

    def test_rejects_unweavable_pattern_as_infeasible(self):
        # 两根经端同穿综框但要求相反：有意义却织不出 -> 无可用卡。
        data = make([pedal("A", [1])], [pattern("P", ["10"])],
                    warp=[{"shaft": 1, "yarn": "w"},
                          {"shaft": 1, "yarn": "w"}], shafts=1, max_pressed=1)
        self.assertEqual(run_card(data)["status"], "infeasible")

    def test_input_order_does_not_affect_result(self):
        first = base_input()
        second = json.loads(json.dumps(first))
        second["pedals"].reverse()
        second["patterns"].reverse()
        second["pedals"] = list(reversed(second["pedals"]))
        second["blocked_pairs"] = [["F", "E"]]
        first["pedals"].reverse()
        first["pedals"].reverse()
        self.assertEqual(run_card(first), run_card(second))

    def test_case_sensitive_ids(self):
        data = base_input()
        data["patterns"][0]["id"] = "p"
        report = run_card(data)
        self.assertEqual(report["patterns"][0]["id"], "p")

    def test_unthreaded_shaft_may_lift(self):
        data = make(
            [pedal("A", [1], [[1], [1, 3]]), pedal("B", [2])],
            [pattern("P", ["10", "010"])], shafts=3, max_pressed=1,
            warp=[{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"},
                  {"shaft": 3, "yarn": "w"}])
        # 只有 1、2 号综框有经端；第 3 号未穿综，可以任意升降。
        data["warp"] = [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}]
        data["patterns"][0]["rows"] = ["10", "01"]
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        check_report(self, data, report)


class GlobalChoiceTests(unittest.TestCase):
    def brute_force_card(self, data):
        """独立遍历全部绑综与常态表，按契约分层规则选卡。"""
        pedals = data["pedals"]
        patterns = data["patterns"]
        ends = [end["shaft"] for end in data["warp"]]
        ids = sorted(pedal["id"] for pedal in pedals)
        by_id = {pedal["id"]: pedal for pedal in pedals}
        current = {pedal["id"]: tuple(sorted(pedal["current"])) for pedal in pedals}
        window = 1 + data["rejoin_limit"]
        outcomes = []
        for selection in itertools.product(
                *[pedal["options"] for pedal in pedals]):
            tie_up = {pid: tuple(sorted(option)) for pid, option in zip(ids, selection)}
            changed = sum(1 for pid in ids if tie_up[pid] != current[pid])
            normals = {}
            feasible = True
            for spec in patterns:
                target_rows = [tuple(int(c) for c in text) for text in spec["rows"]]
                rows_allowed = [allowed_picks(data["shafts"], tie_up, ends, target,
                                              data["max_pressed"], data["blocked_pairs"])
                                for target in target_rows]
                best = None
                for normal in itertools.product(*rows_allowed):
                    if not validate_normal(ends, tie_up, data["blocked_pairs"],
                                           target_rows, normal, data["max_pressed"]):
                        continue
                    ok = True
                    for row, fault in spec["faults"]:
                        prev = None
                        found = False
                        for count in range(1, window + 1):
                            resume = normal[(row + count) % len(normal)]
                            previous = normal[(row - 1) % len(normal)]
                            rows_need = [rows_allowed[(row + s) % len(normal)]
                                         for s in range(count)]
                            for candidate in itertools.product(*rows_need):
                                if fault in candidate[0]:
                                    continue
                                prev_pick = previous
                                valid = True
                                for s, pick in enumerate(candidate):
                                    if set(prev_pick) & set(pick):
                                        valid = False
                                        break
                                    if s == count - 1 and set(pick) & set(resume):
                                        valid = False
                                        break
                                    prev_pick = pick
                                if valid:
                                    found = True
                                    break
                            if found:
                                break
                        if not found:
                            ok = False
                            break
                    if ok and (best is None or normal < best):
                        best = normal
                        break
                if best is None:
                    feasible = False
                    break
                normals[spec["id"]] = best
            if not feasible:
                continue
            tie_up_key = tuple(tie_up[pid] for pid in ids)
            normal_key = tuple(tuple(normals[spec["id"]])
                               for spec in sorted(patterns, key=lambda s: s["id"]))
            outcomes.append((changed, tie_up_key, normal_key, tie_up, normals))
        if not outcomes:
            return None
        outcomes.sort(key=lambda item: (item[0], item[1], item[2]))
        return outcomes[0]

    def test_global_choice_matches_bruteforce_on_small_cases(self):
        random.seed(20261122)
        checked = 0
        for _ in range(60):
            shafts = random.choice([2, 2, 3])
            count = random.choice([2, 3])
            ids = [chr(ord("A") + i) for i in range(count)]
            pedals = []
            for pid in ids:
                options = []
                for _ in range(random.choice([1, 1, 2])):
                    size = random.choice([0, 1, 1, 2])
                    option = sorted(random.sample(range(1, shafts + 1),
                                                  min(size, shafts)))
                    if option not in options:
                        options.append(option)
                if not options:
                    options = [[]]
                pedals.append({"id": pid, "current": options[0], "options": options})
            pattern_count = random.choice([1, 2])
            patterns = []
            for index in range(pattern_count):
                rows = []
                for _ in range(random.choice([2, 3])):
                    rows.append("".join(random.choice("01") for _ in range(shafts)))
                faults = []
                if random.random() < 0.6:
                    faults.append([random.randrange(len(rows)),
                                   random.choice(ids)])
                patterns.append({"id": chr(ord("P") + index), "rows": rows,
                                 "faults": faults})
            blocked = []
            if count >= 2 and random.random() < 0.3:
                pair = random.sample(ids, 2)
                blocked.append(pair)
            data = make(pedals, patterns, shafts=shafts,
                        max_pressed=random.choice([1, 2]),
                        blocked=blocked, rejoin=random.choice([0, 1]))
            expected = self.brute_force_card(data)
            report = run_card(data)
            if expected is None:
                self.assertEqual(report["status"], "infeasible", data)
                continue
            self.assertEqual(report["status"], "ready", data)
            changed, tie_up_key, normal_key, tie_up, normals = expected
            self.assertEqual(report["changed_pedals"], changed)
            self.assertEqual({row["id"]: tuple(row["shafts"])
                              for row in report["tie_up"]}, tie_up)
            got = {entry["id"]: [tuple(pick) for pick in entry["picks"]]
                   for entry in report["patterns"]}
            expected_normals = {pid: [tuple(pick) for pick in picks]
                                for pid, picks in normals.items()}
            self.assertEqual(got, expected_normals)
            check_report(self, data, report)
            checked += 1
        self.assertGreater(checked, 5)


class FixtureTests(unittest.TestCase):
    def test_fixture_report(self):
        data = json.loads((FIXTURES / "card_input.json").read_text())
        report = run_card(data)
        self.assertEqual(report["status"], "ready")
        check_report(self, data, report)

    def test_report_replays_through_old_loom_interface(self):
        # 返回的 tie_up 与常态/应急踩法必须能被旧组织编译直接复算。
        from loom_draft.service import run as run_v1
        data = json.loads((FIXTURES / "card_input.json").read_text())
        report = run_card(data)
        tie_up = [{"id": row["id"], "shafts": row["shafts"]}
                  for row in report["tie_up"]]
        for pattern in report["patterns"]:
            source = next(p for p in data["patterns"] if p["id"] == pattern["id"])
            picks = [{"treadles": pick, "yarn": "w"} for pick in pattern["picks"]]
            v1 = run_v1({"shafts": data["shafts"], "tie_up": tie_up,
                         "warp": data["warp"], "picks": picks,
                         "repeat_x": 1, "repeat_y": 1, "warp_spacing_mm": 1,
                         "pick_spacing_mm": 1, "takeup_permille": 0,
                         "warp_waste_mm": 0, "float_limit": 4})
            expected = [[int(c) for c in text] for text in source["rows"]]
            self.assertEqual(v1["drawdown_warp_above"], expected)

    def test_patterns_sorted_and_picks_sorted(self):
        data = base_input()
        data["patterns"].append({"id": "Aa", "rows": ["10", "01"],
                                 "faults": []})
        report = run_card(data)
        ids = [entry["id"] for entry in report["patterns"]]
        self.assertEqual(ids, sorted(ids))
        for entry in report["patterns"]:
            for pick in entry["picks"]:
                self.assertEqual(pick, sorted(pick))
            for item in entry["fault_playbook"]:
                for pick in item["picks"]:
                    self.assertEqual(pick, sorted(pick))
            self.assertEqual([(i["row"], i["pedal_id"])
                              for i in entry["fault_playbook"]],
                             sorted((i["row"], i["pedal_id"])
                                    for i in entry["fault_playbook"]))


if __name__ == "__main__":
    unittest.main()
