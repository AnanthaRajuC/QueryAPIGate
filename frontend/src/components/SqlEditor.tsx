import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
} from '@codemirror/autocomplete';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { MSSQL, MySQL, PostgreSQL, SQLite, StandardSQL, sql, type SQLDialect } from '@codemirror/lang-sql';
import { bracketMatching, HighlightStyle, indentOnInput, syntaxHighlighting } from '@codemirror/language';
import { Compartment, EditorState } from '@codemirror/state';
import {
  drawSelection,
  EditorView,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  placeholder as placeholderText,
} from '@codemirror/view';
import { tags } from '@lezer/highlight';
import { useEffect, useImperativeHandle, useRef, useState, type Ref } from 'react';

// CodeMirror 6 dressed as the classic editor (.editor.boxed, console.css): the classic syntax colours, gutter and
// focus ring, plus completion that knows the connection's dialect, tables and columns.

const DIALECTS: Record<string, SQLDialect> = {
  postgres: PostgreSQL,
  mysql: MySQL,
  sqlite: SQLite,
  mssql: MSSQL,
  duckdb: PostgreSQL,
};

// The classic .k/.s/.n/.p/.c token styles (classic.css).
const highlight = HighlightStyle.define([
  { tag: [tags.keyword, tags.operatorKeyword], color: 'var(--syn-kw)', fontWeight: '600' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--syn-str)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--syn-num)' },
  { tag: [tags.special(tags.name)], color: 'var(--syn-param)', fontWeight: '600' },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: 'var(--syn-cmt)', fontStyle: 'italic' },
]);

/** Lets the schema browser insert a name at the cursor, or replace the whole query with a starter one, and the
 * API Designer read the selection a double-click made and rewrite a range ("Parameterize"). */
export interface SqlEditorHandle {
  insert: (text: string) => void;
  replace: (text: string) => void;
  selection: () => { from: number; to: number; doc: string };
  replaceRange: (from: number, to: number, text: string) => void;
}

export interface SqlEditorProps {
  handle?: Ref<SqlEditorHandle>;
  id?: string;
  value: string;
  onChange?: (value: string) => void;
  dialect?: string | null;
  schema?: Record<string, string[]>;
  label?: string;
  placeholder?: string;
  /** false for the API Designer's plain .editor (no border of its own, it sits inside the runner panel). */
  boxed?: boolean;
  /** Ctrl/Cmd+Enter. */
  onRun?: () => void;
  onDoubleClick?: (event: MouseEvent) => void;
}

function language(dialect: string | null | undefined, schema: Record<string, string[]> | undefined) {
  return sql({ dialect: (dialect && DIALECTS[dialect]) || StandardSQL, schema, upperCaseKeywords: true });
}

export function SqlEditor({
  handle,
  id,
  value,
  onChange,
  dialect,
  schema,
  label,
  placeholder,
  boxed = true,
  onRun,
  onDoubleClick,
}: SqlEditorProps) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const [languageSlot] = useState(() => new Compartment());
  const onChangeRef = useRef(onChange);
  const onRunRef = useRef(onRun);
  const onDoubleClickRef = useRef(onDoubleClick);
  useEffect(() => {
    onChangeRef.current = onChange;
    onRunRef.current = onRun;
    onDoubleClickRef.current = onDoubleClick;
  }, [onChange, onRun, onDoubleClick]);
  useImperativeHandle(handle, () => ({
    insert(text: string) {
      const editor = view.current;
      if (!editor) return;
      const { from, to } = editor.state.selection.main;
      editor.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } });
      editor.focus();
    },
    replace(text: string) {
      const editor = view.current;
      if (!editor) return;
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: text } });
      editor.focus();
    },
    selection() {
      const editor = view.current;
      if (!editor) return { from: 0, to: 0, doc: '' };
      const { from, to } = editor.state.selection.main;
      return { from, to, doc: editor.state.doc.toString() };
    },
    replaceRange(from: number, to: number, text: string) {
      const editor = view.current;
      if (!editor) return;
      editor.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } });
      editor.focus();
    },
  }));

  useEffect(() => {
    if (!host.current) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          // basicSetup minus the fold gutter, active-line highlight and search panel the classic editor never had
          lineNumbers(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          indentOnInput(),
          bracketMatching(),
          closeBrackets(),
          keymap.of([
            {
              key: 'Mod-Enter',
              run: () => {
                if (!onRunRef.current) return false;
                onRunRef.current();
                return true;
              },
            },
            ...closeBracketsKeymap,
            ...defaultKeymap,
            ...historyKeymap,
            ...completionKeymap,
            indentWithTab,
          ]),
          EditorView.domEventHandlers({
            dblclick: (event) => {
              // Runs after CodeMirror has selected the double-clicked word.
              setTimeout(() => onDoubleClickRef.current?.(event), 0);
              return false;
            },
          }),
          autocompletion({ activateOnTyping: true }),
          languageSlot.of(language(dialect, schema)),
          syntaxHighlighting(highlight),
          EditorView.contentAttributes.of({
            ...(id ? { id } : {}),
            ...(label ? { 'aria-label': label } : {}),
            spellcheck: 'false',
          }),
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
    view.current?.dispatch({ effects: languageSlot.reconfigure(language(dialect, schema)) });
  }, [dialect, schema, languageSlot]);

  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value) {
      editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
    }
  }, [value]);

  return (
    <div ref={host} className={boxed ? 'editor boxed cm-host' : 'editor cm-host'} data-testid="sql-editor" />
  );
}
