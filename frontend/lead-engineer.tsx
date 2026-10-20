import React, { FormEvent, useCallback, useEffect, useState } from "react";
import { MarkdownView } from "./markdown";
import { UserMessage, when } from "./diagnosis";
import { ENGINEER_STATUS, type EngineerJob, type EngineerMessage } from "./engineer";

/** The platform engineer in a task's conversation with its lead: its jobs for the task, their
 * messages among the lead's, and what the user can do with them there. */

export const ENGINEER_WAITING = new Set(["plan_ready", "ready", "needs_input"]);
const ENGINEER_DONE = new Set(["applied", "failed", "rejected", "cancelled"]);
const KIND = { repair: "修复", component: "组件" } as const;
export const MENTION = /^@平台工程师[\s:：,，]*/;

export type Act = (path: string, body?: unknown) => Promise<string | null>;

/** A task's engineer jobs (all jobs without ``runId``), kept fresh while ``live`` or any job is
 * under way; ``signal`` changes when the task does. */
export function useEngineerJobs(apiBase: string, runId: string | null, signal: string, live: boolean) {
  const [jobs, setJobs] = useState<EngineerJob[]>([]), [available, setAvailable] = useState(true);
  const load = useCallback(() => fetch(`${apiBase}/api/engineer${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`)
    .then(response => response.ok ? response.json() : null)
    .then(data => {
      if (!data || !Array.isArray(data.jobs)) return;
      setJobs(runId ? data.jobs.filter((job: EngineerJob) => job.run_id === runId) : data.jobs);
      setAvailable(data.available !== false);
    })
    .catch(() => {}), [apiBase, runId]);
  useEffect(() => { load(); }, [load, signal]);
  const active = live || jobs.some(job => !ENGINEER_DONE.has(job.status));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [active, load]);
  const act: Act = async (path, body) => {
    try {
      const response = await fetch(`${apiBase}${path}`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body ?? {}),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        return typeof detail?.detail === "string" ? detail.detail : `请求失败（${response.status}）`;
      }
      await load();
      return null;
    } catch (failure) {
      return failure instanceof Error ? failure.message : String(failure);
    }
  };
  return { jobs, act, available };
}

/** Whether a request asks for something new (a component) rather than a fix. */
export function kindOf(text: string): "repair" | "component" {
  return /添加|新增|增加|加一个|加个|支持|组件|功能|接入|集成/.test(text) && !/修复|修一下|报错|失败|不能|无法|连不上|bug/i.test(text)
    ? "component" : "repair";
}

/** A message for the platform engineer (``@平台工程师 ...``): an answer to the job waiting for the
 * user, or a new repair job when it has none under way. */
export async function tellEngineer(text: string, runId: string, jobs: EngineerJob[], act: Act): Promise<string | null> {
  const said = text.replace(MENTION, "").trim();
  if (!said) return "请在 @平台工程师 后面写下要对它说的话。";
  const waiting = jobs.find(job => ENGINEER_WAITING.has(job.status));
  if (waiting) return act(`/api/engineer/jobs/${encodeURIComponent(waiting.id)}/reply`, { text: said });
  if (jobs.some(job => !ENGINEER_DONE.has(job.status))) return "平台工程师正在处理这个任务的工作，等它需要你回复时再说。";
  return act(`/api/tasks/${encodeURIComponent(runId)}/engineer`, { request: said, kind: "repair" });
}

/** A long report shows its first paragraph; the rest opens on demand, so the chat stays readable. */
function Folded({ text }: { text: string }) {
  const cut = text.indexOf("\n\n");
  if (text.length <= 400 || cut < 0) return <div className="dx-report"><MarkdownView text={text} /></div>;
  return <div className="dx-report">
    <MarkdownView text={text.slice(0, cut)} />
    <details className="lead-instructions"><summary>完整报告</summary><MarkdownView text={text.slice(cut + 2)} /></details>
  </div>;
}

function EngineerMeta({ note, at }: { note: string; at: string }) {
  return <div className="dx-meta">
    <span className="dx-avatar engineer-avatar" aria-hidden="true">🛠</span>
    <b>平台工程师</b>
    <small>{note} · {when(at)}</small>
  </div>;
}

/** One line of a job's conversation in the group chat; the job's card follows its latest line. */
export function EngineerEntry({ job, message, first, last, act, onOpenEngineer }: {
  job: EngineerJob; message: EngineerMessage; first: boolean; last: boolean; act: Act; onOpenEngineer?: (jobId: string) => void;
}) {
  const kind = KIND[job.kind] ?? job.kind;
  const said = first ? `@平台工程师 ${message.text}` : message.text;
  const line = message.role === "user"
    ? said.length > 240
      ? <div className="dx-msg dx-user engineer-said"><p>{said.slice(0, 160)}…</p>
          <details><summary>完整内容</summary><p>{said}</p></details>
          <small>{when(message.created_at)}</small></div>
      : <UserMessage message={{ text: said, created_at: message.created_at }} />
    : message.role === "lead"
      ? <article className="dx-msg dx-ai">
          <div className="dx-meta"><span className="dx-avatar" aria-hidden="true">🧭</span><b>负责人</b><small>请平台工程师加入 · {when(message.created_at)}</small></div>
          <div className="dx-report"><MarkdownView text={message.text} /></div>
        </article>
      : message.role === "engineer"
        ? <article className="dx-msg dx-ai lead-engineer-msg"><EngineerMeta note={`${kind} · ${job.title}`} at={message.created_at} />
            <Folded text={message.text} /></article>
        : <p className="engineer-system" role="note"><span>🛠 {job.title} · {message.text} · {when(message.created_at)}</span></p>;
  return <>{line}{last && <JobCard job={job} act={act} onOpenEngineer={onOpenEngineer} />}</>;
}

function JobCard({ job, act, onOpenEngineer }: { job: EngineerJob; act: Act; onOpenEngineer?: (jobId: string) => void }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const status = ENGINEER_STATUS[job.status] ?? { label: job.status, tone: "muted" };
  const run = async (path: string, body?: unknown) => {
    setBusy(true); setError("");
    setError(await act(path, body) ?? "");
    setBusy(false);
  };
  const base = `/api/engineer/jobs/${encodeURIComponent(job.id)}`;
  const open = !ENGINEER_DONE.has(job.status) && job.status !== "applying";
  return <div className={`lead-repair engineer-card ${job.status}`} aria-label={`平台工程师：${job.title}`}>
    <div className="lead-repair-head">
      <b>平台工程师</b><span className="lead-model">{KIND[job.kind] ?? job.kind} · {job.title}</span>
      <span className={`dx-chip ${status.tone}`}>{status.tone === "busy" && <i className="pulse" aria-hidden="true" />}{status.label}</span>
    </div>
    {job.status === "plan_ready" && job.plan && <details className="lead-instructions" open><summary>方案</summary><div className="dx-report"><MarkdownView text={job.plan} /></div></details>}
    {job.status === "needs_input" && job.question && <p className="lead-question">{job.question}</p>}
    {job.status === "ready" && <p className="engineer-ready">改动已通过检查{job.files?.length ? `，涉及 ${job.files.join("、")}` : ""}。应用后服务会重启一次。</p>}
    {job.report && (job.status === "ready" || ENGINEER_DONE.has(job.status))
      && !(job.messages ?? []).some(item => item.role === "engineer" && item.text === job.report) && <details className="lead-instructions"><summary>改动说明</summary><div className="dx-report"><MarkdownView text={job.report} /></div></details>}
    {job.status === "applied" && job.commit && <p className="muted">已提交到 git：{job.commit.slice(0, 8)}（没有推送）</p>}
    {job.status === "failed" && job.error && <p className="error">{job.error}</p>}
    <div className="engineer-actions">
      {job.status === "plan_ready" && <button type="button" className="primary" disabled={busy} onClick={() => run(`${base}/approve`)}>批准方案</button>}
      {job.status === "ready" && <button type="button" className="primary" disabled={busy} onClick={() => run(`${base}/approve`)}>应用改动</button>}
      {job.status === "needs_input" && (job.choices ?? []).map(choice =>
        <button type="button" key={choice} className="ghost" disabled={busy} onClick={() => run(`${base}/reply`, { choice, text: "" })}>{choice}</button>)}
      {open && <button type="button" className="ghost" disabled={busy} onClick={() => run(`${base}/reject`)}>放弃</button>}
      {onOpenEngineer && <button type="button" className="link" onClick={() => onOpenEngineer(job.id)}>在平台工程师页面查看</button>}
    </div>
    {ENGINEER_WAITING.has(job.status) && <small className="dx-hint">要改什么，在下面输入「@平台工程师」加上你的意见。</small>}
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}

/** The user brings the platform engineer into the task's conversation. */
export function JoinEngineer({ runId, act, onDone }: { runId: string; act: Act; onDone: () => void }) {
  const [kind, setKind] = useState<"repair" | "component">("repair"), [text, setText] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true); setError("");
    const problem = await act(`/api/tasks/${encodeURIComponent(runId)}/engineer`, { request: text.trim(), kind });
    setBusy(false);
    if (problem) setError(problem); else { setText(""); onDone(); }
  }
  return <form className="engineer-join" onSubmit={submit} aria-label="拉平台工程师进来">
    <div className="engineer-join-kind" role="radiogroup" aria-label="要它做什么">
      {([["repair", "修复问题"], ["component", "添加组件"]] as const).map(([value, label]) =>
        <button type="button" key={value} role="radio" aria-checked={kind === value} className={kind === value ? "on" : ""} onClick={() => setKind(value)}>{label}</button>)}
    </div>
    <textarea aria-label="要平台工程师做的事" rows={3} maxLength={20000} value={text} onChange={event => setText(event.target.value)}
      placeholder={kind === "repair" ? "Super Partner 自身哪里有问题，比如：步骤沙箱连不到宿主机的许可证端口 26585" : "Super Partner 缺什么能力，要它添加什么"} />
    <small className="dx-hint">{kind === "repair" ? "修复通过检查后自动生效。" : "组件先写方案给你批准，改好后再由你确认生效。"}它的方案、提问和进度都会出现在这里，负责人也能看到。</small>
    <div className="engineer-join-bar">
      <button type="button" className="ghost" onClick={onDone}>取消</button>
      <button type="submit" className="primary" disabled={busy || !text.trim()}>{busy ? "提交中…" : "拉它进来"}</button>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}
