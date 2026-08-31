"""兼容旧冒烟入口；拆分后的服务必须通过 HTTP 测试，不能重新 import 到同一进程。"""

from __future__ import annotations

import argparse
import runpy
from pathlib import Path


def main() -> None:
    """显式确认使用本机运行中的平台后，复用统一 E2E，避免维护两份失配测试。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--running-platform", action="store_true",
        help="确认已启动本地 Compose；将创建 e2e 测试发布、任务及审计记录",
    )
    if not parser.parse_args().running_platform:
        parser.error("请先启动本地平台，再传 --running-platform；独立服务不再以同进程导入启动")
    target = Path(__file__).resolve().parents[2] / "scripts" / "platform_e2e.py"
    entry = runpy.run_path(str(target))
    raise SystemExit(entry["main"]())


if __name__ == "__main__":
    main()
