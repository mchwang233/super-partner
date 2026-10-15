"""Files the user attaches to a task: stored locally, then handed to every node's model.

Uploads land in ``<data_dir>/attachments/<id>/<name>`` before the task exists, so the composer
can show them while the user types. CLI agents get the file paths (and Codex the images as
``--image``); API models get images as image inputs and text files inline.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .config import settings
from .models import Attachment

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 10
# Inline at most this much of a text file into an API model's prompt.
MAX_INLINE_TEXT = 200_000
# Image types every vision API accepts. SVG is deliberately not an image here: it can carry script.
IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


class AttachmentError(ValueError):
    pass


def root() -> Path:
    return settings.data_dir / "attachments"


def _image_type(data: bytes) -> Optional[str]:
    for signature, media_type in IMAGE_SIGNATURES.items():
        if data.startswith(signature):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f/]", "_", name)[:120]
    return name if name and name not in {".", ".."} else "attachment"


def save(name: str, data: bytes) -> Attachment:
    if not data:
        raise AttachmentError("文件是空的。")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f"单个附件不能超过 {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB。")
    # The type comes from the content, never from the browser's claim or the file name.
    image = _image_type(data)
    kind = "image" if image else "text" if _is_text(data) else "file"
    attachment = Attachment(
        id=uuid4().hex, name=_safe_name(name), size=len(data), kind=kind,
        media_type=image or ("text/plain" if kind == "text" else "application/octet-stream"),
    )
    folder = root() / attachment.id
    folder.mkdir(parents=True)
    (folder / "content").mkdir()
    (folder / "content" / attachment.name).write_bytes(data)
    (folder / "meta.json").write_text(attachment.model_dump_json())
    return attachment


def get(attachment_id: str) -> Optional[Attachment]:
    if not re.fullmatch(r"[0-9a-f]{32}", attachment_id):
        return None
    try:
        return Attachment.model_validate(json.loads((root() / attachment_id / "meta.json").read_text()))
    except (OSError, ValueError):
        return None


def path(attachment: Attachment) -> Path:
    folder = root() / attachment.id
    stored = folder / "content" / attachment.name
    # Existing uploads retain their old path; new uploads cannot collide with metadata.
    return stored if (folder / "content").is_dir() else folder / attachment.name


def delete(attachment_id: str) -> bool:
    if not get(attachment_id):
        return False
    shutil.rmtree(root() / attachment_id, ignore_errors=True)
    return True


def resolve(ids: list[str]) -> list[Attachment]:
    if len(ids) > MAX_ATTACHMENTS:
        raise AttachmentError(f"一个任务最多附加 {MAX_ATTACHMENTS} 个文件。")
    found = []
    for attachment_id in dict.fromkeys(ids):
        attachment = get(attachment_id)
        if not attachment:
            raise AttachmentError("附件已失效，请重新添加。")
        found.append(attachment)
    return found


def describe(attachments: list[Attachment], with_paths: bool) -> str:
    """The prompt section naming the attachments, with local paths for CLI agents."""
    if not attachments:
        return ""
    kinds = {"image": "image", "text": "text file", "file": "file"}
    lines = [
        f"- {item.name} ({kinds[item.kind]}, {item.size} bytes)"
        + (f": {path(item)}" if with_paths else "")
        for item in attachments
    ]
    return "\n\nThe user attached these files" + (
        "; read them from the paths given:\n" if with_paths else ":\n"
    ) + "\n".join(lines)


def inline_text(attachments: list[Attachment]) -> str:
    """Text files' contents for API models, which cannot open local paths."""
    parts = []
    for item in attachments:
        if item.kind == "text":
            text = path(item).read_text(errors="replace")
            clipped = text[:MAX_INLINE_TEXT]
            note = "\n[truncated]" if len(text) > len(clipped) else ""
            parts.append(f"\n\n--- {item.name} ---\n{clipped}{note}\n--- end of {item.name} ---")
        elif item.kind == "file":
            parts.append(f"\n\n[{item.name} is a binary file that this model cannot read.]")
    return "".join(parts)


def image_data(item: Attachment) -> str:
    return base64.b64encode(path(item).read_bytes()).decode()
