#!/usr/bin/env python3
"""可运行的合成台账操作样例（loom-dispatch）。

在一个全新的临时台账目录上演示完整生命周期，全部使用标准库、无网络：

  1. init 一卷 2 经端 × 4 纬的布（W/F 两个原料批次，归账与证书完整）；
  2. dispatch 裁片 S、R，冻结当次 trace 放行凭据；
  3. ingest 一条新的独立 fail 证据，两块已发裁片同时进入 recall，产生连续事件；
  4. 用新 request_id 重发同面积裁片，即使 cut_id 不同也被 cloth_overlap 拒绝；
  5. ingest 撤回 fail 的新版本，两块裁片恢复 normal 并产生 clear 事件，
     旧 recall 事件仍保持 pending；
  6. ack 批量确认全部通知，再用另一条独立 fail 产生新的 recall 事件；
  7. read_store 输出完整台账视图。

运行：PYTHONPATH=src python3 examples/dispatch_walkthrough.py
"""
import copy
import json
import shutil
import tempfile
from pathlib import Path

from loom_draft import dispatch


def build_source(as_of=3):
    batch = {
        "common": {
            "shafts": 2,
            "tie_up": [{"id": "T1", "shafts": [1]},
                       {"id": "T2", "shafts": [2]}],
            "warp": [{"shaft": 1, "yarn": "w"},
                     {"shaft": 2, "yarn": "w"}],
            "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
            "takeup_permille": 100, "warp_waste_mm": 1, "float_limit": 3},
        "jobs": [{"id": "A", "value": 1, "repeat_y": 1, "phases": [0], "picks": [
            {"treadles": ["T1"], "yarn": "f"},
            {"treadles": ["T2"], "yarn": "f"},
            {"treadles": ["T1"], "yarn": "f"},
            {"treadles": ["T2"], "yarn": "f"}]}],
        "bridges": [],
        "stock": [{"yarn": "w", "length_mm": 10**6},
                  {"yarn": "f", "length_mm": 10**6}],
        "max_length_mm": 10**6}
    return {
        "batch": batch,
        "lots": [{"id": "W", "yarn": "w", "capacity_mm": 12},
                 {"id": "F", "yarn": "f", "capacity_mm": 12}],
        "allocations": [
            {"id": "aw0", "revision": 1, "known_at": 1, "payload": {
                "orientation": "warp", "base_end": 0, "repeat_start": 0,
                "repeat_stop": 1, "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "aw1", "revision": 1, "known_at": 1, "payload": {
                "orientation": "warp", "base_end": 1, "repeat_start": 0,
                "repeat_stop": 1, "pick_start": 0, "pick_stop": 4, "lot_id": "W"}},
            {"id": "wf", "revision": 1, "known_at": 1, "payload": {
                "orientation": "weft", "pick_start": 0, "pick_stop": 4,
                "lot_id": "F"}}],
        "certificates": [
            {"id": "cw", "revision": 1, "known_at": 1,
             "payload": {"lot_id": "W", "pick_start": 0, "pick_stop": 4,
                         "verdict": "pass"}},
            {"id": "cf", "revision": 1, "known_at": 1,
             "payload": {"lot_id": "F", "pick_start": 0, "pick_stop": 4,
                         "verdict": "pass"}}],
        "shipments": [],
        "as_of": as_of}


CUT_S = {"cut_id": "S", "end_start": 0, "end_stop": 1,
         "pick_start": 0, "pick_stop": 2}
CUT_R = {"cut_id": "R", "end_start": 1, "end_stop": 2,
         "pick_start": 0, "pick_stop": 2}


def show(title, receipt):
    print(f"\n=== {title} ===")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def main():
    store = tempfile.mkdtemp(prefix="loom-dispatch-sample-")
    print(f"台账目录（独占）：{store}")
    try:
        source1 = build_source(3)

        show("1. init", dispatch.apply(
            store, {"op": "init", "request_id": "req-init", "source": source1}))

        show("2. dispatch S+R", dispatch.apply(
            store, {"op": "dispatch", "request_id": "D1",
                    "expected_generation": 1, "cuts": [CUT_S, CUT_R]}))

        source2 = copy.deepcopy(source1)
        source2["certificates"].append({
            "id": "ff", "revision": 1, "known_at": 5,
            "payload": {"lot_id": "F", "pick_start": 1, "pick_stop": 2,
                        "verdict": "fail"}})
        source2["as_of"] = 6
        show("3. ingest fail -> recall 事件", dispatch.apply(
            store, {"op": "ingest", "request_id": "ing-fail",
                    "expected_generation": 1, "source": source2}))

        show("4. 同面积换 cut_id 重发 -> cloth_overlap", dispatch.apply(
            store, {"op": "dispatch", "request_id": "D2",
                    "expected_generation": 2,
                    "cuts": [{**CUT_S, "cut_id": "S2"}]}))

        source3 = copy.deepcopy(source2)
        source3["certificates"].append({
            "id": "ff", "revision": 2, "known_at": 7, "payload": None})
        source3["as_of"] = 8
        show("5. ingest 撤回 fail -> clear 事件", dispatch.apply(
            store, {"op": "ingest", "request_id": "ing-clear",
                    "expected_generation": 2, "source": source3}))

        show("6a. 含未知 event_id 的确认 -> 整批拒绝", dispatch.apply(
            store, {"op": "ack", "request_id": "ack-bad",
                    "event_ids": [1, 2, 99]}))
        show("6b. 确认全部 4 个事件", dispatch.apply(
            store, {"op": "ack", "request_id": "ack-all",
                    "event_ids": [1, 2, 3, 4]}))

        source4 = copy.deepcopy(source3)
        source4["certificates"].append({
            "id": "ff2", "revision": 1, "known_at": 9,
            "payload": {"lot_id": "F", "pick_start": 0, "pick_stop": 1,
                        "verdict": "fail"}})
        source4["as_of"] = 10
        show("7. 新独立 fail -> 新 recall 事件", dispatch.apply(
            store, {"op": "ingest", "request_id": "ing-fail2",
                    "expected_generation": 3, "source": source4}))

        print("\n=== 8. read_store 完整视图（摘要）===")
        view = dispatch.read_store(store)
        print(json.dumps({
            "generation": view["generation"],
            "as_of": view["as_of"],
            "states": {d["cut_id"]: d["state"] for d in view["deliveries"]},
            "events": [(e["event_id"], e["kind"], e["cut_id"],
                        e["acknowledged"]) for e in view["events"]],
            "pending_event_ids": view["pending_event_ids"],
            "summary": view["summary"]}, ensure_ascii=False, indent=2))

        # 重放 D1：即使现在已 recall，仍取回首次成功回执。
        replay = dispatch.apply(
            store, {"op": "dispatch", "request_id": "D1",
                    "expected_generation": 1, "cuts": [CUT_S, CUT_R]})
        print("\n=== 9. D1 重放仍为首次成功回执 ===")
        print(json.dumps({"status": replay["status"],
                          "generation": replay["generation"],
                          "cut_ids": [d["cut_id"]
                                      for d in replay["result"]["deliveries"]]},
                         ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(store, ignore_errors=True)


if __name__ == "__main__":
    main()
