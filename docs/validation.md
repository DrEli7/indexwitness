# Local validation

The source was checked on Windows with two installed runtimes on 2026-10-17:

| Python | Runtime SQLite | Standard-library test suite |
|---|---|---|
| 3.13.9 | 3.51.0 | 27 passed |
| 3.12.14 | 3.53.1 | 27 passed |

Run the same suite with:

```sh
python -W error::ResourceWarning -m unittest discover -s tests -v
```

The tests include source byte/schema/statistics preservation, committed WAL
data, protected UNIQUE indexes, substitute indexes with jointly harmful
removal, typed bag/ordered fingerprints, query authorization, resource budgets,
Unicode and JSON validation, report escaping, and refusing to create output
over inputs or reserved SQLite sidecars.

The synthetic demo evaluates 16 subsets of four indexes. On SQLite 3.51.0,
the two customer indexes were interchangeable individually, while removing
both caused a roughly 405x callback-work ratio for the customer lookup. This
is an instrumented work-proxy observation on synthetic data, not a latency
benchmark or a claim about other SQLite builds.

An isolated installation of the wheel was exercised against the demo and
verified to return a complete 16-configuration report and a joint-removal
witness. The wheel has no runtime dependencies.

The GitHub Actions files declare a Linux/macOS/Windows matrix with Python
3.10, 3.12 and 3.13 and a tag-triggered release build. This document records
local checks; it does not claim that remote CI or Python 3.10 has run.
