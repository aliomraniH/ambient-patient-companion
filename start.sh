#!/bin/bash
# Production startup — runs all Ambient Patient Companion services.
# Called by Replit deployment as the run command.
set -e

echo "[start.sh] Starting Ambient Patient Companion services..."

# 1. Phase 1 Clinical Intelligence MCP Server (port 8001)
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
MCP_PID=$!
echo "[start.sh] Clinical Intelligence MCP Server started on port 8001 (PID $MCP_PID)"

# 2. Skills MCP Server (port 8002)
(cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py) &
SKILLS_PID=$!
echo "[start.sh] Skills MCP Server started on port 8002 (PID $SKILLS_PID)"

# 3. Ingestion MCP Server (port 8003)
MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server &
INGEST_PID=$!
echo "[start.sh] Ingestion MCP Server started on port 8003 (PID $INGEST_PID)"

# 4. Config Dashboard (port 8080)
(cd replit_dashboard && python server.py) &
DASH_PID=$!
echo "[start.sh] Config Dashboard started on port 8080 (PID $DASH_PID)"

# 5. Next.js — foreground (production build must exist; built in build step)
echo "[start.sh] Starting Next.js production server (port 5000)..."
cd replit-app && npm run start
