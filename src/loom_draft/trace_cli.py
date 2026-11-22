import argparse
import json
from .io import load, publish
from .trace import run_trace


def main():
    parser = argparse.ArgumentParser(description="织机纱线批次追溯与历史放行复核")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run_trace(load(args.source))
        publish(args.output, result)
        print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-trace: {exc}\n")


if __name__ == "__main__":
    main()
