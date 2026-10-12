import React, { useEffect, useId, useRef } from "react";

type MissionRecord = {
  id: string;
  status: string;
  actual_cost_usd: number;
  plan: { summary: string };
};

export function canDeleteMission(status: string): boolean {
  return ["succeeded", "failed", "cancelled"].includes(status);
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
    className={`danger ${compact ? "mission-delete-compact" : "confirm"}`}
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

export function MissionHistory<T extends MissionRecord>({ runs, selectedId, deletingId, error, onSelect, onDelete }: HistoryProps<T>) {
  return <section className="panel mission-history" aria-label="任务历史">
    <div className="title"><h3>Recent missions</h3><span>{runs.length}</span></div>
    {error && <p className="mission-delete-error" role="alert">{error}</p>}
    {runs.length === 0 ? <p className="empty">No missions yet.</p> : runs.map(run => <div className="run-entry" key={run.id}>
      <button className={`run ${selectedId === run.id ? "active" : ""}`} aria-pressed={selectedId === run.id} onClick={() => onSelect(run)}>
        <span className={`dot ${run.status}`} />
        <span><b>{run.plan.summary}</b><small>{run.status.replaceAll("_", " ")} · ${run.actual_cost_usd.toFixed(6)}</small></span>
      </button>
      <DeleteMissionButton mission={run} deletingId={deletingId} onDelete={onDelete} compact />
    </div>)}
  </section>;
}
