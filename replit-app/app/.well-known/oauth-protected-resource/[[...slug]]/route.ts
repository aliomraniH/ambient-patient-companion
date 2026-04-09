/**
 * GET /.well-known/oauth-protected-resource
 * GET /.well-known/oauth-protected-resource/mcp   (Claude also tries this suffix)
 *
 * RFC 9728 — OAuth 2.0 Protected Resource Metadata.
 * Tells clients where the authorisation server lives.
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
      resource: base,
      authorization_servers: [`${base}`],
      bearer_methods_supported: ["header"],
      resource_signing_alg_values_supported: ["RS256"],
      scopes_supported: ["mcp"],
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
