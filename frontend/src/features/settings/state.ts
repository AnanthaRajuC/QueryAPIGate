// Which Settings tab was open last, kept while the page lives - /settings opens it again, as the classic screen kept
// its section. A tab has its own link too (/settings/<tab>).
let tab = 'general';

export const lastTab = () => tab;

export function rememberTab(id: string) {
  tab = id;
}

/** Back to the first tab - for tests. */
export function resetSettingsTab() {
  tab = 'general';
}
