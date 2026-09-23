#!/usr/bin/env bash
# 服务器端部署脚本：把仓库内的自定义插件同步到 AstrBot 运行目录，并安装缺失的配置模板。
# 用法：在服务器上 clone/pull 本仓库后执行  bash deploy/sync_custom_plugins.sh
# 环境变量 ASTRBOT_DATA 可覆盖运行目录，默认 $HOME/astrbot/data
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ASTRBOT_DATA="${ASTRBOT_DATA:-$HOME/astrbot/data}"

echo "[sync] repo = $REPO_DIR"
echo "[sync] data = $ASTRBOT_DATA"

mkdir -p "$ASTRBOT_DATA/plugins"

# 1. 同步插件源码：覆盖代码，但保留运行数据(data/ 子目录)与本地备份
for src in "$REPO_DIR"/custom_plugins/*/; do
    name="$(basename "$src")"
    rsync -a \
        --exclude '__pycache__/' \
        --exclude 'data/' \
        --exclude '*.bak*' \
        "$src" "$ASTRBOT_DATA/plugins/$name/"
    echo "[sync] plugin -> $name"
done

# 2. 配置模板：仅当目标配置不存在时安装，绝不覆盖线上已调好的配置
shopt -s nullglob
for tpl in "$REPO_DIR"/custom_config_templates/*.json; do
    name="$(basename "$tpl")"
    target="$ASTRBOT_DATA/config/$name"
    if [ ! -e "$target" ]; then
        cp "$tpl" "$target"
        echo "[sync] config installed -> $name"
    else
        echo "[sync] config exists, keep -> $name"
    fi
done

echo "[sync] done. restart astrbot to load changes."
