#!/usr/bin/env bash
# =====================================================================
# 滚动备份脚本（永久保存，仅保留最近 KEEP 份）
# 用途：任何改动代码/配置之前，先调用本脚本把待改文件快照存入
#       /workspace/backups/，再动手修改。备份按修改时间滚动裁剪，
#       超过 KEEP 份则自动删除最旧的，始终只保留最近 30 份。
#
# 用法：
#   /workspace/backups/backup.sh <文件1> [文件2 ...]
# 例：
#   /workspace/backups/backup.sh /workspace/quant_compare/quant_all.py
#   /workspace/backups/backup.sh /workspace/email_config.json /workspace/send_email.py
# =====================================================================
set -euo pipefail

BACKUP_DIR="/workspace/backups"
KEEP=30

mkdir -p "$BACKUP_DIR"

if [ "$#" -eq 0 ]; then
  echo "用法: $0 <文件1> [文件2 ...]" >&2
  exit 1
fi

TS=$(date +%Y%m%d_%H%M%S)
for f in "$@"; do
  if [ ! -f "$f" ]; then
    echo "[skip] 不存在: $f" >&2
    continue
  fi
  # 原绝对路径中的 '/' 替换为 '__'，平铺到备份目录，避免嵌套
  rel="${f#/}"
  safe=$(echo "$rel" | tr '/' '__')
  dest="$BACKUP_DIR/${TS}__${safe}.bak"
  cp -p "$f" "$dest"
  echo "已备份: $f -> $dest"
done

# 滚动裁剪：仅保留最近 KEEP 份（按修改时间，最旧在前）
count=$(ls -1 "$BACKUP_DIR" | wc -l)
if [ "$count" -gt "$KEEP" ]; then
  echo "备份数 $count 超过上限 $KEEP，清理最旧的部分："
  ls -1tr "$BACKUP_DIR" | head -n $((count - KEEP)) | while read -r old; do
    rm -f "$BACKUP_DIR/$old"
    echo "  已清理最旧备份: $old"
  done
fi
echo "当前备份数: $(ls -1 "$BACKUP_DIR" | wc -l) (上限 $KEEP)"
