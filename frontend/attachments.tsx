import React, { useEffect, useRef, useState } from "react";

/** A file stored by the API (`POST /api/attachments`). */
export type Attachment = { id: string; name: string; size: number; kind: "image" | "text" | "file"; media_type: string };
type Pending = { key: string; name: string; size: number; preview?: string; status: "uploading" | "ready" | "error"; error?: string; stored?: Attachment };

const MAX_FILES = 10, MAX_BYTES = 20 * 1024 * 1024;

export function fileSize(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function errorText(e: unknown) { return e instanceof Error ? e.message : String(e); }

/** Files attached to the task being written: uploaded as soon as they are added. */
export function useAttachments(apiBase: string) {
  const [items, setItems] = useState<Pending[]>([]);
  const counter = useRef(0);
  const update = (key: string, change: Partial<Pending>) => setItems(current => current.map(item => item.key === key ? { ...item, ...change } : item));
  useEffect(() => () => items.forEach(item => item.preview && URL.revokeObjectURL(item.preview)), []);

  function add(files: File[]) {
    const room = MAX_FILES - items.length;
    const accepted = files.slice(0, Math.max(0, room));
    const added: Pending[] = accepted.map(file => ({
      key: `a${++counter.current}`, name: file.name || "粘贴的图片.png", size: file.size, status: "uploading",
      preview: file.type.startsWith("image/") && typeof URL.createObjectURL === "function" ? URL.createObjectURL(file) : undefined,
    }));
    if (files.length > accepted.length) added.push({ key: `a${++counter.current}`, name: `另外 ${files.length - accepted.length} 个文件`, size: 0, status: "error", error: `一个任务最多附加 ${MAX_FILES} 个文件` });
    setItems(current => [...current, ...added]);
    accepted.forEach(async (file, index) => {
      const { key, name } = added[index];
      if (file.size > MAX_BYTES) { update(key, { status: "error", error: "超过 20 MB" }); return; }
      try {
        const response = await fetch(`${apiBase}/api/attachments?${new URLSearchParams({ name })}`, { method: "POST", body: file });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || `上传失败（${response.status}）`);
        update(key, { status: "ready", stored: body });
      } catch (e) { update(key, { status: "error", error: errorText(e) }); }
    });
  }

  function remove(key: string) {
    const item = items.find(entry => entry.key === key);
    if (item?.preview) URL.revokeObjectURL(item.preview);
    setItems(current => current.filter(entry => entry.key !== key));
    if (item?.stored) fetch(`${apiBase}/api/attachments/${item.stored.id}`, { method: "DELETE" }).catch(() => {});
  }

  /** After the task is created the files belong to it; just empty the composer. */
  function clear() { items.forEach(item => item.preview && URL.revokeObjectURL(item.preview)); setItems([]); }

  return {
    items, add, remove, clear,
    ids: items.flatMap(item => item.stored ? [item.stored.id] : []),
    uploading: items.some(item => item.status === "uploading"),
  };
}

export function AttachButton({ onFiles }: { onFiles: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null);
  return <>
    <button type="button" className="ghost attach-button" onClick={() => input.current?.click()} title="添加图片或文件，也可以拖进输入框或直接粘贴截图">📎 添加附件</button>
    <input ref={input} type="file" multiple hidden aria-label="选择附件" onChange={event => { onFiles([...(event.target.files ?? [])]); event.target.value = ""; }} />
  </>;
}

export function AttachmentTray({ attachments }: { attachments: ReturnType<typeof useAttachments> }) {
  if (!attachments.items.length) return null;
  return <ul className="attachment-tray" aria-label="已添加的附件">
    {attachments.items.map(item => <li key={item.key} className={`attachment-chip ${item.status}`} title={item.error ?? item.name}>
      {item.preview ? <img src={item.preview} alt="" /> : <span className="attachment-icon" aria-hidden="true">📄</span>}
      <span className="attachment-text"><b>{item.name}</b><small>{item.status === "uploading" ? "上传中…" : item.status === "error" ? item.error : fileSize(item.size)}</small></span>
      <button type="button" className="attachment-remove" onClick={() => attachments.remove(item.key)} aria-label={`移除 ${item.name}`}>×</button>
    </li>)}
  </ul>;
}

/** Attachments of a created task: images as thumbnails, other files as download links. */
export function TaskAttachments({ apiBase, attachments }: { apiBase: string; attachments?: Attachment[] }) {
  if (!attachments?.length) return null;
  return <ul className="attachment-tray task-attachments" aria-label="任务附件">
    {attachments.map(item => {
      const url = `${apiBase}/api/attachments/${item.id}/content`;
      return <li key={item.id} className="attachment-chip ready">
        <a href={url} target="_blank" rel="noreferrer" title={item.kind === "image" ? "查看原图" : "下载"}>
          {item.kind === "image" ? <img src={url} alt={item.name} /> : <span className="attachment-icon" aria-hidden="true">📄</span>}
          <span className="attachment-text"><b>{item.name}</b><small>{fileSize(item.size)}</small></span>
        </a>
      </li>;
    })}
  </ul>;
}
