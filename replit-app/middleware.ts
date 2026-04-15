import { NextRequest, NextResponse } from "next/server";
import { createSessionToken, validateSessionToken, COOKIE_NAME, SESSION_TTL_MS } from "@/lib/session";

export async function middleware(request: NextRequest) {
  const existing = request.cookies.get(COOKIE_NAME)?.value;
  if (existing && (await validateSessionToken(existing))) {
    return NextResponse.next();
  }

  const token = await createSessionToken();
  const response = NextResponse.next();
  response.cookies.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: Math.floor(SESSION_TTL_MS / 1000),
  });

  return response;
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|api/|register|authorize|token|\\.well-known/).*)",
  ],
};
