const devDomain = process.env.REPLIT_DEV_DOMAIN ?? "";

function buildAllowlist(): Set<string> {
  const origins = new Set<string>();

  if (devDomain) {
    origins.add(`https://${devDomain}`);
  }

  origins.add("http://localhost:3000");
  origins.add("http://localhost:5000");
  origins.add("http://127.0.0.1:3000");
  origins.add("http://127.0.0.1:5000");

  const extra = process.env.CORS_ALLOWED_ORIGINS ?? "";
  if (extra) {
    for (const o of extra.split(",")) {
      const trimmed = o.trim();
      if (trimmed) origins.add(trimmed);
    }
  }

  return origins;
}

const allowlist = buildAllowlist();

export function getAllowedOrigin(requestOrigin: string | null): string | null {
  if (!requestOrigin) return null;
  if (allowlist.has(requestOrigin)) return requestOrigin;
  return null;
}

export function corsHeaders(requestOrigin: string | null): Record<string, string> {
  const origin = getAllowedOrigin(requestOrigin);
  const headers: Record<string, string> = {
    "Cache-Control": "no-store",
    Pragma: "no-cache",
  };
  if (origin) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Vary"] = "Origin";
  }
  return headers;
}

export function corsPreflightHeaders(
  requestOrigin: string | null,
  methods: string,
  allowHeaders: string
): Record<string, string> {
  const origin = getAllowedOrigin(requestOrigin);
  const headers: Record<string, string> = {};
  if (origin) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Methods"] = methods;
    headers["Access-Control-Allow-Headers"] = allowHeaders;
    headers["Vary"] = "Origin";
  }
  return headers;
}

export function openCorsHeaders(requestOrigin: string | null): Record<string, string> {
  const headers: Record<string, string> = {
    "Cache-Control": "no-store",
    Pragma: "no-cache",
  };
  if (requestOrigin) {
    headers["Access-Control-Allow-Origin"] = requestOrigin;
    headers["Vary"] = "Origin";
  } else {
    headers["Access-Control-Allow-Origin"] = "*";
  }
  return headers;
}

export function openCorsPreflightHeaders(
  requestOrigin: string | null,
  methods: string,
  allowHeaders: string
): Record<string, string> {
  const headers: Record<string, string> = {};
  if (requestOrigin) {
    headers["Access-Control-Allow-Origin"] = requestOrigin;
    headers["Vary"] = "Origin";
  } else {
    headers["Access-Control-Allow-Origin"] = "*";
  }
  headers["Access-Control-Allow-Methods"] = methods;
  headers["Access-Control-Allow-Headers"] = allowHeaders;
  return headers;
}
