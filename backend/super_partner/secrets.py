import json
import logging
import os
import subprocess
import sys

import keyring
from keyring.errors import KeyringError

SERVICE = "super-partner"
logger = logging.getLogger(__name__)
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
    os.environ[PROVIDER_ENV[provider]] = value


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


def _read_provider_keys() -> dict[str, str]:
    values = {}
    for provider, env_name in PROVIDER_ENV.items():
        if env_name not in os.environ:
            try:
                value = keyring.get_password(SERVICE, provider)
            except KeyringError:
                value = None
            if value:
                values[env_name] = value
    return values


def hydrate_provider_keys(timeout: float = 3.0) -> None:
    """Load saved keys without letting a locked desktop keyring block startup."""
    if all(env_name in os.environ for env_name in PROVIDER_ENV.values()):
        return
    try:
        # A separate process can be killed and reaped if the keyring waits for an
        # unlock dialog. Keep credentials in pipes, never in logs or on disk.
        result = subprocess.run(
            [sys.executable, "-m", "super_partner.secrets"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=True,
        )
        values = json.loads(result.stdout)
        if not isinstance(values, dict):
            raise TypeError("Invalid keyring response")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # Exception output may contain credentials, so only log a fixed message.
        logger.warning(
            "OS keyring unavailable or timed out; continuing with environment credentials. "
            "Unlock the keyring and restart to load saved API keys."
        )
        return
    for env_name in PROVIDER_ENV.values():
        value = values.get(env_name)
        if isinstance(value, str) and value:
            os.environ.setdefault(env_name, value)


def configured_providers() -> list[str]:
    return [provider for provider, env in PROVIDER_ENV.items() if os.getenv(env)]


if __name__ == "__main__":
    print(json.dumps(_read_provider_keys()))
