/**
 * Next.js instrumentation hook — runs once at server startup.
 *
 * Registers a background keep-alive interval that pings the three FastMCP
 * Python servers every 4 minutes.  Without this, Replit's idle-sleep
 * mechanism suspends the Python processes after ~5 min of inactivity.
 * When a new Claude Web session then arrives, the proxy hits ECONNREFUSED
 * before the servers have restarted, producing the
 * "session-terminated / no-approval" error the user sees.
 *
 * The keep-alive also pre-warms the asyncpg connection pools so the first
 * real tool call after a period of inactivity has no cold-start penalty.
 */

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;

  const MCP_PORTS = [8001, 8002, 8003];
  const INTERVAL_MS = 4 * 60 * 1000; // 4 minutes

  async function pingAll() {
    for (const port of MCP_PORTS) {
      try {
        const res = await fetch(`http://localhost:${port}/health`, {
          method: "GET",
          signal: AbortSignal.timeout(5000),
        });
        if (!res.ok) {
          console.warn(`[keepalive] port ${port} returned HTTP ${res.status}`);
        }
      } catch {
        console.warn(`[keepalive] port ${port} unreachable (still starting up?)`);
      }
    }
  }

  // First ping after a short warm-up delay so the Python servers have had
  // time to start before we hit them.
  setTimeout(async () => {
    await pingAll();
    setInterval(pingAll, INTERVAL_MS);
  }, 15_000);

  console.log("[keepalive] MCP server keep-alive registered (interval: 4 min)");
}
