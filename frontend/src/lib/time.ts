// Timestamps from the server are its local wall-clock time, 'YYYY-MM-DD HH:MM:SS', with no zone in them. GET /health
// says which zone that is (an IANA name where the server can tell, and its current UTC offset), so these turn such a
// string into an instant and show it in the zone the viewer picked (Settings > Appearance > Time zone).

export interface ServerZone {
  name: string | null; // e.g. 'Asia/Kolkata'; correct across daylight-saving changes
  offset: string; // e.g. '+05:30'; the fallback when there is no name - right only for times in the current period
}

export type ShowIn = 'Local' | 'UTC' | 'Server';

const WALL = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/;
const pad = (n: number) => String(n).padStart(2, '0');

/** '+05:30' -> 330 minutes east of UTC. */
export function parseOffset(text: string): number {
  const m = /^([+-])(\d{2}):(\d{2})$/.exec(text);
  if (!m) return 0;
  return (m[1] === '-' ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3]));
}

function offsetLabel(minutes: number): string {
  if (minutes === 0) return 'UTC';
  const abs = Math.abs(minutes);
  return `UTC${minutes < 0 ? '-' : '+'}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

const formatters = new Map<string, Intl.DateTimeFormat | null>();

function formatterFor(zone: string): Intl.DateTimeFormat | null {
  if (!formatters.has(zone)) {
    try {
      formatters.set(
        zone,
        new Intl.DateTimeFormat('en-US', {
          timeZone: zone,
          hourCycle: 'h23',
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        }),
      );
    } catch {
      formatters.set(zone, null); // a name this browser doesn't know
    }
  }
  return formatters.get(zone) ?? null;
}

/** The wall-clock fields of `instant` in `zone`, as if they were UTC (so Date.getUTC* reads them back). */
function wallIn(instant: number, format: Intl.DateTimeFormat): number {
  const parts: Record<string, number> = {};
  for (const p of format.formatToParts(new Date(instant))) parts[p.type] = Number(p.value);
  return Date.UTC(parts.year!, parts.month! - 1, parts.day!, parts.hour! % 24, parts.minute!, parts.second!);
}

/** The instant a server timestamp names, or null when it isn't a date and time (e.g. a date-only expiry). */
export function serverInstant(text: string, zone: ServerZone): number | null {
  const m = WALL.exec(text.trim());
  if (!m) return null;
  const wall = Date.UTC(+m[1]!, +m[2]! - 1, +m[3]!, +m[4]!, +m[5]!, +(m[6] ?? 0));
  const format = zone.name ? formatterFor(zone.name) : null;
  if (!format) return wall - parseOffset(zone.offset) * 60_000;
  // The zone's offset at that wall time: guess with the offset at `wall` read as UTC, then correct once - enough
  // everywhere but inside the hour a daylight-saving change skips or repeats.
  let guess = wall - (wallIn(wall, format) - wall);
  guess = wall - (wallIn(guess, format) - guess);
  return guess;
}

/** `instant` as 'YYYY-MM-DD HH:MM:SS' in the chosen zone, and that zone's name for a tooltip. */
export function absolute(instant: number, showIn: ShowIn, zone: ServerZone): { text: string; zone: string } {
  let wall: number;
  let label: string;
  if (showIn === 'UTC') {
    wall = instant;
    label = 'UTC';
  } else if (showIn === 'Server') {
    const format = zone.name ? formatterFor(zone.name) : null;
    wall = format ? wallIn(instant, format) : instant + parseOffset(zone.offset) * 60_000;
    label = `${zone.name ?? offsetLabel(parseOffset(zone.offset))}, the server's time zone`;
  } else {
    const minutes = -new Date(instant).getTimezoneOffset();
    wall = instant + minutes * 60_000;
    label = `${offsetLabel(minutes)}, this computer's time zone`;
  }
  const d = new Date(wall);
  const text =
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  return { text, zone: label };
}

/** '5 min ago', '3 h ago', '2 days ago' - or null past a month, where a date says more. */
export function relative(instant: number, now: number): string | null {
  const seconds = Math.round((now - instant) / 1000);
  if (seconds < 45) return 'just now'; // includes a little clock skew into the future
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 31) return `${days} days ago`;
  return null;
}
