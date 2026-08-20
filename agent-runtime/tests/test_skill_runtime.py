from datetime import UTC, datetime, timedelta

import pytest
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.skills import (
    SkillBinding,
    SkillSpec,
    compile_skill_plan,
    validate_skill_catalog,
)

from agent_runtime_service.runtime.models import RuntimeBudget
from agent_runtime_service.runtime.skill_runtime import (
    GovernedSkillRuntime,
    InMemorySkillCatalog,
    SkillCompositionManager,
    SkillExecutionRequest,
    SkillInvocationError,
)


def _plan():
    return compile_skill_plan(
        SkillSpec.model_validate(
            {
                "skill_id": "document-review",
                "provides": [{"capability_id": "DOCUMENT_REVIEW"}],
                "instructions": {
                    "prompt_id": "review",
                    "prompt_version": "1",
                    "system_template": "review",
                },
                "input_schema": {"type": "object", "required": ["document_id"]},
                "output_schema": {"type": "object", "required": ["decision"]},
                "qualification": {
                    "evaluation_dataset_id": "golden",
                    "qualification_policy_id": "gate",
                },
            }
        ),
        version="1.0.0",
    )


def _context():
    return ExecutionContext.create(
        request_id="req",
        trace_id="trace",
        session_id="session",
        tenant_id="tenant",
        user_id="user",
        agent_id="agent",
        agent_version="1",
        snapshot_id="snap",
        deadline_seconds=60,
        attempt_budget=5,
    )


def _budget():
    return RuntimeBudget(
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
        max_steps=5,
        max_llm_calls=5,
        max_tool_calls=5,
        max_retrieval_rounds=5,
        max_cost_usd=2,
    )


def test_skill_runtime_accepts_only_exact_bound_artifact_and_schema():
    plan = _plan()
    runtime = GovernedSkillRuntime(
        InMemorySkillCatalog([plan]),
        type("E", (), {"execute": lambda _, __, ___: {"decision": "ok"}})(),
    )
    result = runtime.execute(
        SkillExecutionRequest(
            binding=SkillBinding(
                skill_id="document-review", version="1.0.0", artifact_digest=plan.artifact_digest
            ),
            capability_id="DOCUMENT_REVIEW",
            input={"document_id": "d1"},
            context=_context(),
        ),
        _budget(),
    )
    assert result.output == {"decision": "ok"}


def test_skill_runtime_fails_closed_for_unpublished_binding():
    plan = _plan()
    runtime = GovernedSkillRuntime(
        InMemorySkillCatalog([plan]), type("E", (), {"execute": lambda *_: {}})()
    )
    with pytest.raises(SkillInvocationError, match="unavailable"):
        runtime.execute(
            SkillExecutionRequest(
                binding=SkillBinding(
                    skill_id="document-review", version="1.0.0", artifact_digest="a" * 64
                ),
                capability_id="DOCUMENT_REVIEW",
                input={"document_id": "d1"},
                context=_context(),
            ),
            _budget(),
        )


def test_skill_catalog_progressive_disclosure_hides_full_plan():
    plan = _plan()
    catalog = InMemorySkillCatalog([plan])
    card = catalog.cards("tenant", "DOCUMENT_REVIEW")[0]
    assert card.skill_id == plan.skill_id
    assert "instructions" not in card.model_dump()
    assert "tools" not in card.model_dump()


def test_skill_composition_rejects_undeclared_dependency():
    root = _plan()
    dependency_spec = root.model_dump()
    dependency_spec["skill_id"] = "hidden-dependency"
    dependency_spec["contract_hash"] = "b" * 64
    dependency_spec["artifact_digest"] = "b" * 64
    dependency = type(root).model_validate(dependency_spec)
    with pytest.raises(SkillInvocationError, match="not declared"):
        SkillCompositionManager().validate(root, [dependency])


def test_skill_composition_rejects_declared_but_conflicting_dependency():
    """Control Plane 使用的共享校验必须阻断显式冲突组合。"""
    root_data = _plan().model_dump()
    root_data["composition"]["allowed_dependencies"] = ["conflicting-skill"]
    root_data["composition"]["conflicts_with"] = ["conflicting-skill"]
    root = type(_plan()).model_validate(root_data)
    dependency_data = _plan().model_dump()
    dependency_data["skill_id"] = "conflicting-skill"
    dependency_data["contract_hash"] = "b" * 64
    dependency_data["artifact_digest"] = "b" * 64
    dependency = type(root).model_validate(dependency_data)

    with pytest.raises(SkillInvocationError, match="conflict"):
        SkillCompositionManager().validate(root, [dependency])


def test_progressive_catalog_does_not_treat_all_visible_skills_as_active():
    """发布目录可大于 max_active_skills，上限只约束 Resolver 实际激活集。"""
    plans = []
    for index in range(4):
        data = _plan().model_dump()
        data["skill_id"] = f"visible-skill-{index}"
        data["contract_hash"] = f"{index + 1}" * 64
        data["artifact_digest"] = f"{index + 1}" * 64
        plans.append(type(_plan()).model_validate(data))

    validate_skill_catalog(plans)
