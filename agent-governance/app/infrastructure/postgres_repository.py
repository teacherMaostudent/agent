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

from app.infrastructure.sqlite_repository import GovernanceRepositoryOperations

T = TypeVar("T")


class PostgresRepository(GovernanceRepositoryOperations):
    """生产 PostgreSQL 适配器，保持 SQLite 开发实现相同的审计仓储契约。"""

    def __init__(self, dsn: str, schema: str, schema_path: Path) -> None:
        """保存受限 Schema 的连接配置，并初始化本进程写事务串行锁。"""
        self._dsn = dsn
        self._schema = schema
        self._postgres_schema_path = schema_path
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """幂等创建 Schema 并应用结构；服务接流量前保证审计表完整存在。"""

        def operation() -> None:
            """在线程池中建库建表，避免同步驱动阻塞治理 API 的事件循环。"""
            with self._connect() as connection:
                connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
                connection.commit()
            with self._connect() as connection:
                execute_script_file(connection, self._postgres_schema_path)
                connection.commit()

        await asyncio.to_thread(operation)

    async def _read(self, operation: Callable[[PostgresConnection], T]) -> T:
        """将同步只读驱动调用移出事件循环，避免治理 API 被数据库 I/O 阻塞。"""
        return await asyncio.to_thread(lambda: self._run_read(operation))

    async def _write(self, operation: Callable[[PostgresConnection], T]) -> T:
        """在本实例串行执行写事务，失败回滚以保护审计事件与派生数据的一致性。"""
        async with self._write_lock:
            return await asyncio.to_thread(lambda: self._run_write(operation))

    def _run_read(self, operation: Callable[[PostgresConnection], T]) -> T:
        """在短连接内运行只读仓储操作，连接释放由上下文管理器保证。"""
        with self._connect() as connection:
            return operation(connection)

    def _run_write(self, operation: Callable[[PostgresConnection], T]) -> T:
        """执行单个事务并在任何异常时回滚，调用方可据此安全重试。"""
        with self._connect() as connection:
            try:
                result = operation(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def _connect(self) -> PostgresConnection:
        """连接到限定治理 Schema，避免默认搜索路径访问其他服务数据。"""
        return connect_postgres(self._dsn, self._schema)
