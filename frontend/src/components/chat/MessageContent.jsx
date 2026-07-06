import React, { useMemo } from 'react';

function splitByFences(text) {
  const parts = [];
  const raw = String(text || '');
  const chunks = raw.split('```');
  for (let i = 0; i < chunks.length; i += 1) {
    const chunk = chunks[i];
    if (!chunk) continue;
    if (i % 2 === 1) {
      const lines = chunk.replace(/^\n+/, '').split('\n');
      const maybeLang = lines[0].trim();
      const lang = /^[a-zA-Z0-9_-]{1,16}$/.test(maybeLang) ? maybeLang : '';
      const code = lang ? lines.slice(1).join('\n') : chunk.replace(/^\n+/, '');
      parts.push({ type: 'code', lang, value: code.replace(/\n+$/, '') });
    } else {
      parts.push({ type: 'text', value: chunk });
    }
  }
  return parts;
}

export default function MessageContent({ content }) {
  const blocks = useMemo(() => splitByFences(content), [content]);

  return (
    <div className="chatgpt__content">
      {blocks.map((b, idx) =>
        b.type === 'code' ? (
          <pre key={idx} className="chatgpt__pre">
            {b.lang ? <div className="chatgpt__pre-lang">{b.lang}</div> : null}
            <code className="chatgpt__code">{b.value}</code>
          </pre>
        ) : (
          <p key={idx} className="chatgpt__p">
            {b.value}
          </p>
        ),
      )}
    </div>
  );
}
