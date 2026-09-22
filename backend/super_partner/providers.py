from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx

from .cli_channels import CLI_SPECS, CliChannelRegistry
from .config import settings


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    used_model_id: str | None = None


class ProviderError(RuntimeError):
    pass


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 529}


class ProviderRegistry:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=120)
        self.cli = CliChannelRegistry()
        self.max_attempts = 4
        self.retry_base_delay = 1.0
        self.retry_max_delay = 30.0

    def configured_models(self, model_ids: list[str]) -> set[str]:
        configured = {"mock:local", *self.cli.available_model_ids()}
        env_names = {
            "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "google": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
            "qwen": "DASHSCOPE_API_KEY", "doubao": "ARK_API_KEY",
            "glm": "ZHIPU_API_KEY", "minimax": "MINIMAX_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
        }
        for model_id in model_ids:
            provider = model_id.split(":", 1)[0]
            env_name = env_names.get(provider)
            if env_name and os.getenv(env_name):
                configured.add(model_id)
        return configured

    async def complete(self, model_id: str, prompt: str, workspace=None, cli_timeout: int = 900) -> ProviderResult:
        provider, model = model_id.split(":", 1)
        if provider == "cli":
            try:
                text = await self.cli.complete(
                    model_id, prompt, workspace or settings.workspace_root, timeout=cli_timeout,
                )
                return ProviderResult(
                    text=text, input_tokens=0, output_tokens=0, used_model_id=model_id,
                )
            except RuntimeError as exc:
                fallback = CLI_SPECS[model_id].fallback_model_id
                if fallback not in self.configured_models([fallback]):
                    raise ProviderError(f"{exc}; no API fallback is configured") from exc
                result = await self.complete(fallback, prompt, workspace, cli_timeout)
                result.used_model_id = fallback
                return result
        if provider == "mock":
            text = f"Completed by {model_id}: {prompt[:800]}"
            return ProviderResult(text=text, input_tokens=max(1, len(prompt) // 4), output_tokens=max(1, len(text) // 4), used_model_id=model_id)
        if provider == "anthropic":
            return await self._anthropic(model, prompt)
        if provider == "google":
            return await self._gemini(model, prompt)
        return await self._openai_compatible(provider, model, prompt)

    async def test_provider(self, provider: str) -> str:
        configs = {
            "openai": ("https://api.openai.com/v1/models", "OPENAI_API_KEY", {}),
            "anthropic": (
                "https://api.anthropic.com/v1/models", "ANTHROPIC_API_KEY",
                {"anthropic-version": "2023-06-01"},
            ),
            "deepseek": ("https://api.deepseek.com/models", "DEEPSEEK_API_KEY", {}),
            "qwen": (
                "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
                "DASHSCOPE_API_KEY", {},
            ),
            "doubao": ("https://ark.cn-beijing.volces.com/api/v3/models", "ARK_API_KEY", {}),
            "glm": ("https://open.bigmodel.cn/api/paas/v4/models", "ZHIPU_API_KEY", {}),
            "minimax": ("https://api.minimax.io/v1/models", "MINIMAX_API_KEY", {}),
            "kimi": ("https://api.moonshot.cn/v1/models", "MOONSHOT_API_KEY", {}),
        }
        if provider == "google":
            key = os.getenv("GEMINI_API_KEY")
            if not key:
                raise ProviderError("No API key is configured for google")
            response = await self.client.get(
                "https://generativelanguage.googleapis.com/v1beta/models", params={"key": key},
            )
        else:
            if provider not in configs:
                raise ProviderError(f"Unsupported provider: {provider}")
            url, env_name, extra_headers = configs[provider]
            key = os.getenv(env_name)
            if not key:
                raise ProviderError(f"No API key is configured for {provider}")
            auth_header = "x-api-key" if provider == "anthropic" else "Authorization"
            auth_value = key if provider == "anthropic" else f"Bearer {key}"
            response = await self.client.get(
                url, headers={auth_header: auth_value, **extra_headers},
            )
        if response.is_error:
            raise ProviderError(
                f"Connection test failed ({response.status_code}): {response.text[:200]}"
            )
        return "Connection verified"

    async def _post(self, provider: str, url: str, **kwargs) -> httpx.Response:
        """POST with exponential backoff on rate limits, server errors, and network failures."""
        for attempt in range(1, self.max_attempts + 1):
            delay = self.retry_base_delay * 2 ** (attempt - 1)
            try:
                response = await self.client.post(url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise ProviderError(f"{provider} request failed: {exc}") from exc
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                    return response
                try:
                    delay = float(response.headers.get("retry-after", delay))
                except ValueError:
                    pass
            await asyncio.sleep(min(delay, self.retry_max_delay))
        raise AssertionError("unreachable")

    async def _openai_compatible(self, provider: str, model: str, prompt: str) -> ProviderResult:
        configs = {
            "openai": ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"),
            "deepseek": ("https://api.deepseek.com/chat/completions", "DEEPSEEK_API_KEY"),
            "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "DASHSCOPE_API_KEY"),
            "doubao": ("https://ark.cn-beijing.volces.com/api/v3/chat/completions", "ARK_API_KEY"),
            "glm": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "ZHIPU_API_KEY"),
            "minimax": ("https://api.minimax.io/v1/chat/completions", "MINIMAX_API_KEY"),
            "kimi": ("https://api.moonshot.cn/v1/chat/completions", "MOONSHOT_API_KEY"),
        }
        url, env = configs[provider]
        response = await self._post(
            provider, url, headers={"Authorization": f"Bearer {os.environ[env]}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        )
        if response.is_error:
            raise ProviderError(f"{provider} returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage = data.get("usage", {})
        return ProviderResult(
            text=data["choices"][0]["message"]["content"],
            input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
            used_model_id=f"{provider}:{model}",
        )

    async def _anthropic(self, model: str, prompt: str) -> ProviderResult:
        response = await self._post(
            "anthropic", "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
            json={"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]},
        )
        if response.is_error:
            raise ProviderError(f"anthropic returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        return ProviderResult(data["content"][0]["text"], data["usage"]["input_tokens"], data["usage"]["output_tokens"], f"anthropic:{model}")

    async def _gemini(self, model: str, prompt: str) -> ProviderResult:
        response = await self._post(
            "google", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": os.environ["GEMINI_API_KEY"]}, json={"contents": [{"parts": [{"text": prompt}]}]},
        )
        if response.is_error:
            raise ProviderError(f"google returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage = data.get("usageMetadata", {})
        return ProviderResult(
            data["candidates"][0]["content"]["parts"][0]["text"],
            usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0),
            f"google:{model}",
        )
