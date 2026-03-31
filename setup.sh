#!/bin/bash
# Elite Cloud 象棋 - 一键服务器部署脚本
# 在阿里云终端执行: curl -sL https://raw.githubusercontent.com/henryzhang0086/xiangqi-engine-server/main/setup.sh | bash

set -e

echo "=== 1. 配置 nginx WebSocket 代理 ==="
cat > /etc/nginx/conf.d/xiangqi.conf << 'NGINX'
server {
    listen 80;
    server_name 47.88.32.240;

    location / {
        root /var/www/xiangqi;
        index index.html;
        try_files $uri $uri/ /index.html;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINX

nginx -t && nginx -s reload
echo "✅ nginx 配置完成"

echo "=== 2. 检查引擎状态 ==="
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765 2>/dev/null || echo "000")
if [ "$STATUS" = "426" ]; then
    echo "✅ 引擎正在运行 (port 8765)"
else
    echo "⚠️  引擎未运行 (HTTP $STATUS)，请确认 bridge.py 已启动"
fi

echo "=== 3. 部署前端文件 ==="
mkdir -p /var/www/xiangqi
cd /var/www/xiangqi
curl -sL https://raw.githubusercontent.com/henryzhang0086/xiangqi-elite-web/main/index.html -o index.html
curl -sL https://raw.githubusercontent.com/henryzhang0086/xiangqi-elite-web/main/viewer.html -o viewer.html
echo "✅ 前端文件已部署"

echo ""
echo "==============================="
echo "部署完成！访问: http://47.88.32.240"
echo "==============================="
