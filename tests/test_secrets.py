import os
import time
from pathlib import Path

import pytest
from super_partner.secrets import PROVIDER_ENV, hydrate_provider_keys


@pytest.fixture
def fake_keyring(tmp_path: Path, monkeypatch):
    for env_name in PROVIDER_ENV.values():
        monkeypatch.delenv(env_name, raising=False)
    (tmp_path / "fake_keyring.py").write_text('''
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

    def set_password(self, service, username, password):
        pass

    def delete_password(self, service, username):
        pass
''')
    backend_dir = Path(__file__).resolve().parents[1] / "backend"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), str(backend_dir)]))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "fake_keyring.Keyring")
    monkeypatch.delenv("TEST_KEYRING_MODE", raising=False)
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
