import { describe, expect, it } from 'vitest';

import type { HistoryEntry } from './api';
import { dailyCounts, lastDays } from './runDays';

const run = (executed_at: string) => ({ executed_at }) as HistoryEntry;

describe('runs by day', () => {
  it('counts the last seven days, zero-filled, ending today', () => {
    const runs = [run('2026-11-21 23:59:00'), run('2026-11-25 10:00:00'), run('2026-11-25 11:00:00')];
    expect(lastDays(runs, '2026-11-27')).toEqual([
      { date: '2026-11-21', count: 1 },
      { date: '2026-11-22', count: 0 },
      { date: '2026-11-23', count: 0 },
      { date: '2026-11-24', count: 0 },
      { date: '2026-11-25', count: 2 },
      { date: '2026-11-26', count: 0 },
      { date: '2026-11-27', count: 0 },
    ]);
  });

  it('ends on the last run’s day when that is later than today here', () => {
    const week = lastDays([run('2026-11-28 00:30:00')], '2026-11-27');
    expect(week[6]).toEqual({ date: '2026-11-28', count: 1 });
    expect(week[0]!.date).toBe('2026-11-22');
  });

  it('crosses a month end', () => {
    expect(lastDays([], '2026-12-02').map((d) => d.date)).toEqual([
      '2026-11-26',
      '2026-11-27',
      '2026-11-28',
      '2026-11-29',
      '2026-11-30',
      '2026-12-01',
      '2026-12-02',
    ]);
  });

  it('charts every day from the first run to the last', () => {
    expect(dailyCounts([run('2026-11-29 10:00:00'), run('2026-12-01 10:00:00')])).toEqual([
      { date: '2026-11-29', count: 1 },
      { date: '2026-11-30', count: 0 },
      { date: '2026-12-01', count: 1 },
    ]);
  });
});
