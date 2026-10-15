import json

import httpx
from super_partner import attachments as files
from super_partner.catalog import builtin_models
from super_partner.cli_channels import _with_attachments
from super_partner.models import CapabilityScore, ModelProfile
from super_partner.planner import heuristic_plan
from super_partner.providers import ProviderRegistry
from super_partner.routing import eligible_for_node

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ORIGIN = {"origin": "http://localhost:5173"}


async def upload(client, name: str, data: bytes) -> dict:
    response = await client.post("/api/attachments", params={"name": name}, content=data, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()


async def test_uploads_are_typed_by_content_and_only_images_display_inline(task_api):
    _, client = task_api
    image = await upload(client, "../../shot.txt", PNG)
    text = await upload(client, "notes.md", "# 需求\n登录页".encode())
    svg = await upload(client, "logo.svg", b"<svg onload='alert(1)'/>")
    binary = await upload(client, "a.bin", b"\x00\x01\x02")
    assert (image["kind"], image["media_type"], image["name"]) == ("image", "image/png", "shot.txt")
    assert (text["kind"], svg["kind"], binary["kind"]) == ("text", "text", "file")

    shown = await client.get(f"/api/attachments/{image['id']}/content")
    assert shown.headers["content-type"] == "image/png" and shown.content == PNG
    assert shown.headers["content-disposition"].startswith("inline")
    script = await client.get(f"/api/attachments/{svg['id']}/content")
    assert script.headers["content-type"] == "application/octet-stream"
    assert script.headers["content-disposition"].startswith("attachment")
    assert script.headers["x-content-type-options"] == "nosniff"
    assert (await client.get("/api/attachments/../meta.json/content")).status_code == 404

    assert (await client.delete(f"/api/attachments/{binary['id']}", headers=ORIGIN)).status_code == 204
    assert files.get(binary["id"]) is None


async def test_uploads_are_refused_from_other_sites_and_when_too_large(task_api, monkeypatch):
    _, client = task_api
    response = await client.post("/api/attachments", content=PNG, headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    monkeypatch.setattr(files, "MAX_ATTACHMENT_BYTES", 10)
    response = await client.post("/api/attachments", content=PNG, headers=ORIGIN)
    assert response.status_code == 413


async def test_task_keeps_its_attachments_and_deleting_it_removes_them(task_api, monkeypatch):
    api, client = task_api
    demo = next(model for model in builtin_models() if model.id == "mock:local")
    monkeypatch.setattr(api, "models", {demo.id: demo})
    monkeypatch.setattr(api.providers, "configured_models", lambda _ids: {demo.id})
    image = await upload(client, "screen.png", PNG)

    response = await client.post("/api/tasks", json={
        "prompt": "Describe the screenshot", "use_cli": False, "attachments": [image["id"]],
    }, headers=ORIGIN)

    assert response.status_code == 201, response.text
    run = response.json()
    assert [item["id"] for item in run["attachments"]] == [image["id"]]
    assert (await client.delete(f"/api/attachments/{image['id']}", headers=ORIGIN)).status_code == 409
    assert (await client.post(f"/api/tasks/{run['id']}/cancel", headers=ORIGIN)).status_code == 200
    assert (await client.delete(f"/api/tasks/{run['id']}", headers=ORIGIN)).status_code == 204
    assert files.get(image["id"]) is None

    missing = await client.post("/api/tasks", json={"prompt": "Describe it", "attachments": ["0" * 32]}, headers=ORIGIN)
    assert missing.status_code == 400


def test_images_route_only_to_models_that_can_see_them():
    def api_model(model_id: str, vision):
        return ModelProfile(
            id=model_id, provider="x", display_name=model_id, context_window=200_000,
            input_per_million=1, output_per_million=1, supports_vision=vision,
            scores=CapabilityScore(**{key: 90 for key in CapabilityScore.model_fields}),
        )
    models = [api_model("blind", False), api_model("seeing", True), api_model("unknown", None)]
    node = heuristic_plan("Describe it").nodes[0]
    configured = {model.id for model in models}
    assert {m.id for m in eligible_for_node(node, models, configured)} == configured
    assert {m.id for m in eligible_for_node(node, models, configured, needs_vision=True)} == {"seeing", "unknown"}


async def test_api_models_get_images_as_inputs_and_text_inline(task_api, monkeypatch):
    image = files.save("screen.png", PNG)
    note = files.save("spec.txt", "必须支持暗色模式".encode())
    sent = {}

    def respond(request: httpx.Request) -> httpx.Response:
        sent[request.url.host] = json.loads(request.content)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(200, json={"content": [{"text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    registry = ProviderRegistry()
    await registry.client.aclose()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await registry.complete("openai:gpt-x", "Review", attachments=[image, note])
        await registry.complete("anthropic:claude-x", "Review", attachments=[image, note])
    finally:
        await registry.client.aclose()

    openai_parts = sent["api.openai.com"]["messages"][0]["content"]
    assert openai_parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "必须支持暗色模式" in openai_parts[0]["text"] and "screen.png" in openai_parts[0]["text"]
    anthropic_parts = sent["api.anthropic.com"]["messages"][0]["content"]
    assert anthropic_parts[0]["source"]["media_type"] == "image/png"
    assert "必须支持暗色模式" in anthropic_parts[1]["text"]


def test_cli_agents_are_pointed_at_the_attached_files(task_api):
    image = files.save("screen.png", PNG)
    note = files.save("spec.txt", b"spec")
    codex = _with_attachments("cli:codex", ["exec", "--color", "never", "-"], [image, note])
    assert codex == ["exec", f"--image={files.path(image)}", "--color", "never", "-"]
    claude = _with_attachments("cli:claude", ["-p"], [image, note])
    assert claude[:2] == ["-p", "--add-dir"] and str(files.path(note).parent) in claude
    assert str(files.path(note)) in files.describe([image, note], with_paths=True)


def test_claude_cli_runs_commands_only_inside_its_sandbox():
    from super_partner.cli_channels import CLAUDE_SANDBOX_SETTINGS, CLI_SPECS
    args = CLI_SPECS["cli:claude"].invoke_args
    assert "--dangerously-skip-permissions" not in args and "bypassPermissions" not in args
    assert args[args.index("--settings") + 1] == CLAUDE_SANDBOX_SETTINGS
    assert json.loads(CLAUDE_SANDBOX_SETTINGS)["sandbox"] == {
        "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
    }
