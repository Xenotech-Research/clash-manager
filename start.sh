#!/usr/bin/env bash
# -------------------------------------------------------
# 自动启动 Clash Manager（FastAPI + 静态前端由 nginx 提供）
# -------------------------------------------------------

SESSION="clash-manager"
PROJECT_DIR="/home/ubuntu/clash-manager"
CMD="uvicorn server:app --host 0.0.0.0 --port 9988 --reload"

if screen -list | grep -q "\.${SESSION}[[:space:]]"; then
    echo "✔ 服务已经在 screen 会话 '${SESSION}' 中运行。"
    exit 0
fi

screen -dmS "${SESSION}"
screen -S "${SESSION}" -p 0 -X stuff "cd ${PROJECT_DIR}\n"
screen -S "${SESSION}" -p 0 -X stuff "${CMD}\n"

echo "🎉 已在新的 screen 会话 '${SESSION}' 中启动 Clash Manager。"
