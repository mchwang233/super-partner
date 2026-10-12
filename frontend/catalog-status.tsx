import React, { useEffect, useState } from "react";
import { BENCHMARK_SOURCES, DEFAULT_BENCHMARK_SOURCE, type BenchmarkSource } from "./benchmark-sources";

export const PROVIDER_NAMES: Record<string, string> = {
  openai: "OpenAI", anthropic: "Anthropic", google: "Google Gemini", deepseek: "DeepSeek",
  qwen: "通义千问", doubao: "豆包", glm: "智谱 GLM", minimax: "MiniMax", kimi: "Kimi",
};

type ProviderStatus = {
  public_status: string;
  public_error?: string | null;
  last_success_at?: string;
  official_status: string;
  official_error?: string | null;
  official_success_at?: string;
  count?: number;
  added?: number;
};
type SyncStatus = {
  running: boolean;
  auto_sync: boolean;
  interval_hours: number;
  last_finished_at?: string;
  next_sync_at?: string | null;
  added?: number;
  providers?: Record<string, ProviderStatus>;
  benchmarks?: {
    matched_models: number;
    sources: Record<string, { name: string; status?: string; error?: string | null; count?: number; matched_models: number; last_success_at?: string; latest_published_at?: string | null; collection_method?: string; with_details?: number }>;
  };
};

export function dateLabel(value?: string | null): string {
  if (!value) return "尚无记录";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "尚无记录" : date.toLocaleString("zh-CN", { hour12: false });
}

export function CatalogStatus({ apiBase, benchmarkSource = DEFAULT_BENCHMARK_SOURCE }: { apiBase: string; benchmarkSource?: BenchmarkSource }) {
  const [status, setStatus] = useState<SyncStatus | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    async function poll() {
      try {
        const response = await fetch(`${apiBase}/api/models/sync`, { signal: controller.signal });
        if (!response.ok) throw new Error(`读取同步状态失败（${response.status}）`);
        const result = await response.json();
        if (!controller.signal.aborted) { setStatus(result); setError(""); }
      } catch (error) {
        if (!controller.signal.aborted) setError(error instanceof Error ? error.message : "读取同步状态失败");
      }
    }
    poll();
    const timer = setInterval(poll, 2500);
    return () => { controller.abort(); clearInterval(timer); };
  }, [apiBase]);
  async function refresh() {
    setPending(true); setError("");
    try {
      const response = await fetch(`${apiBase}/api/models/sync`, { method: "POST" });
      if (!response.ok) throw new Error(`启动同步失败（${response.status}）`);
      setStatus(await response.json());
    } catch (error) { setError(error instanceof Error ? error.message : "启动同步失败"); }
    finally { setPending(false); }
  }
  return <section className="catalog-sync" aria-label="模型目录自动更新">
    <div className="catalog-sync-heading"><div><h3>模型目录自动更新</h3><p>{status ? status.auto_sync ? `每 ${status.interval_hours} 小时检查 · 启动时补查到期更新` : "自动更新已关闭，可手动更新" : "正在读取同步设置…"}</p></div><button onClick={refresh} disabled={pending || status?.running}>{pending || status?.running ? "正在更新…" : "立即更新"}</button></div>
    <p className="catalog-sync-summary" role="status">{status?.running ? "正在获取最新目录，已有模型仍可使用。" : status?.last_finished_at ? `上次检查 ${dateLabel(status.last_finished_at)} · 新增 ${status.added ?? 0} 个模型` : "首次更新完成后，将显示各家模型与同步结果。"}{status?.next_sync_at && <span>下次检查 {dateLabel(status.next_sync_at)}（应用运行期间）</span>}</p>
    {error && <p className="catalog-sync-error" role="alert">{error}</p>}
    <div className="catalog-providers">{Object.entries(PROVIDER_NAMES).map(([id, name]) => {
      const state = status?.providers?.[id];
      const official = state?.official_status;
      return <div className={`catalog-provider ${state?.public_status === "error" ? "has-error" : ""}`} key={id}>
        <b>{name}<span>{state?.count === undefined ? "—" : `${state.count} 个`}</span></b>
        <small>{state?.public_status === "ok" ? "公开目录已更新" : state?.public_status === "error" ? "更新失败 · 已保留缓存" : "等待同步"}</small>
        <small>{official === "ok" ? "官方接口已核对" : official === "no_key" ? "官方接口：未配置密钥" : official === "error" ? "官方接口：核对失败" : official === "not_supported" ? "来源：公开目录" : "来源待检查"}</small>
        {state?.public_error && <p>{state.public_error}</p>}{state?.official_error && <p>{state.official_error}</p>}
        {state?.last_success_at && <time dateTime={state.last_success_at}>目录成功时间 {dateLabel(state.last_success_at)}</time>}
      </div>;
    })}</div>
    <p className="catalog-source-note">公开规格与参考价格来自 <a href="https://models.dev/" target="_blank" rel="noreferrer">Models.dev 社区目录</a>，可能滞后于厂商发布。OpenAI、Anthropic、Google、DeepSeek 配置密钥后会额外核对官方列表。目录收录不代表当前账户已获调用权限。</p>
    <div className="benchmark-sync-status"><h4>{BENCHMARK_SOURCES[benchmarkSource].name} 评分同步<span>{status?.benchmarks?.sources[benchmarkSource]?.matched_models ?? 0} 个模型已匹配</span></h4><div className="catalog-providers benchmark-providers">{Object.entries(status?.benchmarks?.sources ?? {}).filter(([id]) => id === benchmarkSource).map(([id, state]) => <div className={`catalog-provider ${state.status === "error" ? "has-error" : ""}`} key={id}><b>{state.name}<span>{state.matched_models} 个匹配</span></b><small>{state.status === "ok" ? `已同步 ${state.count} 组成绩` : state.status === "error" ? "更新失败 · 保留上次评分" : "等待同步"}</small>{state.collection_method === "public_page" && <small>公开网页 · 无需密钥 · 综合指数与分项</small>}{state.collection_method === "api" && <small>API · 综合智能、编程、智能体指数</small>}{id === "artificial_analysis" && state.with_details !== undefined && <small>{state.with_details} 组成绩含能力分项</small>}{state.error && <p>{state.error}</p>}<small>来源最新标注日期：{state.latest_published_at ?? "未提供（不等于同步时间）"}</small>{state.last_success_at && <time>同步成功时间 {dateLabel(state.last_success_at)}</time>}{id === "artificial_analysis" && state.collection_method !== "api" && <small><a href="https://artificialanalysis.ai/leaderboards/models" target="_blank" rel="noreferrer">查看公开榜单 ↗</a><br/>可选：配置 SUPER_PARTNER_ARTIFICIAL_ANALYSIS_API_KEY 并重启，通过 API 补充编程、智能体指数。</small>}</div>)}</div><p className="catalog-source-note">与模型目录一起定时更新，也可点“立即更新”。目录收录时间与榜单发布成绩的时间不同；同步成功只说明读取成功。匹配要求服务商与型号一致，不跨快照日期、型号档次或评测版本填分。</p></div>
  </section>;
}
