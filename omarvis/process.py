"""Bounded child-process execution for every helper Omarvis runs.

Each child is started in its own session so the whole process group can be
signalled, its output is streamed through fixed producer-side caps instead of
being buffered wholesale, and every live child is tracked by a supervisor that
can terminate what is still running on shutdown, takeover, or overflow.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Bytes of stdout kept for the caller are bounded by the caller's limit; a
# child that keeps producing past the hard cap is killed rather than drained
# forever. Kept memory never exceeds the caller's limit plus one read chunk.
MIN_HARD_CAP = 1024 * 1024
STDERR_KEEP = 2000
READ_CHUNK = 65536
TERMINATE_GRACE_SECONDS = 1.0
MAX_TRACKED_DETACHED = 8


MEMBER_POLL_SECONDS = 0.05
DETACHED_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    started: bool = False
    overflowed: bool = False
    # More output was produced than the caller kept. Consumers that parse the
    # output as a whole (JSON) must treat a truncated result as invalid.
    truncated: bool = False


class _BoundedReader(threading.Thread):
    """Drain one pipe, keeping at most ``keep`` bytes and never blocking the child."""

    def __init__(self, fd: int, *, keep: int, hard_cap: int, on_overflow) -> None:
        super().__init__(daemon=True)
        self.fd = fd
        self.keep = keep
        self.hard_cap = hard_cap
        self.on_overflow = on_overflow
        self.buffer = bytearray()
        self.total = 0
        self.overflowed = False
        self._discard = threading.Event()

    def discard(self) -> None:
        """Stop retaining output; only drain from now on."""
        self._discard.set()
        self.buffer = bytearray()

    def run(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self.fd, READ_CHUNK)
                except InterruptedError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self.total += len(chunk)
                if self._discard.is_set():
                    continue
                room = self.keep - len(self.buffer)
                if room > 0:
                    self.buffer += chunk[:room]
                if self.total > self.hard_cap and not self.overflowed:
                    self.overflowed = True
                    self.on_overflow()
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass

    def text(self) -> str:
        return bytes(self.buffer).decode("utf-8", "replace")

    @property
    def truncated(self) -> bool:
        return self.total > self.keep


def _leader_exited(pid: int) -> bool:
    """Observe the leader's exit without reaping it, so its pid stays pinned."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _wait_leader_exit(pid: int) -> None:
    """Block until the leader exits, still without reaping it."""
    while True:
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            return
        except InterruptedError:
            continue
        except ChildProcessError:
            return


def live_group_members(pgid: int, *, exclude: int) -> list[int]:
    """Pids of live (non-zombie) processes in ``pgid`` other than ``exclude``."""
    members: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == exclude:
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat_line = handle.read(4096)
        except OSError:
            continue
        # "pid (comm) state ppid pgrp ..."; comm may contain spaces/parens.
        close = stat_line.rfind(b")")
        if close < 0:
            continue
        fields = stat_line[close + 2 :].split()
        if len(fields) < 3:
            continue
        state, group = fields[0], fields[2]
        if state == b"Z":
            continue
        try:
            if int(group) == pgid:
                members.append(pid)
        except ValueError:
            continue
    return members


class TrackedProcess:
    """One child and its process group.

    The leader is never reaped until group cleanup has finished: an unreaped
    leader (even as a zombie) pins its pid and therefore the group id, so no
    signal in the TERM-to-KILL sequence can land on a reused id.
    """

    def __init__(self, process: subprocess.Popen[bytes], argv: tuple[str, ...]) -> None:
        self.process = process
        self.argv = argv
        self.started_at = time.monotonic()
        self._lock = threading.Lock()
        self.terminated = False
        self.reaped = False

    @property
    def pid(self) -> int:
        return self.process.pid

    def _signal_group(self, signum: int) -> bool:
        if self.reaped:
            return False
        try:
            os.killpg(self.process.pid, signum)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def leader_exited(self) -> bool:
        return self.reaped or _leader_exited(self.process.pid)

    def live_members(self) -> list[int]:
        if self.reaped:
            return []
        return live_group_members(self.process.pid, exclude=self.process.pid)

    def group_alive(self) -> bool:
        return not self.leader_exited() or bool(self.live_members())

    def _wait_group(self, grace: float) -> bool:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not self.group_alive():
                return True
            time.sleep(MEMBER_POLL_SECONDS)
        return not self.group_alive()

    def terminate(self, grace: float = TERMINATE_GRACE_SECONDS) -> None:
        """SIGTERM the whole group, SIGKILL what remains, then reap the leader."""
        with self._lock:
            if self.terminated or self.reaped:
                return
            self.terminated = True
        if self._signal_group(signal.SIGTERM) and not self._wait_group(grace):
            self._signal_group(signal.SIGKILL)
            self._wait_group(grace)
        self.reap()

    def reap(self) -> None:
        """Reap the leader once it has exited. Cleans the group first if needed."""
        with self._lock:
            if self.reaped:
                return
            if not self.terminated and self.live_members():
                # The leader is gone but descendants stayed in the group.
                self.terminated = True
                escalate = True
            else:
                escalate = False
        if escalate:
            self._signal_group(signal.SIGTERM)
            if not self._wait_group(TERMINATE_GRACE_SECONDS):
                self._signal_group(signal.SIGKILL)
                self._wait_group(TERMINATE_GRACE_SECONDS)
        with self._lock:
            if self.reaped:
                return
            try:
                self.process.wait()
            except OSError:
                pass
            self.reaped = True


class ProcessSupervisor:
    """Registry of live children so nothing outlives the session that spawned it."""

    def __init__(self, *, max_detached: int = MAX_TRACKED_DETACHED) -> None:
        self._lock = threading.Lock()
        self._live: dict[int, TrackedProcess] = {}
        self._detached: list[TrackedProcess] = []
        self.max_detached = max_detached

    def register(self, tracked: TrackedProcess) -> None:
        with self._lock:
            self._live[tracked.pid] = tracked

    def unregister(self, tracked: TrackedProcess) -> None:
        with self._lock:
            self._live.pop(tracked.pid, None)
            if tracked in self._detached:
                self._detached.remove(tracked)

    def detach(self, tracked: TrackedProcess) -> list[TrackedProcess]:
        """Keep tracking a long-running child; supersede the oldest past the cap."""
        superseded: list[TrackedProcess] = []
        with self._lock:
            self._detached.append(tracked)
            while len(self._detached) > self.max_detached:
                superseded.append(self._detached.pop(0))
        for old in superseded:
            old.terminate()
        return superseded

    def live(self) -> tuple[TrackedProcess, ...]:
        with self._lock:
            return tuple(self._live.values())

    def terminate_all(self, grace: float = TERMINATE_GRACE_SECONDS) -> int:
        """Terminate every tracked process group. Returns how many were signalled."""
        with self._lock:
            tracked = tuple(self._live.values())
        for item in tracked:
            item.terminate(grace)
        return len(tracked)


DEFAULT_SUPERVISOR = ProcessSupervisor()


class ExecutableError(FileNotFoundError):
    """No trusted executable was found for the requested program."""


def _trusted_owner(info: os.stat_result) -> bool:
    return info.st_uid in (0, os.geteuid())


def _trusted_directory(path: str) -> bool:
    """Every component from / down must be root- or self-owned and not shared-writable."""
    parts = os.path.normpath(path).split(os.sep)
    current = os.sep
    for part in parts:
        if part:
            current = os.path.join(current, part)
        try:
            info = os.stat(current)
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
        if not _trusted_owner(info) or stat.S_IMODE(info.st_mode) & 0o022:
            return False
    return True


def _trusted_file(path: str) -> os.stat_result | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    if not _trusted_owner(info) or stat.S_IMODE(info.st_mode) & 0o022:
        return None
    if not os.access(path, os.X_OK):
        return None
    return info


def resolve_executable(program: str, *, search_path: str | None = None) -> str:
    """Return a validated absolute path for ``program``.

    A bare name is searched along PATH, but only entries whose entire directory
    chain is root- or self-owned and not group/other writable are considered,
    so a mutable PATH entry cannot substitute a program. The chosen file must
    satisfy the same ownership and mode rules after following any symlink, and
    the symlink target's directory chain is validated as well.
    """
    candidates: list[str]
    if os.sep in program:
        candidates = [os.path.abspath(program)]
    else:
        entries = (search_path if search_path is not None else os.environ.get("PATH", "")).split(
            os.pathsep
        )
        candidates = [
            os.path.join(entry, program) for entry in entries if entry and os.path.isabs(entry)
        ]
    for candidate in candidates:
        if not _trusted_directory(os.path.dirname(candidate)):
            continue
        target = os.path.realpath(candidate)
        if target != candidate and not _trusted_directory(os.path.dirname(target)):
            continue
        if _trusted_file(target) is None:
            continue
        return target
    raise ExecutableError(f"no trusted executable for {program!r}")


def _hard_cap_for(stdout_limit: int) -> int:
    return max(MIN_HARD_CAP, 2 * stdout_limit)


def _join_with_deadline(readers: Sequence[_BoundedReader], deadline: float) -> bool:
    for reader in readers:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        reader.join(remaining)
        if reader.is_alive():
            return False
    return True


def execute_process(
    argv: Sequence[str],
    *,
    timeout: float,
    kill_on_timeout: bool,
    stdout_limit: int,
    env: Mapping[str, str] | None = None,
    supervisor: ProcessSupervisor | None = None,
    stdin: int | None = subprocess.DEVNULL,
) -> ExecutionResult:
    """Run ``argv`` in its own process group with streamed, capped output.

    Synchronous callers (``kill_on_timeout=True``) get the whole group killed on
    timeout or output overflow. Detached callers get ``started=True`` once the
    timeout passes; the child keeps running, its output is drained and
    discarded from then on, and the supervisor can still terminate it later.
    """
    registry = supervisor or DEFAULT_SUPERVISOR
    argv = list(argv)
    if not argv:
        raise ExecutableError("empty command")
    executable = resolve_executable(argv[0])
    expected = os.stat(executable)

    def bind_executable() -> None:
        # Runs in the child just before exec: refuse if the validated file was
        # swapped between resolution and execution.
        try:
            current = os.stat(executable)
        except OSError:
            os._exit(126)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            os._exit(126)

    process = subprocess.Popen(
        argv,
        executable=executable,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        preexec_fn=bind_executable,
        env=dict(env) if env is not None else None,
    )
    tracked = TrackedProcess(process, tuple(argv))
    registry.register(tracked)
    assert process.stdout is not None and process.stderr is not None
    stdout_fd = os.dup(process.stdout.fileno())
    stderr_fd = os.dup(process.stderr.fileno())
    process.stdout.close()
    process.stderr.close()
    hard_cap = _hard_cap_for(stdout_limit)
    readers = (
        _BoundedReader(
            stdout_fd, keep=stdout_limit, hard_cap=hard_cap, on_overflow=tracked.terminate
        ),
        _BoundedReader(
            stderr_fd, keep=STDERR_KEEP, hard_cap=hard_cap, on_overflow=tracked.terminate
        ),
    )
    for reader in readers:
        reader.start()

    def finish(*, timed_out: bool) -> ExecutionResult:
        # After a kill the group is gone, but a pipe end inherited by something
        # outside the group can keep a reader alive; bound that wait too and
        # leave such readers draining in the background.
        if not _join_with_deadline(readers, time.monotonic() + TERMINATE_GRACE_SECONDS):
            tracked.terminate()
        stdout = readers[0].text()[:stdout_limit]
        stderr = readers[1].text()[:200]
        for reader in readers:
            if reader.is_alive():
                reader.discard()
        # Reaping cleans up any descendants the leader left in its group.
        tracked.reap()
        registry.unregister(tracked)
        return ExecutionResult(
            process.returncode,
            stdout,
            stderr,
            timed_out=timed_out,
            overflowed=any(reader.overflowed for reader in readers),
            truncated=any(reader.truncated for reader in readers),
        )

    deadline = time.monotonic() + timeout
    drained = _join_with_deadline(readers, deadline)
    if drained:
        # Pipes closed; give the leader the rest of the window to exit, still
        # observing without reaping.
        while not tracked.leader_exited():
            if time.monotonic() >= deadline:
                drained = False
                break
            time.sleep(MEMBER_POLL_SECONDS)
    if drained:
        return finish(timed_out=False)
    if kill_on_timeout:
        tracked.terminate()
        return finish(timed_out=True)

    # Detached: the child is a launcher that is expected to keep running. Stop
    # retaining its output, keep draining so it never blocks or gets SIGPIPE,
    # and reap it whenever it finally exits.
    for reader in readers:
        reader.discard()
    registry.detach(tracked)

    def reap() -> None:
        for reader in readers:
            reader.join()
        _wait_leader_exit(process.pid)
        # Keep the exited leader unreaped while descendants remain in its
        # group, so the group id stays pinned until terminate() or they exit.
        while not tracked.terminated and not tracked.reaped and tracked.live_members():
            time.sleep(DETACHED_POLL_SECONDS)
        tracked.reap()
        registry.unregister(tracked)

    threading.Thread(target=reap, daemon=True).start()
    return ExecutionResult(None, started=True)
