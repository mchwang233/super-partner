import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

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

# Everything kept in the keyring: model provider keys plus the GitHub token used to publish.
SECRET_ENV = {**PROVIDER_ENV, "github": "GITHUB_TOKEN"}


KEYRING_TIMEOUT = 5.0


class KeyringUnavailable(RuntimeError):
    """The OS keyring is locked, missing, or did not answer in time."""


def _keyring_write(
    action: str, provider: str, value: Optional[str] = None, timeout: Optional[float] = None,
) -> None:
    """Change the keyring in a child process that can be killed.

    A locked desktop keyring can wait indefinitely for an unlock prompt the user may never
    see; doing that in the API process would hang the request. The secret travels over a pipe.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "super_partner.secrets", action],
            input=json.dumps({"provider": provider, "value": value}),
            capture_output=True, text=True, check=False,
            timeout=KEYRING_TIMEOUT if timeout is None else timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise KeyringUnavailable("The OS keyring did not respond; it may be locked") from None
    if result.returncode:
        raise KeyringUnavailable("No usable OS keyring is available")


def set_provider_key(provider: str, value: str) -> str:
    """Store a key and use it now; returns "keyring", or "session" if only kept in memory."""
    if provider not in SECRET_ENV:
        raise ValueError(f"Unsupported provider: {provider}")
    try:
        _keyring_write("set", provider, value)
        stored = "keyring"
    except KeyringUnavailable:
        logger.warning("OS keyring unavailable; the %s key is kept for this session only.", provider)
        stored = "session"
    os.environ[SECRET_ENV[provider]] = value
    return stored


def delete_provider_key(provider: str) -> str:
    """Stop using a key; returns "keyring", or "session" if a saved copy may still be in the keyring."""
    if provider not in SECRET_ENV:
        raise ValueError(f"Unsupported provider: {provider}")
    try:
        _keyring_write("delete", provider)
        removed = "keyring"
    except KeyringUnavailable:
        removed = "session"
    os.environ.pop(SECRET_ENV[provider], None)
    return removed


def _change_keyring(action: str) -> int:
    request = json.loads(sys.stdin.read())
    try:
        if action == "set":
            keyring.set_password(SERVICE, request["provider"], request["value"])
        else:
            try:
                keyring.delete_password(SERVICE, request["provider"])
            except keyring.errors.PasswordDeleteError:
                pass
    except Exception:
        # Never echo the exception: it may contain the credential.
        return 2
    return 0


def _read_provider_keys() -> dict[str, str]:
    """Saved keys; raises KeyringError if no read worked (a locked or missing keyring), so it is
    not mistaken for a keyring holding no keys."""
    values, asked, failed = {}, 0, 0
    for provider, env_name in SECRET_ENV.items():
        if env_name not in os.environ:
            asked += 1
            try:
                value = keyring.get_password(SERVICE, provider)
            except KeyringError:
                failed += 1
                value = None
            if value:
                values[env_name] = value
    if asked and failed == asked:
        raise KeyringError("keyring unavailable")
    return values


@contextlib.contextmanager
def _one_at_a_time(wait: float):
    """Let processes of this user read the keyring one after another: several at once make a
    desktop keyring slow enough to time out. After ``wait`` seconds a process goes ahead anyway."""
    try:
        import fcntl
    except ImportError:  # no flock (Windows): read as before
        yield
        return
    path = Path(tempfile.gettempdir()) / f"super-partner-keyring-{os.getuid()}.lock"
    try:
        handle = path.open("a")
    except OSError:
        yield
        return
    with handle:
        deadline, locked = time.monotonic() + wait, False
        while not locked and time.monotonic() < deadline:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            if locked:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _read_in_child(timeout: float) -> dict:
    """The saved keys, read by a separate process that can be killed and reaped if the keyring
    waits for an unlock dialog; it is tried once more, given twice the time, if it is only slow.
    Credentials stay in pipes, never in logs or on disk."""
    for limit in (timeout, timeout * 2):
        with _one_at_a_time(limit * 3):
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "super_partner.secrets"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=limit, check=True,
                )
            except subprocess.TimeoutExpired:
                continue
        values = json.loads(result.stdout)
        if not isinstance(values, dict):
            raise TypeError("Invalid keyring response")
        return values
    raise subprocess.TimeoutExpired("keyring", timeout * 3)


# Set when the last read of the keyring failed (locked, missing or too slow): saved keys may
# exist that are not loaded yet. `keep_loading_keys` tries again until a read succeeds.
_keyring_unavailable = False
KEYRING_RETRY_SECONDS = 30.0


def keyring_unavailable() -> bool:
    return _keyring_unavailable


def hydrate_provider_keys(timeout: float = 10.0) -> list[str]:
    """Load saved keys without letting a locked desktop keyring block startup. Returns the
    providers whose key was loaded by this call."""
    global _keyring_unavailable
    if all(env_name in os.environ for env_name in SECRET_ENV.values()):
        _keyring_unavailable = False
        return []
    try:
        values = _read_in_child(timeout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # Exception output may contain credentials, so only log a fixed message.
        if not _keyring_unavailable:
            logger.warning(
                "OS keyring unavailable or timed out; continuing with environment credentials. "
                "Unlock the keyring: saved API keys are loaded on the next retry (every %d s).",
                KEYRING_RETRY_SECONDS,
            )
        _keyring_unavailable = True
        return []
    _keyring_unavailable = False
    loaded = []
    for provider, env_name in SECRET_ENV.items():
        value = values.get(env_name)
        if isinstance(value, str) and value and env_name not in os.environ:
            os.environ[env_name] = value
            loaded.append(provider)
    return loaded


async def keep_loading_keys(on_loaded=None, interval: Optional[float] = None, timeout: float = 5.0) -> None:
    """While the keyring is unavailable, read it again every ``interval`` seconds, so keys
    saved there work once it is unlocked, without a restart. ``on_loaded(providers)`` is told
    which keys arrived."""
    import asyncio

    while _keyring_unavailable:
        await asyncio.sleep(KEYRING_RETRY_SECONDS if interval is None else interval)
        loaded = await asyncio.to_thread(hydrate_provider_keys, timeout)
        if loaded:
            logger.info("Loaded saved API keys from the OS keyring: %s", ", ".join(loaded))
            if on_loaded:
                on_loaded(loaded)


def configured_providers() -> list[str]:
    return [provider for provider, env in PROVIDER_ENV.items() if os.getenv(env)]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"set", "delete"}:
        sys.exit(_change_keyring(sys.argv[1]))
    try:
        print(json.dumps(_read_provider_keys()))
    except KeyringError:
        sys.exit(3)
