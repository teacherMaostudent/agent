from datetime import UTC, datetime, timedelta
from threading import RLock

from app.contracts.context import ConversationMessage

_KIND_SESSION = "agent_context_session"


class ConversationStore:
    """Small persistence adapter; replace with PostgreSQL for multi-node production."""

    def __init__(self, backend=None, *, retention_days: int = 30, max_messages: int = 500) -> None:
        """配置存储后端、保留期和单会话上限；会话 ACL 由服务层生成隔离键。"""
        self._db = backend
        self._memory: dict[str, list[ConversationMessage]] = {}
        self._lock = RLock()
        self._retention = timedelta(days=retention_days)
        self._max_messages = max_messages

    def list_messages(self, session_id: str) -> list[ConversationMessage]:
        """读取仍在保留期内的会话消息，不在读取时物理删除过期历史。"""
        with self._lock:
            if self._db is not None:
                payload = self._db.get(_KIND_SESSION, session_id) or {"messages": []}
                messages = [
                    ConversationMessage.model_validate(item) for item in payload["messages"]
                ]
            else:
                messages = list(self._memory.get(session_id, []))
            cutoff = datetime.now(UTC) - self._retention
            return [item for item in messages if item.created_at >= cutoff]

    def append(self, session_id: str, message: ConversationMessage) -> None:
        """以有限 CAS 重试追加消息，防止并发丢失或邮箱重试重复写入上下文。"""
        with self._lock:
            if self._db is not None:
                for _ in range(8):
                    payload, version = self._db.get_with_version(_KIND_SESSION, session_id)
                    messages = [
                        ConversationMessage.model_validate(item)
                        for item in (payload or {"messages": []})["messages"]
                    ]
                    if self._contains_idempotency_key(messages, message):
                        return
                    messages.append(message)
                    messages = messages[-self._max_messages :]
                    if self._db.put_if_version(
                        _KIND_SESSION,
                        session_id,
                        {"messages": [item.model_dump(mode="json") for item in messages]},
                        version,
                    ):
                        return
                raise RuntimeError("concurrent context update retry limit exceeded")
            else:
                messages = self.list_messages(session_id)
                if self._contains_idempotency_key(messages, message):
                    return
                messages.append(message)
                self._memory[session_id] = messages[-self._max_messages :]

    @staticmethod
    def _contains_idempotency_key(
        messages: list[ConversationMessage], candidate: ConversationMessage
    ) -> bool:
        """仅对调用方明确提供的消息幂等键去重，普通重复对话仍按原语义保留。"""
        key = str(candidate.metadata.get("idempotency_key", "")).strip()
        return bool(
            key
            and any(str(item.metadata.get("idempotency_key", "")).strip() == key for item in messages)
        )

    def delete(self, session_id: str) -> bool:
        """按完整隔离键删除会话；调用方需先将租户、用户与会话组合为该键。"""
        with self._lock:
            if self._db is not None:
                return self._db.delete(_KIND_SESSION, session_id)
            return self._memory.pop(session_id, None) is not None

    def close(self) -> None:
        """关闭底层 KV 后端；内存模式没有外部资源。"""
        if self._db is not None:
            self._db.close()
