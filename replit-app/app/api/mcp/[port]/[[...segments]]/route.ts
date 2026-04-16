import { NextRequest, NextResponse } from "next/server";
import { requireBearerToken } from "@/lib/auth-middleware";
import { checkRateLimit } from "@/lib/rate-limiter";
import { openCorsHeaders, openCorsPreflightHeaders } from "@/lib/cors";

const ALLOWED_PORTS = new Set(["8001", "8002", "8003"]);

const FORWARDED_REQUEST_HEADERS = [
  "content-type",
  "accept",
  // NOTE: mcp-session-id is intentionally NOT forwarded to the upstream.
  // FastMCP 3.x in stateless mode generates a new session UUID per request
  // and immediately terminates it.  Forwarding it to Claude Web causes the
  // client to include Mcp-Session-Id on every subsequent call; the server
  // then returns "session not found" (or 202-terminated) → the "session-
  // terminated" error the user sees.  Keeping the upstream call session-
  // free is the correct posture for stateless servers.
  "last-event-id",
];

// mcp-session-id is deliberately absent from forwarded *response* headers
// for the same reason: Claude Web must not cache a stateless session ID.
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
];

type RouteContext = {
  params: Promise<{ port: string; segments?: string[] }>;
};

function getClientIp(request: NextRequest): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    request.headers.get("x-real-ip") ??
    "unknown"
  );
}

// ---------------------------------------------------------------------------
// Upstream fetch with retry-on-connect-refused
// ---------------------------------------------------------------------------
// When Replit wakes from idle sleep, the Python MCP servers need ~2-5 s to
// restart.  The first proxy request arrives before they are ready →
// ECONNREFUSED → 502 → Claude Web reports "servers unavailable".
// We retry up to MAX_RETRIES times with exponential backoff before giving up.
// ---------------------------------------------------------------------------

const MAX_RETRIES = 4;
const RETRY_BASE_MS = 500; // 500 ms, 1 000 ms, 2 000 ms, 4 000 ms

function isConnectError(err: unknown): boolean {
  if (!(err instanceof Error)) return false;
  const msg = err.message.toLowerCase();
  return (
    msg.includes("econnrefused") ||
    msg.includes("failed to fetch") ||
    msg.includes("fetch failed") ||
    msg.includes("connect") ||
    msg.includes("network")
  );
}

async function fetchWithRetry(
  url: string,
  init: RequestInit,
): Promise<Response> {
  let lastErr: unknown;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      await new Promise((r) => setTimeout(r, RETRY_BASE_MS * Math.pow(2, attempt - 1)));
    }
    try {
      const resp = await fetch(url, init);
      return resp;
    } catch (err) {
      lastErr = err;
      if (!isConnectError(err)) throw err; // not a connectivity issue — don't retry
    }
  }
  throw lastErr;
}

// ---------------------------------------------------------------------------
// tools/search polyfill
// FastMCP 3.x does not implement tools/search (MCP spec 2025-03-26).
// When Claude Web sends tools/search we intercept here: call tools/list on
// the upstream, filter by the query string, return a conformant response.
// ---------------------------------------------------------------------------

interface McpTool {
  name: string;
  description?: string;
  inputSchema?: unknown;
}

async function handleToolsSearch(
  port: string,
  reqBody: Record<string, unknown>,
  authHeader: string,
  origin: string | null,
): Promise<NextResponse> {
  const params = (reqBody.params ?? {}) as Record<string, unknown>;
  const query  = typeof params.query === "string" ? params.query.toLowerCase().trim() : "";
  const id     = reqBody.id ?? 1;

  const listBody = JSON.stringify({
    jsonrpc: "2.0",
    id: id,
    method: "tools/list",
    params: {},
  });

  let tools: McpTool[] = [];
  try {
    const upstream = await fetchWithRetry(`http://localhost:${port}/mcp`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": authHeader,
      },
      body: listBody,
    });
    const data = await upstream.json() as { result?: { tools?: McpTool[] } };
    tools = data?.result?.tools ?? [];
  } catch {
    tools = [];
  }

  const matched: McpTool[] = query
    ? tools.filter((t) => {
        const haystack = `${t.name} ${t.description ?? ""}`.toLowerCase();
        return haystack.includes(query);
      })
    : tools;

  const cors = openCorsHeaders(origin);
  return NextResponse.json(
    {
      jsonrpc: "2.0",
      id,
      result: { tools: matched, nextCursor: null },
    },
    { status: 200, headers: cors }
  );
}

async function proxy(request: NextRequest, context: RouteContext) {
  const ip = getClientIp(request);
  if (checkRateLimit(ip, "mcp-proxy", 60)) {
    return NextResponse.json(
      { error: "too_many_requests", error_description: "Rate limit exceeded" },
      { status: 429 }
    );
  }

  const authError = await requireBearerToken(request);
  if (authError) return authError;

  const { port, segments = [] } = await context.params;

  if (!ALLOWED_PORTS.has(port)) {
    return NextResponse.json({ error: "Port not allowed" }, { status: 403 });
  }

  const path = segments.join("/");
  const search = request.nextUrl.search;
  const upstream_url = `http://localhost:${port}${path ? `/${path}` : ""}${search}`;

  const fwdHeaders: Record<string, string> = {};
  for (const h of FORWARDED_REQUEST_HEADERS) {
    const val = request.headers.get(h);
    if (val) fwdHeaders[h] = val;
  }
  if (!fwdHeaders["accept"]) {
    fwdHeaders["accept"] = "application/json, text/event-stream";
  }

  let body: string | undefined;
  if (request.method === "POST" || request.method === "PUT" || request.method === "PATCH") {
    try {
      const raw = await request.text();
      body = raw || undefined;
    } catch {
      body = undefined;
    }
  }

  // Intercept tools/search — not natively implemented by FastMCP 3.x
  if (request.method === "POST" && body) {
    try {
      const parsed = JSON.parse(body) as Record<string, unknown>;
      if (parsed.method === "tools/search") {
        return handleToolsSearch(
          port,
          parsed,
          request.headers.get("authorization") ?? "",
          request.headers.get("origin"),
        );
      }
    } catch {
      // not JSON or no method field — fall through to normal proxy
    }
  }

  try {
    const upstream = await fetchWithRetry(upstream_url, {
      method: request.method,
      headers: fwdHeaders,
      body,
    });

    const cors = openCorsHeaders(request.headers.get("origin"));
    const upstreamCt = upstream.headers.get("content-type") ?? "";

    const responseHeaders: Record<string, string> = { ...cors };
    for (const h of FORWARDED_RESPONSE_HEADERS) {
      const val = upstream.headers.get(h);
      if (val) responseHeaders[h] = val;
    }

    if (upstreamCt.includes("text/event-stream") && upstream.body) {
      responseHeaders["Content-Type"] = "text/event-stream";
      responseHeaders["Cache-Control"] = "no-cache, no-transform";
      responseHeaders["Connection"] = "keep-alive";
      responseHeaders["X-Accel-Buffering"] = "no";
      return new Response(upstream.body as ReadableStream, {
        status: upstream.status,
        headers: responseHeaders,
      });
    }

    const text = await upstream.text();

    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    return NextResponse.json(data, { status: upstream.status, headers: responseHeaders });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    const cors = openCorsHeaders(request.headers.get("origin"));
    return NextResponse.json(
      {
        jsonrpc: "2.0",
        error: {
          code: -32603,
          message: "MCP server temporarily unavailable — retrying",
          data: { detail: message },
        },
      },
      {
        status: 503,
        headers: {
          ...cors,
          "Retry-After": "5",
        },
      }
    );
  }
}

export async function OPTIONS(request: NextRequest) {
  const origin = request.headers.get("origin");
  const headers = openCorsPreflightHeaders(
    origin,
    "GET, POST, PUT, DELETE, OPTIONS",
    "Authorization, Content-Type, Accept, Mcp-Session-Id, Last-Event-Id"
  );
  return new NextResponse(null, { status: 204, headers });
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function PUT(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxy(request, context);
}
