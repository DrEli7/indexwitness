# IndexWitness

**Which SQLite indexes can you remove together without changing your workload's results or exceeding its work budget?**

IndexWitness answers that question with counterfactual experiments on an in-memory copy of your database. It executes every bounded combination of existing candidate indexes, compares result fingerprints, and produces a Pareto frontier of retained index count versus measured query work.

Python 3.10+. Standard library only. No database server, account, model, or runtime dependency.

[中文说明](README.zh-CN.md) · [Methodology and limits](docs/methodology.md) · [MIT license](LICENSE)

## The problem: individually removable does not mean jointly removable

An application accumulates indexes through years of ORM migrations. Two indexes may cover the same query. Remove either one and the other takes over; remove both and the query falls back to a table scan. A list of individually unused or removable indexes can miss this dependency.

IndexWitness evaluates the combinations. A report can establish that keeping either `orders_customer` or `orders_customer_total` fits your workload, while deleting both exceeds a query's budget. It also preserves evidence for configurations that change results, fail, or exceed execution limits.

Use it when reviewing an index cleanup, trimming an embedded application's schema, or checking the cost of a proposed SQLite migration. Its conclusion is specific to the snapshot, queries, parameters, SQLite runtime, and limits you supplied.

## Try the demo

Run from a checkout; installation is optional:

```sh
git clone https://github.com/DrEli7/indexwitness.git
cd indexwitness
python examples/make_demo.py --directory .local/demo
python -m indexwitness .local/demo/shop.db .local/demo/workload.json
```

Create a standalone HTML report:

```sh
python -m indexwitness .local/demo/shop.db .local/demo/workload.json --format html --output .local/demo/report.html
```

Open `.local/demo/report.html` in a browser. The report is generated locally and does not need a service. Choose a new output filename when repeating a run: IndexWitness refuses to overwrite an existing file and reserves the source database's WAL, SHM and journal paths. A generated [synthetic example report](docs/demo-report.html) is included in the checkout.

The fixture has four candidate indexes, so the experiment attempts 16 configurations. Two are interchangeable customer indexes, `idx_customer_a` and `idx_customer_b`: inspect the joint-removal witness to see why two individually removable indexes cannot automatically be retired together. Measured counts and the chosen set depend on your SQLite runtime.

To install the command from the checkout:

```sh
python -m pip install .
indexwitness .local/demo/shop.db .local/demo/workload.json
```

Runtime execution uses Python's `sqlite3` module. The optional packaging step uses setuptools.

## Describe your workload

Save a UTF-8 JSON file such as `workload.json`:

```json
{
  "version": 1,
  "candidate_indexes": ["orders_customer", "orders_customer_total", "orders_status"],
  "queries": [
    {
      "name": "customer-history",
      "sql": "SELECT id, total FROM orders WHERE customer_id = ? ORDER BY id",
      "params": [42],
      "compare": "ordered",
      "weight": 10,
      "max_step_ratio": 1.25
    },
    {
      "name": "open-order-total",
      "sql": "SELECT sum(total) FROM orders WHERE status = :status",
      "params": {"status": "open"},
      "compare": "bag",
      "weight": 1,
      "max_step_ratio": 1.1
    }
  ]
}
```

The table and indexes in this example must already exist in your database. IndexWitness does not create candidate indexes or rewrite queries.

| Field | Meaning | Default |
|---|---|---|
| `version` | Workload format version; must be integer `1` | Required |
| `queries` | One to 64 named query cases | Required |
| `candidate_indexes` | Explicit nonunique indexes to consider removing; all other indexes remain pinned | All eligible nonunique explicit indexes |
| `name` | Unique case name, at most 128 characters | Required |
| `sql` | One read-only statement, at most 65,536 characters | Required |
| `params` | Positional JSON array for `?`, or object for named parameters | `[]` |
| `compare` | `bag` compares rows with duplicate multiplicity; `ordered` also compares sequence | `"bag"` |
| `weight` | Positive weight for the aggregate work metric | `1` |
| `max_step_ratio` | Maximum candidate-to-baseline work ratio for this case; must be at least `1` | `1.25` |

Each parameter set is a separate query case. Give the same SQL several names and parameter sets to cover hot customers, empty matches, common values, and rare values. Weights express workload frequency or importance; they never let a cheap query hide another query's budget violation.

Use `ordered` when output order is part of the application contract and your query has a total `ORDER BY`, such as a unique ID as the last tie breaker. Unordered `LIMIT`, ties in `ORDER BY`, and bare columns in aggregate queries can legitimately return different rows under a different plan. A result mismatch is evidence to review the query's assumptions, rather than proof of a SQLite bug. See [result comparison](docs/methodology.md#result-comparison).

Input is bounded to 4 MiB. Unknown fields and duplicate JSON keys are rejected to catch misspelled options. Parameter values must be JSON scalars representable by SQLite; bind arrays or objects are not expanded into SQL.

## Run an experiment

```sh
python -m indexwitness app.db workload.json --format markdown --output index-review.md
python -m indexwitness app.db workload.json --format json --output index-review.json
python -m indexwitness app.db workload.json --max-indexes 6 --max-callbacks 500000 --max-rows 20000 --timeout 15
```

| Option | Default | Purpose |
|---|---|---|
| `--format` | `text` | `text`, `json`, `markdown`, or standalone `html` |
| `--output` | Standard output | Write to a new file; existing paths and input collisions are refused |
| `--max-indexes` | `8` | Candidate count limit from 0 to 12; the search evaluates `2^n` subsets |
| `--max-callbacks` | `2000000` | Maximum progress callback count per query execution |
| `--max-rows` | `100000` | Maximum result rows per query execution |
| `--timeout` | `30` | Overall experiment deadline in seconds, including copying and all configurations |
| `--fail-on-removal` | Off | Return exit code `1` when a complete search finds a feasible removal |

The `--max-callbacks` budget counts `progress_callbacks` with SQLite's progress interval set to one. SQLite documents this interface as approximate and may invoke it during preparation. It is not an exact `SQLITE_STMTSTATUS_VM_STEP` count, a latency measurement, or a CPU instruction count. The report uses callback-based metric names to make this distinction visible. [SQLite progress-handler documentation](https://www.sqlite.org/c3ref/progress_handler.html)

The search is exponential: 8 candidates produce 256 configurations; 12 produce 4,096. Select a small explicit `candidate_indexes` list when a database has many indexes. The tool refuses an oversized selection instead of silently sampling or pretending that a partial frontier is exhaustive.

The engine also caps the database snapshot at 256 MiB and encoded result data at 64 MiB per query, with a 16 MiB per-row bound. Result accounting includes type and length framing. These are data-size guards, not a promise that the Python process uses no more memory than those values; SQLite working memory, connection copies, and report evidence add overhead.

| Exit code | Meaning |
|---|---|
| `0` | The experiment completed; removal findings are informational unless the flag is enabled |
| `1` | `--fail-on-removal` was enabled and a complete search found a feasible index removal |
| `2` | Invalid input, failed baseline, snapshot-size limit, overall deadline, or an incomplete search; inspect the diagnostic |
| `130` | Interrupted by the user |

For CI, `--fail-on-removal` is a schema review gate: it asks reviewers to justify indexes that the supplied workload could retire. It is not a performance benchmark gate for all application behavior.

## Read the evidence

The JSON report contains the full evidence. HTML adds a browsable summary and per-query plans; text and Markdown provide review summaries. Across these views you can inspect:

- **The baseline:** all selected indexes retained, query result fingerprints, plans, and callback counts.
- **Every candidate configuration:** retained/removed indexes and per-query outcomes, including errors and result mismatches.
- **Feasibility:** matching results and every query within its own `max_step_ratio`.
- **The Pareto frontier:** feasible configurations where no other feasible configuration uses no more candidate indexes and no more weighted callbacks, with at least one strict improvement.
- **The worst query ratio:** a guard against an aggregate improvement concealing an individual regression.

`weighted_callbacks` is the sum of each case's callback count multiplied by its weight. `worst_callback_ratio` is the largest per-case candidate/baseline ratio. Neither is a measurement of disk space saved, write throughput, energy, or production response time. “Retained index count” counts the selected candidate indexes; pinned indexes still exist in every configuration.

The suggested configuration minimizes retained candidate count first, then weighted callbacks; the frontier also shows alternatives that retain more indexes for less aggregate work. A suggestion is not applied automatically.

“Complete” means every selected subset was attempted under the configured limits. A candidate query that errors or reaches its callback, row, or payload limit makes that configuration conservatively infeasible; its partial count is not used as a successful cost measurement. A failed baseline or overall deadline stops the analysis with exit code `2` and no recommendation.

Read configurations as alternatives. If the report finds several feasible removals, combining their removed-index lists creates another configuration that needs its own evidence. That is the failure mode this tool is designed to expose.

## Source protection and supported SQL

IndexWitness opens the source database using SQLite URI `mode=ro` and takes a consistent backup into memory, including committed data visible through WAL. It starts every candidate from that snapshot. Index drops happen only in those disposable copies. It does not apply a migration to your source.

All UNIQUE indexes, PRIMARY KEY/constraint indexes, and indexes outside the chosen candidate list remain pinned. Virtual-table databases are rejected. The engine disables trusted schema behavior and uses a read-only authorizer plus an allowlist of deterministic functions. Mutation, `ATTACH`, extension loading, random/time functions, and unsupported SQL features fail closed.

Use representative databases and workloads that you are authorized to inspect. The tool does not transmit database contents. Reports omit SQL text, parameter values, and returned rows, but contain query names, schema identifiers, query plans, and fingerprints; review those before publishing a report. A local-only workflow does not make a report free of application metadata.

## Position among existing tools

Useful tools already cover nearby problems:

| Tool | Documented focus | IndexWitness's focus |
|---|---|---|
| [SQLite Expert](https://github.com/sqlite/sqlite/blob/master/ext/expert/README.md) | Propose useful indexes from query features and planner decisions | Test removal combinations of indexes that already exist |
| [Litescope](https://github.com/croc100/Litescope) | SQLite operations, diagnostics, migrations, and index advice | A bounded offline experiment with a report of all selected subsets |
| [RegreSQL](https://github.com/boringSQL/regresql) | PostgreSQL query result/plan baselines and regression testing | SQLite retained-index subset search with per-query work budgets |

Index tuning, regression testing, and exhaustive subset enumeration are established techniques. This project's contribution is a small, dependency-free workflow that joins actual execution, result comparison, and the joint-removal question. It makes no claim to be the first index tool or to prove a universally optimal database design.

## Limits worth reading before acting

- Evidence covers the supplied snapshot and parameter cases. It cannot cover unobserved queries, future data, rare operational jobs, or a different SQLite runtime.
- Progress callbacks are a reproducible work proxy within a controlled run. They do not account equally for expensive operations, disk I/O, cache misses, or Python callback overhead.
- Experiments are read-only workloads. They do not measure insert/update/delete benefits or validate the application's foreign-key maintenance workload.
- SHA-256 result fingerprints are probabilistic witnesses, not a mathematical proof of equality. Comparison is strict about SQLite value types and does not apply floating-point tolerances.
- Input and execution bounds intentionally exclude large or extension-heavy databases. A candidate query that reaches a limit is infeasible; a failed baseline or overall deadline prevents a recommendation. No capped query is assumed to have matching results or acceptable cost.
- Dropping indexes frees SQLite pages for reuse; shrinking an on-disk file generally requires separate maintenance. IndexWitness does not estimate reclaimed filesystem bytes.

The [methodology](docs/methodology.md) documents the experiment, metric, assumptions, and interpretation in detail.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use the standard library and disposable databases. Contributions that improve reproducibility, resource bounds, counterexample fixtures, or report clarity are welcome. When reporting a problem, provide Python and SQLite versions, the failing diagnostic, and a minimal synthetic fixture rather than a private database.

Licensed under [MIT](LICENSE).
