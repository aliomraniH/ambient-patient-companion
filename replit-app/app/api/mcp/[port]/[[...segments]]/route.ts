import { NextRequest, NextResponse } from "next/server";
import { requireBearerToken } from "@/lib/auth-middleware";
import { checkRateLimit } from "@/lib/rate-limiter";
import { openCorsHeaders, openCorsPreflightHeaders } from "@/lib/cors";

const ALLOWED_PORTS = new Set(["8001", "8002", "8003"]);

const FORWARDED_REQUEST_HEADERS = [
  "content-type",
  "accept",
  "mcp-session-id",
  "last-event-id",
];

const FORWARDED_RESPONSE_HEADERS = [
  "mcp-session-id",
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

async function proxy(request: NextRequest, context: RouteContext) {
  const ip = getClientIp(request);
  if (checkRateLimit(ip, "mcp-proxy", 60)) {
    return NextResponse.json(
      { error: "too_many_requests", error_description: "Rate limit exceeded" },
      { status: 429 }
    );
  }

  const authError = requireBearerToken(request);
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

  try {
    const upstream = await fetch(upstream_url, {
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
      { error: "Upstream connection failed", detail: message },
      { status: 502, headers: cors }
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
