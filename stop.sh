#!/bin/bash
# 仅停止本项目的网页和代理服务，保留自动更新。
DOMAIN="gui/$(id -u)"
for service in proxy http; do
  label="com.arisk.$service"
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label"
    echo "✓ 已停止 $label"
  else
    echo "· $label 未在运行"
  fi
done
