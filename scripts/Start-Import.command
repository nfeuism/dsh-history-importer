#!/bin/bash
set -u

cd "$(dirname "$0")" || exit 1
tool="./dsh-history-importer"

if [ ! -x "$tool" ]; then
  printf '未找到导入程序。请重新解压完整的 macOS 下载包。\n'
  read -r -p '按回车键退出… ' _
  exit 1
fi

while true; do
  printf '\nDeepSeek Harness 对话导入器\n'
  printf '1. 预览可导入的对话（不写入）\n'
  printf '2. 导入对话（请先完全退出 DeepSeek Harness）\n'
  printf '3. 退出\n'
  read -r -p '请选择 1、2 或 3：' choice || exit 0
  case "$choice" in
    1) "$tool" ;;
    2) "$tool" --import ;;
    3) exit 0 ;;
    *) printf '请输入 1、2 或 3。\n'; continue ;;
  esac
  status=$?
  if [ "$status" -ne 0 ]; then
    printf '\n操作未完成（退出码 %s）。请查看上方提示。\n' "$status"
  fi
  read -r -p '按回车键返回菜单… ' _ || exit 0
done
