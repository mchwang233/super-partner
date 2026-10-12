import React, { useId, useState } from "react";
import { dateLabel } from "./catalog-status";
import type { Model } from "./model-profiles";
import { BENCHMARK_SOURCES, DEFAULT_BENCHMARK_SOURCE, resultsForSource, type BenchmarkSource } from "./benchmark-sources";

export type BenchmarkMetric = { key: string; label: string; value: number; unit: "percent" | "index"; note?: string | null; benchmark?: string | null };
export type BenchmarkResult = {
  id: string;
  source: string;
  source_name: string;
  source_url: string;
  data_url: string;
  version: string;
  model_name: string;
  variant: string;
  published_at: string | null;
  fetched_at: string;
  matched_by: "model_id" | "display_name";
  metrics: BenchmarkMetric[];
  collection_method?: "dataset" | "api" | "public_page";
};
const aggregateKeys = new Set(["总分", "Overall Acc", "artificial_analysis_intelligence_index"]);
const RADAR_AXES: Record<string, [string, string][]> = {
  artificial_analysis: [
    ["aa_scicode", "科学编程"], ["aa_terminalbench_4", "终端智能体"],
    ["aa_hle", "复杂推理"], ["aa_critpt", "物理推理"], ["aa_lcr", "长文本推理"],
    ["aa_knowledge_accuracy", "知识准确率"], ["aa_non_hallucination", "非幻觉率"],
  ],
  bfcl: [["Non-Live AST Acc", "单轮标准任务"], ["Live Acc", "单轮真实任务"], ["Multi Turn Acc", "多轮工具调用"], ["Web Search Acc", "网页搜索任务"], ["Memory Acc", "记忆任务"]],
  superclue: ["数学推理", "幻觉控制", "科学推理", "精确指令遵循", "智能体编程", "智能体任务规划"].map(key => [key, key]),
};
export const benchmarkNumber = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });

function radarAxes(result: BenchmarkResult) {
  const axes = RADAR_AXES[result.source] ?? result.metrics.filter(metric => metric.unit === "percent" && !aggregateKeys.has(metric.key)).map(metric => [metric.key, metric.label]);
  return axes.map(([key, label]) => ({ key, label, metric: result.metrics.find(metric => metric.key === key && metric.unit === "percent") }));
}

function PublicRadar({ result }: { result: BenchmarkResult }) {
  const title = useId();
  const description = useId();
  const axes = radarAxes(result);
  if (axes.length < 3) return null;
  const available = axes.filter(axis => axis.metric).length;
  const complete = available === axes.length;
  const point = (index: number, value: number, radius = 110) => {
    const angle = index * Math.PI * 2 / axes.length - Math.PI / 2;
    return [230 + Math.cos(angle) * radius * value / 100, 180 + Math.sin(angle) * radius * value / 100];
  };
  return <figure className="profile-radar public-radar"><div className="public-radar-heading"><b>能力雷达图</b><span>{available} / {axes.length} 项已有成绩</span></div><svg viewBox="0 0 460 380" role="img" aria-labelledby={`${title} ${description}`}>
    <title id={title}>{`${result.model_name} 的公开评测能力雷达图`}</title>
    <desc id={description}>{axes.map(axis => `${axis.label} ${axis.metric ? `${benchmarkNumber(axis.metric.value)} / 100` : "暂无分项成绩"}`).join("，")}。缺失项不按零分绘制。</desc>
    {[25, 50, 75, 100].map(level => <g key={level}><polygon className="radar-ring" points={axes.map((_, index) => point(index, level).join(",")).join(" ")} /><text className="radar-scale" x="235" y={180 - 110 * level / 100 - 4}>{level}</text></g>)}
    {axes.map((axis, index) => { const [x, y] = point(index, 100); return <line className={`radar-axis ${axis.metric ? "" : "is-missing"}`} key={axis.key} x1="230" y1="180" x2={x} y2={y} />; })}
    {complete && <polygon className="radar-area" points={axes.map((axis, index) => point(index, axis.metric!.value).join(",")).join(" ")} />}
    {!complete && axes.map((axis, index) => {
      const nextIndex = (index + 1) % axes.length;
      const next = axes[nextIndex];
      if (!axis.metric || !next.metric) return null;
      const [x1, y1] = point(index, axis.metric.value), [x2, y2] = point(nextIndex, next.metric.value);
      return <line key={axis.key} className="radar-partial" x1={x1} y1={y1} x2={x2} y2={y2} />;
    })}
    {available === 0 && <text className="radar-empty-label" x="230" y="195" textAnchor="middle">暂未获取分项</text>}
    {axes.map((axis, index) => {
      const [x, y] = point(index, 100, 148);
      const [dotX, dotY] = point(index, axis.metric?.value ?? 0);
      return <g key={axis.key}>
        {axis.metric && <circle cx={dotX} cy={dotY} r="4" className="radar-point"><title>{`${axis.metric.benchmark ?? axis.label}：${benchmarkNumber(axis.metric.value)} / 100`}</title></circle>}
        <text className={`radar-label ${axis.metric ? "" : "is-missing"}`} x={x} y={y} textAnchor={x > 245 ? "start" : x < 215 ? "end" : "middle"} dominantBaseline="middle"><tspan x={x}>{axis.label}</tspan><tspan className="radar-axis-value" x={x} dy="17">{axis.metric ? benchmarkNumber(axis.metric.value) : "暂无"}</tspan></text>
      </g>;
    })}
  </svg><figcaption>{complete ? "公开分项 · 0–100 刻度" : available ? "空缺维度标为“暂无”，不连线、不补零" : "目前只有综合指数，分项获取后显示轮廓"}</figcaption></figure>;
}

export function PublicBenchmarks({ results, source = DEFAULT_BENCHMARK_SOURCE }: { results: BenchmarkResult[]; source?: BenchmarkSource }) {
  const [selectedId, setSelectedId] = useState("");
  const sourceResults = resultsForSource(results, source);
  const selected = sourceResults.find(row => row.id === selectedId) ?? sourceResults[0];
  if (!selected) return <div className="profile-pending-note">暂无 {BENCHMARK_SOURCES[source].name} 成绩。当前同步数据未匹配到此型号和版本，评分保持空缺。可查看上方来源的同步状态与数据日期。</div>;
  const hasRadar = radarAxes(selected).length >= 3;
  return <section className="public-benchmarks" aria-label="公开评测成绩">
    <div className="public-benchmark-heading"><div><span className="profile-preset-label">公开评测</span><h4>{selected.source_name}</h4></div><label><span>榜单与测试配置</span><select value={selected.id} onChange={event => setSelectedId(event.target.value)}>{sourceResults.map(result => <option key={result.id} value={result.id}>{result.version} · {result.variant}</option>)}</select></label></div>
    <p className="public-benchmark-model">榜单模型：<strong>{selected.model_name}</strong></p>
    <div className={`public-benchmark-visuals ${hasRadar ? "has-radar" : ""}`}>
      <PublicRadar result={selected} />
      <div className="public-metrics">{selected.metrics.map(metric => <div key={metric.key} className="public-metric">
        <div><span>{metric.label}{metric.benchmark && <small className="public-metric-benchmark">{metric.benchmark}</small>}</span><b>{benchmarkNumber(metric.value)}{metric.note && <sup title={metric.note}>*</sup>}<small>{metric.unit === "percent" ? " / 100" : " 指数"}</small></b></div>
        {metric.unit === "percent" && <div className="profile-score-track" role="meter" aria-label={`${selected.source_name} ${metric.label}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={metric.value}><span style={{ width: `${metric.value}%` }} /></div>}
        {metric.note && <p className="profile-footnote">{metric.note}</p>}
      </div>)}</div>
    </div>
    <div className="public-benchmark-meta"><span>榜单版本：{selected.version}</span><span>发布 / 更新日期：{selected.published_at ?? "来源未提供"}</span><span>同步时间：{dateLabel(selected.fetched_at)}</span><span>匹配依据：{selected.matched_by === "model_id" ? "型号标识" : "同服务商唯一名称"}</span></div>
    {selected.source === "artificial_analysis" && <p className="profile-footnote">能力图使用 AA 公开页面中的同型号、同档位分项：通过率 / 正确率由 0–1 换算为 0–100；综合智能指数独立展示。非幻觉率采用 AA-Omniscience 的定义。缺失项保持空白。<a href="https://artificialanalysis.ai/methodology/intelligence-benchmarking" target="_blank" rel="noreferrer">查看评测方法 ↗</a></p>}
    {selected.collection_method === "public_page" && <p className="profile-footnote">综合指数沿用公开榜单的显示精度，分项取自页面图表数据。榜单未注明的版本和评测日期不以同步时间代替。</p>}
    <p className="profile-footnote">成绩对应上述测试配置。滚动模型别名可能随服务商更新而变化；各榜单量表独立展示，公开评测不会覆盖项目的调度预设分。</p>
    <div className="public-source-links"><a href={selected.source_url} target="_blank" rel="noreferrer">来源：{selected.source_name} ↗</a><a href={selected.data_url} target="_blank" rel="noreferrer">查看原始数据 ↗</a></div>
  </section>;
}

export function BenchmarkComparison({ models, onSelect, source = DEFAULT_BENCHMARK_SOURCE }: { models: Model[]; onSelect: (id: string) => void; source?: BenchmarkSource }) {
  const [version, setVersion] = useState("");
  const [variant, setVariant] = useState("all");
  const rows = models.flatMap(model => resultsForSource(model.benchmarks, source).map(result => ({ model, result })));
  const versions = [...new Set(rows.map(row => row.result.version))].sort().reverse();
  const activeVersion = versions.includes(version) ? version : versions[0];
  const inVersion = rows.filter(row => row.result.version === activeVersion);
  const variants = [...new Set(inVersion.map(row => row.result.variant))].sort();
  const activeVariant = variants.includes(variant) ? variant : "all";
  const shown = inVersion.filter(row => activeVariant === "all" || row.result.variant === activeVariant);
  const columns = new Map<string, BenchmarkMetric>();
  inVersion.forEach(row => row.result.metrics.forEach(metric => columns.set(metric.key, metric)));
  return <section className="profile-comparison public-comparison" aria-label="公开评测对比">
    <div className="profile-comparison-heading"><div><h3>{BENCHMARK_SOURCES[source].name} 评测对比</h3><p>同一来源、同一版本内比较；不同测试配置单独列出。缺失项显示 —。</p></div><span>{shown.length} 组成绩</span></div>
    <div className="public-comparison-filters"><label>榜单版本<select value={activeVersion ?? ""} onChange={event => setVersion(event.target.value)}>{versions.length ? versions.map(value => <option key={value}>{value}</option>) : <option value="">暂无成绩</option>}</select></label><label>测试配置<select value={activeVariant} onChange={event => setVariant(event.target.value)}><option value="all">全部配置（逐行标注）</option>{variants.map(value => <option key={value}>{value}</option>)}</select></label></div>
    {shown.length ? <div className="profile-table-scroll"><table><caption className="profile-sr-only">{BENCHMARK_SOURCES[source].name} 原始评测分数；星号为来源附注，可在模型画像中查看。</caption><thead><tr><th>模型 / 配置</th>{[...columns.values()].map(metric => <th key={metric.key}>{metric.label}{metric.unit === "index" ? "（指数）" : " / 100"}</th>)}</tr></thead><tbody>{shown.map(({ model, result }) => <tr key={`${model.id}:${result.id}`}><th><button onClick={() => onSelect(model.id)}>{model.display_name}<span>{result.variant} · {result.published_at ?? "日期未提供"}</span></button></th>{[...columns.keys()].map(key => { const metric = result.metrics.find(item => item.key === key); return <td key={key} title={metric?.note ?? undefined}>{metric ? benchmarkNumber(metric.value) : "—"}{metric?.note && <sup>*</sup>}</td>; })}</tr>)}</tbody></table></div> : <p className="public-no-results">当前筛选范围暂无 {BENCHMARK_SOURCES[source].name} 成绩。可查看上方同步状态。</p>}
  </section>;
}
