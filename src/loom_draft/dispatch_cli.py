"""Command line interface for the local dispatch ledger (loom-dispatch).

Two mutually exclusive modes:

* ``loom-dispatch --store DIR --source COMMAND.json`` prints a full JSON receipt;
* ``loom-dispatch --store DIR --view`` prints the full ledger view.

Business rejections still exit 0; input errors, request-id conflicts and
storage/I/O failures exit 2 with a concise message instead of a traceback.
"""
import argparse
import json

from . import dispatch
from .io import load


def main():
    parser = argparse.ArgumentParser(description="实际裁片发货台账与召回交接")
    parser.add_argument("--store", required=True, help="台账独占的本地目录")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source", help="严格命令 JSON 文件（最多 8MB）")
    mode.add_argument("--view", action="store_true", help="输出完整台账视图")
    args = parser.parse_args()
    try:
        if args.view:
            result = dispatch.read_store(args.store)
        else:
            command = load(args.source)
            result = dispatch.apply(args.store, command)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"loom-dispatch: {exc}\n")


if __name__ == "__main__":
    main()
