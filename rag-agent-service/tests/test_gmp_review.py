from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "UP"


def test_inline_gmp_review_generates_report() -> None:
    response = client.post(
        "/api/v1/reviews/gmp",
        json={
            "content": "SOP编号 SOP-QA-001，版本 V1.0，生效日期 2026-01-01，批准人 QA经理。发生偏差后应记录、调查并完成CAPA。记录人张三，时间2026-07-09，保留原始记录并复核。"
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["overall_risk"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert body["dimensions"], "至少应产出一条维度结果"
    assert body["data_integrity"] is not None
    assert "数据可靠性" in body["report_markdown"]
    assert "GMP 合规审查报告" in body["report_markdown"]


def test_checklist_loaded_from_config_has_13_items() -> None:
    response = client.get("/api/v1/knowledge/checklists")
    assert response.status_code == 200
    assert len(response.json()) == 13


def test_document_type_filter_scopes_checklist() -> None:
    """指定"场地管理文件管理"时，只核查该类文件应满足的 2 条要求(复现 Java 版验证)。"""
    response = client.post(
        "/api/v1/reviews/gmp",
        json={
            "content": "本文件规定场地管理的文件编号、版本号、起草人、审核人、批准人和生效日期。",
            "document_type": "场地管理文件管理",
        },
    )
    assert response.status_code == 200
    body = response.json()
    req_ids = {d["requirement_id"] for d in body["dimensions"]}
    assert req_ids == {"REQ-DOC-001", "REQ-PDCA-001"}


def test_document_types_tree_endpoint() -> None:
    response = client.get("/api/v1/knowledge/document-types")
    assert response.status_code == 200
    body = response.json()
    assert "质量保证" in body["tree"]
    assert "偏差管理" in body["tree"]["质量保证"]


def test_alcoa_detects_backfill_risk() -> None:
    """文件出现"事后补记"红旗词 → 数据可靠性判为存在风险。"""
    response = client.post(
        "/api/v1/reviews/gmp",
        json={"content": "操作记录可在当班结束后统一补录，凭记忆填写即可。"},
    )
    assert response.status_code == 200
    di = response.json()["data_integrity"]
    assert di["verdict"] == "存在数据可靠性风险"
    assert di["risk_found"] >= 1


def test_clarity_detects_vague_words() -> None:
    """含"定期""适当"等模糊词 → 表述清晰度报告应命中并给建议。"""
    response = client.post(
        "/api/v1/reviews/gmp",
        json={"content": "应定期检查设备，发现问题酌情处理，必要时上报相关人员。"},
    )
    assert response.status_code == 200
    clarity = response.json()["clarity"]
    assert clarity is not None
    assert clarity["vague_count"] >= 3
    words = {f["word"] for f in clarity["vague_findings"]}
    assert {"定期", "酌情", "必要时"} <= words
    assert "表述清晰度" in response.json()["report_markdown"]


def test_clarity_detects_term_inconsistency() -> None:
    """同一文件混用"偏差"和"异常" → 提示术语统一。"""
    response = client.post(
        "/api/v1/reviews/gmp",
        json={"content": "出现偏差应记录调查；若发现异常，也需按流程处理。"},
    )
    assert response.status_code == 200
    clarity = response.json()["clarity"]
    assert clarity["term_issue_count"] >= 1
    canonicals = {t["canonical"] for t in clarity["term_inconsistencies"]}
    assert "偏差" in canonicals

