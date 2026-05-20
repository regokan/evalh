"""Execution-grader for the text_to_sql example.

Reads ``query.sql`` (written by the agent), runs it against ``db.sqlite``,
and diffs the rowset against ``expected.csv``. Exit 0 on match, 1 on
mismatch or any I/O / SQL error. Stdout/stderr describe the mismatch for
the trace.

The command evaluator invokes this with ``cwd`` set to the artifact dir
(the post-run workspace snapshot). The agent is responsible for placing
``query.sql``, ``expected.csv``, and a copy of this script into that
directory; ``db.sqlite`` is staged there by the ``tempdir_snapshot``
workspace adapter.
"""

from __future__ import annotations

import contextlib
import csv
import sqlite3
import sys
from pathlib import Path

_DB_FILE = "db.sqlite"
_QUERY_FILE = "query.sql"
_EXPECTED_FILE = "expected.csv"


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def main() -> int:
    root = Path.cwd()
    for required in (_DB_FILE, _QUERY_FILE, _EXPECTED_FILE):
        if not (root / required).is_file():
            print(f"missing required file: {required}", file=sys.stderr)
            return 1

    sql = (root / _QUERY_FILE).read_text(encoding="utf-8").strip()
    if not sql:
        print("query.sql is empty", file=sys.stderr)
        return 1

    with (root / _EXPECTED_FILE).open(encoding="utf-8", newline="") as f:
        expected = [tuple(_cell(c) for c in row) for row in csv.reader(f) if row]

    try:
        conn = sqlite3.connect(root / _DB_FILE)
        actual_rows = conn.execute(sql).fetchall()
    except sqlite3.Error as e:
        print(f"sqlite error: {e}", file=sys.stderr)
        print(f"sql: {sql}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(Exception):
            conn.close()

    actual = [tuple(_cell(c) for c in row) for row in actual_rows]

    if sorted(actual) != sorted(expected):
        print("rowset mismatch", file=sys.stderr)
        print(f"expected: {expected}", file=sys.stderr)
        print(f"actual:   {actual}", file=sys.stderr)
        print(f"sql:      {sql}", file=sys.stderr)
        return 1

    print(f"OK ({len(actual)} row(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
