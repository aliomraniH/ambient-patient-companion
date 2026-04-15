import { createHash, randomBytes } from "crypto";

const SESSION_SECRET = randomBytes(32).toString("hex");

const COOKIE_NAME = "apc_session";
const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

const sessions = new Map<string, { expires_at: number }>();

export function createSessionToken(): string {
  const raw = randomBytes(32).toString("hex");
  const token = createHash("sha256")
    .update(raw + SESSION_SECRET)
    .digest("hex");
  sessions.set(token, { expires_at: Date.now() + SESSION_TTL_MS });
  return token;
}

export function validateSessionToken(token: string): boolean {
  const entry = sessions.get(token);
  if (!entry) return false;
  if (Date.now() > entry.expires_at) {
    sessions.delete(token);
    return false;
  }
  return true;
}

export { COOKIE_NAME, SESSION_TTL_MS };
