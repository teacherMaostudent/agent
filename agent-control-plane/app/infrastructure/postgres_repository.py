from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from platform_infra.postgres import (
    PostgresConnection,
    connect_postgres,
    execute_script_file,
)

from app.infrastructure.sqlite_repository import SqliteRepository

T = TypeVar("T")


class PostgresRepository(SqliteRepository):
    """PostgreSQL production adapter preserving the domain repository contract."""

    def __init__(self, dsn: str, schema: str, schema_path: Path) -> None:
        self._dsn = dsn
        self._schema = schema
        self._postgres_schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                connection.commit()
            with self._connect() as connection:
                execute_script_file(connection, self._postgres_schema_path)
                connection.commit()

        await asyncio.to_thread(operation)

    async def _read(self, operation: Callable[[PostgresConnection], T]) -> T:
        def run() -> T:
            with self._connect() as connection:
                return operation(connection)

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[PostgresConnection], T]) -> T:
        async with self._write_lock:

            def run() -> T:
                with self._connect() as connection:
                    try:
                        result = operation(connection)
                        connection.commit()
                        return result
                    except Exception:
                        connection.rollback()
                        raise

            return await asyncio.to_thread(run)

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn, self._schema)
