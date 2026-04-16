import { NextResponse } from "next/server";
import { openCorsHeaders } from "@/lib/cors";

const MCP_PORTS = [8001, 8002, 8003] as const;

/**
 * GET /api/keepalive
 *
 * Pings all three MCP servers and returns a status object.
 * Used by:
 *  - The instrumentation.ts background interval (implicit, via the same
 *    per-port fetch logic)
 *  - Claude Web / external callers to force-warm the servers after idle sleep
 *  - Monitoring / uptime checks
 *
 * No authentication required — it only calls /health (read-only, no data).
 */
export async function GET(request: Request) {
  const origin = new URL(request.url).origin;
  const cors = openCorsHeaders(origin);

  const results = await Promise.allSettled(
    MCP_PORTS.map(async (port) => {
      const t0 = Date.now();
      const res = await fetch(`http://localhost:${port}/health`, {
        method: "GET",
        signal: AbortSignal.timeout(6000),
      });
      const body = await res.json().catch(() => ({}));
      return { port, status: res.status, ok: res.ok, latency_ms: Date.now() - t0, ...body };
    })
  );

  const servers = results.map((r) =>
    r.status === "fulfilled"
      ? r.value
      : { port: "unknown", ok: false, error: (r.reason as Error).message }
  );

  const allOk = servers.every((s) => s.ok);

  return NextResponse.json(
    {
      ok: allOk,
      timestamp: new Date().toISOString(),
      servers,
    },
    {
      status: allOk ? 200 : 503,
      headers: cors,
    }
  );
}

export async function OPTIONS(request: Request) {
  const origin = new URL(request.url).origin;
  const cors = openCorsHeaders(origin);
  return new NextResponse(null, { status: 204, headers: cors });
}
