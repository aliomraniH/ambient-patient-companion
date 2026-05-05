#!/bin/bash
set -e

echo "=== Post-merge setup ==="

# Install/sync Python dependencies
echo "[1/3] Installing Python dependencies..."
pip install -q -r requirements.txt

# Install/sync Node.js dependencies for the Next.js frontend
echo "[2/3] Installing Node.js dependencies..."
cd replit-app && npm install --legacy-peer-deps --silent && cd ..

# Ensure the DB schema is applied (idempotent — uses IF NOT EXISTS throughout)
echo "[3/3] Applying DB schema (idempotent)..."
python -c "
import asyncio, asyncpg, os, pathlib, sys
async def main():
    sql = pathlib.Path('mcp-server/db/schema.sql').read_text()
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    try:
        await conn.execute(sql)
        print('Schema applied OK')
    except Exception as e:
        print(f'Schema note: {e}', file=sys.stderr)
    finally:
        await conn.close()
asyncio.run(main())
"

echo "=== Post-merge setup complete ==="
