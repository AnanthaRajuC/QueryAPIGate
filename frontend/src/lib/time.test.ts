import { describe, expect, it } from 'vitest';

import { absolute, parseOffset, relative, serverInstant, type ServerZone } from './time';

const KOLKATA: ServerZone = { name: 'Asia/Kolkata', offset: '+05:30' };
const NEW_YORK: ServerZone = { name: 'America/New_York', offset: '-04:00' }; // offset as seen in summer

describe('server timestamps', () => {
  it('reads a wall-clock time in the server zone as the instant it names', () => {
    expect(new Date(serverInstant('2026-10-04 10:36:59', KOLKATA)!).toISOString()).toBe(
      '2026-10-04T05:06:59.000Z',
    );
  });

  it("uses the zone's own rules across daylight saving, not today's offset", () => {
    // New York is UTC-4 in July and UTC-5 in January
    expect(new Date(serverInstant('2026-07-01 12:00:00', NEW_YORK)!).toISOString()).toBe(
      '2026-07-01T16:00:00.000Z',
    );
    expect(new Date(serverInstant('2026-01-15 12:00:00', NEW_YORK)!).toISOString()).toBe(
      '2026-01-15T17:00:00.000Z',
    );
  });

  it('falls back to the offset when the server could not name its zone', () => {
    const zone = { name: null, offset: '-05:45' };
    expect(new Date(serverInstant('2026-10-04 00:00:00', zone)!).toISOString()).toBe(
      '2026-10-04T05:45:00.000Z',
    );
    expect(
      new Date(serverInstant('2026-10-04 00:00', { name: 'Not/AZone', offset: '+01:00' })!).toISOString(),
    ).toBe('2026-10-03T23:00:00.000Z');
  });

  it('leaves alone what is not a date and time', () => {
    expect(serverInstant('2026-10-04', KOLKATA)).toBeNull();
    expect(serverInstant('never', KOLKATA)).toBeNull();
  });

  it('shows an instant in UTC, the server zone, or this computer (UTC under test)', () => {
    const at = serverInstant('2026-10-04 10:36:59', KOLKATA)!;
    expect(absolute(at, 'UTC', KOLKATA)).toEqual({ text: '2026-10-04 05:06:59', zone: 'UTC' });
    expect(absolute(at, 'Server', KOLKATA).text).toBe('2026-10-04 10:36:59');
    expect(absolute(at, 'Server', KOLKATA).zone).toContain('Asia/Kolkata');
    expect(absolute(at, 'Server', { name: null, offset: '+05:30' }).zone).toContain('UTC+05:30');
    expect(absolute(at, 'Local', KOLKATA).text).toBe('2026-10-04 05:06:59');
    expect(absolute(at, 'Local', KOLKATA).zone).toContain('this computer');
  });

  it('says how long ago, up to a month', () => {
    const now = Date.UTC(2026, 9, 4, 12, 0, 0);
    const ago = (seconds: number) => relative(now - seconds * 1000, now);
    expect(ago(10)).toBe('just now');
    expect(ago(-20)).toBe('just now');
    expect(ago(5 * 60)).toBe('5 min ago');
    expect(ago(3 * 3600)).toBe('3 h ago');
    expect(ago(30 * 3600)).toBe('yesterday');
    expect(ago(5 * 86400)).toBe('5 days ago');
    expect(ago(40 * 86400)).toBeNull();
  });

  it('parses offsets', () => {
    expect(parseOffset('+05:30')).toBe(330);
    expect(parseOffset('-03:30')).toBe(-210);
    expect(parseOffset('junk')).toBe(0);
  });
});
