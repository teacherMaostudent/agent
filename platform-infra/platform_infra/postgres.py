from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

_QUESTION_MARK = re.compile(r"\?")


class CompatRow(Mapping[str, Any]):
    """Mapping row that also preserves sqlite-style numeric indexing."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = {
            key: (
                json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else value.isoformat()
                if isinstance(value, (date, datetime))
                else value
            )
            for key, value in values.items()
        }

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return tuple(self._values.values())[key]
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class PostgresCursor:
    def __init__(self, cursor: psycopg.Cursor[Any]) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> CompatRow | None:
        row = self._cursor.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self) -> list[CompatRow]:
        return [CompatRow(row) for row in self._cursor.fetchall()]


class PostgresConnection:
    """Small DB-API compatibility layer used while repositories migrate from SQLite.

    Domain repositories keep their tested SQL and transaction boundaries. This layer
    only translates parameter markers and transaction syntax; production schemas are
    explicit PostgreSQL migrations and never translated at runtime.
    """

    def __init__(self, connection: psycopg.Connection[Any], schema: str = "public") -> None:
        self._connection = connection
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._connection.execute(f'SET search_path TO "{schema}", public')

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self.rollback()
        self.close()

    def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> PostgresCursor:
        statement = _translate_sql(sql)
        try:
            cursor = self._connection.execute(statement, tuple(params or ()))
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        return PostgresCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def connect_postgres(dsn: str, schema: str = "public") -> PostgresConnection:
    connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    return PostgresConnection(connection, schema)


def execute_script(connection: PostgresConnection, script: str) -> None:
    for statement in _statements(script):
        connection.execute(statement)


def execute_script_file(connection: PostgresConnection, path: Path) -> None:
    execute_script(connection, path.read_text(encoding="utf-8"))


def _translate_sql(sql: str) -> str:
    statement = sql.strip()
    if statement.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    return _QUESTION_MARK.sub("%s", statement)


def _statements(script: str) -> Iterable[str]:
    cleaned = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    )
    for statement in cleaned.split(";"):
        if statement.strip():
            yield statement.strip()
