/**
 * Format a date-only value (YYYY-MM-DD or ISO date) as MM/DD/YYYY without
 * timezone shifting.  `new Date("1987-03-25").toLocaleDateString()` parses
 * the string as UTC midnight, then renders in the viewer's local timezone —
 * so a US Pacific client sees "3/24/1987" while the SSR pass (UTC) renders
 * "3/25/1987", which causes a hydration mismatch.
 *
 * This helper takes the raw YYYY-MM-DD prefix of the value and formats it
 * literally, so SSR and client render identically.
 */
export function formatDateOnly(value: string | null | undefined): string {
  if (!value) return "\u2014";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!m) return value;
  const [, y, mo, d] = m;
  return `${parseInt(mo, 10)}/${parseInt(d, 10)}/${y}`;
}
