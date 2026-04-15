import { NextResponse } from "next/server";
import { oauthStore, verifyPkceS256 } from "@/lib/oauth-store";
import { createHash } from "crypto";

function base64UrlEncode(buffer: Buffer): string {
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export async function POST() {
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
