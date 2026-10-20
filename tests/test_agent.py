import json
import os
import shlex
from pathlib import Path

import httpx
import pytest
from super_partner import agent, environment
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderError, ProviderRegistry

needs_bwrap = pytest.mark.skipif(not agent.sandbox_available(), reason="bubblewrap is not installed")


@pytest.fixture
def command_workspace(tmp_path, monkeypatch):
    """Exercise command I/O and file isolation without needing a network namespace."""
    root = tmp_path / "command work"
    root.mkdir()
    monkeypatch.setattr(settings, "sandbox_env", [])
    monkeypatch.setattr(agent, "sandbox_ports", list)
    monkeypatch.setattr(agent, "network_available", lambda: False)
    # Never load this machine's own scripts in these tests.
    monkeypatch.setattr(settings, "sandbox_profiles", [])
    monkeypatch.setattr(environment, "_overrides", dict)
    return agent.Workspace(root, network=True)


@needs_bwrap
async def test_commands_load_eda_profile_before_running_tools(command_workspace, monkeypatch):
    ws = command_workspace
    eda = ws.root / "EDA tool's setup"
    vcs_home, verdi_home = eda / "vcs", eda / "verdi"
    for tool, folder in (("vcs", vcs_home / "linux64/bin"), ("urg", vcs_home / "linux64/bin"),
                         ("verdi", verdi_home / "bin")):
        folder.mkdir(parents=True, exist_ok=True)
        executable = folder / tool
        executable.write_text(
            f"#!/bin/sh\nprintf '%s:%s:%s\\n' {tool} \"$VCS_HOME\" \"$SNPSLMD_LICENSE_FILE\"\n"
        )
        executable.chmod(0o755)
    profile = eda / ".edarc"
    profile.write_text(
        f"export VCS_HOME={shlex.quote(str(vcs_home))}\n"
        f"export VERDI_HOME={shlex.quote(str(verdi_home))}\n"
        "PATH=\"$PATH:$VCS_HOME/linux64/bin:$VERDI_HOME/bin\"\n"
        "export SNPSLMD_LICENSE_FILE=27000@license.test\n"
        "export LM_LICENSE_FILE=\"$SNPSLMD_LICENSE_FILE\"\n"
    )
    monkeypatch.setattr(settings, "sandbox_profiles", [str(profile), str(eda / "absent.sh")])
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-reach-the-command")
    output = await ws.call("Bash", {"command": (
        "vcs && urg && verdi && echo license=$LM_LICENSE_FILE; "
        "echo key=${OPENAI_API_KEY:-none}"
    )})
    assert not output.startswith(("Exit code", "Error"))
    for tool in ("vcs", "urg", "verdi"):
        assert f"{tool}:{vcs_home}:27000@license.test\n" in output
    assert "license=27000@license.test\n" in output
    assert output.endswith("key=none")


@needs_bwrap
@pytest.mark.parametrize("exit_code", [0, 7])
async def test_commands_return_stdout_and_stderr_without_an_eda_profile(command_workspace, exit_code):
    output = await command_workspace.call("Bash", {"command": (
        "echo stdout-visible; printf '%s\\n' 'literal \"$HOME\"'; "
        f"echo stderr-visible >&2; exit {exit_code}"
    )})
    lines = 'stdout-visible\nliteral "$HOME"\nstderr-visible'
    assert output == (f"Exit code {exit_code}\n{lines}" if exit_code else lines)


@needs_bwrap
async def test_command_timeout_keeps_stdout_and_stderr(command_workspace):
    output = await command_workspace.call("Bash", {
        "command": "echo before-timeout; echo error-before-timeout >&2; sleep 5",
        "timeout": 1000,
    })
    assert output.startswith("Command timed out after 1s")
    assert "before-timeout" in output.splitlines()
    assert "error-before-timeout" in output.splitlines()


@needs_bwrap
async def test_bash_keeps_tmp_and_its_directory_through_the_attempt(command_workspace):
    """Like Claude Code's Bash: /tmp and the working directory carry over between commands,
    shell variables do not, and nothing may be written outside the workspace and /tmp."""
    ws = command_workspace
    output = await ws.call("Bash", {"command": (
        "test \"$TMPDIR\" = /tmp && mkdir -p /tmp/sync_chk sub && "
        "printf '%s\\n' 'module sync_tb; endmodule' > /tmp/sync_chk/sync_tb.sv && "
        f"printf '%s\\n' absolute > {shlex.quote(str(ws.root / 'absolute.txt'))} && export SP_VAR=set && cd sub"
    )})
    assert output == "(the command printed no output)"
    assert (ws.root / "absolute.txt").read_text() == "absolute\n"
    output = await ws.call("Bash", {"command": (
        "cat /tmp/sync_chk/sync_tb.sv; pwd; echo var=${SP_VAR:-unset}; "
        "touch /usr/sp-command-write-test 2>/dev/null && echo escaped || echo outside-blocked"
    )})
    assert output == f"module sync_tb; endmodule\n{ws.root / 'sub'}\nvar=unset\noutside-blocked"
    # Leaving the working directory, as into a scratch folder, does not carry over: like Claude
    # Code, the next command starts in the working directory again, and the model is told.
    output = await ws.call("Bash", {"command": "cd /tmp/sync_chk && pwd"})
    assert output == f"/tmp/sync_chk\nShell cwd was reset to {ws.root}"
    assert await ws.call("Bash", {"command": "pwd"}) == str(ws.root)
    tmp = ws.tmp()
    ws.close()
    assert not Path(tmp).exists()
    instructions = ws.instructions()
    assert "/tmp for this whole attempt" in instructions and "working directory between calls" in instructions
    assert "Files outside it are read-only" in instructions


@needs_bwrap
@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
async def test_command_output_reaches_the_next_model_turn(command_workspace, monkeypatch, provider):
    command = {"command": "echo model-stdout; echo model-stderr >&2; exit 3"}
    if provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        replies = [
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "Bash", "arguments": json.dumps(command)}},
            ]}}]},
            {"choices": [{"message": {"content": "received"}}]},
        ]
    elif provider == "anthropic":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
        replies = [
            {"content": [{"type": "tool_use", "id": "c1", "name": "Bash", "input": command}],
             "usage": {"input_tokens": 1, "output_tokens": 1}},
            {"content": [{"type": "text", "text": "received"}],
             "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    else:
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("GOOGLE_API_KEY", "test")
        replies = [
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "Bash", "args": command}},
            ]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "received"}]}}]},
        ]
    sent = []
    registry = registry_with(scripted(replies, sent))
    try:
        result = await registry.complete(f"{provider}:test-model", "Run the command", command_workspace.root,
                                         act=True, network=True)
    finally:
        await registry.client.aclose()
    assert result.text == "received"
    expected = "Exit code 3\nmodel-stdout\nmodel-stderr"
    if provider == "openai":
        assert sent[1]["messages"][-1]["content"] == expected
    elif provider == "anthropic":
        assert sent[1]["messages"][-1]["content"][0]["content"] == expected
    else:
        assert sent[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"] == expected


async def test_file_tools_stay_inside_the_working_directory(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (root / "link").symlink_to(tmp_path / "secret.txt")
    ws = agent.Workspace(root)

    assert await ws.call("Write", {"file_path": "src/app.js", "content": "let a = 1;\n"}) == "Created src/app.js (1 lines)"
    assert await ws.call("Edit", {"file_path": "src/app.js", "old_string": "1", "new_string": "2"}) == (
        "Edited src/app.js (1 replacement):\n     1\tlet a = 2;")
    assert await ws.call("Read", {"file_path": "src/app.js"}) == "     1\tlet a = 2;"
    assert await ws.call("Read", {"file_path": str(root / "src/app.js")}) == "     1\tlet a = 2;"
    assert (await ws.call("LS", {})).startswith("./\nsrc/\n")
    for name, args in [
        ("Read", {"file_path": "../secret.txt"}), ("Read", {"file_path": "/etc/passwd"}),
        ("Read", {"file_path": "link"}), ("Write", {"file_path": "../escape.txt", "content": "x"}),
    ]:
        assert "outside the working directory" in await ws.call(name, args)
    assert not (tmp_path / "escape.txt").exists()
    assert "was not found" in await ws.call("Edit", {"file_path": "src/app.js", "old_string": "zzz", "new_string": "y"})
    assert "JSON object" in await ws.call("Write", {"__invalid__": "{oops"})
    assert "unknown tool" in await ws.call("rm_rf", {})


@needs_bwrap
@pytest.mark.parametrize("override_home", [False, True])
async def test_commands_run_sandboxed_without_keys_network_or_writes_outside(tmp_path, monkeypatch, override_home):
    import os
    import pwd

    if override_home:
        other_home = tmp_path / "temporary-home"
        other_home.mkdir()
        monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    root = Path(settings.workspace_root).resolve() / "_agent_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        ws = agent.Workspace(root)
        args = ws.sandbox_args("true")
        assert str(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()) in [
            args[index + 1] for index, value in enumerate(args[:-1]) if value == "--tmpfs"
        ]
        output = await ws.call("Bash", {"command": (
            "echo made > inside.txt; cat inside.txt; echo key=${OPENAI_API_KEY:-none}; "
            # Above the working directory is a throwaway in-memory home: writes never reach disk.
            "touch \"$(dirname \"$PWD\")/escape.txt\"; "
            "touch /usr/escape.txt 2>/dev/null && echo system-escaped || echo system-blocked; "
            "ls /home/*/.ssh 2>/dev/null | wc -l"
        )})
        assert not output.startswith(("Exit code", "Error"))
        assert "made" in output and "key=none" in output and "system-blocked" in output
        assert output.rstrip().endswith("0")
        assert (root / "inside.txt").read_text() == "made\n"
        assert not (root.parent / "escape.txt").exists()
        assert "timed out" in await ws.call("Bash", {"command": "sleep 5", "timeout": 1000})
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)



@needs_bwrap
@pytest.mark.skipif(not __import__("shutil").which("socat"), reason="socat is not installed")
async def test_only_listed_host_ports_reach_the_sandbox(monkeypatch):
    import asyncio

    async def greet(reader, writer):
        writer.write(b"hello-from-host\n")
        await writer.drain()
        writer.close()

    listed = await asyncio.start_server(greet, "127.0.0.1", 0)
    unlisted = await asyncio.start_server(greet, "127.0.0.1", 0)
    port, other = (server.sockets[0].getsockname()[1] for server in (listed, unlisted))
    monkeypatch.setattr(settings, "sandbox_host_ports", [port])
    monkeypatch.setattr(settings, "sandbox_env", ["SP_TOOL_HOME"])
    monkeypatch.setenv("SP_TOOL_HOME", "/opt/tool")
    root = Path(settings.workspace_root).resolve() / "_agent_ports_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        output = await agent.Workspace(root).call("Bash", {"command": (
            f"timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}; cat <&3' || echo listed-failed; "
            f"timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{other}' 2>/dev/null && echo unlisted-open || echo unlisted-blocked; "
            "echo tool=$SP_TOOL_HOME"
        )})
        assert "hello-from-host" in output and "unlisted-blocked" in output and "tool=/opt/tool" in output
        assert f"local ports {port}" in agent.agent_instructions()
    finally:
        listed.close()
        unlisted.close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)



def fake_proc(root, processes, tcp6=()):
    """A /proc with ``processes``: (pid, name, parent pid, [listening ports][, uid]); ports in tcp6
    go to net/tcp6. The uid defaults to 1000."""
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = {"tcp": [header], "tcp6": [header]}
    for pid, name, parent, ports, *owner in processes:
        uid = owner[0] if owner else 1000
        folder = root / str(pid)
        (folder / "fd").mkdir(parents=True)
        (folder / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} 0 -1 4194560\n")
        (folder / "status").write_text(f"Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (folder / "fd" / "0").symlink_to("/dev/null")
        for fd, port in enumerate(ports, start=3):
            inode = str(pid * 100 + fd)
            (folder / "fd" / str(fd)).symlink_to(f"socket:[{inode}]")
            table, address = ("tcp6", "00000000000000000000000000000000") if port in tcp6 else ("tcp", "0100007F")
            rows[table].append(f"   0: {address}:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {uid}        0 {inode}\n")
            # A connection on the same port is not a listener.
            rows[table].append(f"   1: {address}:{port:04X} 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  {uid}        0 9{inode}\n")
    (root / "net").mkdir()
    for table, lines in rows.items():
        (root / "net" / table).write_text("".join(lines))
    return root


def test_vendor_daemon_ports_of_lmgrd_reach_the_sandbox(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, [
        (100, "lmgrd", 1, [26585]),
        (101, "snpslmd", 100, [41237]),
        (200, "python", 1, [8000]),
    ])
    monkeypatch.setattr(environment, "PROC", proc)
    monkeypatch.setattr(settings, "sandbox_host_ports", [26585])
    assert environment.host_ports() == [26585, 41237]
    # The vendor daemon restarts on another port: the next command follows it.
    (proc / "net" / "tcp").write_text((proc / "net" / "tcp").read_text().replace(f":{41237:04X} ", f":{43001:04X} "))
    assert environment.host_ports() == [26585, 43001]


def test_only_lmgrd_children_count(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, [
        (100, "lmgrd", 1, [26585]),
        (101, "snpslmd", 100, [41237]),
        (150, "snpslmd", 1, [41500]),
        (200, "python", 1, [8000]),
        (201, "worker", 200, [8001]),
    ])
    monkeypatch.setattr(environment, "PROC", proc)
    monkeypatch.setattr(settings, "sandbox_host_ports", [8000])
    assert environment.host_ports() == [8000]
    monkeypatch.setattr(settings, "sandbox_host_ports", [26585])
    assert environment.host_ports() == [26585, 41237]
    assert environment.license_daemon_ports([], proc) == set()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every fd")
def test_unreadable_daemon_fds_fall_back_to_listed_ports(tmp_path, monkeypatch, caplog):
    proc = fake_proc(tmp_path / "proc", [(300, "lmgrd", 1, [26585], 0), (301, "snpslmd", 300, [41237], 0)])
    monkeypatch.setattr(environment, "PROC", proc)
    monkeypatch.setattr(environment, "_probes", {})
    monkeypatch.setattr(settings, "sandbox_host_ports", [26585])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no strace or lmutil to probe with
    (proc / "300" / "fd").chmod(0)
    try:
        with caplog.at_level("WARNING", logger="super_partner.environment"):
            assert environment.host_ports() == [26585]
    finally:
        (proc / "300" / "fd").chmod(0o755)
    assert "VENDOR snpslmd PORT=" in caplog.text


def fake_tracer(folder, connects):
    """strace and lmutil in ``folder``: strace prints connect() lines to the ports in the file
    ``connects`` ("address port" per line) and counts its runs in ``runs``."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "connects").write_text(connects)
    strace = folder / "strace"
    strace.write_text(f"""#!/bin/sh
echo run >> {folder}/runs
while read address port; do
  case "$address" in
    *:*) echo "[pid 7] connect(3, {{sa_family=AF_INET6, sin6_port=htons($port), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, \\"$address\\", &sin6_addr), sin6_scope_id=0}}, 28) = 0" >&2 ;;
    *) echo "connect(3, {{sa_family=AF_INET, sin_port=htons($port), sin_addr=inet_addr(\\"$address\\")}}, 16) = -1 EINPROGRESS" >&2 ;;
  esac
done < {folder}/connects
""")
    lmutil = folder / "lmutil"
    lmutil.write_text("#!/bin/sh\n")
    for tool in (strace, lmutil):
        tool.chmod(0o755)
    return lambda: len((folder / "runs").read_text().split()) if (folder / "runs").exists() else 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every fd")
def test_root_lmgrd_vendor_daemons_are_found_by_tracing_lmstat(tmp_path, monkeypatch):
    processes = [
        (300, "lmgrd", 1, [26585], 0),
        (301, "snpslmd", 300, [48457], 0),
        (400, "sshd", 1, [2222], 0),       # root, but lmstat never connects to it
        (500, "uvicorn", 1, [8000]),       # Super Partner itself, another user
    ]
    proc = fake_proc(tmp_path / "proc", processes)
    for pid in (300, 301, 400):
        (proc / str(pid) / "fd").chmod(0)
    tools = tmp_path / "eda" / "bin"
    runs = fake_tracer(tools, "127.0.0.53 53\n127.0.0.1 26585\n127.0.0.1 48457\n127.0.0.1 8000\n10.0.0.9 2222\n")
    (proc / "300" / "cmdline").write_text(f"{tools}/lmgrd\0-c\0/opt/license.dat\0")
    monkeypatch.setattr(environment, "PROC", proc)
    monkeypatch.setattr(environment, "_probes", {})
    monkeypatch.setattr(settings, "sandbox_host_ports", [26585])
    # strace is on PATH; lmutil only next to lmgrd, found from its command line.
    path = tmp_path / "path"
    path.mkdir()
    (path / "strace").symlink_to(tools / "strace")
    monkeypatch.setenv("PATH", str(path))
    try:
        assert environment.host_ports() == [26585, 48457]
        assert environment.host_ports() == [26585, 48457]
        assert runs() == 1  # reused, not traced for every command

        # snpslmd restarts on another port: the old one stops listening, so it is traced again.
        tcp = proc / "net" / "tcp"
        tcp.write_text(tcp.read_text().replace(f":{48457:04X} ", f":{50111:04X} "))
        (tools / "connects").write_text("127.0.0.1 26585\n::1 50111\n")
        assert environment.host_ports() == [26585, 50111]
        assert runs() == 2

        # And at the latest after PROBE_TTL.
        monkeypatch.setattr(environment, "PROBE_TTL", 0)
        assert environment.host_ports() == [26585, 50111]
        assert runs() == 3
    finally:
        for pid in (300, 301, 400):
            (proc / str(pid) / "fd").chmod(0o755)


def test_tcp6_listeners_are_found(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, [(100, "lmgrd", 1, [26585]), (101, "snpslmd", 100, [41237])], tcp6={26585, 41237})
    monkeypatch.setattr(environment, "PROC", proc)
    monkeypatch.setattr(settings, "sandbox_host_ports", [26585])
    assert environment.host_ports() == [26585, 41237]


@needs_bwrap
@pytest.mark.skipif(not agent.network_available(), reason="pasta is not installed")
async def test_a_task_allowing_the_network_reaches_out_but_not_local_services(monkeypatch, network_enabled):
    import asyncio

    async def greet(reader, writer):
        writer.write(b"hello-from-proxy\n")
        await writer.drain()
        writer.close()

    proxy = await asyncio.start_server(greet, "127.0.0.1", 0)
    service = await asyncio.start_server(greet, "127.0.0.1", 0)
    port, other = (server.sockets[0].getsockname()[1] for server in (proxy, service))
    for name in agent.PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    root = Path(settings.workspace_root).resolve() / "_agent_network_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        ws = agent.Workspace(root, network=True)
        assert agent.proxy_ports() == [port] and "--unshare-net" not in ws.sandbox_args("true")
        output = await ws.call("Bash", {"command": (
            # A route out exists, the user's local proxy is forwarded, other local ports are not.
            "awk '$2 == \"00000000\" {f = 1} END {exit !f}' /proc/net/route && echo routed || echo no-route; "
            f"timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}; cat <&3' || echo proxy-failed; "
            f"timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{other}' 2>/dev/null && echo local-open || echo local-blocked; "
            "echo proxy=$HTTPS_PROXY"
        )})
        assert output.startswith("routed") and "hello-from-proxy" in output and "local-blocked" in output
        assert f"proxy=http://127.0.0.1:{port}" in output
        assert "allows the internet" in ws.instructions() and "no network" not in ws.instructions()
        # Without the task's permission there is no route out at all.
        output = await agent.Workspace(root).call("Bash", {"command": (
            "awk '$2 == \"00000000\" {f = 1} END {exit !f}' /proc/net/route && echo routed || echo no-route"
        )})
        assert "no-route" in output
    finally:
        proxy.close()
        service.close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_cli_sandboxes_open_to_the_internet_only_for_a_task_allowing_it():
    from super_partner import cli_channels

    claude = list(cli_channels.CLI_SPECS["cli:claude"].invoke_args)
    opened = cli_channels._with_network("cli:claude", claude)
    settings_arg = json.loads(opened[opened.index("--settings") + 1])
    assert settings_arg["sandbox"]["network"]["allowedDomains"] == ["*"]
    assert "127.0.0.1" in settings_arg["sandbox"]["network"]["deniedDomains"]
    assert settings_arg["sandbox"]["allowUnsandboxedCommands"] is False
    codex = cli_channels._with_network("cli:codex", list(cli_channels.CLI_SPECS["cli:codex"].invoke_args))
    assert codex[:3] == ["exec", "-c", "sandbox_workspace_write.network_access=true"] and "workspace-write" in codex


@needs_bwrap
async def test_tools_from_environment_repairs_reach_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    tools = environment.prepare()
    (tools / "bin" / "mytool").write_text("#!/bin/sh\necho tool-ran\n")
    (tools / "bin" / "mytool").chmod(0o755)
    (tools / "python" / "mymod.py").write_text("print('module-ran')\n")
    (tools / "sandbox.json").write_text(json.dumps({"env": {"EDA_HOME": "/opt/eda", "PATH": "/evil", "bad name": "x"}}))
    root = Path(settings.workspace_root).resolve() / "_agent_tools_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        output = await agent.Workspace(root).call("Bash", {"command": (
            "mytool; python3 -c 'import mymod'; echo eda=$EDA_HOME; "
            f"touch {tools}/bin/x 2>/dev/null && echo tools-writable || echo tools-readonly"
        )})
        assert "tool-ran" in output and "module-ran" in output and "eda=/opt/eda" in output
        assert "tools-readonly" in output
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


@needs_bwrap
async def test_host_mode_commands_leave_the_sandbox_but_cannot_use_sudo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    root = tmp_path / "work"
    root.mkdir()
    output = await agent.Workspace(root, host=True).call("Bash", {"command": (
        f"echo made > {tmp_path}/outside.txt; echo key=${{OPENAI_API_KEY:-none}}; "
        "sudo -n true 2>/dev/null && echo became-root || echo sudo-blocked"
    )})
    assert "key=none" in output and "sudo-blocked" in output
    assert (tmp_path / "outside.txt").read_text() == "made\n"
    assert "without root" in agent.Workspace(root, host=True).instructions()


def registry_with(respond) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return registry


def scripted(replies: list[dict], sent: list[dict]):
    replies = iter(replies)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))
    return respond


async def test_openai_compatible_models_call_tools_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    sent = []
    registry = registry_with(scripted([
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "Write", "arguments": json.dumps({"file_path": "hello.txt", "content": "hi"})}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"content": "Wrote hello.txt"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 3}},
    ], sent))
    try:
        result = await registry.complete("openai:gpt-x", "Create hello.txt", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert (tmp_path / "hello.txt").read_text() == "hi"
    assert (result.text, result.input_tokens, result.output_tokens) == ("Wrote hello.txt", 30, 8)
    assert {tool["function"]["name"] for tool in sent[0]["tools"]} >= {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"}
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Created hello.txt (1 lines)"}


async def test_anthropic_and_gemini_models_call_tools_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    (tmp_path / "notes.md").write_text("remember this")
    sent = []
    registry = registry_with(scripted([
        {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "notes.md"}}],
         "usage": {"input_tokens": 5, "output_tokens": 2}},
        {"content": [{"type": "text", "text": "It says remember this"}], "usage": {"input_tokens": 7, "output_tokens": 4}},
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "LS", "args": {}}}]}}],
         "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Found notes.md"}]}}],
         "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}},
    ], sent))
    try:
        claude = await registry.complete("anthropic:claude-x", "Read the notes", tmp_path, act=True)
        gemini = await registry.complete("google:gemini-x", "List files", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert claude.text == "It says remember this" and (claude.input_tokens, claude.output_tokens) == (12, 6)
    assert sent[1]["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "     1\tremember this"}
    assert sent[0]["tools"][0]["input_schema"]["type"] == "object"
    assert gemini.text == "Found notes.md"
    assert sent[3]["contents"][-1]["parts"][0]["functionResponse"]["name"] == "LS"
    assert "notes.md" in sent[3]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]


async def test_tool_loops_stop_at_the_budget_and_the_turn_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(settings, "api_agent_max_turns", 3)
    looping = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": "LS", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
    registry = registry_with(lambda request: httpx.Response(200, json=looping))
    try:
        with pytest.raises(ProviderError, match="3 轮"):
            await registry.complete("openai:gpt-x", "loop", tmp_path, act=True)
        with pytest.raises(ProviderError, match="预算"):
            await registry.complete("openai:gpt-x", "loop", tmp_path, act=True, spend_ok=lambda *_: False)
        # Without act it is a single plain call, with no tools offered.
        plain = await registry.complete("openai:gpt-x", "loop", tmp_path)
    finally:
        await registry.client.aclose()
    assert plain.input_tokens == 100


async def test_an_api_model_does_project_work_through_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    project = tmp_path / "project"
    project.mkdir()
    registry = registry_with(scripted([
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "Write", "arguments": json.dumps({"file_path": "feature.py", "content": "print(1)\n"})}}]}}],
         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
        {"choices": [{"message": {"content": "Added feature.py"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 50}},
    ], []))
    gpt = ModelProfile(id="openai:gpt-x", provider="openai", display_name="GPT X", context_window=200_000,
                       input_per_million=1, output_per_million=2, scores=CapabilityScore(coding=90))
    node = TaskNode(id="n", title="Add feature", instructions="add it", capability="coding",
                    model_id=gpt.id, needs_workspace=True)
    database = Database(tmp_path / "test.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1,
                                   project_dir=str(project), plan=TaskPlan(summary="s", nodes=[node])))
    try:
        await Executor(database, registry, {gpt.id: gpt}).execute("r")
    finally:
        await registry.client.aclose()
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and run.plan.nodes[0].status == NodeStatus.SUCCEEDED
    assert (project / "feature.py").read_text() == "print(1)\n"
    assert run.plan.nodes[0].changed_files == ["feature.py"]
    assert run.actual_cost_usd == pytest.approx((2000 * 1 + 150 * 2) / 1_000_000)


def test_a_cli_failure_reports_the_reason_from_its_json_output():
    from super_partner.cli_channels import _failure_detail

    usage = json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached"}).encode()
    assert _failure_detail(usage, b"") == "Claude AI usage limit reached"
    assert _failure_detail(b"plain words", b"") == "plain words"
    assert _failure_detail(b"ignored", b"bad flag\n") == "bad flag"
    assert _failure_detail(b"", b"") == "no output"


def tool_turn_response(text=None, call=True):
    message = {"content": text}
    if call:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "LS", "arguments": "{}"}}]
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if call else "stop"}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


async def run_turns(tmp_path, monkeypatch, max_turns, answer_at):
    """A model that lists the directory every turn until request ``answer_at``."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return tool_turn_response("进度：完成了一半", call=len(bodies) < answer_at)

    registry = registry_with(respond)
    log = []
    try:
        result = await registry.complete("deepseek:deepseek-chat", "do it", tmp_path, act=True,
                                         max_turns=max_turns, tool_log=log)
    finally:
        await registry.client.aclose()
    return result, bodies, log


async def test_out_of_turns_the_model_is_asked_for_a_progress_summary(tmp_path, monkeypatch):
    result, bodies, log = await run_turns(tmp_path, monkeypatch, max_turns=3, answer_at=4)
    assert (result.text, result.turns, result.turn_limit) == ("进度：完成了一半", 3, True)
    assert len(bodies) == 4 and [call["turn"] for call in log] == [1, 2, 3]
    assert log[0] == {"turn": 1, "tool": "LS", "detail": ".", "ok": True, "output": "./\n(empty)"}
    assert "You have at most 3 turns" in json.dumps(bodies[0], ensure_ascii=False)
    assert "No turns of tool calls are left" in json.dumps(bodies[3]["messages"][-1], ensure_ascii=False)


async def test_five_turns_before_the_limit_the_model_is_told_to_wrap_up(tmp_path, monkeypatch):
    result, bodies, _log = await run_turns(tmp_path, monkeypatch, max_turns=8, answer_at=5)
    assert not result.turn_limit and result.turns == 5
    assert "Only 5 turns of tool calls are left" in json.dumps(bodies[3]["messages"][-1], ensure_ascii=False)
    assert "Only 5 turns" not in json.dumps(bodies[2]["messages"][-1], ensure_ascii=False)


async def test_a_model_that_keeps_calling_tools_after_the_limit_fails_with_the_turn_limit(tmp_path, monkeypatch):
    with pytest.raises(ProviderError, match="在 2 轮工具调用后仍未完成"):
        await run_turns(tmp_path, monkeypatch, max_turns=2, answer_at=99)



def test_profiles_come_from_the_setting_and_the_machines_record(monkeypatch):
    """Which scripts commands load is this machine's business: none by default, then the setting
    and the recorded sandbox.json, each once; the instructions name them only when there are some."""
    monkeypatch.setattr(settings, "sandbox_profiles", [])
    monkeypatch.setattr(environment, "_overrides", dict)
    assert environment.profiles() == [] and environment.shell_setup() == ""
    assert "first loads" not in agent.agent_instructions()
    monkeypatch.setattr(settings, "sandbox_profiles", ["/opt/tools/setup.sh"])
    monkeypatch.setattr(environment, "_overrides", lambda: {"profiles": ["/eda/env.sh", "/opt/tools/setup.sh", 3]})
    assert environment.profiles() == ["/opt/tools/setup.sh", "/eda/env.sh"]
    assert "if [ -r /eda/env.sh ]; then . /eda/env.sh" in environment.shell_setup()
    assert "Each command first loads /opt/tools/setup.sh, /eda/env.sh" in agent.agent_instructions()
