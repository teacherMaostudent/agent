"""通用 KV 式 SQLite 存储(持久化基础设施)。

设计(对应设计文档第7节 saveDocument(kind, docId, payload) 思路):
- 单表 kv(kind, id, payload)，按 (kind, id) 存任意 JSON 对象，不为每种数据建专表。
- 只存【结构化业务数据】(文档元数据、审查报告)。**向量绝不进这里**——向量继续
  用 EmbeddingStore 的 JSON 缓存,混进 SQLite 会毁掉检索性能(设计红线)。
- 用 Python 内置 sqlite3,零额外依赖;单机自查场景够用,不上 MySQL。
"""

import json
import sqlite3
import threading
from pathlib import Path


class SqliteKv:
    """开发/单机环境的 JSON KV 适配器，不替代生产数据库的高可用能力。"""

    def __init__(self, db_path: Path) -> None:
        """创建表和乐观锁版本列；调用方必须以 kind 隔离不同业务实体。"""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 锁：FastAPI 多线程下安全共享一个连接。
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY (kind, id))"
        )
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(kv)")}
        if "version" not in columns:
            self._conn.execute("ALTER TABLE kv ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        self._conn.commit()

    def put(self, kind: str, id: str, payload: dict) -> None:
        """覆盖写入对象并递增版本；不提供跨多条记录的事务语义。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (kind, id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload, version = kv.version + 1",
                (kind, id, json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def get(self, kind: str, id: str) -> dict | None:
        """读取单个 JSON 快照；未命中返回 None，由上层决定 404 或降级。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM kv WHERE kind = ? AND id = ?", (kind, id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get_with_version(self, kind: str, id: str) -> tuple[dict | None, int]:
        """读取对象及版本，供上下文等读改写流程实现乐观并发控制。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, version FROM kv WHERE kind = ? AND id = ?",
                (kind, id),
            ).fetchone()
        return (json.loads(row[0]), int(row[1])) if row else (None, 0)

    def put_if_version(self, kind: str, id: str, payload: dict, expected_version: int) -> bool:
        """仅在版本仍匹配时写入，避免并发请求最后写入者无声覆盖。"""
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            if expected_version == 0:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO kv(kind, id, payload, version) VALUES (?, ?, ?, 1)",
                    (kind, id, encoded),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE kv SET payload = ?, version = version + 1 "
                    "WHERE kind = ? AND id = ? AND version = ?",
                    (encoded, kind, id, expected_version),
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def put_many(self, kind: str, items: list[tuple[str, dict]]) -> None:
        """在单个事务中持久化一批记录，避免逐条提交放大数据库开销。

        轻量级批处理可能包含数千份文档；若每份文档单独提交，事务提交成本会
        超过实际处理成本。
        """
        if not items:
            return
        rows = [
            (kind, item_id, json.dumps(payload, ensure_ascii=False)) for item_id, payload in items
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO kv (kind, id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload, version = kv.version + 1",
                rows,
            )
            self._conn.commit()

    def all(self, kind: str) -> list[dict]:
        """返回同类所有快照；仅用于启动重建，不能替代带 ACL 的在线查询。"""
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM kv WHERE kind = ?", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_prefix(self, kind: str, id_prefix: str, *, limit: int) -> list[dict]:
        """按受调用方构造的精确 ID 前缀读取有限记录，避免在线调用全表扫描。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM kv WHERE kind = ? AND id LIKE ? ORDER BY id LIMIT ?",
                (kind, f"{id_prefix}%", limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def delete(self, kind: str, id: str) -> bool:
        """删除精确实体并返回是否实际删除，供上层保持幂等删除语义。"""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM kv WHERE kind = ? AND id = ?", (kind, id))
            self._conn.commit()
            return cursor.rowcount == 1

    def close(self) -> None:
        """关闭 SQLite 连接；容器停止后不应继续使用此适配器。"""
        with self._lock:
            self._conn.close()
