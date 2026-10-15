import React, { useId, useRef, useState } from "react";
import { CatalogStatus, dateLabel, PROVIDER_NAMES } from "./catalog-status";
import { BenchmarkComparison, PublicBenchmarks, type BenchmarkResult } from "./public-benchmarks";
import { BENCHMARK_SOURCES, DEFAULT_BENCHMARK_SOURCE, resultsForSource, type BenchmarkSource } from "./benchmark-sources";
import "./model-profiles.css";

export const CAPABILITIES = [
  { key: "coding", label: "代码", description: "代码理解、实现功能与排查问题" },
  { key: "planning", label: "规划", description: "拆分目标、安排步骤与依赖" },
  { key: "research", label: "调研", description: "整理资料、比较方案与归纳结论" },
  { key: "chinese", label: "中文", description: "中文理解与表达" },
  { key: "tool_use", label: "工具使用", description: "选择工具与组织工具调用" },
  { key: "structured_output", label: "结构化输出", description: "按指定格式生成可处理的结果" },
  { key: "reliability", label: "可靠性", description: "由 AA 非幻觉率换算，不是实测成功率" },
] as const;

export type CapabilityKey = typeof CAPABILITIES[number]["key"];
type ChannelFilter = "all" | "cli" | "api";
type ScoreSource = BenchmarkSource | "preset";
type SortBy = CapabilityKey | "latest" | "name";

export type Model = {
  id: string;
  display_name: string;
  provider: string;
  channel: string;
  auth_type?: string;
  configured: boolean;
  enabled: boolean;
  input_per_million: number | null;
  output_per_million: number | null;
  context_window: number | null;
  pricing_verified: boolean;
  scores: Record<CapabilityKey, number> | null;
  routing_ready: boolean;
  scores_source?: string;
  scores_basis?: string | null;
  catalog_source: string;
  catalog_status: string;
  catalog_checked_at?: string | null;
  discovered_at?: string;
  release_date?: string | null;
  pricing_source: string;
  available_via_api?: boolean | null;
  api_checked_at?: string | null;
  source_url?: string | null;
  description?: string | null;
  supports_tools?: boolean | null;
  supports_vision?: boolean | null;
  supports_reasoning?: boolean | null;
  supports_structured_output?: boolean | null;
  benchmarks?: BenchmarkResult[];
};

type Props = {
  models: Model[];
  selectedId: string;
  onSelect: (id: string) => void;
  onToggle: (model: Model) => Promise<void>;
  onConfigure: (model: Model) => void;
  apiBase: string;
};

export function score(model: Model, key: CapabilityKey): number | null {
  const value = model.scores?.[key];
  return typeof value === "number" && Number.isFinite(value) ? Math.round(Math.min(100, Math.max(0, value))) : null;
}

function channelLabel(model: Model): string {
  return model.provider === "mock" ? "演示" : model.channel === "cli" ? "CLI" : "API";
}

function availability(model: Model, source: ScoreSource = DEFAULT_BENCHMARK_SOURCE): string {
  if (model.catalog_status === "retired") return "已退役";
  if (model.catalog_status === "deprecated") return "来源标记弃用";
  if (model.available_via_api === false) return "官方接口未列出";
  if (!model.scores) return source === "preset" ? "暂无调度评分" : resultsForSource(model.benchmarks, source).length ? `已收录 ${BENCHMARK_SOURCES[source].shortName} 成绩` : `暂无 ${BENCHMARK_SOURCES[source].shortName} 成绩`;
  if (!model.routing_ready) return "规格待补全";
  if (!model.enabled) return "已停用";
  if (model.provider === "mock") return "演示模式";
  if (model.configured) return "已就绪";
  return model.channel === "cli" ? "未检测到" : "未配置密钥";
}

export function Radar({ model }: { model: Model }) {
  const titleId = useId();
  const descriptionId = useId();
  const point = (index: number, radius: number) => {
    const angle = index * Math.PI * 2 / CAPABILITIES.length - Math.PI / 2;
    return [200 + Math.cos(angle) * radius, 175 + Math.sin(angle) * radius];
  };
  const ring = (radius: number) => CAPABILITIES.map((_, index) => point(index, radius).join(",")).join(" ");
  const complete = CAPABILITIES.every(({ key }) => score(model, key) !== null);
  return <figure className="profile-radar">
    <svg viewBox="0 0 400 350" role="img" aria-labelledby={`${titleId} ${descriptionId}`}>
      <title id={titleId}>{`${model.display_name} 的能力雷达图`}</title>
      <desc id={descriptionId}>各轴均为 0–100 分的调度评分。{CAPABILITIES.map(({ key, label }) => `${label} ${score(model, key) ?? "暂无"}`).join("，")}</desc>
      {[25, 50, 75, 100].map(level => <polygon key={level} points={ring(level)} className="radar-ring" />)}
      {CAPABILITIES.map(({ key }, index) => {
        const [x, y] = point(index, 100);
        return <line key={key} x1="200" y1="175" x2={x} y2={y} className="radar-axis" />;
      })}
      {complete && <polygon points={CAPABILITIES.map(({ key }, index) => point(index, score(model, key)!).join(",")).join(" ")} className="radar-area" />}
      {!complete && <text x="200" y="205" textAnchor="middle" className="radar-label">待评估</text>}
      {[25, 50, 75, 100].map(level => <text key={level} x="205" y={175 - level - 4} className="radar-scale">{level}</text>)}
      {CAPABILITIES.map(({ key, label }, index) => {
        const value = score(model, key);
        const [x, y] = point(index, 130);
        const [dotX, dotY] = point(index, value ?? 0);
        return <g key={key}>
          {value !== null && <circle cx={dotX} cy={dotY} r="3.5" className="radar-point" />}
          <text x={x} y={y} textAnchor={x > 210 ? "start" : x < 190 ? "end" : "middle"} dominantBaseline="middle" className="radar-label">{label}</text>
        </g>;
      })}
    </svg>
    <figcaption>统一 0–100 刻度 · {!model.scores ? "尚无能力评分" : model.provider === "mock" ? "模拟画像" : "调度画像"}</figcaption>
  </figure>;
}

function ModelDetail({ model, source, onToggle, onConfigure }: Pick<Props, "onToggle" | "onConfigure"> & { model: Model; source: ScoreSource }) {
  const [pending, setPending] = useState(false);
  const isDemo = model.provider === "mock";
  const isCli = model.channel === "cli";
  const strengths = CAPABILITIES.filter(({ key }) => key !== "reliability" && score(model, key) !== null)
    .sort((a, b) => score(model, b.key)! - score(model, a.key)!).slice(0, 3);
  async function toggle() {
    setPending(true);
    try { await onToggle(model); } finally { setPending(false); }
  }
  return <article className={`model-profile-card ${isDemo ? "demo-profile" : ""}`}>
    <div className="profile-card-heading">
      <div className="profile-identity">
        <span className="profile-monogram" aria-hidden="true">{isDemo ? "DM" : isCli ? "CLI" : model.provider.slice(0, 2).toUpperCase()}</span>
        <div><div className="profile-badges"><span className="profile-badge">{channelLabel(model)}</span><span className={`profile-availability ${model.configured && model.enabled ? "is-ready" : ""}`}>{availability(model, source)}</span></div><h3>{model.display_name}</h3><code>{model.id}</code></div>
      </div>
      <div className="profile-actions">
        {!isCli && !isDemo && <button className="ghost" onClick={() => onConfigure(model)}>配置 API Key</button>}
        <button className={`profile-enable ${model.enabled ? "is-enabled" : ""}`} aria-pressed={model.enabled} aria-label={`${model.enabled ? "停用" : "启用"} ${model.display_name}`} disabled={pending || (!model.enabled && !model.routing_ready)} onClick={toggle}>{pending ? "保存中…" : model.enabled ? "已启用" : !model.routing_ready ? "暂不参与调度" : "启用模型"}</button>
      </div>
    </div>
    {model.description && <p className="profile-description">{model.description}</p>}
    {!model.scores && <p className="profile-footnote">暂无 Artificial Analysis 成绩，暂不参与任务调度。</p>}
    {model.scores && <p className="profile-footnote profile-score-origin">{model.provider === "mock" ? <><b>演示模型使用固定分数</b>，仅用于离线演示。</> : <><b>调度评分来自 Artificial Analysis。</b>{model.scores_basis}</>}</p>}
    {source !== "preset" ? <PublicBenchmarks results={model.benchmarks ?? []} source={source} /> : <>
    <p className="profile-pending-note">调度评分 · {model.provider === "mock" ? "演示用固定分数" : "由 Artificial Analysis 成绩按排名换算"}，用于任务调度，不是公开评测原始分。</p>
    {isDemo ? <p className="profile-demo-note">演示模型只返回模拟结果。以下数值用于演示流程，不代表真实模型能力。</p>
      : <div className="profile-strengths"><span>画像侧重</span>{strengths.map(({ key, label }) => <span className="strength-chip" key={key}>{label}<b>{score(model, key)}</b></span>)}</div>}
    <div className="profile-visuals">
      <Radar model={model} />
      <div className="profile-score-list" aria-label="七项能力的具体评分">
        <div className="profile-score-heading"><span>能力维度</span><span>调度分 / 100</span></div>
        {CAPABILITIES.map(({ key, label, description }) => {
          const value = score(model, key);
          return <div className="profile-score" key={key}>
            <div className="profile-score-label"><span title={description}>{label}</span><b>{value ?? "—"}</b></div>
            <div className="profile-score-track" role={value === null ? undefined : "meter"} aria-label={`${label}调度评分`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined} aria-valuetext={value === null ? "暂无评分" : `${value} / 100`} title={description}><span style={{ width: `${value ?? 0}%` }} /></div>
          </div>;
        })}
      </div>
    </div>
    </>}
    <dl className="profile-facts">
      <div><dt>目录上下文窗口</dt><dd>{model.context_window?.toLocaleString("en-US") ?? "未知"}<small>tokens</small></dd></div>
      <div><dt>输入价格 / 百万 tokens</dt><dd>{isCli ? "未计量" : isDemo ? "模拟" : model.input_per_million === null ? "未知" : `$${model.input_per_million}`}</dd></div>
      <div><dt>输出价格 / 百万 tokens</dt><dd>{isCli ? "未计量" : isDemo ? "模拟" : model.output_per_million === null ? "未知" : `$${model.output_per_million}`}</dd></div>
      <div><dt>价格依据</dt><dd className="profile-fact-text">{isCli ? "取决于 CLI 账户" : isDemo ? "无真实账单" : model.pricing_verified ? "目录标记为已核验" : model.pricing_source === "models.dev" ? "Models.dev 参考起步价" : model.pricing_source === "unknown" ? "尚无定价信息" : "预设价格 · 待核验"}</dd></div>
    </dl>
    {!isCli && !isDemo && <>
      <div className="profile-feature-tags">{([["supports_tools", "工具调用"], ["supports_vision", "图像理解"], ["supports_reasoning", "推理"], ["supports_structured_output", "结构化输出"]] as const).map(([key, label]) => <span key={key}>{label}<b>{model[key] === true ? "支持" : model[key] === false ? "不支持" : "未知"}</b></span>)}</div>
      <dl className="profile-catalog-facts"><div><dt>发布日期</dt><dd>{model.release_date ?? "来源未提供"}</dd></div><div><dt>目录来源</dt><dd>{model.catalog_source === "models.dev" ? "Models.dev 社区目录" : model.catalog_source === "official" ? "官方模型接口" : "项目内置"}{model.source_url?.startsWith("https://") && <a href={model.source_url} target="_blank" rel="noreferrer">查看来源 ↗</a>}</dd></div><div><dt>目录检查时间</dt><dd>{dateLabel(model.catalog_checked_at)}</dd></div><div><dt>官方接口可见性</dt><dd>{model.available_via_api === true ? "上次核对时可见" : model.available_via_api === false ? "上次核对时未列出" : "尚未核对"}{model.api_checked_at && <small>{dateLabel(model.api_checked_at)}</small>}</dd></div></dl>
      {model.catalog_status === "unlisted" && <p className="profile-pending-note">最新公开目录未收录此模型，已保留历史记录；这不等同于厂商已下线。</p>}
    </>}
    <p className="profile-footnote">{isCli ? "CLI 画像代表接入通道，实际能力随 CLI 当前使用的模型和配置变化。本应用尚未统计 CLI 的 Token、会员额度或订阅费用。" : isDemo ? "演示模型不适合执行真实任务，也不用于判断其他模型的优劣。" : "上下文与价格来自当前模型目录；实际可用规格和账单以服务商为准。"}</p>
  </article>;
}

export function ModelProfiles({ models, selectedId, onSelect, onToggle, onConfigure, apiBase }: Props) {
  const [query, setQuery] = useState("");
  const [channel, setChannel] = useState<ChannelFilter>("all");
  const [provider, setProvider] = useState("all");
  const [onlyBenchmarked, setOnlyBenchmarked] = useState(false);
  const [source, setSource] = useState<ScoreSource>(DEFAULT_BENCHMARK_SOURCE);
  const [sortBy, setSortBy] = useState<SortBy>("latest");
  const presetView = source === "preset";
  const benchmarkSource = source === "preset" ? DEFAULT_BENCHMARK_SOURCE : source;
  const sourceLabel = source === "preset" ? "调度评分" : BENCHMARK_SOURCES[source].shortName;
  const hasScores = (model: Model) => source === "preset" ? !!model.scores : resultsForSource(model.benchmarks, source).length > 0;
  const detailRef = useRef<HTMLDivElement>(null);
  const search = query.trim().toLowerCase();
  const filtered = models.filter(model => {
    return (channel === "all" || model.channel === channel)
      && (provider === "all" || (provider === "cli" ? model.channel === "cli" : model.provider === provider))
      && (!onlyBenchmarked || hasScores(model))
      && `${model.display_name} ${model.id} ${model.provider}`.toLowerCase().includes(search);
  });
  if (sortBy === "latest") filtered.sort((a, b) => (b.release_date ?? "").localeCompare(a.release_date ?? "") || a.display_name.localeCompare(b.display_name));
  else if (sortBy === "name") filtered.sort((a, b) => a.display_name.localeCompare(b.display_name));
  else filtered.sort((a, b) => (score(b, sortBy) ?? -1) - (score(a, sortBy) ?? -1));
  const selected = filtered.find(model => model.id === selectedId) ?? filtered[0];
  const selectFromTable = (id: string) => {
    onSelect(id);
    detailRef.current?.scrollIntoView({ block: "start" });
    detailRef.current?.focus({ preventScroll: true });
  };
  return <section className="model-profiles" aria-labelledby="model-profiles-title">
    <div className="profiles-intro"><div><p className="eyebrow">MODEL PROFILES</p><h2 id="model-profiles-title">模型画像<span>了解每一个执行伙伴。</span></h2></div><span className="profiles-total">{models.length} 个接入条目 · {models.filter(hasScores).length} 个有{sourceLabel}{presetView ? "" : "成绩"}</span></div>
    <CatalogStatus apiBase={apiBase} benchmarkSource={benchmarkSource} />
    <div className="profile-provenance"><label className="benchmark-source-picker"><span className="profile-preset-label">评分依据</span><select value={source} onChange={event => { setSource(event.target.value as ScoreSource); setSortBy("latest"); }}>{Object.entries(BENCHMARK_SOURCES).map(([id, value]) => <option value={id} key={id}>{value.name}{id === DEFAULT_BENCHMARK_SOURCE ? "（默认）" : ""}</option>)}<option value="preset">调度评分（高级）</option></select></label><p>{presetView ? "调度评分用于任务分配，全部由 Artificial Analysis 成绩按排名换算；CLI 沿用同厂商 AA 成绩最好的模型；没有 AA 成绩的模型不参与调度。" : `当前仅采用 ${BENCHMARK_SOURCES[benchmarkSource].name}。画像、能力图、筛选和对比使用同一来源；没有成绩的型号保持空缺。`}</p></div>
    <div className="profiles-toolbar">
      <label className="profile-search"><span>搜索模型</span><input type="search" placeholder="模型名称或服务商…" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <div className="profile-filters" aria-label="按接入方式筛选">{([ ["all", "全部"], ["cli", "CLI"], ["api", "API"] ] as const).map(([value, label]) => <button key={value} aria-pressed={channel === value} onClick={() => setChannel(value)}>{label}</button>)}</div>
      <label className="benchmark-only-filter"><input type="checkbox" checked={onlyBenchmarked} onChange={event => setOnlyBenchmarked(event.target.checked)} />仅有{sourceLabel}{presetView ? "" : "成绩"}</label>
      <label className="profile-sort"><span>服务商</span><select value={provider} onChange={event => setProvider(event.target.value)}><option value="all">全部服务商</option>{Object.entries(PROVIDER_NAMES).map(([id, name]) => <option value={id} key={id}>{name}</option>)}<option value="cli">CLI 通道</option></select></label>
      <label className="profile-sort"><span>排序</span><select value={sortBy} onChange={event => setSortBy(event.target.value as SortBy)}><option value="latest">最新发布优先</option><option value="name">模型名称</option>{presetView && CAPABILITIES.map(({ key, label }) => <option value={key} key={key}>调度评分 · {label}从高到低</option>)}</select></label>
    </div>
    {selected ? <>
      <div className="profiles-layout">
        <aside className="profile-selector" aria-label="选择模型">
          <div className="profile-selector-heading"><span>模型目录</span><span>{filtered.length} 个</span></div>
          <div className="profile-selector-list">{filtered.map(model => <button key={model.id} className={`profile-option ${selected.id === model.id ? "is-selected" : ""}`} aria-pressed={selected.id === model.id} onClick={() => onSelect(model.id)}><span className="profile-option-channel">{channelLabel(model)}</span><span className="profile-option-copy"><b>{model.display_name}</b><small>{availability(model, source)}<span>查看画像 →</span></small></span></button>)}</div>
        </aside>
        <div ref={detailRef} className="profile-detail" tabIndex={-1}><ModelDetail key={`${selected.id}:${source}`} model={selected} source={source} onToggle={onToggle} onConfigure={onConfigure} /></div>
      </div>
      {!presetView ? <BenchmarkComparison key={source} models={filtered} source={benchmarkSource} onSelect={selectFromTable} /> : <section className="profile-comparison" aria-labelledby="profile-comparison-title">
        <div className="profile-comparison-heading"><div><h3 id="profile-comparison-title">调度评分</h3><p>当前调度使用的七项分数，均由 AA 成绩按排名换算。没有分数的维度显示 —。</p></div><span>调度分 / 100</span></div>
        <div className="profile-table-scroll" role="region" aria-label="可横向滚动的模型能力对比表" tabIndex={0}>
          <table><caption className="profile-sr-only">各模型七项调度评分，0–100 分；演示模型数值仅供演示。</caption><thead><tr><th scope="col">模型</th>{CAPABILITIES.map(({ key, label, description }) => <th scope="col" key={key} title={description}>{label}</th>)}</tr></thead><tbody>{filtered.map(model => <tr key={model.id} className={`${selected.id === model.id ? "is-selected" : ""} ${model.provider === "mock" ? "is-demo" : ""}`}><th scope="row"><button onClick={() => selectFromTable(model.id)} aria-label={`查看 ${model.display_name} 的完整画像`}>{model.display_name}<span>{channelLabel(model)} · {availability(model, source)}</span></button></th>{CAPABILITIES.map(({ key }) => {
            const value = score(model, key);
            return <td key={key}><span className={`comparison-value ${(value ?? 0) >= 90 ? "score-high" : ""}`}>{value ?? "—"}<span className="comparison-track" aria-hidden="true"><span style={{ width: `${value ?? 0}%` }} /></span></span></td>;
          })}</tr>)}</tbody></table>
        </div>
      </section>}
    </> : <div className="profiles-empty"><h3>{models.length ? "没有匹配的模型" : "正在读取模型画像…"}</h3><p>{models.length ? "试试其他名称，或清除筛选查看全部模型。" : "模型数据加载后，将在这里展示能力评分。"}</p>{models.length > 0 && <button className="ghost" onClick={() => { setQuery(""); setChannel("all"); setProvider("all"); setOnlyBenchmarked(false); }}>清除筛选</button>}</div>}
  </section>;
}
