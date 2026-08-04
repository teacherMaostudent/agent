from pathlib import Path

import pytest

from app.retrieval.controlled_scan import ControlledFileScanner


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


def test_scan_redacts_secrets_before_returning_model_context(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text(
        "password=super-secret user@example.com\n", encoding="utf-8"
    )
    result = ControlledFileScanner({"logs": tmp_path}).scan("logs", "password")
    assert "super-secret" not in result[0].line
    assert "[REDACTED_EMAIL]" in result[0].line
