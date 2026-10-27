import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from super_partner import secrets
from super_partner.secrets import (
    PROVIDER_ENV,
    delete_provider_key,
    hydrate_provider_keys,
    set_provider_key,
)


@pytest.fixture
def fake_keyring(tmp_path: Path, monkeypatch):
    for env_name in PROVIDER_ENV.values():
        monkeypatch.delenv(env_name, raising=False)
    (tmp_path / "fake_keyring.py").write_text('''
import asyncio
import os
import time
from pathlib import Path
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError

class Keyring(KeyringBackend):
    priority = 1

    def get_password(self, service, username):
        mode = os.environ.get("TEST_KEYRING_MODE")
        if mode == "locked":
            Path(os.environ["TEST_KEYRING_PID"]).write_text(str(os.getpid()))
            time.sleep(60)
        if mode == "unavailable":
            raise KeyringError("No keyring available")
        if mode == "broken":
            raise RuntimeError("sensitive-credential-must-not-be-logged")
        return "saved-" + username

    def _write_mode(self):
        mode = os.environ.get("TEST_KEYRING_MODE")
        if mode == "locked":
            Path(os.environ["TEST_KEYRING_PID"]).write_text(str(os.getpid()))
            time.sleep(60)
        if mode == "unavailable":
            raise KeyringError("No keyring available: sensitive-credential-must-not-be-logged")

    def set_password(self, service, username, password):
        self._write_mode()
        Path(os.environ["TEST_KEYRING_STORE"]).write_text(f"{username}={password}")

    def delete_password(self, service, username):
        self._write_mode()
        Path(os.environ["TEST_KEYRING_STORE"]).write_text(f"deleted {username}")
''')
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), str(backend_dir)]))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "fake_keyring.Keyring")
    monkeypatch.delenv("TEST_KEYRING_MODE", raising=False)
    monkeypatch.setenv("TEST_KEYRING_STORE", str(tmp_path / "store.txt"))
    yield tmp_path
    for env_name in PROVIDER_ENV.values():
        os.environ.pop(env_name, None)


def test_hydrate_loads_saved_keys_and_preserves_environment(fake_keyring, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")

    hydrate_provider_keys()

    assert os.environ["OPENAI_API_KEY"] == "environment-key"
    for provider, env_name in PROVIDER_ENV.items():
        if provider != "openai":
            assert os.environ[env_name] == f"saved-{provider}"


def test_locked_keyring_does_not_block_startup_or_leave_worker(fake_keyring, monkeypatch, caplog):
    pid_file = fake_keyring / "keyring.pid"
    monkeypatch.setenv("TEST_KEYRING_MODE", "locked")
    monkeypatch.setenv("TEST_KEYRING_PID", str(pid_file))
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    started = time.monotonic()

    hydrate_provider_keys(timeout=1)

    assert time.monotonic() - started < 5
    assert "keyring unavailable or timed out" in caplog.text
    assert os.environ["OPENAI_API_KEY"] == "environment-key"
    assert "ANTHROPIC_API_KEY" not in os.environ
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize("mode", ["unavailable", "broken"])
def test_keyring_failure_is_nonfatal_and_does_not_log_secrets(
    fake_keyring, monkeypatch, caplog, capfd, mode,
):
    monkeypatch.setenv("TEST_KEYRING_MODE", mode)

    hydrate_provider_keys()

    assert not any(env_name in os.environ for env_name in PROVIDER_ENV.values())
    output = capfd.readouterr()
    assert "sensitive-credential" not in caplog.text + output.out + output.err


def test_saved_key_goes_to_keyring_and_replaces_environment_value(fake_keyring, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "old-key-value")
    assert set_provider_key("openai", "new-key-value") == "keyring"
    assert (fake_keyring / "store.txt").read_text() == "openai=new-key-value"
    assert os.environ["OPENAI_API_KEY"] == "new-key-value"
    assert delete_provider_key("openai") == "keyring"
    assert (fake_keyring / "store.txt").read_text() == "deleted openai"
    assert "OPENAI_API_KEY" not in os.environ


def test_locked_keyring_does_not_hang_saving_and_keeps_key_for_session(fake_keyring, monkeypatch):
    pid_file = fake_keyring / "keyring.pid"
    monkeypatch.setenv("TEST_KEYRING_MODE", "locked")
    monkeypatch.setenv("TEST_KEYRING_PID", str(pid_file))
    monkeypatch.setattr(secrets, "KEYRING_TIMEOUT", 1)
    started = time.monotonic()

    assert set_provider_key("deepseek", "session-key-value") == "session"

    assert time.monotonic() - started < 5
    assert os.environ["DEEPSEEK_API_KEY"] == "session-key-value"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert delete_provider_key("deepseek") == "session"
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_unavailable_keyring_on_save_does_not_leak_the_key(fake_keyring, monkeypatch, caplog, capfd):
    monkeypatch.setenv("TEST_KEYRING_MODE", "unavailable")
    assert set_provider_key("openai", "sensitive-credential-value") == "session"
    output = capfd.readouterr()
    assert "sensitive-credential" not in caplog.text + output.out + output.err


def test_a_slow_keyring_is_asked_again_with_more_time(monkeypatch):
    from super_partner import secrets
    limits = []

    def run(command, timeout, **kwargs):
        limits.append(timeout)
        if len(limits) == 1:
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, stdout='{"DEEPSEEK_API_KEY": "saved"}', stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    try:
        secrets.hydrate_provider_keys(timeout=2)
        assert os.environ["DEEPSEEK_API_KEY"] == "saved" and limits == [2, 4]
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_processes_read_the_keyring_one_after_another(tmp_path):
    """Several processes starting at once (tests, workers) do not crowd the keyring."""
    script = tmp_path / "reader.py"
    script.write_text(
        "import sys, time\n"
        "from super_partner import secrets\n"
        "with secrets._one_at_a_time(10):\n"
        "    print(time.monotonic()); time.sleep(0.3); print(time.monotonic())\n"
    )
    backend = Path(__file__).resolve().parents[1] / "backend"
    env = {**os.environ, "PYTHONPATH": str(backend)}
    readers = [subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, env=env)
               for _ in range(3)]
    spans = sorted(tuple(map(float, reader.communicate(timeout=30)[0].split())) for reader in readers)
    assert all(earlier[1] <= later[0] for earlier, later in zip(spans, spans[1:]))


async def test_keys_load_once_a_keyring_locked_at_startup_is_unlocked(fake_keyring, monkeypatch, caplog):
    monkeypatch.setenv("TEST_KEYRING_MODE", "unavailable")
    assert hydrate_provider_keys() == []
    assert secrets.keyring_unavailable()
    assert "DEEPSEEK_API_KEY" not in os.environ

    monkeypatch.delenv("TEST_KEYRING_MODE")
    arrived = []
    await asyncio.wait_for(secrets.keep_loading_keys(arrived.extend, interval=0), 30)

    assert not secrets.keyring_unavailable()
    assert os.environ["DEEPSEEK_API_KEY"] == "saved-deepseek"
    assert {"deepseek", "minimax"} <= set(arrived)
    assert caplog.text.count("keyring unavailable or timed out") == 1


async def test_no_retry_while_the_keyring_was_read(fake_keyring, monkeypatch):
    hydrate_provider_keys()
    assert not secrets.keyring_unavailable()
    monkeypatch.setattr(secrets, "hydrate_provider_keys", lambda *a: pytest.fail("read again"))
    await asyncio.wait_for(secrets.keep_loading_keys(interval=0), 5)
