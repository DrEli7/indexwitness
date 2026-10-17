# Security and data handling

IndexWitness opens the source through SQLite `mode=ro`, then experiments on
in-memory backups. It does not execute removal statements against the source.
SQLite may use existing WAL/SHM coordination files; use an exported snapshot
if even normal read-only SQLite coordination is inappropriate.

Only read-only queries using the supported function allowlist are accepted.
Extensions, attached databases, schema-declared virtual tables, random/time functions,
PRAGMAs and writes are not supported. The authorizer is defense in depth,
not a sandbox for malicious database files or native SQLite vulnerabilities.
Use a current Python/SQLite runtime and trusted database/workload inputs.

No network requests or telemetry are made by the program. Reports omit bound
parameters and result rows, but include source basename, schema identifiers,
query names, raw query plans and result fingerprints.
Review reports before sharing; a digest is not a confidentiality mechanism.

Use synthetic reproductions when reporting bugs. For a security concern,
contact the repository owner through their GitHub profile before publicly
posting details. No private security reporting channel is claimed.
