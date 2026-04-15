import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";
import { openCorsHeaders, openCorsPreflightHeaders } from "@/lib/cors";
import { checkRateLimit } from "@/lib/rate-limiter";

function getClientIp(request: NextRequest): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    request.headers.get("x-real-ip") ??
    "unknown"
  );
}

export async function GET(req: NextRequest) {
  const origin = req.headers.get("origin");
  const cors = openCorsHeaders(origin);
  const ip = getClientIp(req);
  if (checkRateLimit(ip, "authorize", 20)) {
    return NextResponse.json(
      { error: "too_many_requests", error_description: "Rate limit exceeded" },
      { status: 429, headers: cors }
    );
  }

  const { searchParams } = req.nextUrl;

  const response_type = searchParams.get("response_type");
  const client_id = searchParams.get("client_id");
  const redirect_uri = searchParams.get("redirect_uri");
  const state = searchParams.get("state");
  const code_challenge = searchParams.get("code_challenge") ?? undefined;
  const code_challenge_method = searchParams.get("code_challenge_method") ?? undefined;

  if (response_type !== "code") {
    return NextResponse.json({ error: "unsupported_response_type" }, { status: 400, headers: cors });
  }
  if (!client_id) {
    return NextResponse.json({ error: "invalid_request", error_description: "client_id required" }, { status: 400, headers: cors });
  }
  if (!redirect_uri) {
    return NextResponse.json({ error: "invalid_request", error_description: "redirect_uri required" }, { status: 400, headers: cors });
  }

  const client = await oauthStore.getClient(client_id);
  if (!client) {
    return NextResponse.json({ error: "invalid_client" }, { status: 400, headers: cors });
  }
  if (!client.redirect_uris.includes(redirect_uri)) {
    return NextResponse.json({ error: "invalid_request", error_description: "redirect_uri mismatch" }, { status: 400, headers: cors });
  }

  if (code_challenge && code_challenge_method !== "S256") {
    return NextResponse.json(
      { error: "invalid_request", error_description: "code_challenge_method must be S256" },
      { status: 400, headers: cors }
    );
  }

  const code = await oauthStore.createAuthCode({
    client_id,
    redirect_uri,
    code_challenge,
    code_challenge_method: code_challenge ? "S256" : undefined,
  });

  const redirectUrl = new URL(redirect_uri);
  redirectUrl.searchParams.set("code", code);
  if (state) redirectUrl.searchParams.set("state", state);

  return NextResponse.redirect(redirectUrl.toString(), { status: 302 });
}

export async function OPTIONS(req: NextRequest) {
  const origin = req.headers.get("origin");
  return new NextResponse(null, {
    status: 204,
    headers: openCorsPreflightHeaders(origin, "GET, OPTIONS", "Content-Type"),
  });
}
