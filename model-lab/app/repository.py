"""Model Lab 元数据仓储：本地 SQLite 与生产 PostgreSQL 的明确边界。"""

from __future__ import annotations

import sqlite3
from threading import Lock
from typing import Protocol

from platform_infra.postgres import connect_postgres

from app.settings import Settings


class ModelLabRepository(Protocol):
    """应用服务使用的最小持久化端口，不暴露具体数据库方言。"""

    settings: Settings
    backend: str

    def initialize(self) -> None: ...
    def create(self, record): ...
    def get(self, tenant_id: str, experiment_id: str): ...
    def save(self, record): ...
    def close(self) -> None: ...


class SqliteModelLabRepository:
    """仅供开发和测试的本地持久化；生产 Settings 会拒绝该后端。"""

    backend = "sqlite"

    def __init__(self, settings: Settings) -> None:
        """保存本地路径并将连接创建留给应用生命周期。"""
        self.settings, self._connection, self._lock = settings, None, Lock()

    def initialize(self) -> None:
        """创建实验聚合表与租户索引，使重启不会丢失本地教学数据。"""
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.settings.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("CREATE TABLE IF NOT EXISTS model_lab_experiments (experiment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL)")
        self._connection.execute("CREATE INDEX IF NOT EXISTS model_lab_experiments_tenant_idx ON model_lab_experiments(tenant_id, experiment_id)")
        self._connection.commit()

    def create(self, record):
        """原子写入冻结计划；重复标识符不会覆盖既有证据。"""
        with self._lock:
            self._conn().execute("INSERT INTO model_lab_experiments VALUES (?, ?, ?, ?)", (record.experiment_id, record.plan.tenant_id, record.status, record.model_dump_json()))
            self._conn().commit()
        return record

    def get(self, tenant_id: str, experiment_id: str):
        """在 SQL 层按租户读取聚合，避免跨数据域探测实验是否存在。"""
        from app.main import ExperimentRecord

        with self._lock:
            row = self._conn().execute("SELECT payload_json FROM model_lab_experiments WHERE tenant_id = ? AND experiment_id = ?", (tenant_id, experiment_id)).fetchone()
        return ExperimentRecord.model_validate_json(row["payload_json"]) if row else None

    def save(self, record):
        """保存合法状态迁移；更新零行意味着实验身份不匹配。"""
        with self._lock:
            cursor = self._conn().execute("UPDATE model_lab_experiments SET status = ?, payload_json = ? WHERE tenant_id = ? AND experiment_id = ?", (record.status, record.model_dump_json(), record.plan.tenant_id, record.experiment_id))
            if cursor.rowcount != 1:
                raise KeyError(record.experiment_id)
            self._conn().commit()
        return record

    def close(self) -> None:
        """关闭本地连接，避免开发热重载保留数据库句柄。"""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def _conn(self):
        """拒绝生命周期外访问，防止路由绕过数据库初始化。"""
        if self._connection is None:
            raise RuntimeError("Model Lab repository is not initialized")
        return self._connection


class PostgresModelLabRepository:
    """生产 PostgreSQL 实现；不继承 SQLite 类或在运行时翻译 SQLite SQL。"""

    backend = "postgres"

    def __init__(self, settings: Settings) -> None:
        """保存已验证的 DSN/Schema；每项操作使用短连接事务。"""
        self.settings = settings

    def initialize(self) -> None:
        """创建专属 schema、聚合表和租户索引，避免与其他服务共享隐式表空间。"""
        with self._connect() as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.settings.database_schema}"')
            connection.execute("CREATE TABLE IF NOT EXISTS model_lab_experiments (experiment_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status TEXT NOT NULL, payload_json JSONB NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS model_lab_experiments_tenant_idx ON model_lab_experiments(tenant_id, experiment_id)")
            connection.commit()

    def create(self, record):
        """在原生 PostgreSQL 事务写入不可变计划，主键冲突由数据库拒绝。"""
        with self._connect() as connection:
            connection.execute("INSERT INTO model_lab_experiments(experiment_id, tenant_id, status, payload_json) VALUES (?, ?, ?, ?::jsonb)", (record.experiment_id, record.plan.tenant_id, record.status, record.model_dump_json()))
            connection.commit()
        return record

    def get(self, tenant_id: str, experiment_id: str):
        """使用数据库谓词读取同租户聚合，不在应用层事后过滤。"""
        from app.main import ExperimentRecord

        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM model_lab_experiments WHERE tenant_id = ? AND experiment_id = ?", (tenant_id, experiment_id)).fetchone()
        return ExperimentRecord.model_validate_json(row["payload_json"]) if row else None

    def save(self, record):
        """原子更新完整 JSONB 聚合；零行更新立即失败，防止跨租户覆盖。"""
        with self._connect() as connection:
            cursor = connection.execute("UPDATE model_lab_experiments SET status = ?, payload_json = ?::jsonb WHERE tenant_id = ? AND experiment_id = ?", (record.status, record.model_dump_json(), record.plan.tenant_id, record.experiment_id))
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(record.experiment_id)
            connection.commit()
        return record

    def close(self) -> None:
        """短连接实现不保留进程级资源。"""

    def _connect(self):
        """连接到 Model Lab 专属 schema，避免越界访问平台其他表。"""
        return connect_postgres(self.settings.database_url, self.settings.database_schema)


def build_repository(settings: Settings) -> ModelLabRepository:
    """按启动配置构造存储实现；生产校验已经禁止 SQLite。"""
    return PostgresModelLabRepository(settings) if settings.database_backend == "postgres" else SqliteModelLabRepository(settings)
