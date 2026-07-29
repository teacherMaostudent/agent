from threading import RLock

from app.contracts.context import ConversationMessage
from app.storage.sqlite_kv import SqliteKv

_KIND_SESSION = "agent_context_session"


class ConversationStore:
    """Small persistence adapter; replace with PostgreSQL for multi-node production."""

    def __init__(self, sqlite_path=None) -> None:
        self._db = SqliteKv(sqlite_path) if sqlite_path is not None else None
        self._memory: dict[str, list[ConversationMessage]] = {}
        self._lock = RLock()

    def list_messages(self, session_id: str) -> list[ConversationMessage]:
        with self._lock:
            if self._db is not None:
                payload = self._db.get(_KIND_SESSION, session_id) or {"messages": []}
                return [ConversationMessage.model_validate(item) for item in payload["messages"]]
            return list(self._memory.get(session_id, []))

    def append(self, session_id: str, message: ConversationMessage) -> None:
        with self._lock:
            messages = self.list_messages(session_id)
            messages.append(message)
            if self._db is not None:
                self._db.put(
                    _KIND_SESSION,
                    session_id,
                    {"messages": [item.model_dump(mode="json") for item in messages]},
                )
            else:
                self._memory[session_id] = messages

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
