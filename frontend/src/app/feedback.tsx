import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

// The classic UI's three feedback surfaces, as one context: the page-wide error banner (#error-banner), toasts
// (#toasts) and the right-hand drawer (aside.drawer) that holds every form. Same markup and classes as ui.py.

export interface ErrorDetail {
  status?: number;
  errors?: Record<string, string>;
}

interface Drawer {
  title: string;
  kicker?: string;
  content: ReactNode;
}

interface Feedback {
  showError: (message: string, detail?: ErrorDetail) => void;
  toast: (message: string) => void;
  openDrawer: (drawer: Drawer) => void;
  closeDrawer: () => void;
}

const FeedbackContext = createContext<Feedback | null>(null);

export function useFeedback(): Feedback {
  const value = useContext(FeedbackContext);
  if (!value) throw new Error('useFeedback outside FeedbackProvider');
  return value;
}

interface State {
  error: { message: string; detail: ErrorDetail } | null;
  toasts: { id: number; message: string; fading: boolean }[];
  drawer: Drawer | null;
}

const StateContext = createContext<State | null>(null);

let nextToast = 1;

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [error, setError] = useState<State['error']>(null);
  const [toasts, setToasts] = useState<State['toasts']>([]);
  const [drawer, setDrawer] = useState<Drawer | null>(null);

  const showError = useCallback((message: string, detail: ErrorDetail = {}) => {
    setError(message ? { message, detail } : null);
  }, []);
  const toast = useCallback((message: string) => {
    const id = nextToast++;
    setToasts((all) => [...all, { id, message, fading: false }]);
    setTimeout(() => setToasts((all) => all.map((t) => (t.id === id ? { ...t, fading: true } : t))), 2200);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 2600);
  }, []);
  const closeDrawer = useCallback(() => setDrawer(null), []);
  const openDrawer = useCallback((next: Drawer) => setDrawer(next), []);

  const api = useMemo(
    () => ({ showError, toast, openDrawer, closeDrawer }),
    [showError, toast, openDrawer, closeDrawer],
  );
  const state = useMemo(() => ({ error, toasts, drawer }), [error, toasts, drawer]);
  return (
    <FeedbackContext.Provider value={api}>
      <StateContext.Provider value={state}>{children}</StateContext.Provider>
    </FeedbackContext.Provider>
  );
}

function useFeedbackState(): State {
  const value = useContext(StateContext);
  if (!value) throw new Error('useFeedbackState outside FeedbackProvider');
  return value;
}

export function ErrorBanner() {
  const { error } = useFeedbackState();
  const { showError } = useFeedback();
  if (!error) return <div id="error-banner" role="alert" hidden />;
  const fields = error.detail.errors ? Object.keys(error.detail.errors) : [];
  return (
    <div id="error-banner" role="alert">
      <div className="eb-main">
        {error.detail.status ? <span className="eb-code">{error.detail.status}</span> : null}
        <span className="eb-msg">{error.message}</span>
        <button type="button" className="eb-close" aria-label="Dismiss" onClick={() => showError('')}>
          ×
        </button>
      </div>
      {fields.length > 0 && (
        <ul className="eb-fields">
          {fields.map((f) => (
            <li key={f}>
              <code>{f}</code> — {error.detail.errors?.[f]}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Toasts() {
  const { toasts } = useFeedbackState();
  return (
    <div id="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className="toast" style={t.fading ? { opacity: 0 } : undefined}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

export function DrawerHost() {
  const { drawer } = useFeedbackState();
  const { closeDrawer } = useFeedback();
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => {
      // Not when something inside already used the key - e.g. Escape closing the SQL editor's completion popup.
      if (e.key === 'Escape' && !e.defaultPrevented) closeDrawer();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [drawer, closeDrawer]);
  return (
    <>
      <div className="backdrop" id="drawer-backdrop" hidden={!drawer} onClick={closeDrawer} />
      <aside
        className={drawer ? 'drawer open' : 'drawer'}
        id="drawer"
        aria-hidden={drawer ? 'false' : 'true'}
        role="dialog"
        aria-labelledby="drawer-title"
      >
        <div className="drawer-head">
          <div>
            <div className="kicker" id="drawer-kicker">
              {drawer?.kicker ?? ''}
            </div>
            <h3 id="drawer-title">{drawer?.title ?? ''}</h3>
          </div>
          <span className="spacer" />
          <button
            type="button"
            className="btn ghost icon"
            id="drawer-close"
            aria-label="Close"
            onClick={closeDrawer}
          >
            ×
          </button>
        </div>
        {/* The extra div is the classic form slot: .form's min-height:100% must not take effect, so the form's
            actions sit right after its fields, as in /ui. */}
        <div className="drawer-body">
          <div>{drawer?.content}</div>
        </div>
      </aside>
    </>
  );
}

/** Classic form pieces (ui.py field()/formActions()). */
export function Field({
  id,
  label,
  hint,
  extra,
  children,
}: {
  id?: string;
  label: ReactNode;
  hint?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {extra}
      </label>
      {children}
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

export function FormActions({ onCancel, children }: { onCancel: () => void; children: ReactNode }) {
  return (
    <div className="form-actions">
      <button type="button" className="btn" onClick={onCancel}>
        Cancel
      </button>
      {children}
    </div>
  );
}

export function Loading({ text = 'Loading…' }: { text?: string }) {
  return (
    <div className="loading">
      <span className="spin" />
      {text}
    </div>
  );
}

export function Empty({ title, children }: { title?: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {title ? <strong>{title}</strong> : null}
      {children ? <span>{children}</span> : null}
    </div>
  );
}

export function copyText(text: string, toast: (m: string) => void) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(
      () => toast('Copied'),
      () => toast('Copy failed'),
    );
  }
}
