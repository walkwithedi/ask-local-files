#!/bin/sh
# 测试用的假文件管理器入口，以 xdg-open 的名字放进 PATH：把收到的参数逐行写进 FAKE_OPEN_ARGS 指定的文件。
printf '%s\n' "$@" > "$FAKE_OPEN_ARGS"
