import argparse
import json
import sys
from pathlib import Path

from .engine import compare
from .render import Renderer
from .report import markdown


def main():
    parser = argparse.ArgumentParser(description="Compare two rendered revision directories")
    parser.add_argument("base", type=Path)
    parser.add_argument("head", type=Path)
    parser.add_argument(
        "--native", action="store_true", help="use pinned local binaries for trusted input only"
    )
    parser.add_argument("--artifact", type=Path, help="write the versioned CI envelope")
    args = parser.parse_args()
    try:
        roots = [p.resolve(strict=True) for p in (args.base, args.head)]
        if any(not p.is_dir() for p in roots):
            raise ValueError
        report = compare(*roots, Renderer(not args.native))
        if args.artifact:
            from .publish import context

            args.artifact.write_text(
                json.dumps({"version": 1, "context": context(), "report": report}, allow_nan=False)
            )
        else:
            print(markdown(report))
        return int(bool(report["errors"]))
    except Exception:
        print(
            "Manifest comparison could not complete. Check revisions, input limits and workflow context.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
