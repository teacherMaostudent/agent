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
        return await asyncio.to_thread(lambda: self._run_read(operation))

    async def _write(self, operation: Callable[[PostgresConnection], T]) -> T:
        async with self._write_lock:
            return await asyncio.to_thread(lambda: self._run_write(operation))

    def _run_read(self, operation: Callable[[PostgresConnection], T]) -> T:
        with self._connect() as connection:
            return operation(connection)

    def _run_write(self, operation: Callable[[PostgresConnection], T]) -> T:
        with self._connect() as connection:
            try:
                result = operation(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _connect(self) -> PostgresConnection:
        return connect_postgres(self._dsn, self._schema)
