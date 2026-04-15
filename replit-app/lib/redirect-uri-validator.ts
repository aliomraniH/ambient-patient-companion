const isProduction = process.env.NODE_ENV === "production";

export function validateRedirectUri(uri: string): {
  valid: boolean;
  reason?: string;
} {
  let parsed: URL;
  try {
    parsed = new URL(uri);
  } catch {
    return { valid: false, reason: `Invalid URL: ${uri}` };
  }

  if (parsed.hostname.includes("*")) {
    return { valid: false, reason: `Wildcard hostnames are not allowed: ${uri}` };
  }

  if (parsed.hash) {
    return { valid: false, reason: `Fragment (#) not allowed in redirect URI: ${uri}` };
  }

  if (parsed.username || parsed.password) {
    return { valid: false, reason: `Userinfo not allowed in redirect URI: ${uri}` };
  }

  if (parsed.protocol === "https:") {
    return { valid: true };
  }

  if (parsed.protocol === "http:") {
    const host = parsed.hostname;
    if (
      !isProduction &&
      (host === "localhost" || host === "127.0.0.1")
    ) {
      return { valid: true };
    }
    return {
      valid: false,
      reason: `http redirect URIs are only allowed for localhost in non-production: ${uri}`,
    };
  }

  return {
    valid: false,
    reason: `Unsupported scheme "${parsed.protocol}" in redirect URI: ${uri}`,
  };
}
