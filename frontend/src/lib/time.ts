// Tiny relative-time helper that uses the browser's built-in
// `Intl.RelativeTimeFormat`. Avoids pulling moment.js/date-fns for a single
// cell in a table.

const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

const UNITS: ReadonlyArray<{ name: Intl.RelativeTimeFormatUnit; ms: number }> = [
  { name: "year", ms: 1000 * 60 * 60 * 24 * 365 },
  { name: "month", ms: 1000 * 60 * 60 * 24 * 30 },
  { name: "week", ms: 1000 * 60 * 60 * 24 * 7 },
  { name: "day", ms: 1000 * 60 * 60 * 24 },
  { name: "hour", ms: 1000 * 60 * 60 },
  { name: "minute", ms: 1000 * 60 },
  { name: "second", ms: 1000 },
];

/** Format `when` as a relative time ("3 minutes ago"). `when` may be a
 *  Date, ISO string, or epoch-ms number. */
export function relativeTimeFrom(when: Date | string | number): string {
  const then = typeof when === "string" || typeof when === "number"
    ? new Date(when)
    : when;
  const diffMs = then.getTime() - Date.now();
  const absMs = Math.abs(diffMs);
  for (const unit of UNITS) {
    if (absMs >= unit.ms || unit.name === "second") {
      const value = Math.round(diffMs / unit.ms);
      return RTF.format(value, unit.name);
    }
  }
  return RTF.format(0, "second");
}
