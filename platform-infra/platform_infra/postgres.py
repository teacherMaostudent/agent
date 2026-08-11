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
        """将 PostgreSQL 行规范为兼容 SQLite 的映射，同时序列化 JSON 与时间类型。"""
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
        """支持按列名读取，也保留旧仓储按整数列序号读取的兼容行为。"""
        if isinstance(key, int):
            return tuple(self._values.values())[key]
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        """按映射协议迭代列名，使该行可被既有字典式仓储代码消费。"""
        return iter(self._values)

    def __len__(self) -> int:
        """返回行中可见列数，满足 ``Mapping`` 协议。"""
        return len(self._values)


class PostgresCursor:
    def __init__(self, cursor: psycopg.Cursor[Any]) -> None:
        """包装原生游标，向仍处于迁移期的仓储提供稳定的读取接口。"""
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        """返回最近一次语句影响的行数，保持 DB-API 调用方的语义。"""
        return self._cursor.rowcount

    def fetchone(self) -> CompatRow | None:
        """读取下一行并转换为兼容行；结果耗尽时明确返回 ``None``。"""
        row = self._cursor.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self) -> list[CompatRow]:
        """读取剩余全部结果，并统一转换为可按名称或序号访问的兼容行。"""
        return [CompatRow(row) for row in self._cursor.fetchall()]


class PostgresConnection:
    """Small DB-API compatibility layer used while repositories migrate from SQLite.

    Domain repositories keep their tested SQL and transaction boundaries. This layer
    only translates parameter markers and transaction syntax; production schemas are
    explicit PostgreSQL migrations and never translated at runtime.
    """

    def __init__(self, connection: psycopg.Connection[Any], schema: str = "public") -> None:
        """绑定已建立连接与受校验 Schema，拒绝将 Schema 名拼接为 SQL 注入入口。"""
        self._connection = connection
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._connection.execute(f'SET search_path TO "{schema}", public')

    def __enter__(self) -> PostgresConnection:
        """进入事务作用域并返回本连接，供 ``with`` 语句复用。"""
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """异常时回滚，随后无条件关闭连接，避免跨请求复用未决事务。"""
        if exc_type is not None:
            self.rollback()
        self.close()

    def execute(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> PostgresCursor:
        """翻译迁移期 SQLite 占位符后执行 SQL，并保持旧代码可捕获的完整性异常类型。"""
        statement = _translate_sql(sql)
        try:
            cursor = self._connection.execute(statement, tuple(params or ()))
        except psycopg.IntegrityError as exc:
            raise sqlite3.IntegrityError(str(exc)) from exc
        return PostgresCursor(cursor)

    def commit(self) -> None:
        """提交当前事务；调用方须在所有领域不变量校验完成后才调用。"""
        self._connection.commit()

    def rollback(self) -> None:
        """回滚当前事务，清除失败语句可能留下的数据库错误状态。"""
        self._connection.rollback()

    def close(self) -> None:
        """关闭底层 PostgreSQL 连接，释放套接字和连接池配额。"""
        self._connection.close()


def connect_postgres(dsn: str, schema: str = "public") -> PostgresConnection:
    """使用指定 DSN 建立非自动提交连接，并绑定租户服务所需的 Schema。"""
    connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=False)
    return PostgresConnection(connection, schema)


def execute_script(connection: PostgresConnection, script: str) -> None:
    """按语句顺序执行迁移脚本，事务提交仍由上层迁移流程统一控制。"""
    for statement in _statements(script):
        connection.execute(statement)


def execute_script_file(connection: PostgresConnection, path: Path) -> None:
    """以 UTF-8 读取迁移文件后执行，避免运行时依赖宿主机默认编码。"""
    execute_script(connection, path.read_text(encoding="utf-8"))


def _translate_sql(sql: str) -> str:
    """仅转换已知 SQLite 兼容差异，不尝试在运行时改写数据库模式或业务 SQL。"""
    statement = sql.strip()
    if statement.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    return _QUESTION_MARK.sub("%s", statement)


def _statements(script: str) -> Iterable[str]:
    """去除整行 SQL 注释并按分号产生非空语句，供受控迁移脚本顺序执行。"""
    cleaned = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    )
    for statement in cleaned.split(";"):
        if statement.strip():
            yield statement.strip()
