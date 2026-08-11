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
        """保存受限 Schema 的连接信息，并创建本实例写事务互斥锁。"""
        self._dsn = dsn
        self._schema = schema
        self._postgres_schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """幂等创建 Schema 并应用数据库结构；在请求接入前完成以避免半初始化状态。"""

        def operation() -> None:
            """在线程中创建 Schema 并应用建表脚本，避免阻塞 ASGI 事件循环。"""
            with self._connect() as connection:
                connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                connection.commit()
            with self._connect() as connection:
                execute_script_file(connection, self._postgres_schema_path)
                connection.commit()

        await asyncio.to_thread(operation)

    async def _read(self, operation: Callable[[PostgresConnection], T]) -> T:
        """在线程中执行只读操作，避免同步驱动阻塞 ASGI 事件循环。"""

        def run() -> T:
            """在短生命周期连接中运行只读回调，连接退出时自动归还。"""
            with self._connect() as connection:
                return operation(connection)

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[PostgresConnection], T]) -> T:
        """串行化本实例写事务；异常回滚，保证业务状态与 Outbox 原子一致。"""
        async with self._write_lock:

            def run() -> T:
                """提交成功回调或回滚异常回调，保持事务与 Outbox 原子一致。"""
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
        """以限定 Schema 创建短连接，避免跨 Schema 的默认搜索路径污染。"""
        return connect_postgres(self._dsn, self._schema)
