"""共用绑综工艺卡的可运行合成示例（不连接织机、不需要网络）。

运行：``PYTHONPATH=src python3 examples/card_walkthrough.py``
"""
import json

from loom_draft.card import run_card


def base_input():
    """契约中的关键对照：两综、两经端依次穿 1、2。"""
    return {
        "shafts": 2,
        "warp": [{"shaft": 1, "yarn": "warp-a"},
                 {"shaft": 2, "yarn": "warp-b"}],
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


def show(title, data):
    report = run_card(data)
    print(f"== {title}")
    print(f"   status={report['status']} changed_pedals={report['changed_pedals']}")
    if report["status"] == "ready":
        print(f"   tie_up={json.dumps(report['tie_up'], ensure_ascii=False)}")
        for pattern in report["patterns"]:
            print(f"   {pattern['id']} 常态={pattern['picks']}")
            for item in pattern["fault_playbook"]:
                print(f"     故障 row={item['row']} pedal={item['pedal_id']} "
                      f"应急={item['picks']} resume_row={item['resume_row']}")
    print()


if __name__ == "__main__":
    show("rejoin_limit=1：常态 [A],[B],[C]，应急 [C,E],[F]", base_input())

    relaxed = base_input()
    relaxed["rejoin_limit"] = 0
    show("rejoin_limit=0：常态改为 [A],[B],[F]，应急只踩一拍 [C,E]",
         relaxed)

    reduced = base_input()
    reduced["pedals"] = [p for p in reduced["pedals"] if p["id"] != "F"]
    reduced["blocked_pairs"] = []
    show("删去踏板 F 与含 F 的禁配对：应 infeasible", reduced)

    no_fault = json.loads(json.dumps(reduced))
    no_fault["patterns"][0]["faults"] = []
    show("同一缩减机台在无故障要求时仍能正常织", no_fault)
