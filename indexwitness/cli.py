"""Command line entry point. Never overwrite an existing output file."""

import argparse
from pathlib import Path
import sys

from . import __version__
from .engine import AnalysisError, run_analysis
from .report import render
from .workload import WorkloadError, load_workload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Experiment with SQLite index removals on isolated memory snapshots.")
    parser.add_argument("--version", action="version", version=f"indexwitness {__version__}")
    parser.add_argument("database", help="existing SQLite database, opened read-only")
    parser.add_argument("workload", help="version 1 JSON workload")
    parser.add_argument("--format", choices=("text", "json", "markdown", "html"), default="text")
    parser.add_argument("--output", type=Path, help="new UTF-8 output file (existing paths are refused)")
    parser.add_argument("--max-indexes", type=int, default=8, help="max candidate indexes, at most 12 (default 8)")
    parser.add_argument("--max-callbacks", "--max-vm-steps", dest="max_callbacks", type=int, default=2000000, help="per-query progress callback cap; approximate work proxy, not exact VM steps")
    parser.add_argument("--max-rows", type=int, default=100000, help="per-query result row cap")
    parser.add_argument("--timeout", type=float, default=30, help="overall experiment deadline in seconds")
    parser.add_argument("--fail-on-removal", action="store_true", help="exit 1 when a tested removal is suggested")
    args = parser.parse_args(argv)
    output = args.output
    try:
        if output is not None:
            target = output.resolve()
            database = Path(args.database).expanduser().resolve()
            protected = {database, Path(args.workload).resolve()}
            protected.update(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))
            if target in protected or output.exists() or output.is_symlink():
                raise ValueError("output must be a new path distinct from inputs and SQLite sidecar paths")
            if not output.parent.is_dir():
                raise ValueError("output parent directory does not exist")
        workload = load_workload(args.workload)
        report = run_analysis(args.database, workload, max_indexes=args.max_indexes,
                              max_vm_steps=args.max_callbacks, max_rows=args.max_rows, timeout=args.timeout)
        content = render(report, args.format)
        if output is None:
            sys.stdout.write(content)
        else:
            # O_EXCL via x protects against races and symlink replacement after validation.
            with output.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        return 1 if args.fail_on_removal and report["recommendation"]["removed"] else 0
    except (AnalysisError, WorkloadError, OSError, ValueError) as exc:
        print(f"indexwitness: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("indexwitness: interrupted; no recommendation produced", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
