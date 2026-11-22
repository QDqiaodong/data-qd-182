import argparse
import json
from .io import load, publish
from .inspection import run_inspection


def main():
    parser = argparse.ArgumentParser(description="验布机读数对齐与复验清单")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run_inspection(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-inspect: {exc}\n")


if __name__ == "__main__":
    main()
