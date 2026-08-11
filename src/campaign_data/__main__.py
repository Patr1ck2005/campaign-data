"""CLI entry point for csv_processor.

Usage:
    python -m campaign_data audit <dir> [--path-dims m1,m2] [--dup-freq-tolerance 1e-4]
    python -m campaign_data merge <dir> [--split-by "t_tot (nm)"] [--out PATH]
                                          [--path-dims m1,m2] [--dup-freq-tolerance 1e-4]

The audit subcommand prints a markdown report of file fingerprints, campaigns,
coverage matrix, overlaps, and conflicts.  Use it as the first step when
encountering a messy data directory.

The merge subcommand runs smart_merge (with optional split_by) and writes
the output.  Pass the same flags as audit for path_dims/dup_freq_tolerance.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parse_path_dims(s: str | None) -> list:
    """Parse --path-dims "m1,m2" into [("m1","m2")].  Multiple groups: "m1,m2;a,b"."""
    if not s:
        return []
    groups = []
    for group_str in s.split(";"):
        names = [n.strip() for n in group_str.split(",") if n.strip()]
        if len(names) >= 2:
            groups.append(tuple(names))
    return groups


def _cmd_audit(args: argparse.Namespace) -> int:
    from campaign_data.audit import audit_directory, format_audit_report

    report = audit_directory(
        args.source,
        path_dims=_parse_path_dims(args.path_dims),
        use_cache=not args.no_cache,
        dup_freq_tolerance=args.dup_freq_tolerance,
        recursive=args.recursive,
    )
    print(format_audit_report(report, max_files_per_campaign=args.max_files))
    return 0 if report.file_count > 0 else 1


def _cmd_merge(args: argparse.Namespace) -> int:
    from campaign_data.merge import smart_merge

    result = smart_merge(
        [args.source] if isinstance(args.source, str) else args.source,
        out_path=Path(args.out) if args.out and not args.split_by else None,
        split_by=[args.split_by] if args.split_by else None,
        split_out_dir=Path(args.out) if args.out and args.split_by else None,
        path_dims=_parse_path_dims(args.path_dims),
        use_cache=not args.no_cache,
        dup_freq_tolerance=args.dup_freq_tolerance,
        recursive=args.recursive,
        clean_floats=not args.no_clean_floats,
    )
    if result is None:
        print("Merge failed (check sources).", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m campaign_data",
        description="Analyse and merge messy campaign-data directories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- audit ----
    p_audit = sub.add_parser(
        "audit",
        help="Print a structured report of file fingerprints, campaigns, "
             "coverage, overlaps, and conflicts.",
    )
    p_audit.add_argument("source", help="Directory or file path to audit.")
    p_audit.add_argument("--path-dims", default=None,
                         help='Path-axis column groups, e.g. "m1,m2". '
                              "Multiple groups: \"m1,m2;a,b\".")
    p_audit.add_argument("--dup-freq-tolerance", type=float, default=None,
                         help="Intra-point block-duplicate tolerance "
                              "(default 1e-6; adjust for export noise).")
    p_audit.add_argument("--recursive", action="store_true",
                         help="Scan subdirectories.")
    p_audit.add_argument("--no-cache", action="store_true",
                         help="Disable per-file GridReport disk cache.")
    p_audit.add_argument("--max-files", type=int, default=10,
                         help="Max files to list per campaign (default 10).")
    p_audit.set_defaults(func=_cmd_audit)

    # ---- merge ----
    p_merge = sub.add_parser(
        "merge",
        help="Run smart_merge (with optional split_by) and write output.",
    )
    p_merge.add_argument("source", help="Directory or file path to merge.")
    p_merge.add_argument("--out", default=None,
                         help="Output file (merge mode) or directory (split mode).")
    p_merge.add_argument("--split-by", default=None,
                         help='Column name to split by, e.g. "t_tot (nm)".')
    p_merge.add_argument("--path-dims", default=None,
                         help='Path-axis column groups, e.g. "m1,m2".')
    p_merge.add_argument("--dup-freq-tolerance", type=float, default=None,
                         help="Intra-point block-duplicate tolerance.")
    p_merge.add_argument("--recursive", action="store_true",
                         help="Scan subdirectories.")
    p_merge.add_argument("--no-cache", action="store_true",
                         help="Disable per-file GridReport disk cache.")
    p_merge.add_argument("--no-clean-floats", action="store_true",
                         help="Disable float noise cleaning in output.")
    p_merge.set_defaults(func=_cmd_merge)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
