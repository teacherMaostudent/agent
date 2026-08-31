from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.contracts.rag import ControlledScanRequest
from app.retrieval.controlled_scan import ControlledFileScanner, ControlledScanRequestError
from app.service_api.rag_query_api import scan


def test_controlled_scanner_is_allowlisted_and_bounded(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def answer():\n    return 'audit'\n", encoding="utf-8")
    (tmp_path / "secret.bin").write_bytes(b"audit")
    scanner = ControlledFileScanner({"source": tmp_path}, max_results=10)
    matches = scanner.scan("source", "audit")
    assert len(matches) == 1
    assert matches[0].path == "app.py"
    assert matches[0].line_number == 2

    with pytest.raises(ValueError, match="unknown scan scope"):
        scanner.scan("other", "audit")
    with pytest.raises(ValueError, match="within"):
        scanner.scan("source", "audit", glob="../**/*")


def test_controlled_scanner_supports_safe_regex(tmp_path: Path) -> None:
    (tmp_path / "runtime.log").write_text("ERROR timeout\nINFO ok\n", encoding="utf-8")
    scanner = ControlledFileScanner({"logs": tmp_path})
    assert scanner.scan("logs", r"error\s+timeout", regex=True)[0].line_number == 1


def test_controlled_scanner_accepts_redundant_case_insensitive_prefix(tmp_path: Path) -> None:
    """模型常见的 ``(?i)`` 前缀应等价于扫描器固定的忽略大小写策略。"""
    (tmp_path / "runtime.log").write_text("API_KEY=placeholder\n", encoding="utf-8")
    scanner = ControlledFileScanner({"workspace": tmp_path})
    result = scanner.scan("workspace", r"(?i)(password|api[_-]?key)", regex=True)
    assert result[0].line_number == 1


def test_controlled_scanner_rejects_advanced_and_invalid_regex(tmp_path: Path) -> None:
    """保留 fail-closed 正则边界，并把编译失败统一为调用方可修正错误。"""
    scanner = ControlledFileScanner({"workspace": tmp_path})
    with pytest.raises(ControlledScanRequestError, match="advanced or unsafe"):
        scanner.scan("workspace", r"token(?=value)", regex=True)
    with pytest.raises(ControlledScanRequestError, match="invalid regular expression"):
        scanner.scan("workspace", "[", regex=True)


def test_scan_api_exposes_invalid_regex_as_422_instead_of_500(tmp_path: Path) -> None:
    """RAG HTTP 边界必须让 Tool Gateway 区分可修正参数与真正的上游故障。"""
    scanner = ControlledFileScanner({"workspace": tmp_path})
    query_service = SimpleNamespace(
        scan=lambda scope, pattern, *, regex, glob: [
            item.__dict__ for item in scanner.scan(scope, pattern, regex=regex, glob=glob)
        ]
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(
            query_service=query_service
        )))
    )

    with pytest.raises(HTTPException) as captured:
        scan(
            ControlledScanRequest(
                scope="workspace", pattern=r"token(?=value)", regex=True
            ),
            request,
        )

    assert captured.value.status_code == 422
    assert captured.value.detail["code"] == "controlled_scan_arguments_invalid"


def test_scan_redacts_secrets_before_returning_model_context(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text("password=super-secret user@example.com\n", encoding="utf-8")
    result = ControlledFileScanner({"logs": tmp_path}).scan("logs", "password")
    assert "super-secret" not in result[0].line
    assert "[REDACTED_EMAIL]" in result[0].line
