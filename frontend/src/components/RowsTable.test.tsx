import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { setPref } from '@/app/prefs';
import { grouped } from '@/lib/numbers';

import { RowsTable } from './Results';

afterEach(() => localStorage.clear());

const rows = [
  { id: 1234567, price: 1234.5, ratio: 0.30000000000000004, note: null },
  { id: 2, price: -98765.25, ratio: 1e21, note: 'x' },
];

describe('result grid preferences (Settings > Editor & results)', () => {
  it('shows numbers plain and NULL as NULL by default', () => {
    render(<RowsTable rows={rows} />);
    expect(screen.getByText('1234567')).toBeInTheDocument();
    expect(screen.getByText('NULL')).toHaveClass('null');
  });

  it('groups thousands and blanks NULLs when asked, leaving the value itself alone', () => {
    setPref('numberFormat', 'Grouped');
    setPref('nullDisplay', 'Blank');
    const { container } = render(<RowsTable rows={rows} />);
    const cells = [...container.querySelectorAll('tbody tr:first-child td')].map((td) => td.textContent);
    expect(cells).toEqual(['1', '1,234,567', '1,234.5', '0.30000000000000004', '']);
    expect(screen.queryByText('NULL')).toBeNull();
  });

  it('groups only the whole part, exactly as the number prints', () => {
    expect(grouped(-98765.25)).toBe('-98,765.25');
    expect(grouped(999)).toBe('999');
    expect(grouped(1e21)).toBe('1e+21');
    expect(grouped(NaN)).toBe('NaN');
  });
});
