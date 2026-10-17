# Methodology

IndexWitness is a workload-specific experiment on an existing SQLite index set. Its output is evidence about a finite set of configurations on one snapshot. The terms “feasible” and “Pareto” below always refer to that experiment.

## Experimental question

Let `C` be the selected set of removable candidate indexes and `P` the pinned indexes. A configuration retains `R`, a subset of `C`; its database therefore has indexes `P ∪ R`. The baseline retains all of `C`.

For every selected configuration, execute the same named SQL cases with the same bindings against the same database contents. Record result fingerprints, query plans, and progress callback counts. Ask which configurations preserve the observed results and stay within each case's budget, then show the tradeoff between retained candidate count and aggregate work.

The candidates already exist. The tool does not invent indexes, optimize SQL text, or make changes to the source file.

## Snapshot and isolation

1. Validate the bounded JSON workload and candidate selection.
2. Open the source path as a SQLite URI with `mode=ro`.
3. Use the SQLite backup API to copy a consistent database snapshot into memory. Committed WAL contents visible to the source connection are included by SQLite's normal database access.
4. Inspect the copied schema and determine removable and pinned indexes.
5. Measure the baseline with all candidates retained.
6. For every other retained subset, start a fresh in-memory copy of the same snapshot, drop its excluded candidate indexes, then measure the workload.
7. Report all evaluated configurations and any incomplete-search condition.

Using the backup API matters: copying only a database file at filesystem level can omit committed data still in its WAL. `immutable=1` is not used as a shortcut around SQLite's normal access and locking behavior. See the [SQLite online backup documentation](https://www.sqlite.org/backup.html) and [Python `Connection.backup`](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup).

Fresh candidate copies prevent the previous configuration's index removal from affecting the next experiment. The source snapshot also preserves the existing planner statistics rather than running a new `ANALYZE` for each candidate. Changes to statistics would introduce a second experimental variable.

This is an in-memory experiment. It isolates index availability and observed query work; it does not reproduce a production disk cache, concurrent clients, storage latency, or write contention.

## Candidate eligibility

Only explicit nonunique indexes are eligible for removal. All UNIQUE indexes remain pinned, including explicit `CREATE UNIQUE INDEX` objects: their absence could change application constraints even when every read query returns the same rows. PRIMARY KEY/constraint indexes also remain pinned.

If `candidate_indexes` is supplied, the search considers exactly that selection and retains all other indexes. Otherwise, it selects all eligible indexes, subject to the candidate cap. An unknown, ineligible, duplicated, or oversized selection is an input error.

Consequently, the retained-index count in the objective is `|R|`, not the total number of indexes in the database. The frontier is relative to the selected candidates and pinned set. Selecting a different set changes the experiment.

## Search completeness

For `n = |C|` candidates, the exhaustive search has `2^n` retained subsets, including the baseline and the empty retained set. The default candidate cap is 8. Raising the cap can rapidly make a run impractical:

| Candidates | Configurations | Query executions for 10 cases |
|---|---:|---:|
| 4 | 16 | 160 |
| 6 | 64 | 640 |
| 8 | 256 | 2,560 |
| 10 | 1,024 | 10,240 |
| 12 | 4,096 | 40,960 |

This table describes the search space, not an execution-time prediction. A configuration can require much more work than the baseline after useful indexes are removed.

The search does not assume monotonic performance. SQLite may choose a different plan when an index disappears, and that plan may be better or worse. Greedily removing the best-looking index can therefore miss alternatives. Exhaustive search makes the selected finite space auditable at the cost of exponential growth.

“Complete” means that every selected subset was attempted under the configured execution limits. A candidate query error or per-query callback, row, or payload cap makes that configuration conservatively infeasible. The partial observation remains in the evidence; it is not treated as a complete successful cost measurement. Consequently, the frontier is over configurations successfully measured within the absolute limits, not an uncapped search for all possible performance tradeoffs.

A failed baseline, snapshot-size refusal, user interruption, or overall deadline prevents a recommendation. Invalid input and failed/incomplete analyses use exit code `2`; a user interruption uses `130`. These outcomes must not be interpreted as evidence that all proposed removals are safe.

## What the work metric measures

For query case `q` in retained configuration `R`, let `c(q, R)` be the number of SQLite progress callbacks observed while preparing and consuming that query to completion, with progress interval set to one.

The reported measurement is `progress_callbacks`. The compatibility option `--max-callbacks` places a ceiling on this count; its name is not an assertion that the count equals an exact virtual-machine opcode total.

SQLite documents the interval as an **approximate** number of virtual-machine instructions between callbacks. Since SQLite 3.41, callbacks may also occur directly while preparing complex statements. The official exact statement-status counter is a separate API, `SQLITE_STMTSTATUS_VM_STEP`, which Python's standard `sqlite3` module does not expose. [Progress callback contract](https://www.sqlite.org/c3ref/progress_handler.html), [statement-status counters](https://www.sqlite.org/c3ref/c_stmtstatus_counter.html)

Callback counts are useful because they can expose a switch from selective index access to a scan without relying on a noisy wall-clock microbenchmark. Their interpretation has limits:

- Different VM operations do different amounts of work. A callback is not a constant unit of elapsed time.
- The Python callback itself adds overhead. Timings obtained with this instrumentation are not an uninstrumented throughput result.
- Counts do not model disk I/O, page-cache residency, concurrency, or storage behavior.
- SQLite versions, compile options, planner statistics, and parameter values may change the count or plan.
- Preparation is included consistently. The report should be interpreted within a run, not used to compare unrelated machines or arbitrary runtimes as if it were a universal benchmark score.

Keep the SQLite runtime and fixture fixed when repeating an experiment. Re-run after a version upgrade or meaningful data-distribution change.

## Budgets, feasibility, and the frontier

Let `b(q) = c(q, C)` be baseline callbacks, `w(q) > 0` the workload weight, and `t(q) >= 1` the case's `max_step_ratio`.

For positive baseline counts, the per-case ratio is:

```text
ratio(q, R) = c(q, R) / b(q)
```

A configuration is feasible only when every query completes within the absolute execution limits, its result fingerprint matches the baseline using the declared comparison mode, and every per-case ratio is at most its threshold. A zero-work baseline requires conservative handling rather than dividing by zero or inventing a speedup.

The aggregate cost and diagnostic worst ratio are:

```text
weighted_callbacks(R) = Σq w(q) × c(q, R)
worst_callback_ratio(R) = maxq ratio(q, R)
```

Weights influence aggregate cost only. A high-frequency improvement never compensates for another case exceeding its own ratio threshold.

For feasible configurations `A` and `B`, `A` dominates `B` when:

```text
|A| <= |B|
weighted_callbacks(A) <= weighted_callbacks(B)
```

and at least one inequality is strict. The Pareto frontier contains feasible configurations that no other feasible configuration dominates. Equal objective values may describe different index choices; they are useful alternatives, especially when the application has additional requirements absent from this workload.

The objective counts indexes rather than bytes. A large composite index and a small single-column index each count as one. The tool does not claim to minimize storage, write amplification, or production latency.

The single suggested configuration uses a lexicographic preference: fewest retained candidate indexes, then lowest weighted callback count, with a deterministic index-name tie break. The Pareto frontier preserves other nondominated tradeoffs for review. No selected configuration is automatically applied.

## The joint-removal trap

Consider two candidate indexes that can both support a customer lookup:

```sql
CREATE INDEX orders_customer ON orders(customer_id);
CREATE INDEX orders_customer_total ON orders(customer_id, total);
```

Suppose the workload selects `customer_id` and `total` for one customer. Retaining the composite index may provide a covering lookup. Retaining only the shorter index may require additional table access but still meet the query's allowed ratio. Retaining neither may require a full scan.

The two single-index removal experiments can therefore both be feasible while their combined removal is infeasible. This example is a mechanism, not a guarantee about every dataset; fixture size, query shape, statistics, and thresholds determine the outcome.

Read frontier rows as alternative configurations. Unioning the removed sets of two feasible configurations is not a valid inference. The combined set has its own measured outcome in a complete exhaustive search.

The converse is also possible: removing an index can cause the planner to choose a better plan. The search evaluates observed outcomes rather than assuming that more indexes always make reads cheaper.

## Result comparison

Index availability can affect row order or expose SQL whose output is underspecified. IndexWitness compares the values actually returned for the given snapshot and bindings.

`compare: "ordered"` preserves row sequence. Use it when sequence is part of the application contract and specify a total order. `ORDER BY created_at` is insufficient when timestamps can tie; adding a unique key as the final ordering term makes the intended sequence explicit.

`compare: "bag"` ignores row sequence while retaining duplicate multiplicity. This is not set comparison: two copies of a row differ from one. Fingerprinting distinguishes SQLite result value types, including NULL, integer, real, text, and BLOB. There is no floating-point tolerance or collation-aware normalization of returned strings.

Fingerprints use SHA-256. They avoid publishing or retaining raw result rows in reports, but remain probabilistic witnesses rather than formal equality proofs. They do not show that two SQL statements are equivalent over arbitrary databases.

Several forms of valid SQLite SQL deserve particular care:

- Without `ORDER BY`, row order is unspecified. Use bag comparison if sequence is irrelevant.
- Ties across every `ORDER BY` expression leave tie order unspecified.
- `LIMIT` over an unspecified order can change which rows are returned, so bag comparison alone cannot solve it.
- A bare result column in an aggregate query can come from an arbitrarily chosen row.
- Floating-point aggregation can depend on evaluation order. Strict comparison may reveal a changed numeric result even when the difference is small.

A mismatch marks that configuration as failing the declared result contract. Investigate whether the application requires the observed contract or whether the query should be made deterministic. SQLite documents these order and aggregate behaviors in its [SELECT reference](https://www.sqlite.org/lang_select.html).

## SQL restrictions and execution limits

The workload is constrained to read-only execution. The engine sets `trusted_schema=OFF`, uses a SQLite authorizer to deny mutation and attachment, and permits only an allowlist of deterministic functions. Extension loading and random/time functions are excluded. Unsupported functions fail closed rather than being silently evaluated outside the documented model.

Virtual-table databases are rejected. This keeps the experiment from delegating behavior to arbitrary modules whose external access, semantics, and cost are outside the snapshot model. Custom collations, user functions, and extension-dependent schemas are outside the supported workflow.

Per-query callback and result-row limits, timeout checks, and memory-related size bounds constrain the experiment. `--timeout` is an overall deadline covering snapshot copying and the entire search, not a fresh time allowance for each query. These limits aim to stop unintended work; they do not turn Python/SQLite into a hardened security sandbox. A callback timeout can only be checked when execution reaches a callback; a long operation between callbacks can exceed the requested elapsed time before interruption.

Limits are part of the experiment's identity. If a candidate query hits its callback, row, or payload limit, that configuration is infeasible; the unobserved remainder is not assumed to match or satisfy a cost threshold. If the global deadline is reached, the search stops without a recommendation. The baseline must complete successfully before candidate measurements can be interpreted.

In addition to configurable row and callback caps, the engine limits a database snapshot to 256 MiB, encoded result data per query to 64 MiB, and encoded data in one result row to 16 MiB. Each cell's accounting includes its value encoding, one type byte, and eight length bytes. This is a bound on processed data, not total resident memory. Two in-memory database copies, SQLite internal allocations, Python objects, row fingerprints, and accumulated report evidence need additional memory. These guards make this a tool for modest local databases; they are not a hard operating-system memory sandbox.

The JSON parser also rejects duplicate object keys, unknown fields, nonfinite numeric parameters, oversized inputs, and unsupported parameter structures. Parameters are bound through SQLite, never interpolated into SQL text.

## Reports, privacy, and reproducibility

Reports are generated locally. They omit raw SQL, parameter values, and result rows, while retaining query case names, schema identifiers, query plans, fingerprints, metrics, and configuration outcomes. Identifiers and plan details can still disclose application structure. Review a report before sharing it outside your team.

Fingerprinting does not anonymize the underlying database, and a snapshot should be treated according to the sensitivity of its contents. The tool does not upload the database or a report.

For a reviewable experiment, retain:

1. The workload JSON and its chosen parameter coverage.
2. The exact database fixture or a reproducible way to create it, kept private when necessary.
3. Python and runtime SQLite versions, configured limits, and candidate selection.
4. The complete report, including infeasible and failed configurations.
5. Any operational constraints that are not represented in the read-only workload.

Generated output files are created without overwriting existing paths. The CLI also refuses an output path that collides with either input. The tool never applies a chosen removal to the source file; an actual migration is a separate engineering decision.

## What a feasible removal does not establish

It does not prove safety for future data, omitted query shapes, alternate bindings, maintenance jobs, foreign-key checks, writes, or another runtime. A small fixture can also understate the future cost of scans. Use realistic data distributions and repeat the experiment when those assumptions change.

The report does not measure disk space saved. SQLite normally reuses freed database pages; filesystem size and maintenance such as VACUUM are separate concerns. It also does not measure the write-throughput benefit of maintaining fewer indexes.

“Optimal” would need a much broader objective, workload, and configuration space. IndexWitness reports a nondominated set within the selected finite experiment.

## Related work and scope

[SQLite Expert](https://github.com/sqlite/sqlite/blob/master/ext/expert/README.md) creates candidate indexes from SQL features and recommends those selected by the planner. [Litescope](https://github.com/croc100/Litescope) provides broader SQLite operations, diagnosis, migration, and index advice. [RegreSQL](https://github.com/boringSQL/regresql) provides PostgreSQL result and plan regression testing, with additional planner-testing workflows.

These projects establish that index advice and query regression testing are existing categories. IndexWitness focuses on one practical question: the evidence for jointly retiring an existing SQLite index set. Bounded exhaustive execution, explicit result semantics, and per-query budgets are the workflow; no novelty claim is needed for the underlying algorithms.

The JSON `limits` object records the effective experiment caps. The `runtime` object includes SQLite source ID, compile options and the automatic-index setting, so different SQLite builds are distinguishable. `timeout` is bounded to at most 86,400 seconds. Python 3.10 lacks `Connection.setlimit`: the encoded row/result caps still apply, while the SQLite preallocation value cap additionally applies on Python 3.11 and newer.
