import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";

function getResourceMetadataUrl(): string {
  const domain = process.env.REPLIT_DEV_DOMAIN;
  const base = domain ? `https://${domain}` : "http://localhost:5000";
  return `${base}/.well-known/oauth-protected-resource`;
}

export async function requireBearerToken(request: NextRequest): Promise<NextResponse | null> {
  const authHeader = request.headers.get("authorization");
  const wwwAuth = `Bearer resource_metadata="${getResourceMetadataUrl()}"`;

  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return NextResponse.json(
      { error: "unauthorized", error_description: "Bearer token required" },
      {
        status: 401,
        headers: { "WWW-Authenticate": wwwAuth },
      }
    );
  }

  const token = authHeader.slice(7);
  const valid = await oauthStore.validateToken(token);
  if (!valid) {
    return NextResponse.json(
      { error: "invalid_token", error_description: "Token is invalid or expired" },
      {
        status: 401,
        headers: { "WWW-Authenticate": wwwAuth },
      }
    );
  }

  return null;
}
