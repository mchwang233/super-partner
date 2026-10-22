import React, { useEffect, useState } from "react";

type Call = {
  id: string; model_id: string; node_id?: string; purpose: string; status: string;
  requested_effort?: string; effective_effort: string; warnings: string[];
  input_tokens: number; output_tokens: number; cached_input_tokens: number; cache_write_tokens: number;
  usage_known: boolean; cost_usd: number; requests: number; retries: number; duration_seconds: number; queue_seconds: number;
};
type Data = { calls: Call[]; limit: number; summary: {
  calls: number; requests: number; unknown_usage: number; observed_cost_usd: number;
  input_tokens: number; output_tokens: number; cached_input_tokens: number; retries: number;
} };
const status: Record<string, string> = { running: "调用中", succeeded: "已返回", failed: "失败", cancelled: "已取消", interrupted: "服务中断" };

export function CallInsights({ apiBase, runId }: { apiBase: string; runId: string }) {
  const [data, setData] = useState<Data | null>(null), [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await fetch(`${apiBase}/api/tasks/${encodeURIComponent(runId)}/calls`);
        if (!response.ok) throw new Error(`调用记录读取失败（${response.status}）`);
        const body = await response.json();
        if (active) { setData(body); setError(""); }
      } catch (e) { if (active) setError(e instanceof Error ? e.message : String(e)); }
    };
    setData(null); void load();
    const timer = window.setInterval(load, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [apiBase, runId]);
  if (error) return <p role="alert">{error}</p>;
  if (!data) return <p>读取调用记录…</p>;
  if (!data.calls.length) return <p>还没有调用记录。升级前的用量无法追溯，后续调用会自动记录。</p>;
  const summary = data.summary;
  return <section className="call-insights" aria-label="模型调用记录">
    <p>最近 {summary.calls} 次调用 · {summary.requests} 次 API 请求 · {summary.retries} 次重试</p>
    <p>已报告输入 {summary.input_tokens.toLocaleString()} token，输出 {summary.output_tokens.toLocaleString()} token，
      缓存读取 {summary.cached_input_tokens.toLocaleString()} token。已知用量估算费用 ${summary.observed_cost_usd.toFixed(4)}。</p>
    {!!summary.unknown_usage && <p role="status">{summary.unknown_usage} 次调用的用量不完整，不能按零用量计算。</p>}
    <p className="muted">推理强度显示实际发送的参数；供应商默认值不保证达到请求的强度。费用按模型目录估算，会员 CLI 额外 API 费用为零。</p>
    <div className="call-table-scroll"><table>
      <thead><tr><th>模型／步骤</th><th>状态</th><th>推理：请求 → 发送</th><th>输入／输出</th><th>缓存读／写</th><th>耗时／排队</th><th>请求／重试</th></tr></thead>
      <tbody>{data.calls.map(call => <React.Fragment key={call.id}><tr>
        <td>{call.model_id}<small>{call.node_id ?? call.purpose}</small></td><td>{status[call.status] ?? call.status}</td>
        <td>{call.requested_effort ?? "默认"} → {call.effective_effort === "provider_default" ? "供应商默认值" : call.effective_effort}</td>
        <td>{call.usage_known ? `${call.input_tokens} / ${call.output_tokens}` : "用量未完整报告"}</td>
        <td>{call.cached_input_tokens} / {call.cache_write_tokens}</td>
        <td>{call.duration_seconds.toFixed(1)}s / {call.queue_seconds.toFixed(1)}s</td>
        <td>{call.requests} / {call.retries}</td>
      </tr>{call.warnings.length > 0 && <tr><td colSpan={7} role="status">{call.warnings.join("；")}</td></tr>}</React.Fragment>)}</tbody>
    </table></div>
    {data.calls.length >= data.limit && <p>这里只显示最近 {data.limit} 条记录。</p>}
  </section>;
}
