import { NextRequest, NextResponse } from "next/server";
import { createSessionToken, validateSessionToken, COOKIE_NAME, COOKIE_OPTIONS } from "@/lib/session";

export async function POST(request: NextRequest) {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  const devDomain = process.env.REPLIT_DEV_DOMAIN;

  const allowedHosts: string[] = [];
  if (host) allowedHosts.push(host);
  if (devDomain) allowedHosts.push(devDomain);
  allowedHosts.push("localhost:5000", "127.0.0.1:5000");

  if (!origin) {
    return NextResponse.json({ error: "Origin required" }, { status: 403 });
  }

  try {
    const originHost = new URL(origin).host;
    if (!allowedHosts.some((h) => h === originHost)) {
      return NextResponse.json({ error: "Forbidden origin" }, { status: 403 });
    }
  } catch {
    return NextResponse.json({ error: "Invalid origin" }, { status: 403 });
  }

  const existing = request.cookies.get(COOKIE_NAME)?.value;
  if (existing && validateSessionToken(existing)) {
    return NextResponse.json({ ok: true });
  }

  const token = createSessionToken();
  const response = NextResponse.json({ ok: true });
  response.cookies.set(COOKIE_NAME, token, COOKIE_OPTIONS);
  return response;
}
