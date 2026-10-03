import { useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';

import { useCatalog } from '@/api/queries';
import { getApiKey, setApiKey } from '@/auth/apiKey';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

/** Header control: enter an API key, see who it signs you in as, or sign out. */
export function SignIn() {
  const queryClient = useQueryClient();
  const [key, setKey] = useState(getApiKey);
  const [draft, setDraft] = useState('');
  const catalog = useCatalog(key);

  function apply(value: string) {
    setApiKey(value);
    setKey(value);
    queryClient.invalidateQueries();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    apply(draft.trim());
    setDraft('');
  }

  if (key && catalog.isSuccess) {
    const caller = catalog.data.caller;
    return (
      <div className="flex items-center gap-3 text-sm">
        <span>
          Signed in as <strong>{caller?.name ?? 'admin'}</strong>
          {caller?.admin && <span className="text-ink-3"> (admin)</span>}
        </span>
        <Button variant="outline" size="sm" onClick={() => apply('')}>
          Sign out
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="flex items-center gap-2" aria-label="Sign in">
      {key && catalog.isError && (
        <span role="alert" className="text-xs text-danger">
          That key was refused.
        </span>
      )}
      <Input
        type="password"
        autoComplete="off"
        placeholder="API key"
        aria-label="API key"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        className="w-48"
      />
      <Button type="submit" size="sm" disabled={!draft.trim()}>
        Sign in
      </Button>
    </form>
  );
}
