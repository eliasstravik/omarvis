from __future__ import annotations

import subprocess
import time

from omarvis.process import (
    ProcessSupervisor,
    TrackedProcess,
    execute_process,
    live_group_members,
)


def _live_pids(pattern: str) -> list[str]:
    return subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False
    ).stdout.split()


def test_output_flood_is_killed_and_memory_stays_bounded() -> None:
    supervisor = ProcessSupervisor()
    result = execute_process(
        ["yes"], timeout=10.0, kill_on_timeout=True, stdout_limit=3000, supervisor=supervisor
    )

    assert result.overflowed
    assert not result.timed_out
    assert len(result.stdout) == 3000
    assert result.exit_code != 0
    assert supervisor.live() == ()


def test_stderr_flood_is_also_capped() -> None:
    result = execute_process(
        ["bash", "-c", "yes >&2"], timeout=10.0, kill_on_timeout=True, stdout_limit=100
    )

    assert result.overflowed
    assert len(result.stderr) == 200


def test_timeout_terminates_the_whole_process_group() -> None:
    marker = "sleep 31.5"
    supervisor = ProcessSupervisor()
    started = time.monotonic()
    result = execute_process(
        ["bash", "-c", f"{marker} & {marker}"],
        timeout=0.3,
        kill_on_timeout=True,
        stdout_limit=100,
        supervisor=supervisor,
    )

    assert result.timed_out
    assert time.monotonic() - started < 5.0
    assert supervisor.live() == ()
    assert _live_pids(f"^{marker}$") == []


def test_fast_leader_with_pipe_holding_child_does_not_hang() -> None:
    marker = "sleep 32.5"
    started = time.monotonic()
    result = execute_process(
        ["bash", "-c", f"({marker}) & echo started"],
        timeout=0.3,
        kill_on_timeout=True,
        stdout_limit=100,
    )

    assert result.stdout == "started\n"
    assert time.monotonic() - started < 5.0
    assert _live_pids(f"^{marker}$") == []


def test_successful_command_returns_capped_output() -> None:
    result = execute_process(
        ["printf", "abcdefghij"], timeout=5.0, kill_on_timeout=True, stdout_limit=4
    )

    assert (result.exit_code, result.stdout, result.stderr) == (0, "abcd", "")


def test_detached_launcher_reports_started_and_is_terminated_later() -> None:
    supervisor = ProcessSupervisor()
    result = execute_process(
        ["bash", "-c", "while true; do echo chatter; sleep 0.01; done"],
        timeout=0.2,
        kill_on_timeout=False,
        stdout_limit=40,
        supervisor=supervisor,
    )

    assert result.started
    assert result.exit_code is None
    time.sleep(0.3)
    assert len(supervisor.live()) == 1

    assert supervisor.terminate_all() == 1
    deadline = time.monotonic() + 5.0
    while supervisor.live() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert supervisor.live() == ()


def test_supervisor_supersedes_oldest_detached_process() -> None:
    supervisor = ProcessSupervisor(max_detached=2)
    for _ in range(3):
        execute_process(
            ["sleep", "33.5"],
            timeout=0.05,
            kill_on_timeout=False,
            stdout_limit=10,
            supervisor=supervisor,
        )
    deadline = time.monotonic() + 5.0
    while len(supervisor.live()) > 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert len(supervisor.live()) == 2
    supervisor.terminate_all()


def test_terminate_is_idempotent_for_exited_process() -> None:
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    tracked = TrackedProcess(process, ("true",))

    tracked.terminate()
    tracked.terminate()
    assert tracked.terminated


def test_output_past_the_kept_limit_is_flagged_truncated() -> None:
    result = execute_process(
        ["printf", "abcdefghij"], timeout=5.0, kill_on_timeout=True, stdout_limit=4
    )

    assert result.truncated
    assert not result.overflowed
    assert result.exit_code == 0


def test_descendant_left_in_group_after_leader_exit_is_cleaned_before_reaping() -> None:
    marker = "sleep 36.5"
    supervisor = ProcessSupervisor()
    result = execute_process(
        ["bash", "-c", f"(exec >/dev/null 2>&1; {marker}) & echo done; exit 0"],
        timeout=5.0,
        kill_on_timeout=True,
        stdout_limit=100,
        supervisor=supervisor,
    )

    assert result.exit_code == 0
    assert result.stdout == "done\n"
    assert supervisor.live() == ()
    assert _live_pids(f"^{marker}$") == []


def test_detached_leader_exit_keeps_group_pinned_until_terminated() -> None:
    marker = "sleep 37.5"
    supervisor = ProcessSupervisor()
    result = execute_process(
        ["bash", "-c", f"(exec >/dev/null 2>&1; {marker}) & sleep 0.5; exit 0"],
        timeout=0.1,
        kill_on_timeout=False,
        stdout_limit=100,
        supervisor=supervisor,
    )
    assert result.started
    time.sleep(1.0)

    (tracked,) = supervisor.live()
    assert tracked.leader_exited()
    assert not tracked.reaped, "leader must stay unreaped while a descendant pins the group"
    assert live_group_members(tracked.pid, exclude=tracked.pid)

    supervisor.terminate_all()
    deadline = time.monotonic() + 5.0
    while supervisor.live() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert supervisor.live() == ()
    assert _live_pids(f"^{marker}$") == []
    assert tracked.reaped


def test_resolve_executable_skips_shared_writable_path_entries(tmp_path) -> None:
    import pytest

    from omarvis.process import ExecutableError, _trusted_directory, resolve_executable

    shady = tmp_path / "shady"
    shady.mkdir()
    shady.chmod(0o777)
    fake = shady / "hyprctl"
    fake.write_text("#!/bin/sh\necho pwned\n")
    fake.chmod(0o755)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    trusted.chmod(0o755)
    real = trusted / "hyprctl"
    real.write_text("#!/bin/sh\necho ok\n")
    real.chmod(0o755)
    tmp_path.chmod(0o755)

    if not _trusted_directory(str(tmp_path)):
        # An ancestor such as /tmp is shared-writable, so the whole subtree is
        # untrusted and the resolver must refuse both candidates.
        with pytest.raises(ExecutableError):
            resolve_executable("hyprctl", search_path=f"{shady}:{trusted}")
        return
    assert resolve_executable("hyprctl", search_path=f"{shady}:{trusted}") == str(real)


def test_resolve_executable_accepts_system_binaries_and_rejects_missing() -> None:
    import os

    import pytest

    from omarvis.process import ExecutableError, resolve_executable

    resolved = resolve_executable("sh", search_path="/usr/bin:/bin")
    assert os.path.isabs(resolved) and os.access(resolved, os.X_OK)
    with pytest.raises(ExecutableError):
        resolve_executable("definitely-not-a-program-xyz", search_path="/usr/bin")


def test_execute_process_refuses_untrusted_absolute_path(tmp_path) -> None:
    import pytest

    from omarvis.process import ExecutableError

    from omarvis.process import _trusted_directory

    if _trusted_directory(str(tmp_path)):
        pytest.skip("needs a shared-writable ancestor such as /tmp")
    script = tmp_path / "tool"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    with pytest.raises(ExecutableError):
        execute_process([str(script)], timeout=2.0, kill_on_timeout=True, stdout_limit=10)


# ------------------------------------------------- Fork safety and binding


def test_spawning_never_runs_python_code_in_the_child() -> None:
    import inspect

    from omarvis import process

    source = inspect.getsource(process)
    # preexec_fn is the one Popen hook that runs Python between fork and
    # exec; in a multithreaded daemon it can inherit a held lock and
    # deadlock the child (and Popen itself) forever. It must never be used.
    assert "preexec_fn=" not in source


def test_concurrent_spawns_from_many_threads_never_deadlock() -> None:
    import json
    import logging
    import threading

    stop = threading.Event()
    churn_lock = threading.Lock()
    logger = logging.getLogger("omarvis.stress")
    errors: list[BaseException] = []
    results: list = []
    results_lock = threading.Lock()

    def churn() -> None:
        # Hold locks and touch the allocator, logging, and json machinery
        # continuously, the way the daemon's reader, web, and session
        # threads do while another thread spawns a helper.
        while not stop.is_set():
            with churn_lock:
                json.dumps({"n": list(range(50))})
                logger.debug("churn %s", time.monotonic())

    def forker() -> None:
        while not stop.is_set():
            subprocess.run(["true"], check=False)

    def worker() -> None:
        try:
            for _ in range(12):
                result = execute_process(
                    ["sh", "-c", "echo ok"], timeout=20.0, kill_on_timeout=True, stdout_limit=100
                )
                with results_lock:
                    results.append(result)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion
            errors.append(error)

    background = [threading.Thread(target=churn, daemon=True) for _ in range(3)]
    background.append(threading.Thread(target=forker, daemon=True))
    workers = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
    for thread in background + workers:
        thread.start()
    deadline = time.monotonic() + 90.0
    for thread in workers:
        thread.join(max(0.0, deadline - time.monotonic()))
    stop.set()
    for thread in background:
        thread.join(5.0)

    assert not any(thread.is_alive() for thread in workers), "a spawn deadlocked"
    assert errors == []
    assert len(results) == 96
    assert all(result.stdout == "ok\n" and result.exit_code == 0 for result in results)


def test_execution_is_bound_to_the_validated_file_not_the_pathname(tmp_path, monkeypatch) -> None:
    import os
    import shutil

    from omarvis import process

    program = tmp_path / "program"
    shutil.copy("/usr/bin/true", program)
    program.chmod(0o755)
    monkeypatch.setattr(process, "resolve_executable", lambda name, **_kwargs: str(program))
    real_popen = process.subprocess.Popen

    def swapping_popen(*args, **kwargs):
        # Between validation and exec the pathname is pointed at a different
        # program. The validated descriptor is what runs, so `true` must win.
        replacement = tmp_path / "replacement"
        shutil.copy("/usr/bin/false", replacement)
        replacement.chmod(0o755)
        os.replace(replacement, program)
        assert kwargs["executable"].startswith("/proc/self/fd/")
        assert "preexec_fn" not in kwargs
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process.subprocess, "Popen", swapping_popen)

    result = execute_process([str(program)], timeout=5.0, kill_on_timeout=True, stdout_limit=10)

    assert result.exit_code == 0


def test_scripts_run_through_a_bound_interpreter_with_their_real_path(tmp_path, monkeypatch) -> None:
    from omarvis import process

    script = tmp_path / "tool"
    script.write_text('#!/bin/bash\necho "$0" "$1" "$BASH_SOURCE"\n')
    script.chmod(0o755)
    real_resolve = process.resolve_executable
    monkeypatch.setattr(
        process,
        "resolve_executable",
        lambda name, **kwargs: str(script) if name == str(script) else real_resolve(name, **kwargs),
    )

    bound = process.bind_executable([str(script), "arg"])
    try:
        assert bound.interpreter is not None and bound.interpreter.endswith("bash")
        assert bound.argv == ("/bin/bash", str(script), "arg")
    finally:
        bound.close()
    result = execute_process([str(script), "arg"], timeout=5.0, kill_on_timeout=True, stdout_limit=300)

    # Scripts such as `omarchy` locate their own files via $BASH_SOURCE, so
    # the interpreter gets the validated absolute path, as the kernel would.
    assert result.stdout == f"{script} arg {script}\n"


def test_children_get_only_the_allowlisted_environment(monkeypatch) -> None:
    import pytest

    from omarvis.process import child_environment

    monkeypatch.setenv("OMARVIS_TEST_SECRET", "hunter2")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nonexistent")
    monkeypatch.setenv("PYTHONSTARTUP", "/nonexistent")
    monkeypatch.setenv("OMARCHY_PROBE", "yes")
    monkeypatch.setenv("HERDR_PROBE", "yes")
    monkeypatch.setenv("AGENT_BROWSER_PROBE", "yes")

    result = execute_process(["env"], timeout=5.0, kill_on_timeout=True, stdout_limit=64_000)
    names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}

    assert result.exit_code == 0
    assert "PATH" in names and "HOME" in names
    for leaked in ("OMARVIS_TEST_SECRET", "LD_LIBRARY_PATH", "PYTHONSTARTUP", "OMARCHY_PROBE", "HERDR_PROBE", "AGENT_BROWSER_PROBE"):
        assert leaked not in names

    env = child_environment(("omarchy",), extra={"OMARCHY_SCREENSHOT_DIR": "/cache"})
    assert env["OMARCHY_PROBE"] == "yes" and env["OMARCHY_SCREENSHOT_DIR"] == "/cache"
    assert "HERDR_PROBE" not in env and "OMARVIS_TEST_SECRET" not in env
    assert "HERDR_PROBE" in child_environment(("herdr", "/home/x/.local/bin/herdr"))
    assert "AGENT_BROWSER_PROBE" in child_environment(("/state/agent-browser/agent-browser",))
    # PATH keeps only absolute, trusted entries; relative and shared-writable ones go.
    path = child_environment(("x",), source={"PATH": "/usr/bin:relative:/tmp"})["PATH"]
    assert path.split(":")[0] == "/usr/bin" and "relative" not in path
    with pytest.raises(ValueError):
        child_environment(("x",), extra={"BAD NAME": "1"})
    with pytest.raises(ValueError):
        child_environment(("x",), extra={"X": "a\x00b"})


def test_input_reaches_the_child_over_stdin() -> None:
    result = execute_process(
        ["cat"], timeout=5.0, kill_on_timeout=True, stdout_limit=100, input=b"secret words"
    )

    assert (result.exit_code, result.stdout) == (0, "secret words")


def test_keep_descendants_leaves_a_forked_server_alive() -> None:
    marker = "sleep 38.5"
    result = execute_process(
        ["bash", "-c", f"(exec >/dev/null 2>&1; {marker}) & exit 0"],
        timeout=5.0,
        kill_on_timeout=True,
        stdout_limit=10,
        keep_descendants=True,
        capture_output=False,
    )
    try:
        assert result.exit_code == 0
        assert _live_pids(f"^{marker}$") != []
    finally:
        subprocess.run(["pkill", "-f", f"^{marker}$"], check=False)
