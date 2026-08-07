#!/usr/bin/env python3
"""服务外升级执行器入口。

由 upgrade_core.spawn_worker 以 start_new_session 启动，脱离 Cockpit 进程组。
不要在 server 进程内 import 后同步执行升级主路径。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Cockpit out-of-process upgrade worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--install-dir",
        default=str(Path(__file__).resolve().parent),
        help="Cockpit 安装目录",
    )
    parser.add_argument(
        "--rollback-only",
        action="store_true",
        help="仅执行回滚（半成品恢复），不继续升级事务",
    )
    args = parser.parse_args(argv)
    # 保证可 import 同目录模块
    install = Path(args.install_dir).resolve()
    if str(install) not in sys.path:
        sys.path.insert(0, str(install))
    import upgrade_core

    if args.rollback_only:
        st = upgrade_core.read_state()
        if st.get("job_id") == args.job_id:
            st["rollback_only"] = True
            st["rollback_requested"] = True
            upgrade_core.write_state(st)

    return upgrade_core.run_job(args.job_id, install_dir=install)


if __name__ == "__main__":
    raise SystemExit(main())
