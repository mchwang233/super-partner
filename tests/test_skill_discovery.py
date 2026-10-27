"""Public skill discovery is bounded, reusable, and never a machine verification claim."""
import asyncio
import base64
import json

import httpx
import pytest
from super_partner import skill_discovery, skills
from super_partner.config import settings

TEXT = "---\nname: demo-tool\ndescription: Use demo-tool\n---\n# Usage\nRun `demo-tool --check`.\n"
SHA = "a" * 40


def transport_for(text=TEXT):
    requests = []

    def reply(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert "authorization" not in request.headers
        if request.url.path == "/search/repositories":
            return httpx.Response(200, json={"items": [{"full_name": "example/tool-skills", "default_branch": "main"}]})
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [
                {"path": "skills/demo-tool/SKILL.md", "type": "blob", "mode": "100644", "sha": SHA, "size": len(text)},
            ]})
        if "/git/blobs/" in request.url.path:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(text.encode()).decode()})
        raise AssertionError(request.url)

    return httpx.MockTransport(reply), requests


async def test_discovery_reuses_public_skill_with_provenance_and_no_execution(tmp_path):
    transport, requests = transport_for()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill and result.skill.tools == ["demo-tool"]
    assert not result.skill.machine
    assert "example/tool-skills" in result.skill.source_url
    assert result.skill.source_revision == SHA
    assert len(requests) == 3
    assert "demo-tool" in requests[0].url.params["q"]
    saved = skills.save_discovered(result.skill)
    assert saved.is_file()
    assert saved.read_text() == TEXT  # retain upstream metadata/content verbatim
    [cached] = skills.discovered(["demo-tool"])
    assert cached.source_revision == SHA and not cached.machine
    # Never one of the skills a step is given; only a reference for writing this machine's one.
    assert not [s for s in skills.library() if s.source_url] and skills.discovered(["other"]) == []


@pytest.mark.parametrize("content", ["not a skill", TEXT.replace("demo-tool", "unrelated-tool")])
async def test_discovery_rejects_invalid_or_irrelevant_content(content):
    transport, _ = transport_for(content)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill is None and result.reason


async def test_search_failure_is_nonblocking_and_does_not_retry_rate_limit():
    seen = []

    def limited(request):
        seen.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill is None and "429" in result.reason and len(seen) == 1


async def test_discovery_has_a_total_deadline(monkeypatch):
    monkeypatch.setattr(skill_discovery, "SEARCH_TIMEOUT", 0.01)

    async def stall(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(stall)) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill is None and "timeout" in result.reason.lower()


async def test_discovery_does_not_follow_redirects_or_search_task_text():
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill is None and len(seen) == 1
    assert "demo-tool" in seen[0].url.params["q"]


async def test_malformed_search_result_is_an_advisory_miss():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill is None and result.reason


async def test_unrelated_first_repository_does_not_exhaust_all_candidates():
    transport, _ = transport_for()
    first_blobs = []

    async def reply(request):
        if request.url.path == "/search/repositories":
            return httpx.Response(200, json={"items": [
                {"full_name": "example/unrelated", "default_branch": "main"},
                {"full_name": "example/tool-skills", "default_branch": "main"},
            ]})
        if "/unrelated/git/trees/" in request.url.path:
            return httpx.Response(200, json={"tree": [
                {"path": f"skills/other-{i}/SKILL.md", "type": "blob", "mode": "100644", "sha": SHA, "size": 100}
                for i in range(10)
            ]})
        if "/unrelated/git/blobs/" in request.url.path:
            first_blobs.append(request)
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"unrelated").decode()})
        return await transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    assert result.skill and "tool-skills" in result.skill.source_url
    assert len(first_blobs) <= 2


async def test_modified_cached_text_is_not_silently_reused():
    transport, _ = transport_for()
    async with httpx.AsyncClient(transport=transport) as client:
        result = await skill_discovery.discover(["demo-tool"], client=client)
    path = skills.save_discovered(result.skill)
    path.write_text(path.read_text() + "unrecorded modification")
    assert not [s for s in skills.discovered() if s.path == path]


def test_invalid_cached_provenance_cannot_break_skill_loading(tmp_path):
    root = settings.data_dir / "skills-discovered" / "bad"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(TEXT)
    (root / "source.json").write_text(json.dumps({"source_url": ["bad"]}))
    assert not [s for s in skills.discovered() if s.path == root / "SKILL.md"]
