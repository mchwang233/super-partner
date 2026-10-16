import React, { useEffect, useId, useRef } from "react";

type MissionRecord = {
  id: string;
  status: string;
  actual_cost_usd: number;
  created_at?: string;
  project_dir?: string | null;
  plan: { summary: string; nodes?: { status: string }[] };
};

/** Sidebar groups, most urgent first: what is running, what needs the user, what is done. */
export const MISSION_GROUPS = [
  { id: "active", label: "进行中", statuses: ["queued", "running"] },
  { id: "attention", label: "需要处理", statuses: ["awaiting_confirmation", "paused", "failed", "incomplete"] },
  { id: "done", label: "已结束", statuses: ["succeeded", "cancelled"] },
] as const;

export function needsAttention(status: string): boolean {
  return (MISSION_GROUPS[1].statuses as readonly string[]).includes(status);
}

export function progressOf(mission: MissionRecord): { done: number; total: number } {
  const nodes = mission.plan.nodes ?? [];
  return { done: nodes.filter(node => node.status === "succeeded").length, total: nodes.length };
}

export function relativeTime(iso?: string, now = Date.now()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const minutes = Math.floor((now - then) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} 小时前`;
  if (minutes < 7 * 24 * 60) return `${Math.floor(minutes / 1440)} 天前`;
  return new Date(then).toLocaleDateString("zh-CN");
}

function folder(path?: string | null) { return path ? path.replace(/\/+$/, "").split("/").pop() : ""; }

export const STATUS_LABELS: Record<string, string> = {
  draft: "草稿", awaiting_confirmation: "待确认", queued: "排队中", running: "运行中",
  paused: "已暂停", succeeded: "已完成", failed: "失败", cancelled: "已取消", incomplete: "未能完成",
  pending: "等待中", blocked: "未执行", unable: "无法完成",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status.replaceAll("_", " ");
}

/** Dollar amounts: tiny API charges stay readable without padding every value to 6 places. */
export function formatUsd(value: number): string {
  if (value === 0) return "$0";
  if (value < 0.0001) return "<$0.0001";
  if (value < 1) return `$${value.toFixed(4).replace(/0+$/, "")}`;
  return `$${value.toFixed(2)}`;
}

export function canDeleteMission(status: string): boolean {
  return ["succeeded", "failed", "cancelled", "incomplete"].includes(status);
}

export function canCancelMission(status: string): boolean {
  return ["awaiting_confirmation", "queued", "running", "paused"].includes(status);
}

type DeleteProps<T extends MissionRecord> = {
  mission: T;
  deletingId: string | null;
  onDelete: (mission: T) => void;
  compact?: boolean;
};

export function DeleteMissionButton<T extends MissionRecord>({ mission, deletingId, onDelete, compact = false }: DeleteProps<T>) {
  if (!canDeleteMission(mission.status)) return null;
  return <button
    type="button"
    className={compact ? "mission-delete-compact" : "danger mission-delete-full"}
    aria-label={`删除任务记录：${mission.plan.summary}`}
    title="删除任务记录、结果和日志；保留本地工程文件"
    disabled={deletingId !== null}
    onClick={() => onDelete(mission)}
  >{deletingId === mission.id ? "删除中…" : compact ? "删除" : "删除任务记录"}</button>;
}

type MissionDialogProps<T extends MissionRecord> = {
  mission: T;
  pending: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: () => void;
};

export function DeleteMissionDialog<T extends MissionRecord>(props: MissionDialogProps<T>) {
  return <MissionActionDialog {...props} action="delete" />;
}

export function CancelMissionDialog<T extends MissionRecord>(props: MissionDialogProps<T>) {
  return <MissionActionDialog {...props} action="cancel" />;
}

function MissionActionDialog<T extends MissionRecord>({ mission, pending, error, onCancel, onConfirm, action }: MissionDialogProps<T> & { action: "delete" | "cancel" }) {
  const copy = action === "delete" ? {
    title: "删除任务记录？",
    description: "将删除该任务的记录、结果和日志，保留本地工程文件。此操作无法撤销。",
    dismiss: "取消", confirm: "确认删除", pending: "删除中…", progress: "正在删除，请稍候…",
  } : {
    title: "取消任务？",
    description: "将停止该任务的执行，保留已有的结果、日志和本地工程文件。已经写入工程的修改不会回退。",
    dismiss: "返回", confirm: "确认取消", pending: "取消中…", progress: "正在取消，请稍候…",
  };
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement;
    cancelRef.current?.focus();
    return () => { if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus(); };
  }, []);
  useEffect(() => { if (pending) dialogRef.current?.focus(); }, [pending]);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !pending) onCancel(); }}>
    <section className="provider-modal mission-delete-dialog" ref={dialogRef} tabIndex={-1} role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={pending} onKeyDown={event => {
      if (event.key === "Escape") { event.preventDefault(); if (!pending) onCancel(); }
      if (event.key === "Tab") {
        const buttons = [...(dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (!first) event.preventDefault();
        else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialogRef.current)) { event.preventDefault(); first.focus(); }
      }
    }}>
      <div className="modal-title"><h3 id={titleId}>{copy.title}</h3></div>
      <p className="mission-delete-summary">{mission.plan.summary}</p>
      <p className="modal-note" id={descriptionId}>{copy.description}</p>
      {error && <p className="mission-delete-error" role="alert">{error}</p>}
      {pending && <p className="mission-delete-status" role="status">{copy.progress}</p>}
      <div className="mission-delete-actions">
        <button type="button" className="ghost" ref={cancelRef} disabled={pending} onClick={onCancel}>{copy.dismiss}</button>
        <button type="button" className="danger" disabled={pending} onClick={onConfirm}>{pending ? copy.pending : copy.confirm}</button>
      </div>
    </section>
  </div>;
}

type HistoryProps<T extends MissionRecord> = {
  runs: T[];
  selectedId?: string;
  deletingId: string | null;
  error: string;
  onSelect: (mission: T) => void;
  onDelete: (mission: T) => void;
};

export function MissionHistory<T extends MissionRecord>({ runs, selectedId, deletingId, error, onSelect, onDelete, collapsed = false, onToggle }: HistoryProps<T> & { collapsed?: boolean; onToggle?: () => void }) {
  const groups = MISSION_GROUPS.map(group => ({ ...group, runs: runs.filter(run => (group.statuses as readonly string[]).includes(run.status)) })).filter(group => group.runs.length);
  if (collapsed) return <section className="panel mission-history collapsed" aria-label="任务历史（已收起）">
    <button type="button" className="history-toggle" onClick={onToggle} aria-expanded={false} title="展开任务栏">»<span>{runs.length}</span></button>
    <div className="history-rail">{groups.map(group => <React.Fragment key={group.id}>
      <span className="rail-divider" title={group.label} aria-hidden="true" />
      {group.runs.map(run => <button type="button" key={run.id} className={`rail-run ${selectedId === run.id ? "active" : ""}`} aria-pressed={selectedId === run.id}
        onClick={() => onSelect(run)} title={`${run.plan.summary} · ${statusLabel(run.status)}`} aria-label={run.plan.summary}><span className={`dot ${run.status}`} /></button>)}
    </React.Fragment>)}</div>
  </section>;
  return <section className="panel mission-history" aria-label="任务历史">
    <div className="title"><h3>任务</h3><span className="history-count">{runs.length}</span>{onToggle && <button type="button" className="history-toggle" onClick={onToggle} aria-expanded={true} title="收起任务栏">«</button>}</div>
    {error && <p className="mission-delete-error" role="alert">{error}</p>}
    {runs.length === 0 ? <p className="empty">还没有任务。点右上角的“＋ 新任务”开始。</p> : <div className="run-list">{groups.map(group => <div className={`run-group ${group.id}`} key={group.id}>
      <p className="run-group-title">{group.label}<span>{group.runs.length}</span></p>
      {group.runs.map(run => {
        const { done, total } = progressOf(run);
        return <div className={`run-entry ${selectedId === run.id ? "active" : ""}`} key={run.id}>
          <button className={`run ${selectedId === run.id ? "active" : ""}`} aria-pressed={selectedId === run.id} onClick={() => onSelect(run)} title={run.plan.summary}>
            <span className={`dot ${run.status}`} />
            <span className="run-text"><b>{run.plan.summary}</b>
              <small><span className={`run-status ${run.status}`}>{statusLabel(run.status)}</span>{total > 0 && <> · {done}/{total}</>} · {formatUsd(run.actual_cost_usd)}{run.created_at && <> · {relativeTime(run.created_at)}</>}</small>
              {run.project_dir && <small className="run-project" title={run.project_dir}>📁 {folder(run.project_dir)}</small>}
              {total > 0 && run.status !== "succeeded" && <span className="run-progress" aria-hidden="true"><i style={{ width: `${(done / total) * 100}%` }} /></span>}
            </span>
          </button>
          <DeleteMissionButton mission={run} deletingId={deletingId} onDelete={onDelete} compact />
        </div>;
      })}
    </div>)}</div>}
  </section>;
}
