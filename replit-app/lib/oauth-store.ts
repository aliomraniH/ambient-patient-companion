import { createHash } from "crypto";

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

const clients = new Map<string, OAuthClient>();
const authCodes = new Map<string, AuthCode>();
const accessTokens = new Map<string, AccessToken>();

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

export const oauthStore = {
  registerClient(data: {
    redirect_uris: string[];
    client_name?: string;
  }): OAuthClient {
    const client: OAuthClient = {
      client_id: `mcp-${randomHex(12)}`,
      client_secret: randomHex(32),
      redirect_uris: data.redirect_uris,
      client_name: data.client_name,
      created_at: Date.now(),
    };
    clients.set(client.client_id, client);
    return client;
  },

  getClient(client_id: string): OAuthClient | undefined {
    return clients.get(client_id);
  },

  createAuthCode(data: {
    client_id: string;
    redirect_uri: string;
    code_challenge?: string;
    code_challenge_method?: string;
  }): string {
    const code = randomHex(24);
    authCodes.set(code, {
      code,
      client_id: data.client_id,
      redirect_uri: data.redirect_uri,
      code_challenge: data.code_challenge,
      code_challenge_method: data.code_challenge_method,
      expires_at: Date.now() + 5 * 60 * 1000,
      used: false,
    });
    return code;
  },

  consumeAuthCode(code: string): AuthCode | null {
    const entry = authCodes.get(code);
    if (!entry) return null;
    if (entry.used) return null;
    if (Date.now() > entry.expires_at) {
      authCodes.delete(code);
      return null;
    }
    entry.used = true;
    return entry;
  },

  createAccessToken(client_id: string): string {
    const token = randomHex(40);
    accessTokens.set(token, {
      token,
      client_id,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
    });
    return token;
  },

  validateToken(token: string): boolean {
    const entry = accessTokens.get(token);
    if (!entry) return false;
    if (Date.now() > entry.expires_at) {
      accessTokens.delete(token);
      return false;
    }
    return true;
  },
};
