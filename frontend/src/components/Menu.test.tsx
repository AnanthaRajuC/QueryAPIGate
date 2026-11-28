import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { MenuButton } from './Menu';

// The ⋯ menu: a menu button that works from the keyboard (WAI-ARIA menu button pattern).

function setup() {
  const rename = vi.fn();
  const remove = vi.fn();
  render(
    <MenuButton
      label="More actions"
      entries={[
        { note: 'Granted to 2 keys' },
        'separator',
        { label: 'Rename…', onSelect: rename },
        { label: 'Hidden', hidden: true, onSelect: vi.fn() },
        { label: 'Delete…', danger: true, onSelect: remove },
      ]}
    >
      ⋯icon
    </MenuButton>,
  );
  return { button: screen.getByRole('button', { name: '⋯icon' }), rename, remove };
}

describe('MenuButton', () => {
  it('opens on ArrowDown with the first item focused, and moves with the arrows, Home and End', async () => {
    const { button } = setup();
    expect(button).toHaveAttribute('aria-haspopup', 'menu');
    button.focus();
    await userEvent.keyboard('{ArrowDown}');
    expect(button).toHaveAttribute('aria-expanded', 'true');
    const items = screen.getAllByRole('menuitem');
    expect(items.map((i) => i.textContent)).toEqual(['Granted to 2 keys', 'Rename…', 'Delete…']);
    expect(items[0]).toHaveFocus();
    expect(items[0]).toHaveAttribute('aria-disabled', 'true');
    await userEvent.keyboard('{ArrowDown}');
    expect(items[1]).toHaveFocus();
    await userEvent.keyboard('{End}');
    expect(items[2]).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}'); // wraps
    expect(items[0]).toHaveFocus();
    await userEvent.keyboard('{ArrowUp}');
    expect(items[2]).toHaveFocus();
    await userEvent.keyboard('{Home}');
    expect(items[0]).toHaveFocus();
  });

  it('closes on Escape and gives focus back to its button', async () => {
    const { button } = setup();
    await userEvent.click(button);
    expect(screen.getByRole('menu', { name: 'More actions' })).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).toBeNull();
    expect(button).toHaveFocus();
    expect(button).toHaveAttribute('aria-expanded', 'false');
  });

  it('runs a chosen item once, closing the menu; a note does nothing', async () => {
    const { button, rename, remove } = setup();
    await userEvent.click(button);
    await userEvent.click(screen.getByRole('menuitem', { name: 'Granted to 2 keys' }));
    expect(screen.getByRole('menu')).toBeInTheDocument();
    await userEvent.keyboard('{ArrowDown}{ArrowDown}{Enter}');
    expect(remove).toHaveBeenCalledTimes(1);
    expect(rename).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).toBeNull();
    expect(button).toHaveFocus();
  });

  it('closes on a press outside it', async () => {
    const { button } = setup();
    await userEvent.click(button);
    await userEvent.click(document.body);
    expect(screen.queryByRole('menu')).toBeNull();
  });
});
