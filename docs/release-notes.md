IndexWitness is a SQLite index retirement experiment tool. It tests every
bounded retained-index subset on fresh in-memory database copies, compares
typed result fingerprints, enforces per-query work budgets, and reports
joint-removal traps and Pareto alternatives.

The source database is opened read-only. All UNIQUE and constraint indexes
remain pinned. Workload execution uses only Python's standard library.

Download the wheel and install with `python -m pip install ./indexwitness-0.1.0-py3-none-any.whl`,
or extract the source archive and run `python -m indexwitness --help`.
See the README, Chinese README and methodology for the synthetic demo and
the limits of workload-specific evidence.

The reported progress callbacks are an approximate SQLite work proxy,
including preparation, not elapsed time or exact VM instructions.
No npm/PyPI registry publication or production performance claim is implied.
