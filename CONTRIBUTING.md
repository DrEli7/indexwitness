# Contributing

Run `python -m unittest discover -s tests -v` with Python 3.10 or newer.
There are no runtime or test dependencies beyond the standard library.
The GitHub Actions matrix covers Linux, macOS and Windows with Python 3.10,
3.12 and 3.13. A declared matrix is not a claim that a run has passed.

Please include a minimal synthetic SQLite fixture for planner or result bugs.
Never upload a private database or bound application parameters. Include your
Python and SQLite versions and the workload's comparison contract.

Preserve source read-only behavior, UNIQUE-index protection, fail-closed query
authorization, fresh-snapshot experiments, and bounded complete enumeration.
The callback metric must not be described as exact VM instructions or latency.
Changing the SQLite build can legitimately change plans and callback counts;
tests should assert meaningful relative behavior rather than fixed counts.
