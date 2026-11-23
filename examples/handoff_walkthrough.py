#!/usr/bin/env python3
"""可运行的多班交班样例（loom-handoff）。

一卷 2 经端 × 4 纬的平纹（纬纱依次 F、G、F、G，每纬耗 3mm），实物纱筒
F1、F2 各 6mm、G1 9mm，演示拆纬重织后的耗纱交接：

  1. 首班挂 F1、G1，织三纬，签封一纬，把 checkpoint 交给下一班；
  2. 第二班（模拟新进程，只带 checkpoint 与固定 batch/spools）拆两纬：
     废耗记在原织入班次 1 的 F1、G1 上，续织边界停在纬 2（F1 余量为零）；
  3. 第三班改挂 F2，织三纬并签封三纬，核对各筒最终平衡；
  4. 变体：首班纬 2 改由 F2 织入、结束前换回 F1，第二班输入操作不变，
     这次 F 的废耗归 F2，且第二班可一次续织到卷尾。

运行：PYTHONPATH=src python3 examples/handoff_walkthrough.py
"""
import json

from loom_draft.handoff import run_handoff


def batch():
    return {
        "common": {
            "shafts": 2,
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


SPOOLS = [{"id": "F1", "yarn": "F", "length_mm": 6},
          {"id": "F2", "yarn": "F", "length_mm": 6},
          {"id": "G1", "yarn": "G", "length_mm": 9}]


def handoff(checkpoint, no, events):
    """一班的完整交接：checkpoint 经紧凑 JSON 序列化再解析，模拟新进程接手。"""
    carried = None if checkpoint is None else json.loads(json.dumps(
        checkpoint, ensure_ascii=False, separators=(",", ":")))
    return run_handoff({"batch": batch(), "spools": SPOOLS, "checkpoint": carried,
                        "shift": {"no": no, "events": events}})


def show(title, report):
    print(f"\n=== {title} ===")
    print(json.dumps({k: report[k] for k in
                      ("sealed", "next_pick", "total_picks",
                       "scrap_this_shift", "resume", "spools")},
                     ensure_ascii=False, indent=2))


def main():
    first = handoff(None, 1, [{"op": "mount", "spool_id": "F1"},
                              {"op": "mount", "spool_id": "G1"},
                              {"op": "weave", "count": 3},
                              {"op": "seal", "count": 1}])
    show("1. 首班：挂 F1/G1，织 3 纬，签封 1 纬", first)

    second = handoff(first["checkpoint"], 2, [{"op": "unpick", "count": 2}])
    show("2. 第二班：拆 2 纬（废耗记在原织入班次 1）", second)
    assert second["scrap_this_shift"] == [
        {"woven_shift": 1, "spool_id": "F1", "mm": 3},
        {"woven_shift": 1, "spool_id": "G1", "mm": 3}]
    assert second["resume"] == {"count": 1, "stop_pick": 2,
                                "reason": "insufficient",
                                "yarn": "F", "spool_id": "F1"}

    third = handoff(second["checkpoint"], 3, [{"op": "mount", "spool_id": "F2"},
                                              {"op": "weave", "count": 3},
                                              {"op": "seal", "count": 3}])
    show("3. 第三班：改挂 F2，织 3 纬并签封 3 纬", third)
    balance = {row["spool_id"]: (row["sealed_mm"], row["scrap_mm"],
                                 row["remaining_mm"], row["tail_mm"])
               for row in third["spools"]}
    assert balance == {"F1": (3, 3, 0, 0), "F2": (3, 0, 3, 0),
                       "G1": (6, 3, 0, 0)}, balance

    alt_first = handoff(None, 1, [{"op": "mount", "spool_id": "F1"},
                                  {"op": "mount", "spool_id": "G1"},
                                  {"op": "weave", "count": 2},
                                  {"op": "mount", "spool_id": "F2"},
                                  {"op": "weave", "count": 1},
                                  {"op": "mount", "spool_id": "F1"},
                                  {"op": "seal", "count": 1}])
    alt_second = handoff(alt_first["checkpoint"], 2, [{"op": "unpick", "count": 2}])
    show("4. 变体第二班：纬 2 实织为 F2，同样的拆 2 纬", alt_second)
    assert alt_second["scrap_this_shift"] == [
        {"woven_shift": 1, "spool_id": "F2", "mm": 3},
        {"woven_shift": 1, "spool_id": "G1", "mm": 3}]
    assert alt_second["resume"]["count"] == 3
    assert alt_second["resume"]["reason"] == "plan_end"
    print("\n全部断言通过：废耗按实际织入的（纱筒, 班号）归属，与计划纬号、"
          "最后挂筒、拆后库存无关。")


if __name__ == "__main__":
    main()
