"""Bounded public SKILL.md discovery. No task text, credentials, installs or remote code execution.

Repository search works without a GitHub token; code search does not. Search by tool name,
inspect a few matching trees, then read immutable Markdown blobs. A miss/rate limit is advisory.
The caller owns the task's network permission and persists a successful general reference.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from . import skills

SEARCH_TIMEOUT = 30
MAX_REPOSITORIES = 3
MAX_CANDIDATES = 6
MAX_PER_REPOSITORY = 2
MAX_REQUESTS = 12
MAX_SKILL_BYTES = 40_000
MAX_RESPONSE_BYTES = 2_000_000


@dataclass
class Discovery:
    skill: Optional[skills.Skill] = None
    reason: str = ""


async def discover(tools: list[str], *, client=None) -> Discovery:
    """Return the first structurally valid, tool-matching public skill, or an explicit reason."""
    wanted = list(dict.fromkeys(t.lower() for t in tools if re.fullmatch(r"[\w.+-]{2,64}", t)))[:2]
    if not wanted:
        return Discovery(reason="No searchable tool name")
    try:
        if client is not None:
            return await asyncio.wait_for(_search(client, wanted), SEARCH_TIMEOUT)
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as owned:
            return await asyncio.wait_for(_search(owned, wanted), SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        return Discovery(reason="Public skill search timeout")
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError, binascii.Error) as exc:
        # URLs contain only public tool names/repositories; no task or credential is sent.
        return Discovery(reason=f"Public skill search unavailable: {type(exc).__name__}: {str(exc)[:180]}")


async def _search(client, tools: list[str]) -> Discovery:
    remaining = MAX_REQUESTS

    async def get(path, **params):
        nonlocal remaining
        if remaining <= 0:
            raise ValueError("skill search request limit reached")
        remaining -= 1
        # Fixed public host and no redirects: search results cannot redirect the host to local data.
        async with client.stream("GET", "https://api.github.com" + path, params=params,
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "Super-Partner-Skill-Discovery"},
                                 follow_redirects=False) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("skill discovery response too large")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))

    seen, inspected = set(), 0
    for tool in tools:
        data = await get("/search/repositories", q=f'{tool} skill in:name,description archived:false fork:false',
                         per_page=MAX_REPOSITORIES)
        for repo in data.get("items", [])[:MAX_REPOSITORIES]:
            name, branch = repo.get("full_name", ""), repo.get("default_branch", "main")
            if name in seen or not re.fullmatch(r"[\w.-]+/[\w.-]+", name):
                continue
            seen.add(name)
            tree = await get(f"/repos/{name}/git/trees/{quote(branch, safe='')}", recursive="1")
            candidates = [item for item in tree.get("tree", [])
                          if item.get("type") == "blob" and item.get("mode") == "100644"
                          and item.get("path", "").split("/")[-1] == "SKILL.md"
                          and 0 < item.get("size", 0) <= MAX_SKILL_BYTES
                          and ".." not in item["path"].split("/")
                          and re.fullmatch(r"[a-f0-9]{40,64}", item.get("sha", ""))]
            candidates.sort(key=lambda item: not any(t in item["path"].lower() for t in tools))
            for item in candidates[:MAX_PER_REPOSITORY]:
                if inspected >= MAX_CANDIDATES or remaining <= 0:
                    return Discovery(reason="No matching standalone skill within search limit")
                inspected += 1
                blob = await get(f"/repos/{name}/git/blobs/{item['sha']}")
                if blob.get("encoding") != "base64":
                    continue
                raw = base64.b64decode(blob.get("content", ""))
                if len(raw) > MAX_SKILL_BYTES:
                    continue
                skill = skills.parse(raw.decode("utf-8"))
                if not skill or not skill.description or not skill.body:
                    continue
                text = f"{skill.name}\n{skill.description}\n{skill.body}"
                if not all(skills.names_tool(text, t) for t in tools):
                    continue
                skill.tools = tools
                skill.source_url = f"https://github.com/{name}/blob/{quote(branch, safe='')}/{quote(item['path'])}"
                skill.source_revision = item["sha"]
                return Discovery(skill=skill)
    return Discovery(reason="No matching public SKILL.md found")
