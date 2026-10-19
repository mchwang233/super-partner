import React, { FormEvent, useEffect, useRef, useState } from "react";
import { MarkdownView } from "./markdown";
import { Composer, when, type Diagnosis } from "./diagnosis";

export type EngineerMessage = { role: "user" | "engineer" | "lead" | "system"; text: string; created_at: string };
export type EngineerJob = {
  id: string; kind: "repair" | "component"; origin: "user" | "lead" | "diagnosis"; title: string; request: string;
  status: string; plan?: string | null; report?: string | null; question?: string | null; choices?: string[];
  run_id?: string | null; node_id?: string | null; files?: string[]; checks?: string[]; diff?: string | null;
  commit?: string | null; model_id?: string | null; messages?: EngineerMessage[]; budget_usd: number; cost_usd: number;
  error?: string | null; created_at: string; updated_at: string; finished_at?: string | null;
};
export type EngineerOverview = { available: boolean; working_on?: string | null; pending?: string | null; jobs: EngineerJob[] };
type RunRef = { id: string; plan: { summary: string } };

export const ENGINEER_STATUS: Record<string, { label: string; tone: string }> = {
  queued: { label: "排队中", tone: "busy" }, planning: { label: "写方案中", tone: "busy" },
  plan_ready: { label: "方案待批准", tone: "warn" }, editing: { label: "修改中", tone: "busy" },
  validating: { label: "检查中", tone: "busy" }, ready: { label: "改动待应用", tone: "warn" },
  needs_input: { label: "等你回复", tone: "warn" }, applying: { label: "生效中", tone: "busy" },
  applied: { label: "已生效", tone: "ok" }, failed: { label: "没有完成", tone: "bad" },
  rejected: { label: "已放弃", tone: "muted" }, cancelled: { label: "已取消", tone: "muted" },
};
const KIND = { repair: "修复", component: "组件" } as const;
const ORIGIN = { user: "你提出", lead: "负责人请求", diagnosis: "任务诊断发起" } as const;
const SPEAKER: Record<string, string> = { user: "你", engineer: "平台工程师", lead: "负责人", system: "系统" };

/** Jobs waiting for the user: a plan to approve, a change to apply, or a question. */
export function waitingJobs(jobs: EngineerJob[] = []) {
  return jobs.filter(job => job.status === "plan_ready" || job.status === "ready" || job.status === "needs_input").length;
}

type Act = (path: string, body?: unknown) => Promise<string | null>;

/** The platform engineer: ask it for a repair or a component, approve its plans and changes. */
export function EngineerPage({ apiBase, runs, focusId, onOpenRun }: {
  apiBase: string; runs: RunRef[]; focusId?: string | null; onOpenRun: (id: string) => void;
}) {
  const [data, setData] = useState<EngineerOverview | null>(null), [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(focusId ?? null);
  const revision = useRef(0);
  async function load() {
    const mine = revision.current;
    try {
      const response = await fetch(`${apiBase}/api/engineer`);
      if (!response.ok) throw new Error(`读取失败（${response.status}）`);
      const body = await response.json() as EngineerOverview;
      if (mine === revision.current) { setData(body); setError(""); }
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
  }
  useEffect(() => { load(); const timer = setInterval(load, 2000); return () => clearInterval(timer); }, []);
  useEffect(() => { if (focusId) setSelectedId(focusId); }, [focusId]);
  const jobs = data?.jobs ?? [];
  const selected = jobs.find(job => job.id === selectedId) ?? jobs[0] ?? null;
  const act: Act = async (path, body) => {
    try {
      const response = await fetch(`${apiBase}/api/engineer/${path}`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body ?? {}),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) return result.detail || `操作失败（${response.status}）`;
      revision.current++;
      if (result?.id) {
        setData(current => current ? { ...current, jobs: current.jobs.some(job => job.id === result.id)
          ? current.jobs.map(job => job.id === result.id ? result : job) : [result, ...current.jobs] } : current);
        setSelectedId(result.id);
      }
      await load();
      return null;
    } catch (failure) { return failure instanceof Error ? failure.message : String(failure); }
  };
  return <div className="engineer-page">
    <section className="panel engineer-intro">
      <div>
        <h2>平台工程师</h2>
        <p className="muted">专门修改 Super Partner 自身：修复缺陷，或添加组件（模型和渠道、工具和技能、界面功能、调度和流程）。
          它在一份隔离的源码副本里修改，独立跑完测试和构建才会生效；组件要先由你批准方案，生效前再由你确认改动。生效后会在本地提交到 git，不会推送。</p>
      </div>
      {data && !data.available && <p className="error">平台工程师不可用：平台自修复未启用，或当前安装没有可编辑的 Super Partner 源码。</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {data?.available && <RequestForm act={act} />}
    </section>
    <div className="engineer-layout">
      <nav className="panel engineer-list" aria-label="平台工程师的工作">
        <h3>工作记录</h3>
        {!jobs.length && <p className="empty">还没有记录。</p>}
        {jobs.map(job => <button type="button" key={job.id} aria-pressed={selected?.id === job.id} onClick={() => setSelectedId(job.id)}>
          <span className="engineer-list-title">{job.title || job.request.slice(0, 40)}</span>
          <span className="engineer-list-meta"><StatusChip status={job.status} /> {KIND[job.kind]} · {ORIGIN[job.origin]} · {when(job.created_at)}</span>
        </button>)}
      </nav>
      {selected ? <JobDetail job={selected} working={data?.working_on === selected.id} runs={runs} act={act} onOpenRun={onOpenRun} />
        : <section className="panel empty big">提出第一个需求后，这里会显示平台工程师的方案、改动和结果。</section>}
    </div>
  </div>;
}

function StatusChip({ status }: { status: string }) {
  const known = ENGINEER_STATUS[status] ?? { label: status, tone: "ok" };
  return <span className={`dx-chip ${known.tone}`}>{known.tone === "busy" && <i className="pulse" aria-hidden="true" />}{known.label}</span>;
}

function RequestForm({ act }: { act: Act }) {
  const [kind, setKind] = useState<"component" | "repair">("component"), [text, setText] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !text.trim()) return;
    setBusy(true); setError("");
    const problem = await act("jobs", { request: text.trim(), kind });
    setBusy(false);
    if (problem) setError(problem); else setText("");
  }
  return <form className="engineer-request" onSubmit={submit} aria-label="向平台工程师提需求">
    <div className="engineer-kind" role="radiogroup" aria-label="工作类型">
      {(["component", "repair"] as const).map(value => <label key={value} className={kind === value ? "selected" : ""}>
        <input type="radio" name="engineer-kind" checked={kind === value} onChange={() => setKind(value)} />
        {value === "component" ? "添加组件" : "修复缺陷"}
      </label>)}
    </div>
    <textarea aria-label="需求" rows={3} maxLength={20000} value={text} onChange={event => setText(event.target.value)}
      onKeyDown={event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
      placeholder={kind === "component" ? "要加什么、为什么，例如：给执行模型加一个读取 PDF 的工具" : "哪里出了问题、怎么复现，例如：任务暂停后点继续，状态一直显示排队中"} />
    <div className="engineer-request-bar">
      <small className="muted">{kind === "component" ? "先写方案给你批准，再动手；改好后由你确认才生效。" : "改好并通过检查后自动生效。"} · Ctrl+Enter 提交</small>
      <button type="submit" className="primary" disabled={busy || !text.trim()}>{busy ? "提交中…" : "交给平台工程师"}</button>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}

function JobDetail({ job, working, runs, act, onOpenRun }: {
  job: EngineerJob; working: boolean; runs: RunRef[]; act: Act; onOpenRun: (id: string) => void;
}) {
  const [busy, setBusy] = useState(""), [error, setError] = useState("");
  const run = job.run_id ? runs.find(item => item.id === job.run_id) : undefined;
  async function post(action: string, label: string) {
    setBusy(label); setError("");
    const problem = await act(`jobs/${encodeURIComponent(job.id)}/${action}`);
    setBusy(""); if (problem) setError(problem);
  }
  const reply = (_id: string, body: { text: string; choice?: string }) => act(`jobs/${encodeURIComponent(job.id)}/reply`, body);
  const open = !["applied", "failed", "rejected", "cancelled", "applying"].includes(job.status);
  return <section className="panel engineer-job" aria-label="工作详情">
    <div className="engineer-job-head">
      <h3>{job.title || "平台改动"}</h3>
      <StatusChip status={job.status} />
      <span className="lead-model">{KIND[job.kind]} · {ORIGIN[job.origin]}{job.model_id ? ` · ${job.model_id}` : ""} · 花费 ${job.cost_usd.toFixed(4)}</span>
    </div>
    {job.run_id && <p className="muted">来自任务{run ? <button type="button" className="link" onClick={() => onOpenRun(job.run_id!)}>「{run.plan.summary}」</button> : `（${job.run_id}，已删除）`}{job.origin === "lead" ? "，结束后结果会交回负责人决定那一步怎么办。" : ""}</p>}
    <details className="lead-instructions" open={!job.plan}><summary>需求</summary><p>{job.request}</p></details>
    {working && <p className="diagnosis-running"><span className="dx-typing" aria-hidden="true"><i /><i /><i /></span>
      {job.status === "planning" ? "平台工程师正在读代码、写方案…" : job.status === "validating" ? "正在独立运行测试和构建…" : "平台工程师正在修改源码副本…"}</p>}
    {job.plan && <div className="engineer-section"><h4>方案</h4><div className="dx-report"><MarkdownView text={job.plan} /></div></div>}
    {job.status === "plan_ready" && <div className="engineer-actions">
      <button type="button" className="primary" disabled={!!busy} onClick={() => post("approve", "approve")}>{busy === "approve" ? "提交中…" : "批准方案，开始实施"}</button>
      <button type="button" className="ghost" disabled={!!busy} onClick={() => post("reject", "reject")}>放弃</button>
    </div>}
    {job.report && <div className="engineer-section"><h4>{job.status === "applied" ? "结果" : "工程师的报告"}</h4><div className="dx-report"><MarkdownView text={job.report} /></div></div>}
    {!!job.files?.length && <div className="engineer-section"><h4>改动的文件</h4><ul className="engineer-files">{job.files.map(file => <li key={file}><code>{file}</code></li>)}</ul>
      {!!job.checks?.length && <p className="muted">检查通过：{job.checks.join("、")}</p>}
      {job.diff && <details className="engineer-diff"><summary>查看改动内容</summary><pre>{job.diff}</pre></details>}</div>}
    {job.status === "ready" && <div className="engineer-actions">
      <button type="button" className="primary" disabled={!!busy} onClick={() => post("approve", "apply")}>{busy === "apply" ? "提交中…" : "应用这项改动"}</button>
      <button type="button" className="ghost" disabled={!!busy} onClick={() => post("reject", "reject")}>放弃</button>
      <small className="muted">应用时会等进行中的模型调用结束，再重启服务。</small>
    </div>}
    {job.commit && <p className="muted">已提交到 git：<code>{job.commit.slice(0, 8)}</code>（没有推送）</p>}
    {job.error && <p className="error">{job.error}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {!!job.messages?.length && <div className="engineer-section"><h4>经过</h4><ol className="engineer-thread">
      {job.messages.map((message, index) => <li key={index} className={`engineer-msg ${message.role}`}>
        <small>{SPEAKER[message.role] ?? message.role} · {when(message.created_at)}</small>
        <div>{message.role === "engineer" ? <MarkdownView text={message.text} /> : <p>{message.text}</p>}</div>
      </li>)}
    </ol></div>}
    {(job.status === "plan_ready" || job.status === "ready" || job.status === "needs_input") && <Composer
      item={{ id: job.id, trigger: "engineer", status: "done", created_at: job.created_at, question: job.question, choices: job.choices ?? [] } satisfies Diagnosis}
      waiting={job.status === "needs_input"} disabled={false} onReply={reply}
      sendLabel={job.status === "needs_input" ? "回复" : job.status === "plan_ready" ? "按这个意见改方案" : "按这个意见再改"}
      placeholder={job.status === "needs_input" ? "回答工程师的问题" : "写下要改的地方"} />}
    {open && job.status !== "plan_ready" && job.status !== "ready" && <div className="engineer-actions">
      <button type="button" className="ghost" disabled={!!busy} onClick={() => post("reject", "reject")}>{busy === "reject" ? "提交中…" : "放弃这项工作"}</button>
    </div>}
  </section>;
}
