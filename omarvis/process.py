"""Bounded child-process execution for every helper Omarvis runs.

Each child is started in its own session so the whole process group can be
signalled, its output is streamed through fixed producer-side caps instead of
being buffered wholesale, and every live child is tracked by a supervisor that
can terminate what is still running on shutdown, takeover, or overflow.
"""

from __future__ import annotations

import os
import re
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

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        argv: tuple[str, ...],
        *,
        keep_descendants: bool = False,
    ) -> None:
        self.process = process
        self.argv = argv
        self.started_at = time.monotonic()
        self._lock = threading.Lock()
        self.terminated = False
        self.reaped = False
        # A helper such as wl-copy legitimately leaves a serving child behind;
        # reaping then must not sweep the group. Explicit termination still does.
        self.keep_descendants = keep_descendants

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
            if not self.terminated and not self.keep_descendants and self.live_members():
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


# --------------------------------------------------------------- Environment
#
# Children never inherit the daemon's environment. Every helper gets a fixed
# allowlist of session variables plus a short per-program list, so loader and
# interpreter startup variables (LD_*, PYTHON*, BASH_ENV, NODE_OPTIONS, ...),
# unrelated credentials, and the daemon's own switches never reach a helper.

ENV_VALUE_LIMIT = 8192
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

BASE_ENVIRONMENT: frozenset[str] = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TMPDIR",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_SESSION_TYPE",
        "XDG_SESSION_ID",
        "XDG_CURRENT_DESKTOP",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XAUTHORITY",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "DBUS_SESSION_BUS_ADDRESS",
    }
)
BASE_ENVIRONMENT_PREFIXES: tuple[str, ...] = ("LC_",)

# Per-program additions, keyed by the helper's basename. A trailing
# underscore denotes a prefix.
PROGRAM_ENVIRONMENT: dict[str, tuple[str, ...]] = {
    "omarchy": ("OMARCHY_",),
    "omarchy-shell": ("OMARCHY_",),
    "herdr": ("HERDR_",),
    "agent-browser": (
        "AGENT_BROWSER_",
        "OZONE_PLATFORM",
        "ELECTRON_OZONE_PLATFORM_HINT",
        "GDK_BACKEND",
        "GDK_SCALE",
        "QT_QPA_PLATFORM",
        "XCURSOR_SIZE",
        "XCURSOR_THEME",
        "HYPRCURSOR_SIZE",
        "HYPRCURSOR_THEME",
    ),
}


def _allowed_name(name: str, allowed: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    return name in allowed or any(name.startswith(prefix) for prefix in prefixes)


def _clean_value(name: str, value: str) -> str | None:
    if not _ENV_NAME.match(name) or "\x00" in value or len(value) > ENV_VALUE_LIMIT:
        return None
    return value


def _sanitized_path(value: str) -> str:
    """Keep only absolute PATH entries whose directory chain is trusted."""
    entries = [
        entry
        for entry in value.split(os.pathsep)
        if entry and os.path.isabs(entry) and _trusted_directory(entry)
    ]
    return os.pathsep.join(entries)


def child_environment(
    programs: Sequence[str],
    *,
    extra: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the minimal environment for a helper known under ``programs``.

    ``programs`` lists the names the helper is known by (the requested argv[0]
    and the resolved basename), so per-program additions apply whichever way
    it was addressed. ``extra`` values are set by Omarvis itself for this one
    command and are validated like everything else.
    """
    parent = os.environ if source is None else source
    allowed = set(BASE_ENVIRONMENT)
    prefixes = list(BASE_ENVIRONMENT_PREFIXES)
    for program in programs:
        for item in PROGRAM_ENVIRONMENT.get(os.path.basename(program), ()):
            if item.endswith("_"):
                prefixes.append(item)
            else:
                allowed.add(item)
    frozen_allowed = frozenset(allowed)
    frozen_prefixes = tuple(prefixes)
    env: dict[str, str] = {}
    for name, value in parent.items():
        if not _allowed_name(name, frozen_allowed, frozen_prefixes):
            continue
        cleaned = _clean_value(name, value)
        if cleaned is not None:
            env[name] = cleaned
    path = _sanitized_path(parent.get("PATH", ""))
    if path:
        env["PATH"] = path
    for name, value in (extra or {}).items():
        cleaned = _clean_value(name, str(value))
        if cleaned is None:
            raise ValueError(f"invalid environment entry {name!r}")
        env[name] = cleaned
    return env


# ------------------------------------------------------------ Bound execution
#
# The executable is opened once, validated by fstat on that descriptor, and
# executed through the descriptor (``/proc/self/fd/N``), so the file that
# runs is exactly the file that was validated no matter what happens to the
# pathname in between. Nothing runs in the child between fork and exec except
# CPython's async-signal-safe C launcher: ``preexec_fn`` is never used, which
# keeps spawning safe from every other thread in the daemon.

SHEBANG_LIMIT = 256
MAX_INPUT_BYTES = 1024 * 1024


def _open_trusted_executable(path: str) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ExecutableError(f"cannot open {path!r}: {error}") from error
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISREG(info.st_mode)
            or not _trusted_owner(info)
            or mode & 0o022
            or not mode & 0o111
        ):
            raise ExecutableError(f"{path!r} is not a trusted executable")
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass(frozen=True)
class BoundExecutable:
    """A validated executable held open, plus the argv that runs it."""

    argv: tuple[str, ...]
    fd: int
    path: str
    interpreter: str | None = None

    @property
    def executable(self) -> str:
        return f"/proc/self/fd/{self.fd}"

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def bind_executable(argv: Sequence[str]) -> BoundExecutable:
    """Resolve ``argv[0]`` and hold the exact file (or interpreter) to run.

    ELF programs run straight from their descriptor. For ``#!`` scripts the
    interpreter is what runs from a descriptor, with the script's validated
    absolute path passed as its argument, exactly as the kernel would (bash
    scripts such as ``omarchy`` rely on that path to find their own files).
    """
    if not argv:
        raise ExecutableError("empty command")
    path = resolve_executable(argv[0])
    fd = _open_trusted_executable(path)
    try:
        head = os.pread(fd, SHEBANG_LIMIT, 0)
    except OSError as error:
        os.close(fd)
        raise ExecutableError(f"cannot read {path!r}: {error}") from error
    if not head.startswith(b"#!"):
        return BoundExecutable((argv[0], *argv[1:]), fd, path)
    os.close(fd)
    line, newline, _rest = head[2:].partition(b"\n")
    if not newline:
        raise ExecutableError(f"{path!r} has an unreadable interpreter line")
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ExecutableError(f"{path!r} has an unreadable interpreter line") from error
    parts = text.split(None, 1)
    if not parts or not parts[0].startswith("/"):
        raise ExecutableError(f"{path!r} names no absolute interpreter")
    interpreter = resolve_executable(parts[0])
    ifd = _open_trusted_executable(interpreter)
    if os.pread(ifd, 2, 0) == b"#!":
        os.close(ifd)
        raise ExecutableError(f"{path!r} chains interpreters")
    argument = (parts[1].strip(),) if len(parts) > 1 and parts[1].strip() else ()
    return BoundExecutable(
        (parts[0], *argument, path, *argv[1:]), ifd, path, interpreter=interpreter
    )


def _feed_stdin(fd: int, data: bytes) -> None:
    view = memoryview(data)
    try:
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            view = view[written:]
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def execute_process(
    argv: Sequence[str],
    *,
    timeout: float,
    kill_on_timeout: bool,
    stdout_limit: int,
    extra_env: Mapping[str, str] | None = None,
    supervisor: ProcessSupervisor | None = None,
    stdin: int | None = subprocess.DEVNULL,
    input: bytes | None = None,
    keep_descendants: bool = False,
    capture_output: bool = True,
) -> ExecutionResult:
    """Run ``argv`` in its own process group with streamed, capped output.

    Synchronous callers (``kill_on_timeout=True``) get the whole group killed on
    timeout or output overflow. Detached callers get ``started=True`` once the
    timeout passes; the child keeps running, its output is drained and
    discarded from then on, and the supervisor can still terminate it later.
    ``input`` is written to the child's stdin from a helper thread so secrets
    and transcripts never travel in argv. With ``capture_output=False`` both
    output streams go to /dev/null and only the leader's exit is awaited,
    which is what a helper that forks a long-lived server (wl-copy) needs.
    """
    registry = supervisor or DEFAULT_SUPERVISOR
    argv = list(argv)
    if input is not None and len(input) > MAX_INPUT_BYTES:
        raise ValueError("stdin input exceeds the bounded size")
    bound = bind_executable(argv)
    env = child_environment((argv[0], bound.path), extra=extra_env)
    feed_fd: int | None = None
    if input is not None:
        stdin, feed_fd = os.pipe()
        os.set_inheritable(feed_fd, False)
    try:
        process = subprocess.Popen(
            list(bound.argv),
            executable=bound.executable,
            stdin=stdin,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            pass_fds=(bound.fd,),
            env=env,
        )
    except BaseException:
        if feed_fd is not None:
            os.close(feed_fd)
            os.close(stdin)  # type: ignore[arg-type]
        raise
    finally:
        bound.close()
    if feed_fd is not None:
        os.close(stdin)  # type: ignore[arg-type]
        threading.Thread(target=_feed_stdin, args=(feed_fd, input or b""), daemon=True).start()
    tracked = TrackedProcess(process, tuple(argv), keep_descendants=keep_descendants)
    registry.register(tracked)
    readers: tuple[_BoundedReader, ...] = ()
    if capture_output:
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
        stdout = readers[0].text()[:stdout_limit] if readers else ""
        stderr = readers[1].text()[:200] if readers else ""
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
