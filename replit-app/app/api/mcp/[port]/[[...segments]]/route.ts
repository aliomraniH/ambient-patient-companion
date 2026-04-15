import { NextRequest, NextResponse } from "next/server";
import { requireBearerToken } from "@/lib/auth-middleware";
import { checkRateLimit } from "@/lib/rate-limiter";

const ALLOWED_PORTS = new Set(["8001", "8002", "8003"]);

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

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json",
  };

  let body: string | undefined;
  if (request.method === "POST" || request.method === "PUT") {
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
      headers,
      body,
    });

    const text = await upstream.text();

    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }

    return NextResponse.json(data, { status: upstream.status });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      { error: "Upstream connection failed", detail: message },
      { status: 502 }
    );
  }
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
