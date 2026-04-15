const windows = new Map<string, number[]>();

const CLEANUP_INTERVAL = 60_000;
let lastCleanup = Date.now();

function cleanup(windowMs: number) {
  const now = Date.now();
  if (now - lastCleanup < CLEANUP_INTERVAL) return;
  lastCleanup = now;
  const cutoff = now - windowMs;
  for (const [key, timestamps] of windows) {
    const filtered = timestamps.filter((t) => t > cutoff);
    if (filtered.length === 0) {
      windows.delete(key);
    } else {
      windows.set(key, filtered);
    }
  }
}

export function checkRateLimit(
  ip: string,
  route: string,
  maxRequests: number,
  windowMs: number = 60_000
): boolean {
  cleanup(windowMs);

  const key = `${route}:${ip}`;
  const now = Date.now();
  const cutoff = now - windowMs;

  const timestamps = windows.get(key) ?? [];
  const recent = timestamps.filter((t) => t > cutoff);
  recent.push(now);
  windows.set(key, recent);

  return recent.length > maxRequests;
}
