import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { CAPABILITIES, Radar, score, type CapabilityKey, type Model } from "./model-profiles";
import { formatUsd, statusLabel } from "./mission-history";
import { MarkdownView } from "./markdown";

export type PlanNode = {
  id: string; title: string; model_id: string; status: string; estimated_cost_usd: number;
  capability?: string; dependencies?: string[]; needs_workspace?: boolean; result?: string;
  difficulty?: string; quality_threshold?: number; routing_note?: string | null; started_at?: string | null;
  acceptance_criteria?: string; verifies?: string[]; attempts?: NodeAttempt[]; failed_attempts?: number;
  feedback?: string | null; unable_reason?: string | null; review_passed?: boolean | null; review?: string | null; summary?: string | null;
  changed_files?: string[]; inputs?: { node_id: string; form: string; changed_files: number }[];
  files?: string[]; reads?: string[]; provides?: string; check?: string;
};
type ToolCall = { turn: number; tool: string; detail?: string; ok?: boolean };
type NodeAttempt = { model_id?: string | null; outcome: string; reason?: string | null; reviewer_model_id?: string | null; cost_usd: number; finished_at?: string;
  turns?: number; turn_limit?: boolean; tool_calls?: ToolCall[] };
const TOOL_LABELS: Record<string, string> = { list_dir: "列目录", read_file: "读文件", write_file: "写文件", edit_file: "改文件", run_command: "运行命令" };

/** Where an API model's turns went: calls per tool, then every call, folded by default. */
function ToolLog({ calls }: { calls: ToolCall[] }) {
  const counts = new Map<string, [number, number]>();
  for (const call of calls) {
    const [n, bad] = counts.get(call.tool) ?? [0, 0];
    counts.set(call.tool, [n + 1, bad + (call.ok === false ? 1 : 0)]);
  }
  return <details className="tool-log">
    <summary>工具调用 {calls.length} 次：{[...counts].map(([tool, [n, bad]]) => `${TOOL_LABELS[tool] ?? tool} ${n}${bad ? `（失败 ${bad}）` : ""}`).join(" · ")}</summary>
    <ol>{calls.map((call, index) => <li key={index} className={call.ok === false ? "bad" : undefined}>
      <span className="tool-turn">第 {call.turn} 轮</span><span className="tool-name">{TOOL_LABELS[call.tool] ?? call.tool}</span><code>{call.detail}</code>{call.ok === false && <em>失败</em>}
    </li>)}</ol>
  </details>;
}

const OUTCOME_LABELS: Record<string, string> = { passed: "通过", rejected: "验收未通过", unverified: "尚未验证", superseded: "上游返工，结果失效", budget: "预算不足", cancelled: "已中止", error: "执行出错", verify_failed: "验证发现问题", quota: "额度用完", unavailable: "模型服务暂时不可用", unable: "无法完成" };
const INPUT_LABELS: Record<string, string> = { full: "完整结果", summary: "摘要", truncated: "截断的结果" };

/** One line on the card saying where the node stands right now. */
function nodeLine(node: PlanNode, nodes: PlanNode[], number: Map<string, string>): { tone: string; text: string } {
  const attempts = node.attempts ?? [], last = attempts[attempts.length - 1];
  if (node.status === "succeeded") return { tone: "ok", text: node.verifies?.length ? "✓ 验证通过" : node.review_passed ? "✓ 验收通过" : "✓ 完成" };
  if (node.status === "failed") return { tone: "bad", text: `✗ ${last ? OUTCOME_LABELS[last.outcome] ?? "失败" : "失败"}` };
  if (node.status === "unable") return { tone: "bad", text: "⊘ 当前环境无法完成" };
  if (node.status === "blocked") return { tone: "muted", text: "前置步骤无法完成，未执行" };
  if (node.status === "running") return { tone: node.feedback ? "warn" : "run", text: node.feedback ? `↻ 第 ${attempts.length + 1} 次尝试中` : "执行中…" };
  if (node.status === "cancelled") return { tone: "muted", text: "已取消" };
  if (node.feedback) return { tone: "warn", text: `↻ 待重做（${last ? OUTCOME_LABELS[last.outcome] ?? last.outcome : "返工"}）` };
  const waiting = (node.dependencies ?? []).filter(id => nodes.find(item => item.id === id)?.status !== "succeeded").map(id => number.get(id)).filter(Boolean);
  return { tone: "muted", text: waiting.length ? `等待 ${waiting.join("、")}` : "可以开始" };
}

const DIFFICULTY_LABELS: Record<string, string> = { easy: "简单", normal: "一般", hard: "困难" };

/** Why the router picked this model for the node, in the router's own terms (routing.py). */
function selectionReason(node: PlanNode, model?: Model): string {
  if (node.routing_note) return `${DIFFICULTY_LABELS[node.difficulty ?? "normal"] ?? ""}任务：${node.routing_note}`;
  const label = CAPABILITY_LABELS[node.capability ?? ""] ?? node.capability ?? "";
  const value = model && node.capability ? score(model, node.capability as CapabilityKey) : null;
  const threshold = node.quality_threshold !== undefined ? ` ≥ 门槛 ${Math.round(node.quality_threshold)}` : "";
  const fit = value !== null ? `${label} ${value}${threshold}` : `${label}${threshold}`;
  return node.difficulty === "hard"
    ? `困难任务：在可用模型中选${label}分最高的。${fit}。`
    : `${DIFFICULTY_LABELS[node.difficulty ?? "normal"]}任务：在达到门槛的模型中，优先会员 CLI，其次最便宜。${fit}。`;
}

const CAPABILITY_LABELS: Record<string, string> = Object.fromEntries(CAPABILITIES.map(({ key, label }) => [key, label]));

/** Group nodes into stages: a node sits one stage after its latest dependency. */
export function planStages(nodes: PlanNode[]): PlanNode[][] {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const depth = new Map<string, number>();
  const visit = (node: PlanNode, trail: Set<string>): number => {
    if (depth.has(node.id)) return depth.get(node.id)!;
    if (trail.has(node.id)) return 0; // a cycle; the executor rejects it, keep the view usable
    trail.add(node.id);
    const parents = (node.dependencies ?? []).map(id => byId.get(id)).filter((item): item is PlanNode => !!item);
    const value = parents.length ? Math.max(...parents.map(parent => visit(parent, trail))) + 1 : 0;
    trail.delete(node.id);
    depth.set(node.id, value);
    return value;
  };
  const stages: PlanNode[][] = [];
  for (const node of nodes) (stages[visit(node, new Set())] ??= []).push(node);
  return stages.filter(Boolean);
}

export function PlanGraph({ apiBase, runId, runStatus, nodes, inProject, models, onOpenProfile, reworkRounds = 0 }: { apiBase: string; runId: string; runStatus: string; nodes: PlanNode[]; inProject: boolean; models: Model[]; onOpenProfile: (model: Model) => void; reworkRounds?: number }) {
  const [active, setActive] = useState<string | null>(null), [openNode, setOpenNode] = useState<PlanNode | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => setSelectedId(null), [runId]);
  const stages = planStages(nodes);
  const number = new Map(nodes.map((node, index) => [node.id, String(index + 1).padStart(2, "0")]));
  const modelById = new Map(models.map(model => [model.id, model]));
  const widest = Math.max(0, ...stages.map(stage => stage.length));
  const activeNode = nodes.find(node => node.id === active);
  const related = new Set(activeNode ? [activeNode.id, ...(activeNode.dependencies ?? []), ...nodes.filter(node => node.dependencies?.includes(activeNode.id)).map(node => node.id)] : []);
  const retries = nodes.reduce((sum, node) => sum + Math.max(0, (node.attempts?.length ?? 0) - 1), 0);
  const selected = nodes.find(node => node.id === selectedId);
  // Dependency lines between cards, measured after layout and redrawn when the board resizes.
  const board = useRef<HTMLDivElement>(null);
  const [links, setLinks] = useState<{ id: string; from: string; to: string; d: string; done: boolean }[]>([]);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const shape = nodes.map(node => `${node.id}:${node.status}:${(node.dependencies ?? []).join(",")}`).join("|");
  useLayoutEffect(() => {
    const element = board.current;
    if (!element) return;
    const draw = () => {
      const box = element.getBoundingClientRect();
      const cards = new Map([...element.querySelectorAll<HTMLElement>("[data-node]")].map(card => [card.dataset.node!, card.getBoundingClientRect()]));
      const next = [];
      for (const node of nodes) for (const dependency of node.dependencies ?? []) {
        const from = cards.get(dependency), to = cards.get(node.id);
        if (!from || !to || !from.width) continue;
        const x1 = from.right - box.left + element.scrollLeft, y1 = from.top + from.height / 2 - box.top;
        const x2 = to.left - box.left + element.scrollLeft, y2 = to.top + to.height / 2 - box.top;
        const middle = (x1 + x2) / 2;
        next.push({ id: `${dependency}>${node.id}`, from: dependency, to: node.id, d: `M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`,
          done: nodes.find(item => item.id === dependency)?.status === "succeeded" });
      }
      setLinks(next);
      // Size from the stages themselves; sizing from the scroll box would keep it from shrinking.
      const stagesList = element.querySelector<HTMLElement>(".plan-stages");
      setSize({ width: stagesList?.offsetWidth ?? 0, height: stagesList?.offsetHeight ?? 0 });
    };
    draw();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(draw);
    observer?.observe(element);
    window.addEventListener("resize", draw);
    return () => { observer?.disconnect(); window.removeEventListener("resize", draw); };
  }, [shape]);
  if (!nodes.length) return null;
  return <div className="plan-graph">
    <div className="plan-summary" aria-label="计划概览">
      <span><b>{nodes.length}</b> 个子任务</span>
      <span><b>{stages.length}</b> 个阶段</span>
      <span>{widest > 1 ? <>最多 <b>{widest}</b> 项可并行</> : "依次执行"}</span>
      {retries > 0 && <span>重试 <b>{retries}</b> 次</span>}
      {reworkRounds > 0 && <span>返工 <b>{reworkRounds}</b> 轮</span>}
      <small className="plan-hint">点击子任务查看进行情况</small>
    </div>
    <div className="plan-board" ref={board}>
      <svg className="plan-links" width={size.width} height={size.height} aria-hidden="true">
        {links.map(link => <path key={link.id} d={link.d} className={`${link.done ? "done" : ""} ${active ? link.from === active || link.to === active ? "lit" : "faded" : ""}`} />)}
      </svg>
      <ol className="plan-stages">
        {stages.map((stage, index) => {
          const editing = inProject ? stage.filter(node => node.needs_workspace).length : 0;
          return <li key={index} className="plan-stage">
            <div className="plan-stage-head">
              <b>阶段 {index + 1}</b>
              <span className={`plan-stage-mode ${stage.length > 1 ? "parallel" : ""}`}>{stage.length > 1 ? `${stage.length} 项并行` : "单项"}</span>
              {editing > 1 && <small>改动项目的依次执行</small>}
            </div>
            <div className="plan-stage-nodes">
              {stage.map(node => {
                const model = modelById.get(node.model_id);
                const line = nodeLine(node, nodes, number);
                const state = active ? related.has(node.id) ? node.id === active ? "focus" : "related" : "dimmed" : "";
                const open = () => setSelectedId(current => current === node.id ? null : node.id);
                return <article key={node.id} data-node={node.id} className={`plan-node ${node.status} ${state} ${selectedId === node.id ? "selected" : ""}`}
                  onClick={open} onMouseEnter={() => setActive(node.id)} onMouseLeave={() => setActive(null)}>
                  <div className="plan-node-line"><span className="num">{number.get(node.id)}</span>
                    <button type="button" className="plan-node-title" aria-expanded={selectedId === node.id} onClick={event => { event.stopPropagation(); open(); }} onFocus={() => setActive(node.id)} onBlur={() => setActive(null)}>{node.title}</button>
                    <span className={`state ${node.status}`}>{statusLabel(node.status)}</span></div>
                  <p className={`plan-check ${line.tone}`} title={node.review ?? undefined}>{line.text}</p>
                  <button type="button" className="plan-model" onClick={event => { event.stopPropagation(); setOpenNode(node); }} title={`${selectionReason(node, model)}点击查看模型能力图。`}>{model?.display_name ?? node.model_id ?? "未分配模型"}</button>
                </article>;
              })}
            </div>
          </li>;
        })}
      </ol>
    </div>
    {selected && <NodeProgress apiBase={apiBase} runId={runId} runStatus={runStatus} node={selected} nodes={nodes} number={number}
      model={modelById.get(selected.model_id)} onOpenModel={() => setOpenNode(selected)} onClose={() => setSelectedId(null)} />}
    {openNode && <ModelCapabilityDialog node={openNode} model={modelById.get(openNode.model_id)} onClose={() => setOpenNode(null)} onOpenProfile={model => { setOpenNode(null); onOpenProfile(model); }} />}
  </div>;
}

type TimelineEvent = { id: number; kind: string; data: Record<string, any>; created_at: string };
const ACTIVE_RUN = new Set(["queued", "running"]);

function duration(ms: number) {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} 分 ${seconds % 60} 秒` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function clock(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("zh-CN", { hour12: false });
}

/** One line of the node's timeline, or null for events about other nodes. */
function describe(event: TimelineEvent, nodeId: string, number: Map<string, string>): string | null {
  const data = event.data ?? {};
  if (event.kind === "run.rework" && (data.reopened ?? []).includes(nodeId)) {
    return data.verifier === nodeId ? `验证未通过，第 ${data.round} 轮返工：${data.problems ?? ""}` : `被 ${number.get(data.verifier) ?? "验证步骤"} 退回返工（第 ${data.round} 轮）：${data.problems ?? ""}`;
  }
  if (data.node_id !== nodeId) return null;
  switch (event.kind) {
    case "node.started": return `开始第 ${data.attempt ?? 1} 次执行${data.model_id ? `（${data.model_id}）` : ""}`;
    case "node.succeeded": return `完成${data.used_model_id && data.used_model_id !== data.selected_model_id ? `（实际由 ${data.used_model_id} 完成）` : ""} · ${formatUsd(data.cost_usd ?? 0)}`;
    case "node.retry": return `未通过，准备第 ${data.attempt} 次尝试${data.model_id ? `（换用 ${data.model_id}）` : ""}：${data.reason ?? ""}`;
    case "node.failed": return `失败：${data.error ?? ""}`;
    default: return null;
  }
}

/** Live progress of one node: what it is doing now, its timeline, checks, inputs and output. */
function NodeProgress({ apiBase, runId, runStatus, node, nodes, number, model, onOpenModel, onClose }: { apiBase: string; runId: string; runStatus: string; node: PlanNode; nodes: PlanNode[]; number: Map<string, string>; model?: Model; onOpenModel: () => void; onClose: () => void }) {
  const [events, setEvents] = useState<TimelineEvent[]>([]), [now, setNow] = useState(() => Date.now());
  const attempts = node.attempts ?? [], inputs = node.inputs ?? [], files = node.changed_files ?? [];
  const live = ACTIVE_RUN.has(runStatus);
  useEffect(() => {
    let cancelled = false;
    const load = () => fetch(`${apiBase}/api/tasks/${encodeURIComponent(runId)}/timeline`).then(r => r.ok ? r.json() : []).then(list => { if (!cancelled && Array.isArray(list)) setEvents(list); }).catch(() => {});
    load();
    const timer = live ? window.setInterval(load, 3000) : undefined;
    return () => { cancelled = true; if (timer) window.clearInterval(timer); };
  }, [apiBase, runId, live, node.id, node.status, attempts.length]);
  useEffect(() => {
    if (node.status !== "running") return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [node.status]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  const started = node.started_at ? new Date(node.started_at).getTime() : null;
  const ended = attempts.length ? new Date(attempts[attempts.length - 1].finished_at ?? "").getTime() : NaN;
  const waitingFor = (node.dependencies ?? []).filter(id => nodes.find(item => item.id === id)?.status !== "succeeded").map(id => number.get(id)).filter(Boolean);
  const now_text =
    node.status === "running" ? `第 ${attempts.length + 1} 次执行中${started ? ` · 已运行 ${duration(now - started)}` : ""}`
    : node.status === "pending" ? (waitingFor.length ? `等待 ${waitingFor.join("、")} 完成` : node.feedback ? "等待重新执行" : live ? "等待空位或预算" : "尚未开始")
    : node.status === "succeeded" ? `已完成${started && !Number.isNaN(ended) ? ` · 本次用时 ${duration(ended - started)}` : ""}`
    : node.status === "failed" ? `已失败（共 ${attempts.length} 次尝试）`
    : statusLabel(node.status);
  const lines = events.map(event => ({ event, text: describe(event, node.id, number) })).filter(item => item.text);
  return <section className={`node-progress drawer ${node.status}`} role="dialog" aria-label={`${node.title} 的进行情况`}>
    <div className="node-progress-head">
      <div><p className="eyebrow">子任务 {number.get(node.id)} · 进行情况</p><h4>{node.title}</h4></div>
      <span className={`state ${node.status}`}>{statusLabel(node.status)}</span>
      <button type="button" className="modal-close" onClick={onClose} aria-label="关闭进行情况">×</button>
    </div>
    <p className={`node-progress-now ${node.status}`}>{node.status === "running" && <i className="pulse" aria-hidden="true" />}{now_text}</p>
    <dl className="node-progress-facts">
      <div><dt>模型</dt><dd><button type="button" className="plan-model" onClick={onOpenModel}>{model?.display_name ?? node.model_id ?? "未分配"}</button></dd></div>
      {node.difficulty && <div><dt>难度</dt><dd>{DIFFICULTY_LABELS[node.difficulty] ?? node.difficulty} · 门槛 {Math.round(node.quality_threshold ?? 0)}</dd></div>}
      {node.capability && <div><dt>能力</dt><dd>{CAPABILITY_LABELS[node.capability] ?? node.capability}</dd></div>}
      <div><dt>依赖</dt><dd className="plan-deps">{(node.dependencies ?? []).filter(id => number.has(id)).map(id => <i key={id}>{number.get(id)}</i>)}{!(node.dependencies ?? []).length && "无"}</dd></div>
      {!!node.verifies?.length && <div><dt>验证</dt><dd className="plan-verifies">{node.verifies.filter(id => number.has(id)).map(id => <i key={id}>{number.get(id)}</i>)}</dd></div>}
      {node.needs_workspace && <div><dt>工作位置</dt><dd><em className="chip">改动项目</em></dd></div>}
      <div><dt>花费</dt><dd>{formatUsd(attempts.reduce((sum, attempt) => sum + attempt.cost_usd, 0))} / 预估 {formatUsd(node.estimated_cost_usd)}</dd></div>
    </dl>
    <p className="node-progress-reason">{selectionReason(node, model)}</p>
    <div className="node-progress-body">
      <section className="node-progress-timeline"><h5>时间线</h5>
        {lines.length ? <ol>{lines.map(({ event, text }) => <li key={event.id} className={event.kind.replace(".", "-")}><time>{clock(event.created_at)}</time><span>{text}</span></li>)}</ol> : <p className="muted">还没有执行记录。</p>}
      </section>
      <div className="node-progress-side">
        {node.unable_reason && <section className="unable-reason"><h5>无法完成的原因</h5><MarkdownView text={node.unable_reason} /></section>}
        {node.acceptance_criteria && <section><h5>验收标准</h5><p>{node.acceptance_criteria}</p></section>}
        {!!(node.files?.length || node.reads?.length) && <section className="node-scope"><h5>工作范围</h5>
          {!!node.files?.length && <p>改动：{node.files.join("、")}</p>}
          {!!node.reads?.length && <p>参考：{node.reads.join("、")}</p>}
          {node.provides && <p>交付：{node.provides}</p>}
          {node.check && <p>检查：<code>{node.check}</code></p>}
        </section>}
        {node.review && <section><h5>验收意见</h5><p>{node.review}</p></section>}
        {node.feedback && node.status !== "succeeded" && <section><h5>需要修改</h5><p>{node.feedback}</p></section>}
        {!!inputs.length && <section><h5>收到的上游结果</h5><p>{inputs.map(input => `${number.get(input.node_id) ?? "?"} 的${INPUT_LABELS[input.form] ?? input.form}${input.changed_files ? `（改动 ${input.changed_files} 个文件）` : ""}`).join("，")}</p></section>}
        {!!files.length && <section><h5>改动的文件（{files.length}）</h5><ul className="plan-files">{files.slice(0, 50).map(file => <li key={file}>{file}</li>)}{files.length > 50 && <li>…另外 {files.length - 50} 个</li>}</ul></section>}
      </div>
    </div>
    {!!attempts.length && <section className="node-progress-attempts"><h5>尝试记录</h5><ol className="plan-attempts">{attempts.map((attempt, index) => <li key={index} className={attempt.outcome}>
      <b>第 {index + 1} 次 · {OUTCOME_LABELS[attempt.outcome] ?? attempt.outcome}</b>
      <small>{attempt.model_id ?? "未知模型"}{attempt.reviewer_model_id ? ` · 验收：${attempt.reviewer_model_id}` : ""} · {formatUsd(attempt.cost_usd)}{attempt.finished_at ? ` · ${clock(attempt.finished_at)}` : ""}
        {!!attempt.turns && <span className={`attempt-turns${attempt.turn_limit ? " limit" : ""}`}>{attempt.turns} 轮{attempt.turn_limit ? " · 轮次用完" : ""}</span>}</small>
      {attempt.reason && <p>{attempt.reason}</p>}
      {!!attempt.tool_calls?.length && <ToolLog calls={attempt.tool_calls} />}
    </li>)}</ol></section>}
    {node.result && <section className="node-progress-output"><h5>{node.status === "failed" ? "错误信息" : "输出"}</h5><MarkdownView text={node.result} markdown={node.status !== "failed"} /></section>}
  </section>;
}

function ModelCapabilityDialog({ node, model, onClose, onOpenProfile }: { node: PlanNode; model?: Model; onClose: () => void; onOpenProfile: (model: Model) => void }) {
  const id = node.model_id;
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  return <div className="modal-backdrop" onMouseDown={onClose}>
    <section className="provider-modal model-capability-modal" role="dialog" aria-modal="true" aria-label={`${model?.display_name ?? id} 的能力图`} onMouseDown={event => event.stopPropagation()}>
      <div className="modal-title"><div><p className="eyebrow">执行模型</p><h3>{model?.display_name ?? id}</h3></div><button type="button" className="modal-close" onClick={onClose} aria-label="关闭">×</button></div>
      <p className="selection-reason"><b>为什么选它</b>{selectionReason(node, model)}</p>
      {!model ? <p className="modal-note">模型目录里已没有这个模型的信息。</p> : <>
        <Radar model={model} />
        {model.scores && <dl className="capability-scores">{CAPABILITIES.map(({ key, label }) => <div key={key}><dt>{label}</dt><dd>{score(model, key) ?? "—"}</dd></div>)}</dl>}
        {model.scores_basis && <p className="modal-note">{model.scores_basis}</p>}
        <button type="button" className="ghost capability-profile-link" onClick={() => onOpenProfile(model)}>查看完整画像 →</button>
      </>}
    </section>
  </div>;
}
