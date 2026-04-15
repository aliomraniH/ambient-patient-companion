import { NextRequest, NextResponse } from "next/server";
import { oauthStore } from "@/lib/oauth-store";
import { validateSessionToken, COOKIE_NAME } from "@/lib/session";

export function requireBearerToken(request: NextRequest): NextResponse | null {
  const authHeader = request.headers.get("authorization");
  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return NextResponse.json(
      { error: "unauthorized", error_description: "Bearer token required" },
      { status: 401 }
    );
  }

  const token = authHeader.slice(7);
  if (!oauthStore.validateToken(token)) {
    return NextResponse.json(
      { error: "invalid_token", error_description: "Token is invalid or expired" },
      { status: 401 }
    );
  }

  return null;
}

export async function requireAuth(request: NextRequest): Promise<NextResponse | null> {
  const authHeader = request.headers.get("authorization");
  if (authHeader && authHeader.startsWith("Bearer ")) {
    const token = authHeader.slice(7);
    if (oauthStore.validateToken(token)) {
      return null;
    }
    return NextResponse.json(
      { error: "invalid_token", error_description: "Token is invalid or expired" },
      { status: 401 }
    );
  }

  const sessionCookie = request.cookies.get(COOKIE_NAME)?.value;
  if (sessionCookie && (await validateSessionToken(sessionCookie))) {
    return null;
  }

  return NextResponse.json(
    { error: "unauthorized", error_description: "Bearer token or valid session required" },
    { status: 401 }
  );
}
