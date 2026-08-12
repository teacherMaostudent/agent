"""Runtime 集群已部署执行器的只读目录。

目录在进程启动期间由 Container 装配；请求处理阶段只能解析已部署的 Profile，
不能依据发布快照动态加载业务代码或注册新的执行器。
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_runtime_service.runtime.harness import ExecutorAdapter


class ExecutorCatalog:
    """保存当前 Runtime 集群允许执行的 Profile 与执行器映射。"""

    def __init__(self, entries: Mapping[str, ExecutorAdapter]) -> None:
        """冻结启动期装配的执行器目录，拒绝空 Profile 与重复归一化键。"""
        self._entries: dict[str, ExecutorAdapter] = {}
        for profile, executor in entries.items():
            self._add(profile, executor)

    @property
    def profiles(self) -> tuple[str, ...]:
        """返回稳定排序的已部署 Profile，供能力接口和发布前校验读取。"""
        return tuple(sorted(self._entries))

    def resolve(self, profile: str) -> ExecutorAdapter:
        """解析发布快照声明的执行器；未知 Profile 必须在执行前失败。"""
        key = profile.strip()
        try:
            return self._entries[key]
        except KeyError as exc:
            raise LookupError(f"executor profile is not deployed: {key or '<empty>'}") from exc

    def _add(self, profile: str, executor: ExecutorAdapter) -> None:
        """仅在构造阶段写入目录，保持运行期目录对请求不可变。"""
        key = profile.strip()
        if not key or key in self._entries:
            raise ValueError(f"invalid or duplicate executor profile: {profile}")
        self._entries[key] = executor
