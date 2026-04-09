/**
 * POST /register
 *
 * RFC 7591 — OAuth 2.0 Dynamic Client Registration.
 * Registers an MCP client (Claude) and returns client credentials.
 */
import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";

export async function POST(req: NextRequest) {
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_request" }, { status: 400 });
  }

  const redirect_uris = (body.redirect_uris as string[]) ?? [];
  if (!Array.isArray(redirect_uris) || redirect_uris.length === 0) {
    return NextResponse.json(
      { error: "invalid_client_metadata", error_description: "redirect_uris required" },
      { status: 400 }
    );
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
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}
