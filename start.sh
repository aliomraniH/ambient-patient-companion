#!/bin/bash
# Production startup — runs all Ambient Patient Companion services.
# Called by Replit deployment as the run command.
set -e

echo "[start.sh] Starting Ambient Patient Companion services..."

# 0. Regenerate .mcp.json with the current public domain so Claude can discover tools
python scripts/generate_mcp_json.py

# 1. Clinical Intelligence MCP Server — ambient-clinical-intelligence (port 8001)
MCP_TRANSPORT=streamable-http MCP_PORT=8001 python -m server.mcp_server &
MCP_PID=$!
echo "[start.sh] ambient-clinical-intelligence MCP Server started on port 8001 (PID $MCP_PID)"

# 2. Skills MCP Server — ambient-skills-companion (port 8002)
(cd mcp-server && MCP_TRANSPORT=streamable-http MCP_PORT=8002 python server.py) &
SKILLS_PID=$!
echo "[start.sh] ambient-skills-companion MCP Server started on port 8002 (PID $SKILLS_PID)"

# 3. Ingestion MCP Server — ambient-ingestion (port 8003)
MCP_TRANSPORT=streamable-http MCP_PORT=8003 python -m ingestion.server &
INGESTION_PID=$!
echo "[start.sh] ambient-ingestion MCP Server started on port 8003 (PID $INGESTION_PID)"

# 4. Config Dashboard (port 8080)
(cd replit_dashboard && python server.py) &
DASH_PID=$!
echo "[start.sh] Config Dashboard started on port 8080 (PID $DASH_PID)"

# 4b. Daily refresh of the atom_pressure_scores materialized view.
#     Replit's PostgreSQL has no pg_cron, so this lightweight Python
#     daemon is the schedule. It refreshes once on startup, then every
#     ATOM_PRESSURE_REFRESH_INTERVAL_HOURS (default 24h). Each refresh
#     stamps system_config.atom_pressure_scores_last_refresh so
#     `python scripts/refresh_atom_pressure_scores.py --check` can
#     verify freshness for monitoring.
python scripts/refresh_atom_pressure_scores.py &
REFRESH_PID=$!
echo "[start.sh] atom_pressure_scores refresh daemon started (PID $REFRESH_PID)"

# 5. Next.js — foreground (production build must exist; built in build step)
echo "[start.sh] Starting Next.js production server (port 5000)..."
cd replit-app && npm run start
