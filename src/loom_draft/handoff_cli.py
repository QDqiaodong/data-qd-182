import argparse
import json
from .io import load, publish
from .handoff import run_handoff


def main():
    parser = argparse.ArgumentParser(description="拆纬重织后的耗纱交班单")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run_handoff(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-handoff: {exc}\n")


if __name__ == "__main__":
    main()
