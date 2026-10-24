#!/bin/sh
# 测试用的假目录选择框，以 zenity 或 kdialog 的名字放进 PATH。
# 环境变量：FAKE_PICK 为要「选中」的路径（FAKE_PICK_FROM 不为空时改从这个文件读），为 cancel 时按取消退出（码 1），为 fail 时报错退出（码 255）；
# FAKE_PICK_SLEEP 为弹框前等待的秒数；FAKE_PICK_ARGS 不为空时把收到的参数逐行写进这个文件。
if [ -n "$FAKE_PICK_ARGS" ]; then
  printf '%s\n' "$@" > "$FAKE_PICK_ARGS"
fi
if [ -n "$FAKE_PICK_FROM" ]; then
  FAKE_PICK=$(cat "$FAKE_PICK_FROM")
fi
sleep "${FAKE_PICK_SLEEP:-0}"
case "$FAKE_PICK" in
  cancel) exit 1 ;;
  fail) echo "cannot open display" >&2; exit 255 ;;
esac
printf '%s\n' "$FAKE_PICK"
