# Ambient Patient Companion

A multi-agent AI system that generates a continuously derived patient health UX from Role x Context x Patient State x Time.

## Architecture

```
S = f(R, C, P, T)  →  optimal clinical surface
```

Seven specialized agents communicate through a shared MCP tool registry. All agents read from a local PostgreSQL warehouse. No agent calls an external API directly.

## Project Structure

```
ambient-patient-companion/
├── replit-app/          ← Next.js 16 frontend (main web UI)
│   ├── app/             ← App Router pages + API routes
│   ├── components/      ← React UI components
│   └── lib/db.ts        ← PostgreSQL pool (pg)
├── mcp-server/          ← FastMCP Python agent server
│   ├── db/schema.sql    ← 22-table PostgreSQL schema (source of truth)
│   ├── skills/          ← Agent skill implementations
│   └── seed.py          ← Data seeding: python mcp-server/seed.py --patients 10 --months 6
└── ingestion/           ← Data ingestion service (Synthea FHIR)
```

## Running the App

- **Workflow**: "Start application" runs `cd replit-app && npm run dev` on port 5000
- **Dev server**: Next.js on `0.0.0.0:5000`

## Database

- **Provider**: Replit built-in PostgreSQL
- **Schema**: `mcp-server/db/schema.sql` (22 tables)
- **Connection**: `DATABASE_URL` environment variable (auto-set by Replit)
- **Note**: `is_stale` column in `source_freshness` table is a regular boolean (not a generated column — PostgreSQL requires immutable expressions for generated columns and `NOW()` is not immutable)

## Environment Variables

- `DATABASE_URL` — PostgreSQL connection string (set automatically by Replit database)
- `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` — also set automatically

## Seeding Data

```bash
python mcp-server/seed.py --patients 10 --months 6
```

## Package Manager

- Frontend: `npm` (package-lock.json in replit-app/)
- Backend: Python 3.12 (pip / requirements)

## Key Notes

- Next.js configured with `-p 5000 -H 0.0.0.0` for Replit compatibility
- All DB queries are server-side only (API routes + server components)
- Phase 1: Synthea synthetic data only; Phase 2+ adds HealthEx, device APIs, multi-user auth
