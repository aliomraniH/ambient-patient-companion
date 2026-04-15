const COOKIE_NAME = "apc_session";
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

const SESSION_SECRET = process.env.SESSION_SECRET || "apc-dev-session-key-change-in-prod";

async function hmacSign(data: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(SESSION_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(data));
  return Array.from(new Uint8Array(sig), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacVerify(data: string, signature: string): Promise<boolean> {
  const expected = await hmacSign(data);
  return expected === signature;
}

async function createSessionToken(): Promise<string> {
  const issued = Date.now().toString(36);
  const sig = await hmacSign(issued);
  return `${issued}.${sig}`;
}

async function validateSessionToken(token: string): Promise<boolean> {
  const dotIdx = token.indexOf(".");
  if (dotIdx < 0) return false;
  const issued = token.slice(0, dotIdx);
  const sig = token.slice(dotIdx + 1);

  const valid = await hmacVerify(issued, sig);
  if (!valid) return false;

  const issuedMs = parseInt(issued, 36);
  if (isNaN(issuedMs)) return false;
  if (Date.now() - issuedMs > SESSION_TTL_MS) return false;

  return true;
}

export { COOKIE_NAME, SESSION_TTL_MS, createSessionToken, validateSessionToken };
