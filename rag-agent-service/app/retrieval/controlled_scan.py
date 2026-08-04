"""Bounded, allow-listed text scanning for logs, source and plain-text files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScanMatch:
    scope: str
    path: str
    line_number: int
    line: str


class ControlledFileScanner:
    """Never accepts an arbitrary filesystem path from a request."""

    def __init__(
        self,
        roots: dict[str, str | Path],
        *,
        allowed_extensions: set[str] | None = None,
        max_file_bytes: int = 2_000_000,
        max_files: int = 200,
        max_results: int = 200,
    ) -> None:
        self.roots = {name: Path(value).resolve() for name, value in roots.items()}
        self.allowed_extensions = {
            item.lower()
            for item in (
                allowed_extensions
                or {
                    ".log",
                    ".txt",
                    ".md",
                    ".py",
                    ".java",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".xml",
                }
            )
        }
        self.max_file_bytes, self.max_files, self.max_results = (
            max_file_bytes,
            max_files,
            max_results,
        )

    def scan(
        self, scope: str, pattern: str, *, regex: bool = False, glob: str = "**/*"
    ) -> list[ScanMatch]:
        """Search only approved trees and return bounded, redacted excerpts."""
        # Scope is an allow-list key, never a caller-provided filesystem path.
        # This keeps source/log search useful without granting arbitrary reads.
        if scope not in self.roots:
            raise ValueError("unknown scan scope")
        if not pattern or len(pattern) > 500:
            raise ValueError("pattern must contain 1-500 characters")
        if not glob or ".." in Path(glob).parts or glob.startswith(("/", "\\")):
            raise ValueError("glob must remain within the configured scan scope")
        if regex and ("(?" in pattern or "\\C" in pattern):
            raise ValueError("advanced or unsafe regex constructs are not allowed")
        matcher = re.compile(pattern, re.IGNORECASE) if regex else None
        root = self.roots[scope]
        if not root.is_dir():
            raise ValueError("configured scan scope does not exist")
        matches: list[ScanMatch] = []
        files_seen = 0
        for candidate in root.glob(glob):
            if files_seen >= self.max_files or len(matches) >= self.max_results:
                break
            if not candidate.is_file() or candidate.suffix.lower() not in self.allowed_extensions:
                continue
            resolved = candidate.resolve()
            # Resolve symlinks before checking containment so a link inside an
            # approved tree cannot escape into a sensitive directory.
            if root not in resolved.parents:
                continue
            if resolved.stat().st_size > self.max_file_bytes:
                continue
            files_seen += 1
            try:
                with resolved.open("r", encoding="utf-8", errors="strict") as handle:
                    for line_number, line in enumerate(handle, 1):
                        hit = (
                            bool(matcher.search(line))
                            if matcher
                            else pattern.casefold() in line.casefold()
                        )
                        if hit:
                            matches.append(
                                ScanMatch(
                                    scope,
                                    str(resolved.relative_to(root)),
                                    line_number,
                                    redact_text(line.rstrip()[:4000]),
                                )
                            )
                            if len(matches) >= self.max_results:
                                break
            except (UnicodeDecodeError, OSError):
                continue
        return matches


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"), r"\1=[REDACTED]"),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "[REDACTED_EMAIL]"),
)


def redact_text(value: str) -> str:
    """Remove common credentials and direct identifiers before return or prompting."""
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def bound_untrusted(value: Any, max_chars: int = 12_000) -> Any:
    """Bound and redact tool output before it enters a model decision prompt."""
    if isinstance(value, str):
        return redact_text(value[:max_chars])
    if isinstance(value, list):
        return [bound_untrusted(item, max_chars) for item in value[:200]]
    if isinstance(value, dict):
        return {
            str(key): bound_untrusted(item, max_chars) for key, item in list(value.items())[:200]
        }
    return value
