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
