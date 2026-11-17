"""Command line: python -m patchgate {validate,compile,compile-all}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from patchgate.catalog import Catalog, CatalogError, encode_plan

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "catalog"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patchgate")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="Validate every catalog document")
    compile_one = sub.add_parser("compile", help="Print the install plan for a profile")
    compile_one.add_argument("profile")
    compile_all = sub.add_parser("compile-all", help="Write every plan to a JSON file")
    compile_all.add_argument("--out", type=Path, required=True)
    compile_all.add_argument(
        "--encoded",
        action="store_true",
        help="Write {profile: {platform, digest, plan}} with transport-encoded plans",
    )
    args = parser.parse_args(argv)

    try:
        catalog = Catalog.load(args.catalog)
        if args.command == "validate":
            print(
                f"catalog OK: {len(catalog.agents)} agents, {len(catalog.controls)} controls, "
                f"{len(catalog.baselines)} baselines, {len(catalog.appsets)} app sets, "
                f"{len(catalog.profiles)} profiles"
            )
        elif args.command == "compile":
            print(json.dumps(catalog.compile(args.profile), indent=2))
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            plans = catalog.compile_all()
            output: dict[str, object] = (
                {
                    name: {
                        "platform": plan["platform"],
                        "digest": plan["digest"],
                        "plan": encode_plan(plan),
                    }
                    for name, plan in plans.items()
                }
                if args.encoded
                else dict(plans)
            )
            args.out.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
            print(f"wrote {len(catalog.profiles)} plans to {args.out}")
    except CatalogError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
