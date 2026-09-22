import os

import keyring
from keyring.errors import KeyringError

SERVICE = "super-partner"
PROVIDER_ENV = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "doubao": "ARK_API_KEY",
    "glm": "ZHIPU_API_KEY", "minimax": "MINIMAX_API_KEY", "kimi": "MOONSHOT_API_KEY",
}


def set_provider_key(provider: str, value: str) -> None:
    if provider not in PROVIDER_ENV:
        raise ValueError(f"Unsupported provider: {provider}")
    try:
        keyring.set_password(SERVICE, provider, value)
    except KeyringError as exc:
        raise RuntimeError("No secure OS keyring is available; use an environment variable") from exc


def delete_provider_key(provider: str) -> None:
    if provider not in PROVIDER_ENV:
        raise ValueError(f"Unsupported provider: {provider}")
    try:
        keyring.delete_password(SERVICE, provider)
    except keyring.errors.PasswordDeleteError:
        pass
    except KeyringError as exc:
        raise RuntimeError("No secure OS keyring is available") from exc
    os.environ.pop(PROVIDER_ENV[provider], None)


def hydrate_provider_keys() -> None:
    for provider, env_name in PROVIDER_ENV.items():
        if env_name not in os.environ:
            try:
                value = keyring.get_password(SERVICE, provider)
            except KeyringError:
                value = None
            if value:
                os.environ[env_name] = value


def configured_providers() -> list[str]:
    return [provider for provider, env in PROVIDER_ENV.items() if os.getenv(env)]
