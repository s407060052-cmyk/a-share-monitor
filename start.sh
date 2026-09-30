#!/bin/bash
# 本机安装：由 macOS launchd 持续管理服务。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"
URL="http://localhost:8788/arisk_monitor_local.html"
mkdir -p "$DIR/logs"
for service in proxy http; do
  label="com.arisk.$service"
  if ! launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootstrap "$DOMAIN" "$DIR/$label.plist"
  fi
done
for attempt in {1..15}; do
  if curl --noproxy '*' -fsS -m 2 "$URL" >/dev/null 2>&1 && curl --noproxy '*' -fsS -m 2 http://localhost:8899/pe >/dev/null 2>&1; then
    echo "✓ A股风险监视器已启动：$URL"
    if [ "${ARISK_NO_OPEN:-0}" != 1 ]; then open "$URL"; fi
    exit 0
  fi
  sleep 1
done
echo "启动失败，请查看 $DIR/logs/ 中的日志。"
exit 1
