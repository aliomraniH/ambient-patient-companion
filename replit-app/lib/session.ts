import { createHmac } from "crypto";

const SECRET = process.env.SESSION_SECRET || "dev-session-secret-change-in-production";
const TTL_MS = 24 * 60 * 60 * 1000;

export const COOKIE_NAME = "apc_session";
export const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: process.env.NODE_ENV === "production",
  sameSite: "strict" as const,
  path: "/",
  maxAge: Math.floor(TTL_MS / 1000),
};

export function createSessionToken(): string {
  const issuedMs = Date.now().toString(36);
  const hmac = createHmac("sha256", SECRET).update(issuedMs).digest("hex");
  return `${issuedMs}.${hmac}`;
}

export function validateSessionToken(token: string): boolean {
  const parts = token.split(".");
  if (parts.length !== 2) return false;
  const [issuedMs, hmac] = parts;
  const expected = createHmac("sha256", SECRET).update(issuedMs).digest("hex");
  if (hmac !== expected) return false;
  const issued = parseInt(issuedMs, 36);
  if (isNaN(issued)) return false;
  return Date.now() - issued < TTL_MS;
}
