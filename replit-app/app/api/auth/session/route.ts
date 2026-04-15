import { NextResponse } from "next/server";
import { createSessionToken, COOKIE_NAME, SESSION_TTL_MS } from "@/lib/session";

export async function POST() {
  const token = createSessionToken();

  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: Math.floor(SESSION_TTL_MS / 1000),
  });

  return response;
}
