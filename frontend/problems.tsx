import React, { useEffect, useState } from "react";

// Problems across finished tasks, grouped by what they are about (retrospect.topics): one cause
// recorded under several names (a module, a program) is one group here.
export type Topic = {
  topic: string; runs: string[]; repaired_in: string[]; members: string[]; label: string;
  recurred_after_repair: boolean; last_seen: string;
};
type Run = { id: string; plan: { summary: string } };

export function ProblemsPage({ apiBase, runs, onOpenRun }: { apiBase: string; runs: Run[]; onOpenRun: (id: string) => void }) {
  const [topics, setTopics] = useState<Topic[] | null>(null), [error, setError] = useState("");
  useEffect(() => {
    fetch(`${apiBase}/api/retrospectives/topics`)
      .then(async response => { if (!response.ok) throw new Error(`请求失败（${response.status}）`); setTopics(await response.json()); })
      .catch(err => setError(err instanceof Error ? err.message : "加载失败"));
  }, [apiBase]);
  const title = (id: string) => runs.find(run => run.id === id)?.plan.summary || "已删除的任务";
  return <section className="problems-page" aria-label="重复问题">
    <h2>重复问题</h2>
    <p className="muted">每个任务结束后，平台用最便宜的模型按问题的实际原因起名，并尽量沿用已有的名字；同一原因在不同记录里的问题归为一类。</p>
    {error && <p className="error" role="alert">{error}</p>}
    {topics === null && !error && <p className="muted">加载中…</p>}
    {topics?.length === 0 && <p className="empty">还没有已归类的问题。任务结束后会自动归类。</p>}
    {topics && topics.length > 0 && <ol className="problem-topics">{topics.map(group => <li key={group.topic} className="problem-topic">
      <div className="problem-head"><b>{group.topic}</b>
        <span className="problem-count">{group.runs.length} 个任务</span>
        {group.repaired_in.length > 0 && <span className="problem-tag">修复过 {group.repaired_in.length} 次</span>}
        {group.recurred_after_repair && <span className="problem-tag bad">修复后又出现</span>}
      </div>
      {group.label && <p>{group.label}</p>}
      <small className="muted">记录里的归类：{group.members.join("、")} · 最近 {group.last_seen.slice(0, 10)}</small>
      <div className="problem-runs">{group.runs.map(id => <button key={id} type="button" className="link-button" onClick={() => onOpenRun(id)} disabled={!runs.some(run => run.id === id)}>{title(id)}</button>)}</div>
    </li>)}</ol>}
  </section>;
}
