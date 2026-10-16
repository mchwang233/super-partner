import React, { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { MarkdownView } from "./markdown";
import { Composer, UserMessage, when, type Diagnosis, type SendReply } from "./diagnosis";

export type Decision = {
  id: string; node_id?: string | null; event: string; action: string; reason?: string;
  instructions?: string | null; model_id?: string | null; targets?: string[]; created_at: string;
};
export type LeadMessage = { id: string; role: "user" | "lead"; text: string; status: string; created_at: string };
type RunLike = {
  status: string; diagnoses?: Diagnosis[]; waiting_diagnosis_id?: string | null;
  supervisor?: { model_id?: string | null; calls?: number } | null;
  decisions?: Decision[]; lead_messages?: LeadMessage[];
  plan: { nodes: { id: string; title: string }[] };
};
type ModelLike = { id: string; display_name: string };
export type AskLead = (text: string) => Promise<string | null>;

const EVENTS: Record<string, string> = { attempt_failed: "尝试失败", verify_failed: "验证没通过", user_answer: "你的回答", all_done: "全部步骤完成后检查" };
const TONES: Record<string, string> = { unable: "warn", ask_user: "warn", fallback: "bad" };

function actionLabel(item: Decision, title: (id: string) => string, model: (id?: string | null) => string) {
  const labels: Record<string, string> = {
    retry: "按要求重试", switch_model: `换成 ${model(item.model_id)}`, accept: "接受结果", unable: "判定无法完成",
    ask_user: "问你", repair_environment: "修复环境", repair_platform: "修复 Super Partner",
    rework: `返工 ${(item.targets ?? []).map(title).join("、")}`, fallback: "交给诊断处理",
    replan: "调整计划", done: "确认完成",
  };
  return labels[item.action] ?? item.action;
}

/** Whether a task has a lead, or anything the lead did, to show. */
export function hasLead(run: RunLike) {
  return !!run.supervisor?.model_id || !!run.decisions?.length || !!run.lead_messages?.length
    || (run.diagnoses ?? []).some(item => item.trigger === "lead");
}

type Entry =
  | { kind: "decision"; at: string; item: Decision }
  | { kind: "question"; at: string; item: Diagnosis }
  | { kind: "message"; at: string; item: LeadMessage };

/** The task's lead: everything it decided and why, its questions to you, and a box to write to it. */
export function LeadPanel({ run, models, onReply, onAsk }: { run: RunLike; models: ModelLike[]; onReply: SendReply; onAsk: AskLead }) {
  const [open, setOpen] = useState(true);
  const model = (id?: string | null) => models.find(m => m.id === id)?.display_name ?? id ?? "—";
  const title = (id: string) => run.plan.nodes.find(n => n.id === id)?.title ?? id;
  const entries: Entry[] = [
    ...(run.decisions ?? []).filter(item => item.action !== "ask_user").map(item => ({ kind: "decision" as const, at: item.created_at, item })),
    ...(run.diagnoses ?? []).filter(item => item.trigger === "lead").map(item => ({ kind: "question" as const, at: item.created_at, item })),
    ...(run.lead_messages ?? []).map(item => ({ kind: "message" as const, at: item.created_at, item })),
  ].sort((a, b) => a.at.localeCompare(b.at));
  const answering = (run.lead_messages ?? []).some(item => item.status === "pending");
  const waiting = (run.diagnoses ?? []).find(item => item.id === run.waiting_diagnosis_id && item.trigger === "lead");
  const decided = (run.decisions ?? []).length;
  const status = waiting ? { label: "等你回复", tone: "warn" } : answering ? { label: "思考中", tone: "busy" }
    : { label: decided ? `已决策 ${decided} 次` : "跟进中", tone: "ok" };
  return <section className={`diagnosis-card dx-panel lead-panel${waiting ? " waiting" : ""}`} aria-label="负责人">
    <div className="diagnosis-head">
      <b>负责人</b>
      <span className="lead-model">{model(run.supervisor?.model_id)}</span>
      <span className={`dx-chip ${status.tone}`}>{status.tone === "busy" && <i className="pulse" aria-hidden="true" />}{status.label}</span>
      <button type="button" className="ghost diagnosis-fold" onClick={() => setOpen(o => !o)} aria-expanded={open}>{open ? "收起" : "展开"}</button>
    </div>
    {open && <div className="dx-thread">
      <p className="lead-intro">规划这个任务的模型，会全程跟进：步骤失败、验证没通过时由它决定怎么办，并写明理由。需要你决定的事它会在这里问你；你也可以随时给它留言。</p>
      {entries.map(entry => entry.kind === "decision"
        ? <DecisionMessage key={entry.item.id} item={entry.item} label={actionLabel(entry.item, title, model)} step={entry.item.node_id ? title(entry.item.node_id) : undefined} />
        : entry.kind === "question"
          ? <QuestionMessage key={entry.item.id} item={entry.item} step={entry.item.node_id ? title(entry.item.node_id) : undefined}
              waiting={entry.item.id === waiting?.id} onReply={onReply} />
          : <ChatMessage key={entry.item.id} item={entry.item} />)}
      {!waiting && <AskBox disabled={answering} onAsk={onAsk} />}
    </div>}
  </section>;
}

function LeadMeta({ note, at }: { note: string; at: string }) {
  return <div className="dx-meta">
    <span className="dx-avatar" aria-hidden="true">🧭</span>
    <b>负责人</b>
    <small>{note} · {when(at)}</small>
  </div>;
}

function DecisionMessage({ item, label, step }: { item: Decision; label: string; step?: string }) {
  return <article className="dx-msg dx-ai lead-decision">
    <LeadMeta note={[EVENTS[item.event] ?? item.event, step].filter(Boolean).join(" · ")} at={item.created_at} />
    <p className="lead-verdict"><span className={`dx-chip ${TONES[item.action] ?? "ok"}`}>{label}</span>{item.reason}</p>
    {item.instructions && <details className="lead-instructions"><summary>具体要求</summary><p>{item.instructions}</p></details>}
  </article>;
}

function QuestionMessage({ item, step, waiting, onReply }: { item: Diagnosis; step?: string; waiting: boolean; onReply: SendReply }) {
  return <>
    <article className="dx-msg dx-ai">
      <LeadMeta note={["提问", step].filter(Boolean).join(" · ")} at={item.created_at} />
      {item.report && <p className="lead-verdict">{item.report}</p>}
      {!waiting && item.question && <p className="lead-question">{item.question}</p>}
    </article>
    {(item.messages ?? []).map((message, i) => <UserMessage key={i} message={message} />)}
    {waiting && <Composer item={item} waiting disabled={false} onReply={onReply} sendLabel="回复负责人"
      placeholder="补充说明（可选），或直接写下你的决定" />}
  </>;
}

function ChatMessage({ item }: { item: LeadMessage }) {
  if (item.role === "user") return <UserMessage message={{ text: item.text, created_at: item.created_at }} />;
  return <article className="dx-msg dx-ai">
    <LeadMeta note="回复" at={item.created_at} />
    {item.status === "pending"
      ? <p className="diagnosis-running"><span className="dx-typing" aria-hidden="true"><i /><i /><i /></span>负责人正在看任务的情况…</p>
      : item.status === "failed" ? <p className="error">{item.text}</p>
      : <div className="dx-report"><MarkdownView text={item.text} /></div>}
  </article>;
}

function AskBox({ disabled, onAsk }: { disabled: boolean; onAsk: AskLead }) {
  const [text, setText] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const form = useRef<HTMLFormElement>(null), box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { const el = box.current; if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; } }, [text]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (disabled || busy || !text.trim()) return;
    setBusy(true); setError("");
    try { const problem = await onAsk(text.trim()); if (problem) setError(problem); else setText(""); }
    catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setBusy(false); }
  }
  function keys(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.current?.requestSubmit(); }
  }
  return <form ref={form} className="dx-composer compact" onSubmit={submit} aria-label="给负责人留言">
    <fieldset disabled={disabled || busy}>
      <div className="dx-input">
        <textarea ref={box} aria-label="给负责人的消息" rows={1} maxLength={6000} value={text}
          onChange={event => setText(event.target.value)} onKeyDown={keys}
          placeholder="问它进展、为什么这样决定，或告诉它你的要求" />
        <div className="dx-input-bar">
          <small className="dx-hint">{disabled ? "负责人正在回复上一条消息…" : "它记得这个任务从规划起的全部经过 · Ctrl+Enter 发送"}</small>
          <button type="submit" className="primary" disabled={!text.trim()}>{busy ? "发送中…" : "发送"}</button>
        </div>
      </div>
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}
