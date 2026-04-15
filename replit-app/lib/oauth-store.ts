import { createHash } from "crypto";
import { readFile } from "fs/promises";
import pg from "pg";

export interface OAuthClient {
  client_id: string;
  client_secret: string;
  redirect_uris: string[];
  client_name?: string;
  created_at: number;
}

export interface AuthCode {
  code: string;
  client_id: string;
  redirect_uri: string;
  code_challenge?: string;
  code_challenge_method?: string;
  expires_at: number;
  used: boolean;
}

export interface AccessToken {
  token: string;
  client_id: string;
  expires_at: number;
}

function randomHex(bytes = 32): string {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return Array.from(arr, (b) => b.toString(16).padStart(2, "0")).join("");
}

function base64UrlEncode(buffer: Buffer): string {
  return buffer
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export function verifyPkceS256(
  codeVerifier: string,
  codeChallenge: string
): boolean {
  const hash = createHash("sha256").update(codeVerifier).digest();
  const computed = base64UrlEncode(hash);
  return computed === codeChallenge;
}

let _pool: pg.Pool | null = null;
let _initPromise: Promise<void> | null = null;

async function ensureSchema(pool: pg.Pool): Promise<void> {
  const sqlUrl = new URL("../db/migrations/001_oauth.sql", import.meta.url);
  const sql = await readFile(sqlUrl, "utf8");
  await pool.query(sql);
}

function getPool(): pg.Pool {
  if (!_pool) {
    _pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
    _initPromise = ensureSchema(_pool).catch((err) => {
      console.error("oauth schema init failed:", err);
    });
  }
  return _pool;
}

async function withReadyPool<T>(fn: (p: pg.Pool) => Promise<T>): Promise<T> {
  const pool = getPool();
  if (_initPromise) await _initPromise;
  return fn(pool);
}

export const oauthStore = {
  async registerClient(data: {
    redirect_uris: string[];
    client_name?: string;
  }): Promise<OAuthClient> {
    const client: OAuthClient = {
      client_id: `mcp-${randomHex(12)}`,
      client_secret: randomHex(32),
      redirect_uris: data.redirect_uris,
      client_name: data.client_name,
      created_at: Date.now(),
    };
    await withReadyPool((p) =>
      p.query(
        `INSERT INTO oauth_clients (client_id, client_secret, redirect_uris, client_name, created_at)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (client_id) DO NOTHING`,
        [
          client.client_id,
          client.client_secret,
          JSON.stringify(client.redirect_uris),
          client.client_name ?? null,
          client.created_at,
        ]
      )
    );
    return client;
  },

  async getClient(client_id: string): Promise<OAuthClient | undefined> {
    const res = await withReadyPool((p) =>
      p.query(
        `SELECT client_id, client_secret, redirect_uris, client_name, created_at
         FROM oauth_clients WHERE client_id = $1`,
        [client_id]
      )
    );
    if (res.rows.length === 0) return undefined;
    const row = res.rows[0];
    return {
      client_id: row.client_id,
      client_secret: row.client_secret,
      redirect_uris:
        typeof row.redirect_uris === "string"
          ? JSON.parse(row.redirect_uris)
          : row.redirect_uris,
      client_name: row.client_name ?? undefined,
      created_at: Number(row.created_at),
    };
  },

  async createAuthCode(data: {
    client_id: string;
    redirect_uri: string;
    code_challenge?: string;
    code_challenge_method?: string;
  }): Promise<string> {
    const code = randomHex(24);
    const expiresAt = Date.now() + 5 * 60 * 1000;
    await withReadyPool((p) =>
      p.query(
        `INSERT INTO oauth_auth_codes (code, client_id, redirect_uri, code_challenge, code_challenge_method, expires_at, used)
         VALUES ($1, $2, $3, $4, $5, $6, FALSE)`,
        [
          code,
          data.client_id,
          data.redirect_uri,
          data.code_challenge ?? null,
          data.code_challenge_method ?? null,
          expiresAt,
        ]
      )
    );
    return code;
  },

  async consumeAuthCode(code: string): Promise<AuthCode | null> {
    const res = await withReadyPool((p) =>
      p.query(
        `UPDATE oauth_auth_codes
         SET used = TRUE
         WHERE code = $1 AND used = FALSE AND expires_at > $2
         RETURNING code, client_id, redirect_uri, code_challenge, code_challenge_method, expires_at, used`,
        [code, Date.now()]
      )
    );
    if (res.rows.length === 0) return null;
    const row = res.rows[0];
    return {
      code: row.code,
      client_id: row.client_id,
      redirect_uri: row.redirect_uri,
      code_challenge: row.code_challenge ?? undefined,
      code_challenge_method: row.code_challenge_method ?? undefined,
      expires_at: Number(row.expires_at),
      used: true,
    };
  },

  async createAccessToken(client_id: string): Promise<string> {
    const token = randomHex(40);
    const expiresAt = Date.now() + 24 * 60 * 60 * 1000;
    await withReadyPool((p) =>
      p.query(
        `INSERT INTO oauth_access_tokens (token, client_id, expires_at)
         VALUES ($1, $2, $3)`,
        [token, client_id, expiresAt]
      )
    );
    return token;
  },

  async validateToken(token: string): Promise<boolean> {
    const res = await withReadyPool((p) =>
      p.query(
        `SELECT 1 FROM oauth_access_tokens WHERE token = $1 AND expires_at > $2`,
        [token, Date.now()]
      )
    );
    return res.rows.length > 0;
  },
};
