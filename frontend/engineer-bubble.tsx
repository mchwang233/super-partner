import React, { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { ENGINEER_STATUS, type EngineerJob } from "./engineer";
import { ENGINEER_WAITING, EngineerEntry, kindOf, shortTitle, useEngineerJobs } from "./lead-engineer";

type RunRef = { id: string; plan: { summary: string } };

/** A bubble in the page's corner that opens a chat with the platform engineer, outside any task:
 * every job's thread, newest last, and a box to ask for a fix or a component, or to answer it. */
export function EngineerBubble({ apiBase, runs, tick, onOpenPage }: {
  apiBase: string; runs: RunRef[]; tick: string; onOpenPage: (jobId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Closed, it looks again whenever the app reloads its tasks (``tick``), for the badge.
  const { jobs, act, available } = useEngineerJobs(apiBase, null, `${open}|${tick}`, open);
  const waiting = jobs.filter(job => ENGINEER_WAITING.has(job.status));
  const [target, setTarget] = useState<string | null>(null);
  const [text, setText] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  // Finished work folds to its header; the newest and anything under way stay open.
  const [toggled, setToggled] = useState<Set<string>>(new Set());
  const end = useRef<HTMLDivElement>(null);
  // Answers go to the job waiting for the user, unless another one is picked.
  const replyTo = jobs.find(job => job.id === target && ENGINEER_WAITING.has(job.status)) ?? (target === "new" ? undefined : waiting[0]);
  const threads = [...jobs].slice(0, 20).sort((a, b) => a.updated_at.localeCompare(b.updated_at));
  useEffect(() => {
    if (!open) return;
    const close = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [open]);
  useEffect(() => { if (open) end.current?.scrollIntoView?.({ block: "end" }); }, [open, jobs.length]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const said = text.trim();
    if (!said || busy) return;
    setBusy(true); setError("");
    const problem = replyTo
      ? await act(`/api/engineer/jobs/${encodeURIComponent(replyTo.id)}/reply`, { text: said })
      : await act("/api/engineer/jobs", { request: said, kind: kindOf(said) });
    setBusy(false);
    if (problem) setError(problem); else { setText(""); setTarget(null); }
  }
  function keys(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); }
  }
  const taskOf = (job: EngineerJob) => runs.find(run => run.id === job.run_id)?.plan.summary;
  return <>
    <button type="button" className={`engineer-bubble${open ? " open" : ""}`} aria-expanded={open} aria-label="平台工程师"
      title="和平台工程师聊：修复 Super Partner 自身，或给它添加组件" onClick={() => setOpen(value => !value)}>
      <span aria-hidden="true">{open ? "×" : "🛠"}</span>
      {!open && waiting.length > 0 && <i className="engineer-bubble-badge" title="等你处理">{waiting.length}</i>}
    </button>
    {open && <section className="engineer-chat diagnosis-card" role="dialog" aria-label="平台工程师聊天">
      <div className="engineer-chat-head">
        {/* The same mark as the engineer's avatar in a task's conversation. */}
        <span className="dx-avatar engineer-avatar engineer-chat-avatar" aria-hidden="true">🛠</span>
        <div><b>平台工程师</b><small>修复 Super Partner 自身，或给它添加组件</small></div>
        <button type="button" className="modal-close" onClick={() => setOpen(false)} aria-label="关闭平台工程师聊天">×</button>
      </div>
      <div className="engineer-chat-body dx-thread">
        {!available && <p className="error">平台工程师不可用：平台自修复没有启用，或这里没有可编辑的 Super Partner 源码。</p>}
        {!threads.length && <p className="muted engineer-chat-empty">还没有工作记录。直接在下面说要修什么、加什么；修复通过检查后自动生效，组件先写方案给你批准。</p>}
        {threads.map((job, index) => {
          const done = ["applied", "failed", "rejected", "cancelled"].includes(job.status) && index < threads.length - 1;
          const expanded = done ? toggled.has(job.id) : !toggled.has(job.id);
          const status = ENGINEER_STATUS[job.status] ?? { label: job.status, tone: "muted" };
          return <div key={job.id} className={`engineer-thread${expanded ? "" : " folded"}`}>
          <button type="button" className="engineer-thread-head" aria-expanded={expanded}
            onClick={() => setToggled(current => { const next = new Set(current); next.has(job.id) ? next.delete(job.id) : next.add(job.id); return next; })}>
            <span className="engineer-thread-title"><b title={job.title}>{shortTitle(job.title)}</b>
              <small>{taskOf(job) ? `来自任务「${taskOf(job)}」` : "在这里提出"} · {job.kind === "component" ? "组件" : "修复"}</small></span>
            <span className={`dx-chip ${status.tone}`}>{status.label}</span>
          </button>
          {expanded && (job.messages ?? []).map((message, index, all) => <EngineerEntry key={index} job={job} message={message} first={index === 0}
            last={index === all.length - 1} act={act} onOpenEngineer={onOpenPage} />)}
          {ENGINEER_WAITING.has(job.status) && replyTo?.id !== job.id &&
            <button type="button" className="link engineer-reply-to" onClick={() => setTarget(job.id)}>回复这一项</button>}
        </div>; })}
        <div ref={end} />
      </div>
      <form className="dx-composer compact engineer-chat-composer" onSubmit={submit} aria-label="给平台工程师留言">
        <div className="engineer-chat-target">
          {replyTo ? <><span className="engineer-chat-tag">回复：{shortTitle(replyTo.title)}</span><button type="button" className="link" onClick={() => setTarget("new")}>改为提新需求</button></>
            : <><span className="engineer-chat-tag new">新需求</span>{waiting.length > 0 && <button type="button" className="link" onClick={() => setTarget(null)}>改为回复等你的那一项</button>}</>}
        </div>
        <fieldset disabled={busy || !available}>
          <div className="dx-input">
            <textarea aria-label="给平台工程师的消息" rows={2} maxLength={20000} value={text} onChange={event => setText(event.target.value)} onKeyDown={keys}
              placeholder={replyTo ? "回复它的方案或问题，比如要改的地方" : "说要修什么，或要加什么能力"} />
            <div className="dx-input-bar">
              <small className="dx-hint">Ctrl+Enter 发送</small>
              <button type="submit" className="primary" disabled={!text.trim()}>{busy ? "发送中…" : "发送"}</button>
            </div>
          </div>
        </fieldset>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </section>}
  </>;
}

