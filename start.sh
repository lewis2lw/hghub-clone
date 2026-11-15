#!/bin/sh
set -e
PORT=8080 python3 /app/proxy/server.py &
PROXY_BASE=http://127.0.0.1:8080 PORT=8081 exec python3 /app/web/server.py
