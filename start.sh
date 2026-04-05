#!/bin/bash
# Production startup — runs all Ambient Patient Companion services.
# Called by Replit deployment as the run command.
set -e

echo "[start.sh] Starting Ambient Patient Companion services..."

# 1. Phase 1 Clinical Intelligence MCP Server (port 8001)
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
MCP_PID=$!
echo "[start.sh] Clinical Intelligence MCP Server started on port 8001 (PID $MCP_PID)"

# 2. Config Dashboard (port 8080)
(cd replit_dashboard && python server.py) &
DASH_PID=$!
echo "[start.sh] Config Dashboard started on port 8080 (PID $DASH_PID)"

# 3. Next.js — foreground (production build must exist; built in build step)
echo "[start.sh] Starting Next.js production server (port 5000)..."
cd replit-app && npm run start
