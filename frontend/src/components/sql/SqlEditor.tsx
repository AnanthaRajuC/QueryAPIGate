import { autocompletion } from '@codemirror/autocomplete';
import { MSSQL, MySQL, PostgreSQL, SQLite, StandardSQL, sql, type SQLDialect } from '@codemirror/lang-sql';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { Compartment, EditorState } from '@codemirror/state';
import { EditorView, placeholder as placeholderText } from '@codemirror/view';
import { tags } from '@lezer/highlight';
import { basicSetup } from 'codemirror';
import { useEffect, useRef } from 'react';

// CodeMirror 6 (ADR 0001's editor spike: a fraction of Monaco's size, built-in dialect- and schema-aware SQL
// completion). Colours come from the Console's design tokens, so it follows light and dark mode.

const DIALECTS: Record<string, SQLDialect> = {
  postgres: PostgreSQL,
  mysql: MySQL,
  sqlite: SQLite,
  mssql: MSSQL,
  duckdb: PostgreSQL, // DuckDB's SQL is PostgreSQL-flavoured
};

const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.operatorKeyword, tags.typeName], color: 'var(--color-syn-kw)' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--color-syn-str)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--color-syn-num)' },
  { tag: [tags.special(tags.name), tags.variableName], color: 'var(--color-syn-param)' },
  {
    tag: [tags.comment, tags.lineComment, tags.blockComment],
    color: 'var(--color-ink-3)',
    fontStyle: 'italic',
  },
]);

const theme = EditorView.theme({
  '&': {
    backgroundColor: 'var(--color-surface)',
    color: 'var(--color-ink)',
    fontSize: '13px',
    border: '1px solid var(--color-line-strong)',
    borderRadius: '6px',
  },
  '&.cm-focused': { outline: '2px solid var(--color-accent)', outlineOffset: '-1px' },
  '.cm-content': { fontFamily: 'var(--font-mono)', caretColor: 'var(--color-ink)' },
  '.cm-gutters': {
    backgroundColor: 'var(--color-surface-2)',
    color: 'var(--color-ink-3)',
    border: 'none',
    borderRadius: '6px 0 0 6px',
  },
  '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'var(--color-accent-soft)' },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
    backgroundColor: 'var(--color-accent-soft) !important',
  },
  '.cm-tooltip': {
    backgroundColor: 'var(--color-surface)',
    border: '1px solid var(--color-line-strong)',
    color: 'var(--color-ink)',
  },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
    backgroundColor: 'var(--color-accent-soft)',
    color: 'var(--color-ink)',
  },
});

export interface SqlEditorProps {
  value: string;
  onChange?: (value: string) => void;
  /** The connection's `db` type (postgres, mysql, ...) - picks the SQL dialect for highlighting and completion. */
  dialect?: string | null;
  /** table -> column names, for completion. */
  schema?: Record<string, string[]>;
  readOnly?: boolean;
  label: string;
  placeholder?: string;
  minHeight?: string;
}

function language(dialect: string | null | undefined, schema: Record<string, string[]> | undefined) {
  return sql({ dialect: (dialect && DIALECTS[dialect]) || StandardSQL, schema, upperCaseKeywords: true });
}

export function SqlEditor({
  value,
  onChange,
  dialect,
  schema,
  readOnly = false,
  label,
  placeholder,
  minHeight = '12rem',
}: SqlEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const languageSlot = useRef(new Compartment());
  const readOnlySlot = useRef(new Compartment());
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    if (!host.current) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          autocompletion({ activateOnTyping: true }),
          languageSlot.current.of(language(dialect, schema)),
          readOnlySlot.current.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          syntaxHighlighting(highlight),
          EditorView.lineWrapping,
          theme,
          EditorView.theme({ '.cm-content, .cm-gutter': { minHeight } }),
          EditorView.contentAttributes.of({ 'aria-label': label }),
          ...(placeholder ? [placeholderText(placeholder)] : []),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current?.(update.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = editor;
    return () => {
      editor.destroy();
      view.current = null;
    };
    // Created once; later prop changes are applied by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    view.current?.dispatch({ effects: languageSlot.current.reconfigure(language(dialect, schema)) });
  }, [dialect, schema]);

  useEffect(() => {
    view.current?.dispatch({
      effects: readOnlySlot.current.reconfigure([
        EditorState.readOnly.of(readOnly),
        EditorView.editable.of(!readOnly),
      ]),
    });
  }, [readOnly]);

  // Replace the document when the value changes from outside (e.g. a version loaded), never while typing.
  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
    }
  }, [value]);

  return <div ref={host} data-testid="sql-editor" />;
}
