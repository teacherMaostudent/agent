from __future__ import annotations

import json
import re
from typing import Any

from platform_infra.postgres import connect_postgres


class PostgresKv:
    """Tenant-agnostic KV primitive; payloads contain and enforce tenant ownership."""

    def __init__(self, dsn: str, schema: str) -> None:
        """校验模式名并初始化带版本列的通用 KV 表，作为持久化适配层基础。"""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid PostgreSQL schema")
        self._dsn = dsn
        self._schema = schema
        with connect_postgres(dsn, schema) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            connection.commit()
        with connect_postgres(dsn, schema) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS platform_kv(
                kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                version BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(kind, id))"""
            )
            connection.commit()

    def put(self, kind: str, id: str, payload: dict) -> None:
        """写入独立事务中的 JSON 快照并递增版本；租户 ACL 由业务载荷层执行。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            connection.execute(
                """INSERT INTO platform_kv(kind, id, payload) VALUES (?, ?, ?)
                ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload,
                version = platform_kv.version + 1, updated_at = now()""",
                (kind, id, json.dumps(payload, ensure_ascii=False)),
            )
            connection.commit()

    def get(self, kind: str, id: str) -> dict | None:
        """读取单个快照；不在这个通用 KV 层推断业务权限。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            row = connection.execute(
                "SELECT payload FROM platform_kv WHERE kind = ? AND id = ?", (kind, id)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_with_version(self, kind: str, id: str) -> tuple[dict | None, int]:
        """返回快照和版本，供服务层的 CAS 更新抵抗并发写冲突。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            row = connection.execute(
                "SELECT payload, version FROM platform_kv WHERE kind = ? AND id = ?",
                (kind, id),
            ).fetchone()
        return (json.loads(row["payload"]), int(row["version"])) if row else (None, 0)

    def put_if_version(self, kind: str, id: str, payload: dict, expected_version: int) -> bool:
        """执行比较并交换写入；返回 False 让调用方有限重试或明确报冲突。"""
        encoded = json.dumps(payload, ensure_ascii=False)
        with connect_postgres(self._dsn, self._schema) as connection:
            if expected_version == 0:
                cursor = connection.execute(
                    """INSERT INTO platform_kv(kind, id, payload, version)
                    VALUES (?, ?, ?, 1) ON CONFLICT(kind, id) DO NOTHING""",
                    (kind, id, encoded),
                )
            else:
                cursor = connection.execute(
                    """UPDATE platform_kv SET payload = ?, version = version + 1,
                    updated_at = now() WHERE kind = ? AND id = ? AND version = ?""",
                    (encoded, kind, id, expected_version),
                )
            connection.commit()
            return cursor.rowcount == 1

    def all(self, kind: str) -> list[dict[str, Any]]:
        """读取同类全部记录，仅适合启动加载等受控批量场景。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            rows = connection.execute(
                "SELECT payload FROM platform_kv WHERE kind = ?", (kind,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def delete(self, kind: str, id: str) -> bool:
        """删除精确 key；权限校验必须在调用此前完成。"""
        with connect_postgres(self._dsn, self._schema) as connection:
            cursor = connection.execute(
                "DELETE FROM platform_kv WHERE kind = ? AND id = ?", (kind, id)
            )
            connection.commit()
            return cursor.rowcount == 1

    def close(self) -> None:
        """连接逐操作创建，关闭为保持 KV 适配器统一接口的空实现。"""
        return None
