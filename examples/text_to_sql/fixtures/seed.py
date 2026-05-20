"""Regenerate ``db.sqlite`` from scratch.

Run from the example root to rebuild the seeded fixture DB:

    python examples/text_to_sql/fixtures/seed.py

The committed ``db.sqlite`` is the canonical fixture — running this script
should produce an identical database (rows + schema). It exists so a reader
can audit what the binary contains without opening sqlite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "db.sqlite"

_SCHEMA = """
CREATE TABLE customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT NOT NULL,
    signup_date  TEXT NOT NULL
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    amount       REAL NOT NULL,
    order_date   TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
"""

_CUSTOMERS = [
    (1, "Alice Apple", "alice@example.com", "2025-01-15"),
    (2, "Bob Brown", "bob@example.com", "2025-02-20"),
    (3, "Carol Chen", "carol@example.com", "2025-03-10"),
]

_ORDERS = [
    (101, 1, 49.99, "2025-04-01"),
    (102, 1, 19.50, "2025-04-15"),
    (103, 2, 199.00, "2025-04-05"),
    (104, 2, 75.25, "2025-04-22"),
    (105, 3, 12.00, "2025-04-10"),
    (106, 3, 250.00, "2025-04-30"),
]


def build() -> None:
    if _DB_PATH.exists():
        _DB_PATH.unlink()
    conn = sqlite3.connect(_DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?);", _CUSTOMERS)
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?);", _ORDERS)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    build()
    print(f"wrote {_DB_PATH}")
