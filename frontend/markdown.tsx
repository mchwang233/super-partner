import React, { Suspense, lazy, useState } from "react";

// The renderer is loaded on its own, right after the page, so the page shows sooner.
const loadRenderer = () => import("./markdown-render");
const MarkdownRender = lazy(loadRenderer);
if (typeof window !== "undefined") setTimeout(() => { void loadRenderer(); }, 0);

/** Model output and .md files rendered as Markdown; plain text until the renderer has loaded. */
export function Markdown({ text, onLink }: { text: string; onLink?: (href: string) => boolean }) {
  return <div className="markdown-body">
    <Suspense fallback={<div className="markdown-loading">{text}</div>}>
      <MarkdownRender text={text} onLink={onLink} />
    </Suspense>
  </div>;
}

export function isMarkdownFile(path: string) { return /\.(md|markdown|mdx)$/i.test(path); }

/** Markdown with a preview / source switch; ``markdown=false`` shows plain text only. */
export function MarkdownView({ text, markdown = true, className = "", onLink }: { text: string; markdown?: boolean; className?: string; onLink?: (href: string) => boolean }) {
  const [source, setSource] = useState(false);
  if (!markdown) return <pre className={className}>{text}</pre>;
  return <div className={`markdown-view ${className}`}>
    <div className="markdown-toggle" role="group" aria-label="显示方式">
      <button type="button" aria-pressed={!source} onClick={() => setSource(false)}>预览</button>
      <button type="button" aria-pressed={source} onClick={() => setSource(true)}>源码</button>
    </div>
    {source ? <pre>{text}</pre> : <Markdown text={text} onLink={onLink} />}
  </div>;
}
