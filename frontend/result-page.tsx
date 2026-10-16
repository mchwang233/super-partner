import React, { FormEvent, useEffect, useRef, useState } from "react";
import { fileSize } from "./attachments";
import { isMarkdownFile, MarkdownView } from "./markdown";
import { statusLabel } from "./mission-history";

type ResultRun = {
  id: string; status: string; prompt?: string; created_at?: string; project_dir?: string | null; budget_usd?: number;
  allow_network?: boolean; use_cli?: boolean;
  final_result?: string | null; github_url?: string | null;
  plan: { summary: string; nodes: { id: string; title: string; status: string; model_id: string; result?: string | null; unable_reason?: string | null }[] };
};
type Listing = { root: string; in_project: boolean; files: { path: string; size: number }[]; github_url?: string | null };

function errorText(e: unknown) { return e instanceof Error ? e.message : String(e); }

async function request(url: string, init?: RequestInit) {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`);
  return body;
}

export type ChangeOptions = { allow_network: boolean; use_cli: boolean };
export type ChangeRequest = (prompt: string, budget: number, options: ChangeOptions) => Promise<string | null>;
const CHANGEABLE = new Set(["succeeded", "failed", "cancelled", "incomplete"]);

/** 需求变更: describe what to add or change; a new task continues from these results. */
function ChangePanel({ run, onSubmit, onClose }: { run: ResultRun; onSubmit: ChangeRequest; onClose: () => void }) {
  const [text, setText] = useState(""), [budget, setBudget] = useState(String(run.budget_usd ?? 2));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  // The change starts with the earlier task's settings.
  const [network, setNetwork] = useState(!!run.allow_network), [cli, setCli] = useState(run.use_cli ?? true);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { box.current?.focus(); }, []);
  const amount = Number(budget), ready = text.trim().length >= 3 && amount > 0 && !busy;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true); setError("");
    try { const problem = await onSubmit(text.trim(), amount, { allow_network: network, use_cli: cli }); if (problem) setError(problem); }
    catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  }
  return <form className="change-panel" onSubmit={submit} aria-label="需求变更">
    <div className="change-head">
      <b>需求变更</b>
      <small>在当前结果上继续：沿用同一个{run.project_dir ? "项目" : "工作目录"}和已有文件，先生成计划，你确认后再执行。</small>
    </div>
    <textarea ref={box} aria-label="新的需求" rows={4} maxLength={20000} value={text} onChange={event => setText(event.target.value)}
      onKeyDown={event => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
      placeholder="要新增或修改什么？例如：给待办加上到期提醒；把登录改成邮箱验证码；修复手机上按钮重叠。" />
    <div className="change-actions">
      <label className="budget-field">预算上限 <span>$</span><input type="number" min="0.01" step="any" inputMode="decimal" aria-label="需求变更的预算上限（美元）" value={budget} onChange={event => setBudget(event.target.value)} /></label>
      <label className="switch" title="关闭后只使用按量计费的 API 模型"><input type="checkbox" checked={cli} onChange={event => setCli(event.target.checked)} /><span />允许使用会员 CLI</label>
      <label className="switch" title="执行命令时可以访问互联网（下载依赖、调用外部接口、部署），本机上的服务仍然访问不到"><input type="checkbox" checked={network} onChange={event => setNetwork(event.target.checked)} /><span />允许联网</label>
      <span className="change-buttons">
        <button type="button" className="ghost" onClick={onClose} disabled={busy}>取消</button>
        <button type="submit" className="primary" disabled={!ready}>{busy ? "正在生成计划…" : "生成变更计划"}</button>
      </span>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
  </form>;
}

/** A full page for a task's output: final result, files, each step's output, save and publish. */
export function ResultPage({ apiBase, run, onBack, onChange, embedded = false }: { apiBase: string; run: ResultRun; onBack?: () => void; onChange?: ChangeRequest; embedded?: boolean }) {
  const [changing, setChanging] = useState(false);
  useEffect(() => setChanging(false), [run.id]);
  const [listing, setListing] = useState<Listing | null>(null), [listError, setListError] = useState("");
  const [tab, setTab] = useState<"result" | "files" | "steps">("result");
  const [file, setFile] = useState<string | null>(null), [text, setText] = useState(""), [fileError, setFileError] = useState("");
  const [publishing, setPublishing] = useState(false), [copied, setCopied] = useState(false);
  const base = `${apiBase}/api/tasks/${encodeURIComponent(run.id)}`;
  const reload = () => request(`${base}/files`).then(setListing).catch(e => setListError(errorText(e)));
  useEffect(() => { setListing(null); setFile(null); reload(); }, [run.id]);
  useEffect(() => {
    if (!file) return;
    setText(""); setFileError("");
    request(`${base}/files/content?${new URLSearchParams({ path: file })}`).then(body => setText(body.text)).catch(e => setFileError(errorText(e)));
  }, [file]);
  useEffect(() => {
    if (embedded || !onBack) return;
    const back = (event: KeyboardEvent) => { if (event.key === "Escape" && !publishing) onBack(); };
    window.addEventListener("keydown", back);
    return () => window.removeEventListener("keydown", back);
  }, [publishing]);
  const files = listing?.files ?? [];
  /** Opens a linked output file (relative, or absolute under the output folder) in the files tab. */
  function openLinked(href: string) {
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith("file:")) return false;
    let path = decodeURIComponent(href.replace(/^file:\/\//, "").split("#")[0]);
    const root = listing?.root?.replace(/\/+$/, "");
    if (root && path.startsWith(`${root}/`)) path = path.slice(root.length + 1);
    path = path.replace(/^\.\//, "");
    const match = files.find(item => item.path === path) ?? files.find(item => item.path.endsWith(`/${path}`));
    if (!match) return false;
    setTab("files"); setFile(match.path);
    return true;
  }
  const githubUrl = listing?.github_url ?? run.github_url;
  const canPublish = run.status === "succeeded" && !run.project_dir;
  async function copy() {
    try { await navigator.clipboard.writeText(run.final_result ?? ""); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* clipboard unavailable */ }
  }
  return <section className={`result-page ${embedded ? "embedded" : "panel"}`} aria-label="任务结果">
    <div className="result-page-head">
      {!embedded && <>
        <button type="button" className="ghost result-back" onClick={onBack}>← 返回任务</button>
        <div className="result-page-title">
          <p className="eyebrow">任务结果 · {statusLabel(run.status)}{run.created_at ? ` · ${new Date(run.created_at).toLocaleString("zh-CN", { hour12: false })}` : ""}</p>
          <h2>{run.plan.summary}</h2>
          {run.prompt && run.prompt !== run.plan.summary && <p className="result-prompt">{run.prompt}</p>}
        </div>
      </>}
      <div className="result-actions">
        {onChange && CHANGEABLE.has(run.status) && <button type="button" className="primary change-button" aria-expanded={changing}
          onClick={() => setChanging(open => !open)} title="在这个结果的基础上增加或修改需求">✎ 需求变更</button>}
        <a className="button-link primary" href={`${base}/archive`} download>⬇ 另存为 ZIP</a>
        <button type="button" className="ghost" onClick={copy} disabled={!run.final_result}>{copied ? "已复制" : "复制结果"}</button>
        <button type="button" className="ghost github-button" onClick={() => setPublishing(true)} disabled={!canPublish}
          title={run.project_dir ? "项目任务的改动在你自己的仓库里，请在项目中提交并推送" : run.status !== "succeeded" ? "任务完成后才能上传" : "把输出提交到 GitHub 仓库"}>上传到 GitHub</button>
      </div>
    </div>
    {run.status === "incomplete" && <p className="result-incomplete" role="status"><b>任务没有全部完成。</b>{" "}
      {run.plan.nodes.filter(node => node.status === "unable").length} 个步骤在当前环境中做不到，最终结果开头写明了原因和需要你做什么；已完成的部分照常保留。</p>}
    {changing && onChange && <ChangePanel run={run} onSubmit={onChange} onClose={() => setChanging(false)} />}
    {githubUrl && <p className="result-published">已上传到 <a href={githubUrl} target="_blank" rel="noreferrer">{githubUrl}</a></p>}
    <p className="result-location">输出位置：<code>{listing?.root ?? "…"}</code>{listing?.in_project && "（只列出任务改动过的文件）"}</p>
    <nav className="result-tabs" aria-label="结果内容">
      {([["result", "最终结果"], ["files", `文件（${files.length}）`], ["steps", `各步骤输出（${run.plan.nodes.length}）`]] as const).map(([id, label]) =>
        <button key={id} type="button" aria-pressed={tab === id} onClick={() => setTab(id)}>{label}</button>)}
    </nav>
    {tab === "result" && (run.final_result ? <MarkdownView className="result-text" text={run.final_result} onLink={openLinked} /> : <p className="empty">任务还没有最终结果。</p>)}
    {tab === "files" && <div className="result-files">
      {listError ? <p className="error">{listError}</p> : !files.length ? <p className="empty">{listing ? "没有输出文件。" : "正在读取文件…"}</p> : <>
        <ul className="result-file-list">{files.map(item => <li key={item.path}>
          <button type="button" aria-pressed={file === item.path} onClick={() => setFile(item.path)}><span>{item.path}</span><small>{fileSize(item.size)}</small></button>
        </li>)}</ul>
        <div className="result-file-view">
          {!file ? <p className="empty">选择左侧的文件查看内容。</p> : <>
            <p className="result-file-name">{file}</p>
            {fileError ? <p className="error">{fileError}</p> : !text ? <pre>加载中…</pre> : <MarkdownView key={file} text={text} markdown={isMarkdownFile(file)} onLink={openLinked} />}
          </>}
        </div>
      </>}
    </div>}
    {tab === "steps" && <ol className="result-steps">{run.plan.nodes.map((node, index) => <li key={node.id}>
      <details open={run.plan.nodes.length <= 3}>
        <summary><b>{String(index + 1).padStart(2, "0")} {node.title}</b><span className={`state ${node.status}`}>{statusLabel(node.status)}</span><small>{node.model_id}</small></summary>
        {node.unable_reason && <div className="unable-reason"><h5>无法完成的原因</h5><MarkdownView text={node.unable_reason} /></div>}
        {node.result ? <MarkdownView text={node.result} onLink={openLinked} /> : <pre>（无输出）</pre>}
      </details>
    </li>)}</ol>}
    {publishing && <GitHubDialog apiBase={apiBase} run={run} onClose={() => setPublishing(false)} onPublished={() => reload()} />}
  </section>;
}

function GitHubDialog({ apiBase, run, onClose, onPublished }: { apiBase: string; run: ResultRun; onClose: () => void; onPublished: () => void }) {
  const [status, setStatus] = useState<{ token: boolean; git: boolean } | null>(null);
  const [mode, setMode] = useState<"create" | "existing">("create");
  const [name, setName] = useState(""), [privateRepo, setPrivateRepo] = useState(true), [description, setDescription] = useState(run.plan.summary);
  const [remote, setRemote] = useState(""), [token, setToken] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [url, setUrl] = useState<string | null>(null);
  useEffect(() => { request(`${apiBase}/api/github`).then(setStatus).catch(e => setError(errorText(e))); }, []);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) { event.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", close, true);
    return () => window.removeEventListener("keydown", close, true);
  }, [busy]);
  async function saveToken() {
    setBusy(true); setError("");
    try { await request(`${apiBase}/api/github/token`, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ token }) }); setToken(""); setStatus(current => ({ git: current?.git ?? true, token: true })); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  async function publish() {
    setBusy(true); setError("");
    const body = mode === "existing" ? { remote } : { name: name.trim() || null, private: privateRepo, description };
    try {
      const result = await request(`${apiBase}/api/tasks/${encodeURIComponent(run.id)}/github`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      setUrl(result.url); onPublished();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <div className="modal-backdrop" onMouseDown={() => !busy && onClose()}>
    <section className="provider-modal github-modal" role="dialog" aria-modal="true" aria-label="上传到 GitHub" onMouseDown={event => event.stopPropagation()}>
      <div className="modal-title"><div><p className="eyebrow">发布输出</p><h3>上传到 GitHub</h3></div><button type="button" className="modal-close" onClick={onClose} disabled={busy} aria-label="关闭">×</button></div>
      {url ? <div className="github-done"><p>✓ 已上传。</p><a href={url} target="_blank" rel="noreferrer">{url}</a><button type="button" className="primary" onClick={onClose}>完成</button></div> : <>
        <p className="modal-note">会把任务的输出文件和一份 RESULT.md（任务描述、最终结果和各步骤输出）提交为一个新的提交，推送到仓库的 main 分支。</p>
        <div className="github-modes" role="radiogroup" aria-label="上传方式">
          <label><input type="radio" checked={mode === "create"} onChange={() => setMode("create")} />新建仓库</label>
          <label><input type="radio" checked={mode === "existing"} onChange={() => setMode("existing")} />推送到已建好的空仓库</label>
        </div>
        {mode === "create" && (status && !status.token ? <div className="github-token">
          <p>新建仓库需要一个 GitHub 令牌（Fine-grained token，授予 Administration 和 Contents 的读写权限，或经典令牌的 repo 权限）。令牌只保存在本机的系统密钥环里。</p>
          <div className="key-form"><input type="password" value={token} onChange={e => setToken(e.target.value)} placeholder="ghp_… 或 github_pat_…" aria-label="GitHub 令牌" autoComplete="off" /><button type="button" className="primary" disabled={busy || token.trim().length < 10} onClick={saveToken}>保存令牌</button></div>
        </div> : <div className="github-form">
          <label><span>仓库名</span><input value={name} onChange={e => setName(e.target.value)} placeholder="留空则自动命名" aria-label="仓库名" /></label>
          <label><span>描述</span><input value={description} onChange={e => setDescription(e.target.value)} aria-label="仓库描述" /></label>
          <label className="github-private"><input type="checkbox" checked={privateRepo} onChange={e => setPrivateRepo(e.target.checked)} />私有仓库（只有你能看到）</label>
        </div>)}
        {mode === "existing" && <div className="github-form">
          <label><span>仓库地址</span><input value={remote} onChange={e => setRemote(e.target.value)} placeholder="git@github.com:you/repo.git" aria-label="仓库地址" /></label>
          <p className="modal-note">使用本机 git 已配置的 SSH 密钥推送。仓库需要是空的，否则推送会被拒绝。</p>
        </div>}
        {error && <p className="error" role="alert">{error}</p>}
        <div className="github-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>取消</button>
          <button type="button" className="primary" onClick={publish}
            disabled={busy || !status?.git || (mode === "create" ? !status?.token : !remote.trim())}>{busy ? "上传中…" : mode === "create" ? (privateRepo ? "创建私有仓库并上传" : "创建公开仓库并上传") : "推送"}</button>
        </div>
        {status && !status.git && <p className="error">这台机器没有安装 git。</p>}
      </>}
    </section>
  </div>;
}
