#!/usr/bin/env bash
# Wiki13 J0：V1 一键升级引擎已退役（fail-closed）。
# 旧脚本的所有升级执行动作（代码拉取、依赖安装、Hub 自愈、服务重启）
# 已全部移除；生产路径不可达，升级统一走受管人工发布。
set -euo pipefail

cat >&2 <<'EOF'
upgrade_engine_retired: 一键升级已退役（fail-closed）。

请使用受管人工发布流程安装新版本，不要在安装目录执行
代码拉取 / 依赖安装 / 直接重启服务。
EOF
exit 1
