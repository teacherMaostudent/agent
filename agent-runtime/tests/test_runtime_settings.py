"""Runtime 配置迁移测试：执行平面只从其专属前缀读取运行边界。"""

from agent_runtime_service.core.config import RuntimeSettings


def test_runtime_settings_accept_runtime_prefix_and_ignore_legacy_rag_prefix(monkeypatch) -> None:
    """避免 RAG 环境残留悄然改变 Runtime 的状态存储、模型或安全策略。"""
    monkeypatch.setenv("RAG_PERSISTENCE", "postgres")
    monkeypatch.setenv("RAG_AGENT_MODEL", "legacy-model")
    monkeypatch.setenv("RUNTIME_PERSISTENCE", "sqlite")
    monkeypatch.setenv("RUNTIME_AGENT_MODEL", "runtime-model")

    settings = RuntimeSettings(_env_file=None)

    assert settings.persistence == "sqlite"
    assert settings.agent_model == "runtime-model"


def test_runtime_settings_default_is_not_affected_by_legacy_rag_prefix(monkeypatch) -> None:
    """仅设置旧前缀时保持 Runtime 默认值，迫使部署在迁移时显式声明新配置。"""
    monkeypatch.setenv("RAG_SNAPSHOT_REQUIRED", "true")

    settings = RuntimeSettings(_env_file=None)

    assert settings.snapshot_required is False


def test_runtime_production_rejects_incomplete_independent_configuration() -> None:
    """生产 Runtime 不能只改变环境名就回退到内存状态或未经认证的内部调用。"""
    import pytest

    with pytest.raises(ValueError, match="RUNTIME_PERSISTENCE"):
        RuntimeSettings(environment="production", _env_file=None)
