"""Pluggable isolated command runner for Agent Lab code/tool evaluation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxRequest:
    """Immutable execution request; callers provide argv, never a shell command string."""

    image: str
    command: tuple[str, ...]
    timeout_seconds: int = 60
    network: str = "none"


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    provider: str


class SandboxProvider:
    """Stable provider contract; Docker can later be replaced by Kata or Firecracker."""

    def execute(self, request: SandboxRequest) -> SandboxResult:  # pragma: no cover - interface
        raise NotImplementedError


class DockerSandboxProvider(SandboxProvider):
    """Run an ephemeral non-privileged Docker container with no host mounts or network by default."""

    def execute(self, request: SandboxRequest) -> SandboxResult:
        """执行只允许 argv 的短生命周期容器，并把 Docker/超时错误归一成可审计失败。"""
        argv = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network",
            request.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--user",
            "10001:10001",
            "--tmpfs",
            "/tmp:size=128m,noexec,nosuid,nodev",
            request.image,
            *request.command,
        ]
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Docker CLI is not available on the sandbox worker") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"sandbox timed out after {request.timeout_seconds} seconds") from exc
        return SandboxResult(
            completed.returncode,
            completed.stdout[-16_000:],
            completed.stderr[-16_000:],
            "docker",
        )


class MicroVmSandboxProvider(SandboxProvider):
    """Explicit future seam: production operators install a MicroVM runner behind this contract."""

    def execute(self, request: SandboxRequest) -> SandboxResult:
        raise RuntimeError("microvm sandbox provider is not installed on this worker")
