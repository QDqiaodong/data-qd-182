import argparse
import json
from .io import load, publish
from .batch import run_plan


def main():
    parser = argparse.ArgumentParser(description="织机多工单连续织造计划")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run_plan(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-batch: {exc}\n")


if __name__ == "__main__":
    main()
