#!/usr/bin/env python3
"""服务外升级执行器入口（Wiki13 J0 已退役）。

V1 升级引擎 fail-closed：入口一律拒绝执行，生产路径不可达。
旧引擎算法保留供审计与隔离测试，但不再有任何生产入口能触发它。
"""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Agent Cockpit upgrade worker (RETIRED, fail-closed)"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--install-dir",
        help="Cockpit 安装目录（已退役，忽略）",
    )
    parser.add_argument(
        "--rollback-only",
        action="store_true",
        help="仅执行回滚（已退役，忽略）",
    )
    parser.parse_args(argv)
    print(
        "upgrade_engine_retired: V1 升级引擎已退役，拒绝执行 worker。"
        "请使用受管人工发布流程。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
