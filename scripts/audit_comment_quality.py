"""审计生产方法注释的覆盖率、语言和模板残留。

脚本只读源码，不根据方法名自动生成文字。注释是否准确仍由代码评审判断；本工具负责阻止
缺失 docstring、纯英文说明以及已知占位模板重新进入主分支。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (
    "agent-control-plane/app",
    "agent-runtime/src",
    "rag-agent-service/app",
    "agent-governance/app",
    "tool-gateway/app",
    "platform-sdk/platform_sdk",
    "platform-infra/platform_infra",
    "agent-lab/app",
    "model-lab/app",
)
FORBIDDEN = re.compile(
    r"对应的(?:受控业务步骤|当前组件内部业务步骤|生命周期阶段|内部步骤)|"
    r"处理.+对应|Internal helper|Perform .+ within|Initialize .+ dependencies|"
    r"固定关联评测资产版本|所有查询都带租户或业务主键约束|"
    r"保持租户隔离、版本绑定和状态迁移不变量"
)


def _is_production(path: Path) -> bool:
    """排除测试、缓存和虚拟环境，只审计会随工作负载交付的 Python 源码。"""
    return not any(
        part in {"tests", "test", "__pycache__", ".venv", ".venv312"}
        for part in path.parts
    )


def _contains_chinese(value: str) -> bool:
    """确认说明至少包含一个中文字符，避免完整中文工程重新退化为纯英文模板。"""
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def audit_python() -> list[str]:
    """逐个生产函数检查 docstring，并返回可直接定位到文件和行号的问题列表。"""
    findings: list[str] = []
    for relative_root in PYTHON_ROOTS:
        for path in (ROOT / relative_root).rglob("*.py"):
            if not _is_production(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                document = ast.get_docstring(node) or ""
                label = f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}"
                if not document.strip():
                    findings.append(f"{label}: 缺少方法注释")
                elif not _contains_chinese(document):
                    findings.append(f"{label}: 注释不含中文职责说明")
                elif FORBIDDEN.search(document):
                    findings.append(f"{label}: 命中模板化或泛化说明")
    return findings


def audit_java() -> list[str]:
    """扫描 Java Gateway 的已知模板句；Javadoc 与方法绑定由 Maven/Ruff 等价检查补充。"""
    findings: list[str] = []
    java_root = ROOT / "llm-gateway/src/main/java"
    for path in java_root.rglob("*.java"):
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if FORBIDDEN.search(line):
                findings.append(
                    f"{path.relative_to(ROOT)}:{line_number}: Java 注释命中模板化说明"
                )
    return findings


def main() -> int:
    """执行全仓只读审计；发现任一问题时打印定位并返回非零退出码，供 CI 直接使用。"""
    findings = [*audit_python(), *audit_java()]
    if findings:
        print("\n".join(findings))
        return 1
    print("comment-quality audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
