import React, { useCallback, useEffect, useState } from "react";
import { MarkdownView } from "./markdown";
import { ENGINEER_STATUS, type EngineerJob } from "./engineer";

type Preferences = { enabled: boolean; budget_usd: number };
type Metrics = { steps: number; cost_per_step: number; turns_per_step: number; failures_per_step: number; success_rate: number; problem_rate: number };
type Effect = { run_id: string; before: Metrics; after: Metrics; delta: Partial<Metrics>; comparable: boolean; note: string };
type Improvement = {
  id: string; kind: "skill" | "rule" | "platform"; title: string; rationale: string; content: string; status: string;
  tools: string[]; capability?: string; source_nodes: string[]; effects: Effect[]; engineer_job?: EngineerJob | null;
  validation: { passed?: boolean; reason?: string; output?: string; command?: string; checks?: string[] };
};
type LearningData = {
  automatic_skip_reason?: string | null;
  review?: { status: string; conclusion: string; budget_usd: number; cost_usd: number; allocated_usd: number; error?: string } | null;
  retrospective?: { estimated_usd: number; actual_usd: number; problems: { label: string; kind: string; key: string; topic?: string }[] } | null;
  improvements: Improvement[];
  applied: { id: string; title: string; node_ids: string[]; planning: boolean; source_run_id: string }[];
};
const STATUS: Record<string, string> = {
  queued: "等待复盘", analyzing: "生成结论中", validating: "验证中", done: "复盘完成", blocked: "等待处理",
  failed: "未通过", proposed: "待验证", active: "已生效", engineering: "平台工程师处理中", disabled: "已停用", skipped: "已跳过自动复盘",
};
const KIND = { skill: "Skill", rule: "执行规则", platform: "平台改进" };
const money = (n: number) => `$${n.toFixed(4)}`;

async function request(url: string, body?: unknown, method = "POST") {
  const response = await fetch(url, body === undefined ? undefined : {
    method, headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `请求失败（${response.status}）`);
  return data;
}

function Effects({ effects }: { effects: Effect[] }) {
  if (!effects.length) return <p className="muted">等待后续任务应用，再对比效果。</p>;
  const valid = effects.filter(item => item.comparable);
  const latest = valid.at(-1);
  return <div className="learning-effects">
    <p>后续应用 {effects.length} 次 · 可对比 {valid.length} 次</p>
    {latest && <table><caption>最近一次可对比任务：{latest.run_id.slice(0, 12)}</caption>
      <thead><tr><th>指标</th><th>改进前</th><th>应用后</th></tr></thead>
      <tbody>{([
        ["每步费用", money(latest.before.cost_per_step), money(latest.after.cost_per_step)],
        ["每步轮数", latest.before.turns_per_step, latest.after.turns_per_step],
        ["每步失败尝试", latest.before.failures_per_step, latest.after.failures_per_step],
        ["步骤成功率", `${latest.before.success_rate * 100}%`, `${latest.after.success_rate * 100}%`],
        ["每步同类问题", latest.before.problem_rate, latest.after.problem_rate],
      ]).map(([label, before, after]) => <tr key={label}><th>{label}</th><td>{before}</td><td>{after}</td></tr>)}</tbody>
    </table>}
    <small>{(latest ?? effects.at(-1))?.note}</small>
  </div>;
}

export function RetrospectivePanel({ apiBase, runId }: { apiBase: string; runId: string }) {
  const [data, setData] = useState<LearningData | null>(null), [options, setOptions] = useState<Preferences | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<Record<string, string>>({});
  const base = `${apiBase}/api/tasks/${encodeURIComponent(runId)}/learning`;
  const load = useCallback(async () => { setData(await request(base)); }, [base]);
  useEffect(() => {
    let alive = true;
    setData(null); setError("");
    Promise.all([request(base), request(`${apiBase}/api/improvements/settings`)]).then(([result, prefs]) => {
      if (alive) { setData(result); setOptions(prefs); }
    }).catch(e => { if (alive) setError(String(e.message)); });
    return () => { alive = false; };
  }, [base, apiBase]);
  const working = !!data?.review && ["queued", "analyzing", "validating"].includes(data.review.status);
  const engineering = data?.improvements.some(item => item.status === "engineering");
  useEffect(() => {
    if (!working && !engineering) return;
    const timer = window.setInterval(() => load().catch(e => setError(e.message)), 3000);
    return () => window.clearInterval(timer);
  }, [working, engineering, load]);
  async function act(url: string, body = {}, method = "POST") {
    setBusy(true); setError(""); setNotice("");
    try { await request(url, body, method); await load(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  async function saveOptions() {
    if (!options) return;
    setBusy(true); setError("");
    try { setOptions(await request(`${apiBase}/api/improvements/settings`, options, "PUT")); setNotice("自动改进设置已保存。"); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  return <section className="learning-panel" aria-label="复盘与改进">
    <p>复盘结论 → 改进项 → 验证与应用 → 后续任务使用 → 效果对比</p>
    {options && <details className="learning-settings"><summary>自动改进设置 · {options.enabled ? "已开启" : "未开启"}</summary>
      <label><input type="checkbox" checked={options.enabled} onChange={e => setOptions({ ...options, enabled: e.target.checked })} />任务自然成功或失败后自动复盘并验证改进（手动取消不触发）</label>
      <label>每次复盘与改进的额外预算上限（美元）<input type="number" min="0" max="100" step="0.1" value={options.budget_usd}
        onChange={e => setOptions({ ...options, budget_usd: Number(e.target.value) })} /></label>
      <small>预算包括结论生成、改进验证和分配给平台工程师的额度。Skill 与执行规则验证后自动用于匹配的后续任务；平台代码改动只写成方案，要你在平台工程师页批准后才改，改好后再批准才生效。</small>
      <button onClick={saveOptions} disabled={busy || !Number.isFinite(options.budget_usd) || options.budget_usd < 0 || options.budget_usd > 100}>保存自动改进设置</button>
    </details>}
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!data ? <p className="empty">{error ? "无法读取复盘记录。" : "正在读取复盘…"}</p> : <>
      {data.automatic_skip_reason && <p className="muted">{data.automatic_skip_reason}</p>}
      <div className="learning-head">
        <span>{data.review ? STATUS[data.review.status] ?? data.review.status : "尚未生成复盘结论"}</span>
        <button className="primary" disabled={busy || working || data.review?.status === "done"} onClick={() => act(base)}>
          {working ? "复盘进行中…" : data.automatic_skip_reason ? "手动生成复盘与改进项" : data.review ? "继续复盘" : "生成复盘与改进项"}</button>
      </div>
      {data.retrospective && <p className="muted">任务估算 {money(data.retrospective.estimated_usd)} · 实际 {money(data.retrospective.actual_usd)} · 记录问题 {data.retrospective.problems.length} 项</p>}
      {data.retrospective && data.retrospective.problems.some(p => p.topic) && <ul className="problem-list">{data.retrospective.problems.map((p, i) =>
        <li key={i}><b>{p.topic ?? "未归类"}</b> <small>{p.kind}:{p.key}</small>{p.label && <> · {p.label}</>}</li>)}</ul>}
      {data.review && <>
        <p className="muted">复盘与验证已花 {money(data.review.cost_usd)} · 平台工程师已分配额度 {money(data.review.allocated_usd)} · 合计预算上限 {money(data.review.budget_usd)}</p>
        {data.review.error && <p className="error">{data.review.error}</p>}
        {data.review.conclusion && <MarkdownView text={data.review.conclusion} />}
      </>}
      {!!data.applied.length && <details open><summary>这次任务使用的历史改进（{data.applied.length}）</summary>
        <ul>{data.applied.map(item => <li key={item.id}>{item.title} · {item.planning ? "已用于规划；" : ""}用于 {item.node_ids.length} 个步骤</li>)}</ul>
      </details>}
      <div className="learning-items">{data.improvements.map(item => <article className="learning-item" key={item.id}>
        <div className="learning-head"><b>{item.title}</b><span>{KIND[item.kind]} · {STATUS[item.status] ?? item.status}</span></div>
        <p>{item.rationale}</p>
        <small>适用：{item.tools.join("、") || item.capability} · 来源步骤：{item.source_nodes.join("、")}</small>
        <details><summary>改进内容与验证记录</summary><MarkdownView text={item.content} />
          {item.validation.reason && <p>{item.validation.reason}</p>}
          {item.validation.command && <pre>{item.validation.command}</pre>}
          {item.validation.output && <pre>{item.validation.output}</pre>}
          {!!item.validation.checks?.length && <p>通过：{item.validation.checks.join("、")}</p>}
        </details>
        {item.engineer_job && <div className="learning-engineer">
          <p>平台工程师：{ENGINEER_STATUS[item.engineer_job.status]?.label ?? item.engineer_job.status} · 已花 {money(item.engineer_job.cost_usd)}</p>
          {(item.engineer_job.plan || item.engineer_job.report || item.engineer_job.diff) && <details><summary>方案、报告与改动</summary>
            <MarkdownView text={item.engineer_job.plan ?? ""} /><MarkdownView text={item.engineer_job.report ?? ""} />
            {item.engineer_job.diff && <pre>{item.engineer_job.diff}</pre>}
          </details>}
          {item.engineer_job.error && <p className="error">{item.engineer_job.error}</p>}
          {["plan_ready", "ready"].includes(item.engineer_job.status) && <button disabled={busy}
            onClick={() => act(`${apiBase}/api/engineer/jobs/${item.engineer_job!.id}/approve`)}>
            {item.engineer_job.status === "plan_ready" ? "批准改进方案" : "应用已验证改动"}</button>}
          {item.engineer_job.status === "needs_input" && <>
            <p>{item.engineer_job.question}</p>
            <textarea aria-label="回复平台工程师" value={reply[item.id] ?? ""} onChange={e => setReply({ ...reply, [item.id]: e.target.value })} />
            <button disabled={busy || !reply[item.id]?.trim()} onClick={() => act(`${apiBase}/api/engineer/jobs/${item.engineer_job!.id}/reply`, { text: reply[item.id] })}>回复平台工程师</button>
          </>}
        </div>}
        <Effects effects={item.effects} />
        {item.kind !== "platform" && item.status === "active" && <button className="ghost" disabled={busy}
          onClick={() => act(`${apiBase}/api/improvements/${item.id}/disable`)}>后续任务停用</button>}
      </article>)}</div>
      {data.review?.status === "done" && !data.improvements.length && <p className="empty">本次没有证据充分、需要新增的改进项。</p>}
    </>}
  </section>;
}
