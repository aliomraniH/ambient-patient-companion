/**
 * GET /authorize
 *
 * OAuth 2.0 Authorization Endpoint (RFC 6749 §3.1).
 *
 * This is a PUBLIC, no-user-auth server — there is no login page.
 * The endpoint immediately issues an authorization code and redirects back
 * to the client's redirect_uri. This satisfies the OAuth PKCE flow that
 * Claude runs when connecting to a remote MCP server.
 */
import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";

export async function GET(req: NextRequest) {
  const { searchParams } = req.nextUrl;

  const response_type = searchParams.get("response_type");
  const client_id = searchParams.get("client_id");
  const redirect_uri = searchParams.get("redirect_uri");
  const state = searchParams.get("state");
  const code_challenge = searchParams.get("code_challenge") ?? undefined;
  const code_challenge_method = searchParams.get("code_challenge_method") ?? undefined;

  if (response_type !== "code") {
    return NextResponse.json({ error: "unsupported_response_type" }, { status: 400 });
  }
  if (!client_id) {
    return NextResponse.json({ error: "invalid_request", error_description: "client_id required" }, { status: 400 });
  }
  if (!redirect_uri) {
    return NextResponse.json({ error: "invalid_request", error_description: "redirect_uri required" }, { status: 400 });
  }

  const client = oauthStore.getClient(client_id);
  if (!client) {
    return NextResponse.json({ error: "invalid_client" }, { status: 400 });
  }
  if (!client.redirect_uris.includes(redirect_uri)) {
    return NextResponse.json({ error: "invalid_request", error_description: "redirect_uri mismatch" }, { status: 400 });
  }

  const code = oauthStore.createAuthCode({
    client_id,
    redirect_uri,
    code_challenge,
    code_challenge_method,
  });

  const redirectUrl = new URL(redirect_uri);
  redirectUrl.searchParams.set("code", code);
  if (state) redirectUrl.searchParams.set("state", state);

  return NextResponse.redirect(redirectUrl.toString(), { status: 302 });
}

export async function OPTIONS() {
  return new NextResponse(null, {
    status: 204,
    headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS" },
  });
}
