import React, { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { MarkdownView } from "./markdown";

export type DiagnosisMessage = { text: string; choice?: string | null; created_at?: string };
export type Diagnosis = {
  id: string; trigger: string; node_id?: string | null; model_id?: string | null; status: string;
  report?: string | null; advice?: string | null; verdict?: string | null; user_action?: string | null;
  scope?: string; reply_to?: string | null; question?: string | null; choices?: string[];
  messages?: DiagnosisMessage[]; error?: string | null; created_at: string;
};
type RunLike = {
  status: string; diagnoses?: Diagnosis[]; waiting_diagnosis_id?: string | null; maintenance_id?: string | null;
  plan: { nodes: { id: string; title: string }[] };
};
type ModelLike = { id: string; display_name: string };
export type SendReply = (diagnosisId: string, body: { text: string; choice?: string }) => Promise<string | null>;

const VERDICTS: Record<string, { label: string; tone: string; note: (model: string, item: Diagnosis) => string }> = {
  fixed: { label: "已修复环境", tone: "ok", note: model => `${model} 已修复环境，这一步会重新执行。` },
  retry: { label: "按建议重试", tone: "ok", note: model => `${model} 没有发现环境问题，这一步会带着建议重试。` },
  quota_exhausted: { label: "换用备用模型", tone: "ok", note: model => `${model} 确认执行模型额度耗尽，会自动换用备用模型；没有备用模型时才暂停。` },
  model_unavailable: { label: "换用备用模型", tone: "ok", note: model => `${model} 确认模型服务不可用，会自动换用备用模型，其他项目也会暂时避开它。` },
  blocked: { label: "需要你处理", tone: "warn", note: (model, item) => `${model} 判断需要你先处理。${item.user_action ?? "处理后在下方告诉我，或补充信息。"}` },
  unable: { label: "无法完成", tone: "warn", note: (model, item) => `${model} 判断这一步在当前环境中做不到，不再重试，其余步骤继续。${item.user_action ? `需要你处理：${item.user_action}` : ""}` },
  needs_input: { label: "等你回复", tone: "warn", note: () => "诊断需要你的意见，选择方案或写下想法后会继续。" },
  platform_pending: { label: "平台修复生效中", tone: "ok", note: () => "Super Partner 修复已通过验证，正在自动生效；完成后继续原任务。" },
  platform_fixed: { label: "平台已修复", tone: "ok", note: () => "Super Partner 修复已生效，所有项目都将使用修复后的版本。" },
  platform_failed: { label: "平台修复失败", tone: "warn", note: () => "Super Partner 修复未通过验证或生效检查，任务数据已保留。" },
};

/** Diagnoses grouped into conversations: a reply's follow-up joins the one it answers. */
function conversations(list: Diagnosis[]): Diagnosis[][] {
  const byId = new Map(list.map(item => [item.id, item]));
  const rootOf = (item: Diagnosis) => {
    const seen = new Set<string>();
    let current = item;
    while (current.reply_to && byId.has(current.reply_to) && !seen.has(current.id)) {
      seen.add(current.id);
      current = byId.get(current.reply_to)!;
    }
    return current.id;
  };
  const groups = new Map<string, Diagnosis[]>();
  for (const item of list) {
    const root = rootOf(item);
    groups.set(root, [...(groups.get(root) ?? []), item]);
  }
  return [...groups.values()];
}

export function when(iso: string) {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function chip(run: RunLike, last: Diagnosis) {
  if (last.status === "running") return { label: "分析中", tone: "busy" };
  if (last.status === "failed") return { label: "诊断失败", tone: "bad" };
  if (run.waiting_diagnosis_id === last.id) return { label: "等你回复", tone: "warn" };
  const verdict = last.verdict ? VERDICTS[last.verdict] : undefined;
  return verdict ? { label: verdict.label, tone: verdict.tone } : { label: "诊断完成", tone: "ok" };
}

/** A long report starts folded so the conversation stays readable. */
function Report({ text }: { text: string }) {
  const long = text.length > 700 || text.split("\n").length > 14;
  const [open, setOpen] = useState(!long);
  return <div className={`dx-report${open ? "" : " folded"}`}>
    <MarkdownView text={text} />
    {long && <button type="button" className="dx-more" onClick={() => setOpen(value => !value)}>{open ? "收起报告" : "展开完整报告"}</button>}
  </div>;
}

function AssistantMessage({ item, model, node, run }: { item: Diagnosis; model: string; node?: string; run: RunLike }) {
  const verdict = item.verdict ? VERDICTS[item.verdict] : undefined;
  const running = item.scope === "super_partner" ? `${model} 正在修复 Super Partner 自身问题，并验证修改…`
    : item.reply_to ? `${model} 正在结合你的回复继续诊断…`
    : item.trigger === "auto" ? `${model} 正在排查卡住的原因，并尝试修复…`
    : `正在由 ${model} 分析任务的计划、每次尝试和改动…`;
  return <article className="dx-msg dx-ai">
    <div className="dx-meta">
      <span className="dx-avatar" aria-hidden="true">{item.trigger === "lead" ? "🧭" : "🩺"}</span>
      <b>{model}</b>
      <small>{item.trigger === "lead" ? "负责人提问" : item.reply_to ? "回复" : item.trigger === "auto" ? "自动诊断" : "手动诊断"}{node ? ` · ${node}` : ""} · {when(item.created_at)}</small>
    </div>
    {item.status === "running" ? <p className="diagnosis-running"><span className="dx-typing" aria-hidden="true"><i /><i /><i /></span>{running}</p>
      : item.status === "failed" ? <p className="error">诊断没有完成：{item.error}</p>
      : <>
        {verdict && <p className={`diagnosis-verdict ${verdict.tone}`}>{verdict.note(model, item)}</p>}
        {item.report && <Report text={item.report} />}
        {item.advice && <div className="diagnosis-advice"><b>给失败步骤的建议</b><p>{item.advice}</p>
          {(run.status === "failed" || run.status === "paused") && !run.waiting_diagnosis_id &&
            <small>点“{run.status === "failed" ? "从失败处继续" : "按此预算继续"}”时，这条建议会交给失败的步骤。</small>}</div>}
      </>}
  </article>;
}

export function UserMessage({ message }: { message: DiagnosisMessage }) {
  return <div className="dx-msg dx-user" aria-label="你的诊断回复">
    {message.choice && <span className="dx-choice-tag">✓ {message.choice}</span>}
    {message.text && <p>{message.text}</p>}
    {message.created_at && <small>{when(message.created_at)}</small>}
  </div>;
}

export function Composer({ item, waiting, disabled, onReply, sendLabel = "发送并继续诊断", placeholder }: {
  item: Diagnosis; waiting: boolean; disabled: boolean; onReply: SendReply; sendLabel?: string; placeholder?: string;
}) {
  const [text, setText] = useState(""), [choice, setChoice] = useState(""), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const submitting = useRef(false), form = useRef<HTMLFormElement>(null), box = useRef<HTMLTextAreaElement>(null);
  const choices = item.choices ?? [], asking = !!item.question || choices.length > 0;
  // The box grows with what is typed, up to a limit.
  useEffect(() => { const el = box.current; if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 240)}px`; } }, [text]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (disabled || submitting.current || (!text.trim() && !choice)) return;
    submitting.current = true; setBusy(true); setError("");
    try {
      const problem = await onReply(item.id, { text: text.trim(), ...(choice ? { choice } : {}) });
      if (problem) setError(problem); else { setText(""); setChoice(""); }
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { submitting.current = false; setBusy(false); }
  }
  function keys(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); form.current?.requestSubmit(); }
  }
  return <form ref={form} className={`dx-composer${waiting ? " waiting" : ""}${asking ? "" : " compact"}`} onSubmit={submit} aria-label="回复诊断">
    <fieldset disabled={disabled || busy}>
      <legend className={asking ? "dx-question" : "dx-sr"}>{item.question || (asking ? "选择一个方案" : "告诉我你的想法")}</legend>
      {choices.length > 0 && <div className="diagnosis-choices" role="radiogroup" aria-label="可选方案">
        {choices.map((option, index) => <label className={choice === option ? "selected" : ""} key={option}>
          <input type="radio" name={`diagnosis-${item.id}`} value={option} checked={choice === option}
            onChange={() => undefined} onClick={() => setChoice(current => current === option ? "" : option)} />
          <span className="dx-key" aria-hidden="true">{String.fromCharCode(65 + index)}</span>
          <span>{option}</span>
        </label>)}
      </div>}
      {/* One box: the text on top, the hint and the send button in its bottom bar. */}
      <div className="dx-input">
        <textarea ref={box} aria-label="补充你的想法" rows={asking ? 2 : 1} maxLength={6000} value={text}
          onChange={event => setText(event.target.value)} onKeyDown={keys}
          placeholder={placeholder ?? (choices.length ? "补充说明（可选）：限制条件、你的方案，或纠正诊断的判断" : "告诉诊断你的想法：补充信息、你已经做了什么，或纠正它的判断")} />
        <div className="dx-input-bar">
          <small className="dx-hint">{disabled ? "当前诊断或平台修复完成后，可以继续回复。"
            : choice ? `已选「${choice}」，再点一次可取消 · Ctrl+Enter 发送` : "可以只选方案、只写文字，或两者都给 · Ctrl+Enter 发送"}</small>
          <button type="submit" className="primary" disabled={!text.trim() && !choice}>{busy ? "发送中…" : sendLabel}</button>
        </div>
      </div>
    </fieldset>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}

/** Diagnoses as a conversation: the model's findings, your replies, and a reply box on the latest. */
export function DiagnosisPanel({ run, models, onReply }: { run: RunLike; models: ModelLike[]; onReply: SendReply }) {
  const groups = conversations(run.diagnoses ?? []);
  const [index, setIndex] = useState(groups.length - 1), [open, setOpen] = useState(true);
  const lastId = run.diagnoses?.[run.diagnoses.length - 1]?.id;
  // A new diagnosis or reply brings the newest conversation back into view.
  useEffect(() => { setIndex(groups.length - 1); setOpen(true); }, [groups.length, lastId]);
  const thread = groups[Math.min(Math.max(index, 0), groups.length - 1)];
  if (!thread) return null;
  const last = thread[thread.length - 1], status = chip(run, last), latest = index >= groups.length - 1;
  const name = (item: Diagnosis) => models.find(m => m.id === item.model_id)?.display_name ?? item.model_id ?? "—";
  const nodeTitle = (item: Diagnosis) => run.plan.nodes.find(n => n.id === item.node_id)?.title;
  const busy = (run.diagnoses ?? []).some(item => item.status === "running") || !!run.maintenance_id || last.verdict === "platform_pending";
  const waiting = run.waiting_diagnosis_id === last.id;
  return <section className={`diagnosis-card dx-panel${waiting ? " waiting" : ""}`} aria-label="诊断报告">
    <div className="diagnosis-head">
      <b>诊断{last.scope === "super_partner" ? " · Super Partner 自身问题" : ""}</b>
      <span className={`dx-chip ${status.tone}`}>{status.tone === "busy" && <i className="pulse" aria-hidden="true" />}{status.label}</span>
      {groups.length > 1 && <span className="diagnosis-nav">
        <button type="button" className="ghost" disabled={index <= 0} onClick={() => setIndex(i => i - 1)} aria-label="上一次诊断">‹</button>
        第 {index + 1} / {groups.length} 次
        <button type="button" className="ghost" disabled={index >= groups.length - 1} onClick={() => setIndex(i => i + 1)} aria-label="下一次诊断">›</button>
      </span>}
      <button type="button" className="ghost diagnosis-fold" onClick={() => setOpen(o => !o)} aria-expanded={open}>{open ? "收起" : "展开"}</button>
    </div>
    {open && <div className="dx-thread">
      {thread.map(item => <React.Fragment key={item.id}>
        <AssistantMessage item={item} model={name(item)} node={nodeTitle(item)} run={run} />
        {(item.messages ?? []).map((message, i) => <UserMessage key={i} message={message} />)}
      </React.Fragment>)}
      {latest && last.status !== "running" &&
        <Composer key={last.id} item={last} waiting={waiting} disabled={busy} onReply={onReply} />}
    </div>}
  </section>;
}
