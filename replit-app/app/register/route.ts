import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";
import { validateRedirectUri } from "@/lib/redirect-uri-validator";
import { corsHeaders, corsPreflightHeaders } from "@/lib/cors";
import { checkRateLimit } from "@/lib/rate-limiter";

function getClientIp(request: NextRequest): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    request.headers.get("x-real-ip") ??
    "unknown"
  );
}

export async function POST(req: NextRequest) {
  const origin = req.headers.get("origin");
  const ip = getClientIp(req);

  if (checkRateLimit(ip, "register", 5)) {
    return NextResponse.json(
      { error: "too_many_requests", error_description: "Rate limit exceeded" },
      { status: 429, headers: corsHeaders(origin) }
    );
  }

  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json(
      { error: "invalid_request" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  const redirect_uris = (body.redirect_uris as string[]) ?? [];
  if (!Array.isArray(redirect_uris) || redirect_uris.length === 0) {
    return NextResponse.json(
      { error: "invalid_client_metadata", error_description: "redirect_uris required" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  for (const uri of redirect_uris) {
    const result = validateRedirectUri(uri);
    if (!result.valid) {
      return NextResponse.json(
        { error: "invalid_client_metadata", error_description: result.reason },
        { status: 400, headers: corsHeaders(origin) }
      );
    }
  }

  const client = oauthStore.registerClient({
    redirect_uris,
    client_name: (body.client_name as string) ?? "MCP Client",
  });

  return NextResponse.json(
    {
      client_id: client.client_id,
      client_secret: client.client_secret,
      redirect_uris: client.redirect_uris,
      client_name: client.client_name,
      grant_types: ["authorization_code"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
    },
    {
      status: 201,
      headers: corsHeaders(origin),
    }
  );
}

export async function OPTIONS(req: NextRequest) {
  const origin = req.headers.get("origin");
  return new NextResponse(null, {
    status: 204,
    headers: corsPreflightHeaders(origin, "POST, OPTIONS", "Content-Type"),
  });
}
