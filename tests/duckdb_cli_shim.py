"""
Test-only stand-in for the `duckdb` Python module, backed by the DuckDB CLI.

Used only when the real `duckdb` package isn't installed (some sandboxes can't
reach PyPI). It runs every statement through the same DuckDB engine version
(set DUCKDB_CLI to the binary), so SQL syntax, constraints and ON CONFLICT
behaviour are the real thing. Values come back as JSON (DECIMAL and DATE as
strings), which the revenue code accepts. Never used in production.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
from decimal import Decimal

__version__ = "cli-shim"


class Error(Exception):
    pass


class ConstraintException(Error):
    pass


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, dt.datetime):
        return f"TIMESTAMP '{v.isoformat(sep=' ')}'"
    if isinstance(v, dt.date):
        return f"DATE '{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def bind(sql: str, params) -> str:
    params = list(params or [])
    out, i, q = [], 0, None
    for ch in sql:
        if q:
            out.append(ch)
            if ch == q:
                q = None
            continue
        if ch in ("'", '"'):
            q = ch
            out.append(ch)
        elif ch == "?":
            if i >= len(params):
                raise Error("not enough parameters")
            out.append(_literal(params[i]))
            i += 1
        else:
            out.append(ch)
    if i != len(params):
        raise Error(f"{len(params)} parameters for {i} placeholders")
    return "".join(out)


class Cursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.description = [(k,) for k in rows[0].keys()] if rows else []

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]

    def fetchone(self):
        r = self.fetchall()
        return r[0] if r else None


class Connection:
    def __init__(self, path: str):
        self.binary = os.getenv("DUCKDB_CLI") or shutil.which("duckdb")
        if not self.binary:
            raise Error("set DUCKDB_CLI to a DuckDB CLI binary")
        if path in (":memory:", ""):
            path = os.path.join(tempfile.mkdtemp(prefix="duckshim-"), "test.duckdb")
        self.path = path

    def execute(self, sql: str, params=None) -> Cursor:
        text = bind(sql, params)
        p = subprocess.run([self.binary, "-json", "-bail", self.path], input=text + ";\n", capture_output=True, text=True)
        if p.returncode != 0 or p.stderr.strip():
            msg = p.stderr.strip() or p.stdout.strip()
            raise (ConstraintException if "Constraint Error" in msg else Error)(msg)
        out = p.stdout.strip()
        return Cursor(json.loads(out) if out else [])

    def close(self):
        pass


def connect(path: str = ":memory:", config=None) -> Connection:
    return Connection(path)
