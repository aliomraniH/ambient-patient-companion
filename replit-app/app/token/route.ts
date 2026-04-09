/**
 * POST /token
 *
 * OAuth 2.0 Token Endpoint (RFC 6749 §3.2).
 * Exchanges an authorization code for an access token.
 *
 * PKCE (RFC 7636) code_verifier is accepted but not cryptographically
 * verified here since this is a public no-auth server — the code_challenge
 * was stored for reference only.
 */
import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
    Pragma: "no-cache",
  };
}

export async function POST(req: NextRequest) {
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
        { status: 400, headers: corsHeaders() }
      );
    }
  }

  const grant_type = params.get("grant_type");
  if (grant_type !== "authorization_code") {
    return NextResponse.json(
      { error: "unsupported_grant_type" },
      { status: 400, headers: corsHeaders() }
    );
  }

  const code = params.get("code");
  if (!code) {
    return NextResponse.json(
      { error: "invalid_request", error_description: "code required" },
      { status: 400, headers: corsHeaders() }
    );
  }

  const authCode = oauthStore.consumeAuthCode(code);
  if (!authCode) {
    return NextResponse.json(
      { error: "invalid_grant", error_description: "code invalid or expired" },
      { status: 400, headers: corsHeaders() }
    );
  }

  const redirect_uri = params.get("redirect_uri");
  if (redirect_uri && redirect_uri !== authCode.redirect_uri) {
    return NextResponse.json(
      { error: "invalid_grant", error_description: "redirect_uri mismatch" },
      { status: 400, headers: corsHeaders() }
    );
  }

  const accessToken = oauthStore.createAccessToken(authCode.client_id);

  return NextResponse.json(
    {
      access_token: accessToken,
      token_type: "Bearer",
      expires_in: 86400,
      scope: "mcp",
    },
    { status: 200, headers: corsHeaders() }
  );
}

export async function OPTIONS() {
  return new NextResponse(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    },
  });
}
