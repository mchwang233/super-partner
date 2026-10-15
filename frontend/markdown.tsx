import React, { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

/** Model output and .md files rendered as Markdown. Raw HTML is never rendered, links open in a
 * new tab, and react-markdown drops unsafe URLs such as javascript:. */
export function Markdown({ text, onLink }: { text: string; onLink?: (href: string) => boolean }) {
  return <div className="markdown-body">
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} skipHtml
      components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener"
        // A link to one of the task's own files opens it in place instead of a broken URL.
        onClick={event => { if (props.href && onLink?.(props.href)) event.preventDefault(); }} /> }}>
      {text}
    </ReactMarkdown>
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
