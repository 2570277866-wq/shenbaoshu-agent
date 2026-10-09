#!/bin/bash
# 启动申报书 Agent 服务 —— 模型配置读 agent_service/.env（key 只放那一个文件）
# 用法：bash start.sh
cd "$(dirname "$0")/agent_service" || exit 1

if [ ! -f .env ]; then
  echo "缺 agent_service/.env：先复制样例填 OPENAI_API_KEY" >&2
  exit 1
fi
if grep -q "在这里粘贴你的key" .env; then
  echo "agent_service/.env 里的 OPENAI_API_KEY 还没填" >&2
  exit 1
fi

set -a; source .env; set +a
exec .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
