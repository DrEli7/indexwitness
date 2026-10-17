"""Generate deterministic synthetic data; refuses to overwrite a database."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def make_demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "shop.db"
    workload_path = directory / "workload.json"
    if database.exists() or workload_path.exists():
        raise ValueError("demo database/workload already exists; choose a new directory")
    # Reserve the pathname exclusively before SQLite opens the empty file.
    with database.open("xb"):
        pass
    with closing(sqlite3.connect(database)) as con, con:
        con.executescript('''CREATE TABLE orders (
            id INTEGER PRIMARY KEY, customer INTEGER NOT NULL,
            region TEXT NOT NULL, amount INTEGER NOT NULL, reference TEXT UNIQUE);
            CREATE INDEX idx_customer_a ON orders(customer);
            CREATE INDEX idx_customer_b ON orders(customer);
            CREATE INDEX idx_region_amount ON orders(region,amount);
            CREATE INDEX idx_amount ON orders(amount);''')
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", (
            (i, i % 1000, ("east", "west", "north", "south")[i % 4], (i * 37) % 10000, f"ref-{i}")
            for i in range(1, 12001)))
        con.execute("ANALYZE")
    workload = {"version": 1, "queries": [
        {"name": "customer_receipts", "sql": "SELECT id, amount FROM orders WHERE customer = ? ORDER BY id", "params": [42], "compare": "ordered", "weight": 10, "max_step_ratio": 1.25},
        {"name": "regional_total", "sql": "SELECT sum(amount) FROM orders WHERE region = :region AND amount > :minimum", "params": {"region": "east", "minimum": 8000}, "weight": 1, "max_step_ratio": 1.25}
    ]}
    with workload_path.open("x", encoding="utf-8") as stream:
        json.dump(workload, stream, indent=2)
        stream.write("\n")
    return database, workload_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".local/demo")
    args = parser.parse_args()
    for path in make_demo(args.directory):
        print(path)
