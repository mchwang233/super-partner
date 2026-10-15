import React, { useEffect, useMemo, useRef, useState } from "react";

type Suggestion = { path: string; name: string; source: "recent" | "found" };
type Inspection = { path: string; git: boolean; branch?: string | null; uncommitted_files?: number };

const RECENT_KEY = "super-partner.recent-projects";
const MAX_RECENT = 6;

function errorText(e: unknown) { return e instanceof Error ? e.message : String(e); }
function baseName(path: string) { return path.replace(/\/+$/, "").split("/").pop() || path; }
function parentOf(path: string) { const trimmed = path.replace(/\/+$/, ""); return trimmed.slice(0, trimmed.lastIndexOf("/")) || "/"; }

function storedRecents(): string[] {
  try { const value = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); return Array.isArray(value) ? value.filter(item => typeof item === "string") : []; }
  catch { return []; }
}

/** Remember a project after a task is planned with it, newest first. */
export function rememberProject(path: string) {
  if (!path) return;
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([path, ...storedRecents().filter(item => item !== path)].slice(0, MAX_RECENT))); } catch { /* storage unavailable */ }
}

/** Path status under the field: whether the folder is usable, and whether git can undo agent edits. */
function useInspection(apiBase: string, value: string) {
  const [state, setState] = useState<{ path: string; result?: Inspection; error?: string; loading: boolean }>({ path: "", loading: false });
  useEffect(() => {
    const path = value.trim();
    if (!path) { setState({ path: "", loading: false }); return; }
    setState(current => ({ ...current, path, loading: true }));
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/projects/inspect?${new URLSearchParams({ path })}`, { signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || `无法检查目录（${response.status}）`);
        setState({ path, result: body, loading: false });
      } catch (e) { if (!controller.signal.aborted) setState({ path, error: errorText(e), loading: false }); }
    }, 350);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [apiBase, value]);
  return state;
}

function ProjectStatus({ value, inspection }: { value: string; inspection: ReturnType<typeof useInspection> }) {
  if (!value.trim()) return null;
  if (inspection.loading || inspection.path !== value.trim()) return <p className="project-status idle">正在检查目录…</p>;
  if (inspection.error) return <p className="project-status bad" role="alert">{inspection.error}</p>;
  const result = inspection.result!;
  if (!result.git) return <p className="project-status warn">⚠ 这个目录不是 git 仓库，代理的改动无法用 git 查看或回退。</p>;
  const dirty = result.uncommitted_files ?? 0;
  return <p className="project-status ok">✓ git 仓库 · 分支 <b>{result.branch || "（无分支）"}</b> · {dirty ? <span className="warn-text">{dirty} 个未提交改动，建议先提交或暂存</span> : "工作区干净"}</p>;
}

export function ProjectField({ apiBase, value, onChange, usedProjects }: { apiBase: string; value: string; onChange: (path: string) => void; usedProjects: string[] }) {
  const [open, setOpen] = useState(false), [active, setActive] = useState(0), [query, setQuery] = useState("");
  const [found, setFound] = useState<{ projects: { name: string; path: string }[] } | null>(null), [loadingFound, setLoadingFound] = useState(false);
  const inspection = useInspection(apiBase, value);
  const [pickError, setPickError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  async function loadFound(refresh = false) {
    if (loadingFound || (found && !refresh)) return;
    setLoadingFound(true);
    try { const response = await fetch(`${apiBase}/api/projects/discover${refresh ? "?refresh=true" : ""}`); if (response.ok) setFound(await response.json()); }
    catch { /* suggestions are optional */ }
    finally { setLoadingFound(false); }
  }

  const suggestions = useMemo(() => {
    const recent = [...new Set([...storedRecents(), ...usedProjects])].slice(0, MAX_RECENT);
    const items: Suggestion[] = [
      ...recent.map(path => ({ path, name: baseName(path), source: "recent" as const })),
      ...(found?.projects ?? []).filter(project => !recent.includes(project.path)).map(project => ({ ...project, source: "found" as const })),
    ];
    const needle = query.trim().toLowerCase();
    return needle ? items.filter(item => item.path.toLowerCase().includes(needle)) : items;
  }, [found, usedProjects, query, open]);

  function choose(path: string) { onChange(path); setQuery(""); setOpen(false); }
  function show() { setOpen(true); setActive(0); loadFound(); }

  return <div className="project-field">
    <div className="project-row">
      <span className="project-icon" aria-hidden="true">📁</span>
      <div className="project-combo">
        <input ref={inputRef} className="project-input" value={value} spellCheck={false} aria-label="项目目录" role="combobox" aria-expanded={open} aria-autocomplete="list"
          placeholder="项目目录（可选）：点击选择最近或本机的 git 项目，或直接粘贴路径"
          onFocus={show} onClick={show} onBlur={() => setOpen(false)}
          onChange={e => { onChange(e.target.value); setQuery(e.target.value); setOpen(true); setActive(0); loadFound(); }}
          onKeyDown={e => {
            if (e.key === "Escape") { setOpen(false); return; }
            if (e.key === "ArrowDown") { e.preventDefault(); if (!open) show(); else setActive(i => Math.min(i + 1, suggestions.length - 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setActive(i => Math.max(i - 1, 0)); }
            if (e.key === "Enter" && open && suggestions[active]) { e.preventDefault(); choose(suggestions[active].path); }
          }} />
        {open && <div className="project-menu" role="listbox" onMouseDown={e => e.preventDefault()}>
          {suggestions.length === 0 && <p className="project-menu-empty">{loadingFound ? "正在查找本机项目…" : query ? "没有匹配的项目，会直接使用输入的路径。" : "没有找到本机 git 项目，可以直接粘贴目录路径。"}</p>}
          {suggestions.map((item, index) => {
            const header = index === 0 || suggestions[index - 1].source !== item.source;
            return <React.Fragment key={item.path}>
              {header && <p className="project-menu-group">{item.source === "recent" ? "最近使用" : "本机 git 项目"}</p>}
              <button type="button" role="option" aria-selected={index === active} className={`project-option ${index === active ? "active" : ""}`} onMouseEnter={() => setActive(index)} onClick={() => choose(item.path)}>
                <b>{item.name}</b><small>{parentOf(item.path)}</small>
              </button>
            </React.Fragment>;
          })}
          {found && <button type="button" className="project-menu-refresh" onClick={() => loadFound(true)}>{loadingFound ? "正在重新查找…" : "重新查找本机项目"}</button>}
        </div>}
      </div>
      {value && <button type="button" className="ghost project-clear" onClick={() => { onChange(""); inputRef.current?.focus(); }} aria-label="清除项目目录">×</button>}
      <BrowseButton apiBase={apiBase} start={value.trim() || (storedRecents()[0] ? parentOf(storedRecents()[0]) : "")} onPick={path => { setPickError(""); onChange(path); }} onError={setPickError} />
    </div>
    {pickError ? <p className="project-status bad" role="alert">{pickError}</p> : <ProjectStatus value={value} inspection={inspection} />}
  </div>;
}

/** Opens the desktop's own folder dialog on the machine running the API; hidden when it has none. */
function BrowseButton({ apiBase, start, onPick, onError }: { apiBase: string; start: string; onPick: (path: string) => void; onError: (message: string) => void }) {
  const [available, setAvailable] = useState(false), [waiting, setWaiting] = useState(false);
  useEffect(() => { fetch(`${apiBase}/api/projects/native-picker`).then(r => r.json()).then(status => setAvailable(!!status?.available)).catch(() => setAvailable(false)); }, [apiBase]);
  if (!available) return null;
  async function pick() {
    setWaiting(true); onError("");
    try {
      const query = start ? `?${new URLSearchParams({ start })}` : "";
      const response = await fetch(`${apiBase}/api/projects/native-picker${query}`, { method: "POST" }); const body = await response.json();
      if (!response.ok) throw new Error(body.detail || `无法打开文件夹对话框（${response.status}）`);
      if (body.path) onPick(body.path);
    } catch (e) { onError(errorText(e)); } finally { setWaiting(false); }
  }
  return <button type="button" className="ghost project-browse" onClick={pick} disabled={waiting} title="打开系统的文件夹选择对话框">{waiting ? "请在对话框中选择…" : "浏览…"}</button>;
}
