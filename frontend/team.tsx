import React, { useEffect, useState } from "react";
import { MarkdownView } from "./markdown";
import { nodeStatusLabel } from "./mission-history";
import { CAPABILITIES, type Model } from "./model-profiles";
import { NodeProgress, type PlanNode } from "./plan-graph";
import type { PlanPart } from "./plan-parts";

export type Employee = {
  id: string; name: string; role: string; model_id: string; capabilities: string[];
  skills: string[]; skill_tools?: string[]; reason?: string; skill_context?: string;
};

type Props = {
  apiBase: string; runId: string; runStatus: string; employees?: Employee[]; nodes: PlanNode[];
  parts?: PlanPart[]; brief?: string; models: Model[]; onOpenModel: (model: Model) => void;
};

export function TeamPage({ apiBase, runId, runStatus, employees = [], nodes, parts = [], brief, models, onOpenModel }: Props) {
  const [filter, setFilter] = useState(""), [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => { setFilter(""); setSelectedId(null); }, [runId]);
  const byId = new Map(nodes.map(n => [n.id, n]));
  const staff = new Map(employees.map(e => [e.id, e]));
  const modelById = new Map(models.map(m => [m.id, m]));
  const number = new Map(nodes.map((n, i) => [n.id, String(i + 1).padStart(2, "0")]));
  const selected = selectedId ? byId.get(selectedId) : undefined;
  const active = !["cancelled", "succeeded", "failed", "incomplete"].includes(runStatus);
  const matches = (node: PlanNode) => !filter || `${node.title} ${node.provides ?? ""}`.toLowerCase().includes(filter.toLowerCase());
  const waitingChildren = (node: PlanNode) => nodes.filter(n => n.delegation_parent_id === node.id && n.status !== "succeeded");

  function task(node: PlanNode) {
    const parent = node.delegation_parent_id ? byId.get(node.delegation_parent_id) : undefined;
    const waiting = node.status === "pending" ? waitingChildren(node) : [];
    const failedChild = waiting.some(n => ["blocked", "unable", "failed"].includes(n.status));
    const label = active && waiting.length ? failedChild ? "子任务受阻" : "等待子任务" : nodeStatusLabel(node);
    return <li key={node.id}>
      <div className="team-task-line"><button type="button" className="link-button" onClick={() => setSelectedId(node.id)}>{node.title}</button>
        <span className={`state ${node.status}`}>{label}</span></div>
      {parent && <p className="team-task-meta">由 {staff.get(parent.employee_id ?? "")?.name ?? "原任务执行者"} 委派 ·
        <button type="button" className="link-button" onClick={() => setSelectedId(parent.id)}>{parent.title}</button></p>}
      {!!waiting.length && <p className="team-task-meta">子任务：{waiting.map(n => n.title).join("、")}</p>}
      {node.model_id && <p className="team-task-meta">执行模型：{modelById.get(node.model_id)?.display_name ?? node.model_id}</p>}
      {node.review && ["failed", "blocked", "unable"].includes(node.status) && <p className="team-task-meta">{node.review}</p>}
    </li>;
  }

  const unassigned = nodes.filter(n => !staff.has(n.employee_id ?? ""));
  return <section className="team-page" aria-label="项目员工">
    <div className="team-overview"><div><h4>员工与任务分配</h4><p>{employees.length} 名员工 · {nodes.length} 个任务 · {nodes.filter(n => n.status === "succeeded").length} 个已完成</p></div>
      <label>查找任务<input aria-label="查找员工任务" value={filter} onChange={e => setFilter(e.target.value)} placeholder="输入任务名称" /></label></div>
    {!employees.length && <p className="empty">此项目尚未配置员工。历史任务保留原有分配记录。</p>}
    {brief && <details className="team-agreement" open><summary>项目协作约定</summary><MarkdownView text={brief} />
      {!!parts.length && <details><summary>模块接口与验收约定</summary>{parts.filter(p => !p.parent_id).map(p => <section key={p.id}><b>{p.title}</b>
        <p>{p.provides || p.goal}</p>{p.acceptance && <p>验收：{p.acceptance}</p>}</section>)}</details>}
    </details>}
    <div className="team-grid">{employees.map(employee => {
      const own = nodes.filter(n => n.employee_id === employee.id), shown = own.filter(matches);
      const assignedParts = parts.filter(p => p.employee_id === employee.id && !p.parent_id && p.status !== "ready");
      if (filter && !shown.length) return null;
      return <article key={employee.id} className="team-card" data-employee={employee.id}>
        <div className="team-card-head"><h4>{employee.name}</h4><span>{own.filter(n => n.status === "succeeded").length} / {own.length} 完成</span></div>
        <p>{employee.role}</p><dl><dt>配置模型</dt><dd>{modelById.get(employee.model_id)?.display_name ?? employee.model_id}</dd>
          <dt>负责能力</dt><dd>{employee.capabilities.map(key => CAPABILITIES.find(c => c.key === key)?.label ?? key).join("、")}</dd>
          <dt>Skill</dt><dd>{employee.skills.length ? employee.skills.join("、") : "未指定"}</dd>
          {!!employee.skill_tools?.length && <><dt>工具知识</dt><dd>{employee.skill_tools.join("、")}</dd></>}
        </dl>
        {employee.reason && <details><summary>负责人分配依据</summary><p>{employee.reason}</p></details>}
        {employee.skill_context && <details><summary>员工携带的 Skill</summary><MarkdownView text={employee.skill_context} /></details>}
        {!!assignedParts.length && <p className="team-task-meta">负责大项：{assignedParts.map(p => p.title).join("、")}（{active ? "等待任务细化" : "尚未完成细化"}）</p>}
        <ul className="team-tasks">{shown.map(task)}</ul>
        {!own.length && <p className="team-task-meta">尚未分配具体任务</p>}
      </article>;
    })}</div>
    {!!unassigned.length && <section className="team-unassigned"><h4>未分配员工的任务 · {unassigned.length}</h4><ul className="team-tasks">{unassigned.filter(matches).map(task)}</ul></section>}
    {filter && !nodes.some(matches) && <p className="empty">没有匹配的任务</p>}
    {selected && <NodeProgress apiBase={apiBase} runId={runId} runStatus={runStatus} node={selected} nodes={nodes} number={number}
      model={modelById.get(selected.model_id)} onOpenModel={() => { const m = modelById.get(selected.model_id); if (m) onOpenModel(m); }} onClose={() => setSelectedId(null)} />}
  </section>;
}
