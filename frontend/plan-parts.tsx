import React, { useEffect, useRef, useState } from "react";
import type { Model } from "./model-profiles";
import { formatUsd, statusLabel } from "./mission-history";
import { ModelCapabilityDialog, NodeProgress, PlanGraph, nodeLine, planStages, useLinks, type PlanNode } from "./plan-graph";

/** A part of a plan made top-down (backend outline.py): 大项 at the top, then smaller parts, then tasks. */
export type PlanPart = { id: string; title: string; goal?: string; acceptance?: string; parent_id?: string | null; dependencies?: string[] };

type Props = { apiBase: string; runId: string; runStatus: string; nodes: PlanNode[]; parts?: PlanPart[]; inProject: boolean; models: Model[]; onOpenProfile: (model: Model) => void; reworkRounds?: number };

/** Where a part stands, from its tasks. */
export function partState(tasks: PlanNode[]): { state: string; label: string } {
  const done = tasks.filter(task => task.status === "succeeded").length;
  if (tasks.length && done === tasks.length) return { state: "succeeded", label: "已完成" };
  if (tasks.some(task => task.status === "failed")) return { state: "failed", label: "有失败" };
  if (tasks.some(task => task.status === "unable" || task.status === "blocked")) return { state: "failed", label: "有无法完成的任务" };
  if (tasks.some(task => task.status === "running")) return { state: "running", label: "进行中" };
  if (done) return { state: "partial", label: "部分完成" };
  if (tasks.some(task => task.status === "cancelled")) return { state: "cancelled", label: "已取消" };
  return { state: "pending", label: "未开始" };
}

/** The progress view: a plan made in parts shows its major parts as stages, each opening on its
 * sub-parts and tasks; a flat plan shows its tasks. */
export function PlanView(props: Props) {
  return props.parts?.length ? <PartBoard {...props} parts={props.parts} /> : <PlanGraph {...props} />;
}

function PartBoard({ apiBase, runId, runStatus, parts, nodes, models, onOpenProfile, reworkRounds = 0, inProject }: Props & { parts: PlanPart[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null), [openModel, setOpenModel] = useState<PlanNode | null>(null);
  useEffect(() => { setExpanded(new Set()); setSelectedId(null); }, [runId]);
  const byId = new Map(parts.map(part => [part.id, part]));
  const parentOf = (part: PlanPart) => part.parent_id && byId.has(part.parent_id) ? part.parent_id : null;
  const childrenOf = (id: string | null) => parts.filter(part => parentOf(part) === id);
  const within = (id: string): Set<string> => new Set([id, ...childrenOf(id).flatMap(child => [...within(child.id)])]);
  const tasksOf = (id: string) => { const ids = within(id); return nodes.filter(node => node.part_id && ids.has(node.part_id)); };
  const numberOf = (part: PlanPart): string => {
    const parent = parentOf(part), own = String(childrenOf(parent).findIndex(item => item.id === part.id) + 1);
    return parent ? `${numberOf(byId.get(parent)!)}.${own}` : own;
  };
  const number = new Map(nodes.map((node, index) => [node.id, String(index + 1).padStart(2, "0")]));
  const modelById = new Map(models.map(model => [model.id, model]));
  const toggle = (id: string) => setExpanded(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const allOpen = parts.every(part => expanded.has(part.id));

  const top = childrenOf(null), shown = new Set(top.map(part => part.id));
  const stages = planStages(top.map(part => ({ ...part, dependencies: (part.dependencies ?? []).filter(id => shown.has(id)) })));
  const loose = nodes.filter(node => !node.part_id || !byId.has(node.part_id));
  const board = useRef<HTMLDivElement>(null);
  const edges = top.flatMap(part => (part.dependencies ?? []).filter(id => shown.has(id)).map(from => ({ from, to: part.id, done: partState(tasksOf(from)).state === "succeeded" })));
  const shape = [...expanded].join(",") + "|" + top.map(part => `${part.id}:${(part.dependencies ?? []).join(",")}`).join("|") + "|" + edges.map(edge => edge.done).join("");
  const { links, size } = useLinks(board, edges, "part", shape);
  const done = nodes.filter(node => node.status === "succeeded").length;
  const retries = nodes.reduce((sum, node) => sum + Math.max(0, (node.attempts?.length ?? 0) - 1), 0);
  const selected = nodes.find(node => node.id === selectedId);

  /** A part's own tasks and parts, in plan order. */
  function body(part: PlanPart): React.ReactNode {
    const first = (id: string) => { const index = nodes.findIndex(node => within(id).has(node.part_id ?? "")); return index < 0 ? nodes.length : index; };
    const items: [number, React.ReactNode][] = [
      ...nodes.map((node, index) => [index, node] as const).filter(([, node]) => node.part_id === part.id)
        .map(([index, node]): [number, React.ReactNode] => [index, task(node)]),
      ...childrenOf(part.id).map((child): [number, React.ReactNode] => [first(child.id), sub(child)]),
    ];
    return <ul className="part-body">{items.sort((a, b) => a[0] - b[0]).map(([, item]) => item)}</ul>;
  }

  function task(node: PlanNode): React.ReactNode {
    const line = nodeLine(node, nodes, number);
    return <li key={node.id} className={`part-task ${node.status} ${selectedId === node.id ? "selected" : ""}`}>
      <button type="button" className="part-task-title" aria-expanded={selectedId === node.id} onClick={() => setSelectedId(current => current === node.id ? null : node.id)}>
        <span className="num">{number.get(node.id)}</span><span>{node.title}</span><span className={`state ${node.status}`}>{statusLabel(node.status)}</span>
      </button>
      <p className={`plan-check ${line.tone}`}>{line.text}</p>
    </li>;
  }

  function sub(part: PlanPart): React.ReactNode {
    const tasks = tasksOf(part.id), state = partState(tasks), open = expanded.has(part.id);
    return <li key={part.id} className={`part-sub ${state.state}`}>
      <button type="button" className="part-sub-head" aria-expanded={open} onClick={() => toggle(part.id)}>
        <span className="part-caret" aria-hidden="true">{open ? "▾" : "▸"}</span><span className="num">{numberOf(part)}</span>
        <b>{part.title}</b><small>{tasks.filter(task => task.status === "succeeded").length}/{tasks.length}</small><span className={`state ${state.state}`}>{state.label}</span>
      </button>
      {open && body(part)}
    </li>;
  }

  return <div className="plan-graph part-board">
    <div className="plan-summary" aria-label="计划概览">
      <span><b>{top.length}</b> 个大项</span>
      <span><b>{nodes.length}</b> 个任务</span>
      <span>已完成 <b>{done}</b></span>
      {retries > 0 && <span>重试 <b>{retries}</b> 次</span>}
      {reworkRounds > 0 && <span>返工 <b>{reworkRounds}</b> 轮</span>}
      <button type="button" className="ghost part-toggle-all" aria-pressed={allOpen} onClick={() => setExpanded(allOpen ? new Set() : new Set(parts.map(part => part.id)))}>{allOpen ? "全部关闭" : "全部展开"}</button>
    </div>
    <div className="plan-board" ref={board}>
      <svg className="plan-links" width={size.width} height={size.height} aria-hidden="true">
        {links.map(link => <path key={link.id} d={link.d} className={link.done ? "done" : ""} />)}
      </svg>
      <ol className="plan-stages">
        {stages.map((stage, index) => <li key={index} className="plan-stage">
          <div className="plan-stage-head">
            <b>阶段 {index + 1}</b>
            <span className={`plan-stage-mode ${stage.length > 1 ? "parallel" : ""}`}>{stage.length > 1 ? `${stage.length} 项并行` : "单项"}</span>
          </div>
          <div className="plan-stage-nodes">{stage.map(part => {
            const tasks = tasksOf(part.id), finished = tasks.filter(task => task.status === "succeeded").length;
            const running = tasks.filter(task => task.status === "running").length, state = partState(tasks), open = expanded.has(part.id);
            const sub = childrenOf(part.id).length;
            const cost = tasks.reduce((sum, task) => sum + (task.attempts ?? []).reduce((total, attempt) => total + attempt.cost_usd, 0), 0);
            return <article key={part.id} data-part={part.id} className={`part-card ${state.state} ${open ? "open" : ""}`}>
              <button type="button" className="part-card-head" aria-expanded={open} onClick={() => toggle(part.id)} title={part.acceptance ? `完成标准：${part.acceptance}` : undefined}>
                <span className="part-card-line"><span className="num">{numberOf(part)}</span><b>{part.title}</b><span className={`state ${state.state}`}>{state.label}</span></span>
                {part.goal && <span className="part-goal">{part.goal}</span>}
                <span className="part-progress"><span className="part-bar" aria-hidden="true"><i style={{ width: `${tasks.length ? finished / tasks.length * 100 : 0}%` }} /></span>
                  <small>{finished} / {tasks.length} 个任务完成{running ? ` · ${running} 个进行中` : ""}</small></span>
                <span className="part-meta"><small>{sub ? `${sub} 个子项 · ` : ""}花费 {formatUsd(cost)}</small><span className="part-expand">{open ? "收起 ▴" : "展开 ▾"}</span></span>
              </button>
              {open && body(part)}
            </article>;
          })}</div>
        </li>)}
      </ol>
    </div>
    {!!loose.length && <section className="part-tasks"><h4>其他任务</h4>
      <PlanGraph apiBase={apiBase} runId={runId} runStatus={runStatus} nodes={loose} all={nodes} inProject={inProject} models={models} onOpenProfile={onOpenProfile} />
    </section>}
    {selected && <NodeProgress apiBase={apiBase} runId={runId} runStatus={runStatus} node={selected} nodes={nodes} number={number}
      model={modelById.get(selected.model_id)} onOpenModel={() => setOpenModel(selected)} onClose={() => setSelectedId(null)} />}
    {openModel && <ModelCapabilityDialog node={openModel} model={modelById.get(openModel.model_id)} onClose={() => setOpenModel(null)} onOpenProfile={model => { setOpenModel(null); onOpenProfile(model); }} />}
  </div>;
}
