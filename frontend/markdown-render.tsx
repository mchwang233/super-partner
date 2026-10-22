import React from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

/** The Markdown renderer, loaded apart from the page (markdown.tsx): it is most of the bundle.
 * Raw HTML is never rendered, links open in a new tab, and react-markdown drops unsafe URLs
 * such as javascript:. */
export default function MarkdownRender({ text, onLink }: { text: string; onLink?: (href: string) => boolean }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} skipHtml
    components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener"
      // A link to one of the task's own files opens it in place instead of a broken URL.
      onClick={event => { if (props.href && onLink?.(props.href)) event.preventDefault(); }} /> }}>
    {text}
  </ReactMarkdown>;
}
