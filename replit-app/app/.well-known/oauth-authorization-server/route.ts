/**
 * GET /.well-known/oauth-authorization-server
 *
 * RFC 8414 — OAuth 2.0 Authorization Server Metadata.
 * Describes the token/authorize/register endpoints so Claude can run the
 * OAuth flow against this public MCP server.
 */
import { NextResponse } from "next/server";

function getBaseUrl(): string {
  const domain = process.env.REPLIT_DEV_DOMAIN;
  if (domain) return `https://${domain}`;
  return process.env.NEXTAUTH_URL ?? "http://localhost:5000";
}

export async function GET() {
  const base = getBaseUrl();
  return NextResponse.json(
    {
      issuer: base,
      authorization_endpoint: `${base}/authorize`,
      token_endpoint: `${base}/token`,
      registration_endpoint: `${base}/register`,
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code"],
      code_challenge_methods_supported: ["S256"],
      token_endpoint_auth_methods_supported: ["none"],
      scopes_supported: ["mcp"],
      subject_types_supported: ["public"],
    },
    {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
      },
    }
  );
}

export async function OPTIONS() {
  return new NextResponse(null, {
    status: 204,
    headers: { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS" },
  });
}
