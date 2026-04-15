import { NextRequest, NextResponse } from "next/server";
import { oauthStore, verifyPkceS256 } from "@/lib/oauth-store";
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

  if (checkRateLimit(ip, "token", 10)) {
    return NextResponse.json(
      { error: "too_many_requests", error_description: "Rate limit exceeded" },
      { status: 429, headers: corsHeaders(origin) }
    );
  }

  let params: URLSearchParams;

  const contentType = req.headers.get("content-type") ?? "";
  if (contentType.includes("application/x-www-form-urlencoded")) {
    const text = await req.text();
    params = new URLSearchParams(text);
  } else {
    try {
      const body = await req.json() as Record<string, string>;
      params = new URLSearchParams(body);
    } catch {
      return NextResponse.json(
        { error: "invalid_request" },
        { status: 400, headers: corsHeaders(origin) }
      );
    }
  }

  const grant_type = params.get("grant_type");
  if (grant_type !== "authorization_code") {
    return NextResponse.json(
      { error: "unsupported_grant_type" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  const code = params.get("code");
  if (!code) {
    return NextResponse.json(
      { error: "invalid_request", error_description: "code required" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  const authCode = oauthStore.consumeAuthCode(code);
  if (!authCode) {
    return NextResponse.json(
      { error: "invalid_grant", error_description: "code invalid or expired" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  const redirect_uri = params.get("redirect_uri");
  if (!redirect_uri) {
    return NextResponse.json(
      { error: "invalid_request", error_description: "redirect_uri required" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }
  if (redirect_uri !== authCode.redirect_uri) {
    return NextResponse.json(
      { error: "invalid_grant", error_description: "redirect_uri mismatch" },
      { status: 400, headers: corsHeaders(origin) }
    );
  }

  if (authCode.code_challenge) {
    const code_verifier = params.get("code_verifier");
    if (!code_verifier) {
      return NextResponse.json(
        { error: "invalid_request", error_description: "code_verifier required for PKCE" },
        { status: 400, headers: corsHeaders(origin) }
      );
    }
    if (!verifyPkceS256(code_verifier, authCode.code_challenge)) {
      return NextResponse.json(
        { error: "invalid_grant", error_description: "PKCE verification failed" },
        { status: 400, headers: corsHeaders(origin) }
      );
    }
  }

  const accessToken = oauthStore.createAccessToken(authCode.client_id);

  return NextResponse.json(
    {
      access_token: accessToken,
      token_type: "Bearer",
      expires_in: 86400,
      scope: "mcp",
    },
    { status: 200, headers: corsHeaders(origin) }
  );
}

export async function OPTIONS(req: NextRequest) {
  const origin = req.headers.get("origin");
  return new NextResponse(null, {
    status: 204,
    headers: corsPreflightHeaders(origin, "POST, OPTIONS", "Content-Type, Authorization"),
  });
}
