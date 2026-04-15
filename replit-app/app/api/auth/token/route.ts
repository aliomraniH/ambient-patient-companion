import { NextRequest, NextResponse } from "next/server";
import { oauthStore, verifyPkceS256 } from "@/lib/oauth-store";
import { createHash } from "crypto";
import { checkRateLimit } from "@/lib/rate-limiter";

function base64UrlEncode(buffer: Buffer): string {
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const referer = request.headers.get("referer");
  const host = request.headers.get("host");
  const devDomain = process.env.REPLIT_DEV_DOMAIN;

  if (!origin && !referer) {
    return false;
  }

  const allowedHosts: string[] = [];
  if (host) allowedHosts.push(host);
  if (devDomain) allowedHosts.push(devDomain);
  allowedHosts.push("localhost:5000", "127.0.0.1:5000");

  if (origin) {
    try {
      const originHost = new URL(origin).host;
      return allowedHosts.some((h) => h === originHost);
    } catch {
      return false;
    }
  }

  if (referer) {
    try {
      const refererHost = new URL(referer).host;
      return allowedHosts.some((h) => h === refererHost);
    } catch {
      return false;
    }
  }

  return false;
}

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return NextResponse.json(
      { error: "forbidden", error_description: "Cross-origin requests not allowed" },
      { status: 403 }
    );
  }

  const ip =
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    "unknown";
  const limited = checkRateLimit(ip, "auth-token", 5, 60_000);
  if (limited) {
    return NextResponse.json(
      { error: "rate_limited", retry_after: 60 },
      { status: 429, headers: { "Retry-After": "60" } }
    );
  }

  const redirectUri = "urn:ietf:wg:oauth:2.0:oob";

  const client = oauthStore.registerClient({
    redirect_uris: [redirectUri],
    client_name: "Dashboard-Auto",
  });

  const verifierBytes = new Uint8Array(32);
  crypto.getRandomValues(verifierBytes);
  const verifier = base64UrlEncode(Buffer.from(verifierBytes));

  const challengeHash = createHash("sha256").update(verifier).digest();
  const challenge = base64UrlEncode(challengeHash);

  const code = oauthStore.createAuthCode({
    client_id: client.client_id,
    redirect_uri: redirectUri,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });

  const authCode = oauthStore.consumeAuthCode(code);
  if (!authCode) {
    return NextResponse.json({ error: "auth_code_failed" }, { status: 500 });
  }

  if (!verifyPkceS256(verifier, authCode.code_challenge!)) {
    return NextResponse.json({ error: "pkce_failed" }, { status: 500 });
  }

  const accessToken = oauthStore.createAccessToken(client.client_id);

  return NextResponse.json({
    access_token: accessToken,
    token_type: "Bearer",
    expires_in: 86400,
  });
}
