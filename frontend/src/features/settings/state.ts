// Which Settings section is open, kept while the page lives - as the classic tab, which only hides itself, did.
let section = 'general';

export const lastSection = () => section;

export function rememberSection(id: string) {
  section = id;
}

/** Back to the first section - for tests. */
export function resetSettingsSection() {
  section = 'general';
}
