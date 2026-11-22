#!/usr/bin/env python3
"""生成验布机读数对齐大场景验收输入（确定性合成数据，loom-inspect）。

用法（在仓库根目录）：

    python3 examples/inspection_acceptance.py run/big_input.json
    .venv/bin/loom-inspect --source run/big_input.json --output run/big_report.json

场景（不依赖网络、机台或数据库）：

* 批次：128 纬交替平纹周期 repeat_y=1000、横向 repeat_x=1000，
  计划共 128000 条实际纬、2000 根实际经端，库存恰好够用；
* 扫描共 120000 帧：首帧固定纬 0；下标 i=1..119998 的帧窗口 [i, i+511]、
  步长 1..2；末帧固定纬 120510、步长 1..2；探针全为问号、无缺陷；
* 裁区：ALL 为整幅 [0,2000) × 纬 [0,120511)，LAST 只查纬 120510。

任一完整对齐在首尾之间总共跳过 511 纬；每一条内部纬都能在某条完整对齐中
被跳过，因此 ALL 确定验过 2 纬、最早未确认纬为 1，应判 recheck；LAST 的
纬 120510 被每条路线拍到，应判 clear。预期 summary：

    {"clear": 1, "cuts": 2, "damaged": 0, "recheck": 1}
"""
import json
import sys


def build():
    picks = [{"treadles": ["T1" if i % 2 == 0 else "T2"], "yarn": "w"}
             for i in range(128)]
    batch = {
        "common": {
            "shafts": 2,
            "tie_up": [{"id": "T1", "shafts": [1]}, {"id": "T2", "shafts": [2]}],
            "warp": [{"shaft": 1, "yarn": "w"}, {"shaft": 2, "yarn": "w"}],
            "repeat_x": 1000, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
            "takeup_permille": 0, "warp_waste_mm": 0, "float_limit": 1},
        "jobs": [{"id": "BIG", "value": 1, "repeat_y": 1000, "phases": [0],
                  "picks": picks}],
        "bridges": [],
        "stock": [{"yarn": "w", "length_mm": 512000000}],
        "max_length_mm": 128000}
    frames = [[0, 0, 0, 0, "?", None]]
    frames += [[i, i + 511, 1, 2, "?", None] for i in range(1, 119999)]
    frames.append([120510, 120510, 1, 2, "?", None])
    return {"batch": batch, "probe_ends": [0], "frames": frames,
            "cuts": [
                {"id": "ALL", "end_start": 0, "end_stop": 2000,
                 "pick_start": 0, "pick_stop": 120511},
                {"id": "LAST", "end_start": 0, "end_stop": 2000,
                 "pick_start": 120510, "pick_stop": 120511}]}


def main():
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} OUTPUT.json")
    with open(sys.argv[1], "w", encoding="utf-8") as target:
        json.dump(build(), target, ensure_ascii=False)
        target.write("\n")
    print(f"wrote {sys.argv[1]}")


if __name__ == "__main__":
    main()
