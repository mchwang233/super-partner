from .models import CapabilityScore, ModelProfile


def builtin_models() -> list[ModelProfile]:
    """Conservative seed data. Unverified prices cannot be used for real billing claims.

    Routing scores come only from Artificial Analysis (see scoring.py); only the offline demo
    model carries fixed scores.
    """
    return [
        ModelProfile(
            id="cli:codex", provider="openai", display_name="Codex CLI (membership)",
            context_window=1_000_000, input_per_million=0, output_per_million=0,
            supports_vision=True, pricing_verified=True, channel="cli", execution_priority=0,
        ),
        ModelProfile(
            id="cli:claude", provider="anthropic", display_name="Claude Code CLI (membership)",
            context_window=200_000, input_per_million=0, output_per_million=0,
            supports_vision=True, pricing_verified=True, channel="cli", execution_priority=0,
        ),
        ModelProfile(
            id="cli:gemini", provider="google", display_name="Gemini CLI (membership)",
            context_window=1_000_000, input_per_million=0, output_per_million=0,
            supports_vision=True, pricing_verified=True, channel="cli", execution_priority=0,
        ),
        ModelProfile(
            id="cli:qwen", provider="qwen", display_name="Qwen Code CLI (membership)",
            context_window=128_000, input_per_million=0, output_per_million=0,
            pricing_verified=True, channel="cli", execution_priority=0,
        ),
        ModelProfile(
            id="openai:gpt-5.6-sol", provider="openai", display_name="GPT-5.6 Sol",
            context_window=1_050_000, input_per_million=4, output_per_million=20,
            cached_input_per_million=0.4, supports_vision=True,
            source_url="https://developers.openai.com/api/docs/models", pricing_verified=True,
        ),
        ModelProfile(
            id="openai:gpt-5.6-terra", provider="openai", display_name="GPT-5.6 Terra",
            context_window=1_050_000, input_per_million=2, output_per_million=12,
            cached_input_per_million=.2, supports_vision=True,
            source_url="https://developers.openai.com/api/docs/models", pricing_verified=True,
        ),
        ModelProfile(
            id="openai:gpt-5.6-luna", provider="openai", display_name="GPT-5.6 Luna",
            context_window=1_050_000, input_per_million=.2, output_per_million=1.2,
            cached_input_per_million=.02, supports_vision=True,
            source_url="https://developers.openai.com/api/docs/models", pricing_verified=True,
        ),
        ModelProfile(
            id="anthropic:claude-sonnet-5", provider="anthropic", display_name="Claude Sonnet 5",
            context_window=200_000, input_per_million=3, output_per_million=15,
            supports_vision=True, pricing_verified=False,
        ),
        ModelProfile(
            id="google:gemini-pro", provider="google", display_name="Gemini Pro",
            context_window=1_000_000, input_per_million=2, output_per_million=12,
            supports_vision=True, pricing_verified=False,
        ),
        ModelProfile(
            id="deepseek:deepseek-chat", provider="deepseek", display_name="DeepSeek Chat",
            context_window=128_000, input_per_million=.3, output_per_million=1.2,
            pricing_verified=False,
        ),
        ModelProfile(
            id="qwen:qwen-plus", provider="qwen", display_name="Qwen Plus",
            context_window=128_000, input_per_million=.4, output_per_million=1.2,
            pricing_verified=False,
        ),
        ModelProfile(
            id="doubao:doubao-pro", provider="doubao", display_name="Doubao Pro",
            context_window=128_000, input_per_million=.4, output_per_million=1.2,
            pricing_verified=False,
        ),
        ModelProfile(
            id="glm:glm-5.2", provider="glm", display_name="GLM-5.2",
            context_window=200_000, input_per_million=1, output_per_million=4,
            supports_vision=False, pricing_verified=False,
            source_url="https://docs.bigmodel.cn/cn/api/introduction",
        ),
        ModelProfile(
            id="minimax:MiniMax-M2.7", provider="minimax", display_name="MiniMax M2.7",
            context_window=204_800, input_per_million=1, output_per_million=4,
            supports_vision=False, pricing_verified=False,
            source_url="https://platform.minimax.io/docs/api-reference/api-overview",
        ),
        ModelProfile(
            id="kimi:kimi-k3", provider="kimi", display_name="Kimi K3",
            context_window=1_000_000, input_per_million=3, output_per_million=15,
            supports_vision=True, pricing_verified=False,
            source_url="https://platform.kimi.com/docs/api/overview",
        ),
        ModelProfile(
            id="mock:local", provider="mock", display_name="Local Demo Model",
            context_window=128_000, input_per_million=.01, output_per_million=.02,
            scores=CapabilityScore(coding=80, planning=80, research=80, chinese=80,
                                   tool_use=80, structured_output=80, reliability=80),
            scores_source="demo", pricing_verified=True,
            execution_priority=1000,
        ),
    ]
