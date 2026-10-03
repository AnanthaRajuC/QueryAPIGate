import { tokenize } from '@/lib/sql';

/** Read-only SQL with line numbers, coloured by the classic tokenizer (ui.py codeBox). */
export function CodeBox({ sql }: { sql: string }) {
  return (
    <div className="codebox">
      <div className="ln-rows">
        {sql.split('\n').map((line, i) => (
          <div className="ln-r" key={i}>
            <span className="ln-n">{i + 1}</span>
            <span className="ln-t">
              {line
                ? tokenize(line).map((t, j) =>
                    t.cls ? (
                      <span key={j} className={t.cls}>
                        {t.text}
                      </span>
                    ) : (
                      t.text
                    ),
                  )
                : ' '}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
