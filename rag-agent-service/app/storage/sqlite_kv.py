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
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + 锁：FastAPI 多线程下安全共享一个连接。
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY (kind, id))"
        )
        self._conn.commit()

    def put(self, kind: str, id: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (kind, id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(kind, id) DO UPDATE SET payload = excluded.payload",
                (kind, id, json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def get(self, kind: str, id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM kv WHERE kind = ? AND id = ?", (kind, id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM kv WHERE kind = ?", (kind,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
