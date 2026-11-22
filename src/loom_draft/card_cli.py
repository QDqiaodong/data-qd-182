import argparse
import json

from .card import run_card
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(description="共用绑综的连续织造工艺卡")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run_card(load(args.source))
        publish(args.output, result)
        print(json.dumps({"status": result["status"],
                          "changed_pedals": result["changed_pedals"],
                          "patterns": len(result["patterns"])},
                         ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-card: {exc}\n")


if __name__ == "__main__":
    main()
