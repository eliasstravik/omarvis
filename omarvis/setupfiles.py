"""Setup-time operations for ``bin/omarvis-setup``.

The installer's shell script keeps the conversation with the user; every
operation that touches a file, a secret, or the network runs here, on the
system interpreter with the standard library only, so that:

* runtime files (config, key, profile, setup log) are created through the
  same descriptor-based, no-follow, owner/type/link/size-checked, atomic
  path as the daemon uses to read them (``privatefiles``);
* the ElevenLabs key never appears in any argv (it arrives on stdin and is
  sent as a request header from this process);
* every download is HTTPS-only, size-capped, deadline-bound, written to a
  private temporary file, and verified against a digest committed in this
  file before it is installed; no package-manager lifecycle script runs;
* the Hyprland bindings edit is transactional: backup, atomic replace,
  syntax and reload validation, automatic rollback;
* a journal records what setup changed so a failed or crashed run is undone
  on exit or on the next start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import ssl
import stat
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .daemon import API_KEY_PATH, CONFIG_DIR, CONFIG_PATH, DEFAULT_CONFIG
from .privatefiles import (
    MAX_CONFIG_BYTES,
    MAX_SECRET_BYTES,
    PrivateFileError,
    open_private_file,
    private_dir,
    read_private_file,
    read_private_path,
    write_private_file,
    write_private_path,
)
from .process import ExecutableError, execute_process

PLUGIN_ID = "io.github.eliasstravik.omarvis"
STATE_DIR = Path.home() / ".local" / "share" / "omarvis"
LOG_PATH = STATE_DIR / "setup.log"
JOURNAL_PATH = STATE_DIR / "setup.journal"
VENDOR_DIR = STATE_DIR / "vendor"
AGENT_BROWSER_DIR = STATE_DIR / "agent-browser"
AGENT_BROWSER_PATH = AGENT_BROWSER_DIR / "agent-browser"
PROFILE_PATH = CONFIG_DIR / "profile.md"
BINDINGS_PATH = Path.home() / ".config" / "hypr" / "bindings.lua"

LOG_LIMIT = 4 * 1024 * 1024
BINDINGS_LIMIT = 1024 * 1024
KEY_RESPONSE_LIMIT = 64 * 1024
NETWORK_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
USER_AGENT = "omarvis-setup"

KEY_CHECK_URL = "https://api.elevenlabs.io/v1/convai/agents?page_size=1"
KEY_SCOPE_HINT = (
    "The key works but lacks Agents Platform / Conversational AI access. Edit "
    "the key's permissions in the ElevenLabs dashboard (or create an "
    "unrestricted key) and paste it again."
)


class SetupError(RuntimeError):
    """A setup step failed; the message is meant for the terminal."""


# ------------------------------------------------------------------ Artifacts


@dataclass(frozen=True)
class Artifact:
    """One pinned download: a tarball digest plus the digest of the member kept."""

    name: str
    url: str
    tarball_sha256: str
    tarball_limit: int
    member: str
    member_sha256: str
    member_limit: int
    destination: Path
    mode: int


# The phone page's ElevenLabs browser client: @elevenlabs/client 1.23.0,
# dist/lib.iife.js, from the npm registry.
ELEVENLABS_CLIENT_VERSION = "1.23.0"
ELEVENLABS_CLIENT = Artifact(
    name=f"@elevenlabs/client {ELEVENLABS_CLIENT_VERSION} browser bundle",
    url=(
        "https://registry.npmjs.org/@elevenlabs/client/-/"
        f"client-{ELEVENLABS_CLIENT_VERSION}.tgz"
    ),
    tarball_sha256="4c7be4be814674f625d7aa71c79ea4c36913a81413b353498e136953f06f570c",
    tarball_limit=4 * 1024 * 1024,
    member="package/dist/lib.iife.js",
    member_sha256="b6adb12bd5df649af3ce3ac9205fd0e7d1c099513481c58bd1990f2d50903204",
    member_limit=2 * 1024 * 1024,
    destination=VENDOR_DIR / f"elevenlabs-client-{ELEVENLABS_CLIENT_VERSION}.iife.js",
    mode=0o644,
)

# agent-browser 0.34.0. The npm package tarball ships the native binary for
# every platform, so the one for this machine is taken straight out of the
# integrity-verified tarball; the package's postinstall script (which would
# fetch from a GitHub release URL) never runs, and no node runtime is needed.
AGENT_BROWSER_VERSION = "0.34.0"
AGENT_BROWSER_URL = (
    f"https://registry.npmjs.org/agent-browser/-/agent-browser-{AGENT_BROWSER_VERSION}.tgz"
)
AGENT_BROWSER_TARBALL_SHA256 = (
    "a4744fb189e598467abcfb3acdde07118d9e5cb43dc3b31727f869af4eb9d598"
)
# The same tarball as recorded by npm's own integrity field (sha512, base64).
AGENT_BROWSER_TARBALL_SHA512_B64 = (
    "eR6Ey4I/DMs9zZ60b3ziV6pgLIgpxXWzggr3dfFbtskLmeXPJAgXCIIwVL4PihVYJqEUpvWgUKlZ2CIjY1u44g=="
)
AGENT_BROWSER_TARBALL_LIMIT = 64 * 1024 * 1024
AGENT_BROWSER_BINARY_LIMIT = 32 * 1024 * 1024
AGENT_BROWSER_BINARIES: dict[str, tuple[str, str]] = {
    "x86_64": (
        "package/bin/agent-browser-linux-x64",
        "69eadf5d8d6003a06a5cd2f914ebb261c7754fe1335a9190122c334e91909789",
    ),
    "aarch64": (
        "package/bin/agent-browser-linux-arm64",
        "ca70bf7c2d269a152b3824cbb65befb7b8258b8aa1cf34767c64ada2abc3d7c8",
    ),
}


def agent_browser_artifact(machine: str | None = None) -> Artifact:
    arch = machine or platform.machine()
    try:
        member, digest = AGENT_BROWSER_BINARIES[arch]
    except KeyError as error:
        raise SetupError(f"agent-browser has no pinned binary for {arch}") from error
    return Artifact(
        name=f"agent-browser {AGENT_BROWSER_VERSION} ({arch})",
        url=AGENT_BROWSER_URL,
        tarball_sha256=AGENT_BROWSER_TARBALL_SHA256,
        tarball_limit=AGENT_BROWSER_TARBALL_LIMIT,
        member=member,
        member_sha256=digest,
        member_limit=AGENT_BROWSER_BINARY_LIMIT,
        destination=AGENT_BROWSER_PATH,
        mode=0o755,
    )


# -------------------------------------------------------------------- Network


def _https_only(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SetupError(f"refusing non-HTTPS URL {url!r}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def https_open(
    url: str, *, headers: dict[str, str], timeout: float, max_redirects: int
) -> Any:
    """Open ``url`` over HTTPS only, following at most ``max_redirects`` HTTPS hops."""
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _NoRedirect()
    )
    current = url
    for _hop in range(max_redirects + 1):
        _https_only(current)
        request = urllib.request.Request(
            current, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", **headers}
        )
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                location = error.headers.get("Location", "")
                error.close()
                if not location:
                    raise SetupError("redirect without a Location header") from error
                current = urllib.parse.urljoin(current, location)
                continue
            raise
    raise SetupError(f"too many redirects fetching {url}")


def _read_capped(response: Any, limit: int, sink: Callable[[bytes], None]) -> int:
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise SetupError(f"response exceeded {limit} bytes")
        sink(chunk)


def check_key(
    key: str,
    *,
    url: str = KEY_CHECK_URL,
    opener: Callable[..., Any] = https_open,
) -> tuple[int, str]:
    """0: valid, 1: rejected (message says why), 2: could not reach the API."""
    _https_only(url)
    if not key or "\n" in key or "\r" in key or len(key.encode()) > MAX_SECRET_BYTES:
        return 1, "The key is empty or malformed."
    try:
        response = opener(
            url,
            headers={"xi-api-key": key},
            timeout=NETWORK_TIMEOUT_SECONDS / 2,
            max_redirects=0,
        )
    except urllib.error.HTTPError as error:
        body = bytearray()
        try:
            _read_capped(error, KEY_RESPONSE_LIMIT, body.extend)
        except (OSError, SetupError):
            pass
        finally:
            error.close()
        if b"missing_permissions" in body:
            return 1, KEY_SCOPE_HINT
        return 1, f"ElevenLabs rejected the key (HTTP {error.code}). Check it for truncation and paste it again."
    except (OSError, SetupError, ValueError) as error:
        return 2, f"Could not reach api.elevenlabs.io: {error}"
    with response:
        status = int(getattr(response, "status", 200) or 200)
        try:
            _read_capped(response, KEY_RESPONSE_LIMIT, lambda _chunk: None)
        except SetupError:
            pass
    if status == 200:
        return 0, "Key accepted."
    return 1, f"ElevenLabs rejected the key (HTTP {status})."


# ------------------------------------------------------------------ Downloads


def _temporary_name(prefix: str) -> str:
    return f".{prefix}.{secrets.token_hex(8)}.tmp"


def _write_bounded(
    dir_fd: int,
    name: str,
    chunks: Iterable[bytes],
    *,
    limit: int,
    mode: int,
) -> tuple[str, str]:
    """Write ``chunks`` to a fresh private temp file; return (temp name, sha256)."""
    temp = _temporary_name(name)
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > limit:
                raise SetupError(f"{name} exceeded {limit} bytes")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(fd, view)
                view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(temp, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.close(fd)
    return temp, digest.hexdigest()


def _installed_digest(path: Path, *, limit: int) -> str | None:
    try:
        raw = read_private_path(path, limit=limit, private=False)
    except (OSError, PrivateFileError):
        return None
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _member_chunks(tarball_path: str, dir_fd: int, member_name: str, limit: int) -> Iterable[bytes]:
    tar_fd = os.open(tarball_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    with os.fdopen(tar_fd, "rb") as handle, tarfile.open(fileobj=handle, mode="r:gz") as archive:
        for member in archive:
            if member.name != member_name:
                continue
            if not member.isreg():
                raise SetupError(f"{member_name} is not a regular file in the tarball")
            if member.size > limit:
                raise SetupError(f"{member_name} exceeds {limit} bytes")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise SetupError(f"{member_name} cannot be extracted")
            with extracted:
                while True:
                    chunk = extracted.read(65536)
                    if not chunk:
                        return
                    yield chunk
            return
    raise SetupError(f"{member_name} is not in the tarball")


def install_artifact(
    artifact: Artifact,
    *,
    state_dir: Path = STATE_DIR,
    opener: Callable[..., Any] = https_open,
    report: Callable[[str], None] = print,
) -> bool:
    """Download, verify, and install ``artifact``. Returns False when already present."""
    if _installed_digest(artifact.destination, limit=artifact.member_limit) == artifact.member_sha256:
        report(f"already present and verified: {artifact.destination}")
        return False
    with private_dir(state_dir / "tmp") as tmp_fd:
        response = opener(
            artifact.url,
            headers={},
            timeout=NETWORK_TIMEOUT_SECONDS,
            max_redirects=MAX_REDIRECTS,
        )
        with response:
            def chunks() -> Iterable[bytes]:
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        return
                    total += len(chunk)
                    if total > artifact.tarball_limit:
                        raise SetupError(f"{artifact.name} download exceeded {artifact.tarball_limit} bytes")
                    yield chunk

            tarball, tarball_digest = _write_bounded(
                tmp_fd, "download", chunks(), limit=artifact.tarball_limit, mode=0o600
            )
        try:
            if tarball_digest != artifact.tarball_sha256:
                raise SetupError(
                    f"{artifact.name}: tarball sha256 {tarball_digest} does not match the pinned "
                    f"{artifact.tarball_sha256}; nothing was installed"
                )
            with private_dir(artifact.destination.parent, private=False) as dest_fd:
                temp, member_digest = _write_bounded(
                    dest_fd,
                    artifact.destination.name,
                    _member_chunks(tarball, tmp_fd, artifact.member, artifact.member_limit),
                    limit=artifact.member_limit,
                    mode=artifact.mode,
                )
                try:
                    if member_digest != artifact.member_sha256:
                        raise SetupError(
                            f"{artifact.name}: {artifact.member} sha256 {member_digest} does not "
                            f"match the pinned {artifact.member_sha256}; nothing was installed"
                        )
                    os.rename(
                        temp, artifact.destination.name, src_dir_fd=dest_fd, dst_dir_fd=dest_fd
                    )
                    os.fsync(dest_fd)
                except BaseException:
                    try:
                        os.unlink(temp, dir_fd=dest_fd)
                    except OSError:
                        pass
                    raise
        finally:
            try:
                os.unlink(tarball, dir_fd=tmp_fd)
            except OSError:
                pass
    report(f"verified sha256 and installed {artifact.destination}")
    return True


# ------------------------------------------------------- Runtime files & log


def prepare(state_dir: Path = STATE_DIR, config_dir: Path = CONFIG_DIR) -> None:
    """Make both runtime directories exist as private, self-owned directories."""
    with private_dir(state_dir):
        pass
    with private_dir(config_dir):
        pass


def append_log(
    stream: Any, *, log_path: Path = LOG_PATH, limit: int = LOG_LIMIT
) -> int:
    """Append ``stream`` to the setup log through a validated descriptor.

    The log is opened no-follow inside its directory descriptor, must be a
    self-owned regular file with a single link, and is truncated when it
    grows past ``limit`` so it can never be grown without bound.
    """
    with private_dir(log_path.parent) as dir_fd:
        # O_NONBLOCK so a planted FIFO fails to open instead of blocking setup;
        # it has no effect on the regular file this must turn out to be.
        fd = os.open(
            log_path.name,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PrivateFileError(f"{log_path} is not a regular file")
            if info.st_uid != os.geteuid():
                raise PrivateFileError(f"{log_path} is not owned by the current user")
            if info.st_nlink != 1:
                raise PrivateFileError(f"{log_path} has extra hard links")
            if stat.S_IMODE(info.st_mode) & 0o077:
                os.fchmod(fd, 0o600)
            if info.st_size > limit:
                os.ftruncate(fd, 0)
                os.write(fd, b"-- log rotated: earlier output discarded --\n")
            written = 0
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                if written + len(chunk) > limit:
                    os.write(fd, b"-- output truncated --\n")
                    break
                os.write(fd, chunk)
                written += len(chunk)
            return written
        finally:
            os.close(fd)


def store_key(key: str, *, path: Path = API_KEY_PATH) -> None:
    key = key.strip()
    if not key:
        raise SetupError("no key was provided")
    if len(key.encode()) > MAX_SECRET_BYTES - 1:
        raise SetupError("the key is implausibly long")
    write_private_path(path, key.encode() + b"\n")


def _current(existing: dict[str, Any], key: str, default: Any) -> Any:
    value = existing.get(key)
    return default if value is None else value


def merged_config(
    existing: dict[str, Any],
    *,
    browser_mode: str,
    agent_browser_path: str,
    browser_executable_path: str,
) -> dict[str, Any]:
    """The same merge ``omarvis-setup`` has always applied, as data."""
    merged = {
        key: value for key, value in existing.items() if key not in ("vision", "ask_agent_id")
    }
    for key, default in DEFAULT_CONFIG.items():
        if key in ("ui", "dictation", "browser_mode", "agent_browser_path"):
            continue
        merged[key] = _current(existing, key, default)
    merged["browser_mode"] = browser_mode
    merged["agent_browser_path"] = agent_browser_path
    merged["browser_executable_path"] = browser_executable_path
    dictation = dict(existing.get("dictation") or {})
    dictation.pop("chunk_size", None)
    for key, default in DEFAULT_CONFIG["dictation"].items():
        dictation[key] = _current(dictation, key, default)
    merged["dictation"] = dictation
    ui = dict(existing.get("ui") or {})
    ui.pop("earcons", None)
    for key, default in DEFAULT_CONFIG["ui"].items():
        ui[key] = _current(ui, key, default)
    merged["ui"] = ui
    return merged


def merge_config(
    *,
    browser_mode: str,
    agent_browser_path: str,
    browser_executable_path: str,
    path: Path = CONFIG_PATH,
    journal: Path = JOURNAL_PATH,
) -> None:
    """Rewrite the config atomically, keeping the previous file for rollback."""
    with private_dir(path.parent) as dir_fd:
        raw = read_private_file(dir_fd, path.name, limit=MAX_CONFIG_BYTES)
        existing: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                existing = loaded
        previous = None
        if raw is not None:
            previous = f"{path.name}.prev"
            write_private_file(dir_fd, previous, raw)
        journal_add({"action": "config", "path": str(path), "previous": previous}, journal=journal)
        updated = merged_config(
            existing,
            browser_mode=browser_mode,
            agent_browser_path=agent_browser_path,
            browser_executable_path=browser_executable_path,
        )
        write_private_file(
            dir_fd, path.name, (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode()
        )


PROFILE_TEMPLATE = (
    "# Omarvis profile\n\n- Preferred name: \n- Work and projects: \n- Omarchy preferences: \n"
)


def seed_profile(path: Path = PROFILE_PATH) -> bool:
    with private_dir(path.parent) as dir_fd:
        try:
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            return False
        try:
            os.write(fd, PROFILE_TEMPLATE.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return True


# -------------------------------------------------------------------- Journal


def journal_add(entry: dict[str, Any], *, journal: Path = JOURNAL_PATH) -> None:
    line = (json.dumps(entry, sort_keys=True) + "\n").encode()
    with private_dir(journal.parent) as dir_fd:
        existing = read_private_file(dir_fd, journal.name, limit=MAX_CONFIG_BYTES) or b""
        write_private_file(dir_fd, journal.name, existing + line)


def journal_entries(journal: Path = JOURNAL_PATH) -> list[dict[str, Any]]:
    try:
        raw = read_private_path(journal, limit=MAX_CONFIG_BYTES)
    except (OSError, PrivateFileError):
        return []
    entries: list[dict[str, Any]] = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            entries.append(loaded)
    return entries


def journal_clear(journal: Path = JOURNAL_PATH) -> None:
    try:
        with private_dir(journal.parent, create=False) as dir_fd:
            os.unlink(journal.name, dir_fd=dir_fd)
    except (FileNotFoundError, PrivateFileError):
        pass


def _run(argv: Sequence[str], *, timeout: float = 20.0) -> tuple[int | None, str]:
    """Run a helper through the bounded runner; (exit code, combined text)."""
    result = execute_process(list(argv), timeout=timeout, kill_on_timeout=True, stdout_limit=64_000)
    text = (result.stdout + "\n" + result.stderr).strip()
    if result.timed_out:
        return None, f"{argv[0]} timed out"
    return result.exit_code, text


def restore_previous_config(entry: dict[str, Any]) -> str:
    path = Path(str(entry.get("path") or CONFIG_PATH))
    previous = entry.get("previous")
    with private_dir(path.parent) as dir_fd:
        if previous is None:
            try:
                os.unlink(path.name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            return f"removed {path} (it did not exist before setup)"
        raw = read_private_file(dir_fd, str(previous), limit=MAX_CONFIG_BYTES)
        if raw is None:
            return f"could not restore {path}: previous copy is missing"
        write_private_file(dir_fd, path.name, raw)
        return f"restored {path} from before setup"


def recover(
    *,
    journal: Path = JOURNAL_PATH,
    runner: Callable[[Sequence[str]], tuple[int | None, str]] = _run,
    report: Callable[[str], None] = print,
) -> list[str]:
    """Undo every journaled change of an unfinished run, newest first."""
    restored: list[str] = []
    for entry in reversed(journal_entries(journal)):
        action = entry.get("action")
        try:
            if action == "bindings":
                restored.append(
                    restore_bindings(
                        Path(str(entry["path"])), str(entry["backup"]), runner=runner
                    )
                )
            elif action == "config":
                restored.append(restore_previous_config(entry))
            elif action == "plugin" and not entry.get("was_enabled"):
                code, text = runner(["omarchy", "plugin", "disable", PLUGIN_ID])
                restored.append(
                    "disabled the plugin again" if code == 0 else f"could not disable the plugin: {text[:200]}"
                )
        except (OSError, PrivateFileError, ExecutableError, KeyError) as error:
            restored.append(f"could not undo {action}: {error}")
    for line in restored:
        report(line)
    journal_clear(journal)
    return restored


# ------------------------------------------------------------------- Bindings

AGENT_BINDING = 'o.bind("SUPER + CTRL + J", "Omarvis", "omarchy-shell omarvis toggle")'
DICTATE_UNBIND = 'hl.unbind("SUPER + J")'
DICTATE_START_BINDING = (
    'o.bind("SUPER + J", "Omarvis Dictate", "omarchy-shell omarvis dictate start")'
)
# Holding SUPER+J enters a Hyprland submap so SPACE can chord into hands-free
# without the global SUPER+SPACE menu binding firing; the universal release
# binds stop the recording and leave the submap whichever map is active.
DICTATE_SUBMAP_ENTER = 'hl.bind("SUPER + J", hl.dsp.submap("omarvis-dictate"))'
DICTATE_STOP_BINDING = (
    'o.bind("SUPER + J", "Omarvis Dictate Stop", "omarchy-shell omarvis dictate stop", '
    "{ release = true, submap_universal = true })"
)
DICTATE_SUBMAP_LEAVE = (
    'hl.bind("SUPER + J", hl.dsp.submap("reset"), { release = true, submap_universal = true })'
)
DICTATE_SUBMAP = (
    'hl.define_submap("omarvis-dictate", function()\n'
    '  o.bind("SUPER + SPACE", "Omarvis Hands-free", "omarchy-shell omarvis dictate handsfree")\n'
    '  o.bind("ESCAPE", "Omarvis Cancel", "omarchy-shell omarvis dictate cancel")\n'
    '  hl.bind("ESCAPE", hl.dsp.submap("reset"))\n'
    "end)"
)
# Once the keys are up after a Space chord, the shell moves Hyprland into
# this submap for as long as the recording stays open: SUPER+J sends and
# ESCAPE discards, as static binds.
HANDSFREE_SUBMAP = (
    'hl.define_submap("omarvis-handsfree", function()\n'
    '  o.bind("SUPER + J", "Omarvis Hands-free Send", "omarchy-shell omarvis dictate start")\n'
    '  hl.bind("SUPER + J", hl.dsp.submap("reset"))\n'
    '  o.bind("ESCAPE", "Omarvis Hands-free Cancel", "omarchy-shell omarvis dictate cancel")\n'
    '  hl.bind("ESCAPE", hl.dsp.submap("reset"))\n'
    "end)"
)
PANEL_BINDING = 'o.bind("SUPER + ALT + J", "Omarvis Panel", "omarchy-shell omarvis panel")'
REMOTE_BINDING = (
    'o.bind("SUPER + SHIFT + J", "Omarvis Remote", "omarchy-shell omarvis toggleRemote")'
)
ALL_BINDINGS = (
    AGENT_BINDING,
    DICTATE_UNBIND,
    DICTATE_START_BINDING,
    DICTATE_SUBMAP_ENTER,
    DICTATE_STOP_BINDING,
    DICTATE_SUBMAP_LEAVE,
    DICTATE_SUBMAP,
    HANDSFREE_SUBMAP,
    PANEL_BINDING,
    REMOTE_BINDING,
)
DICTATION_UNIT = (
    DICTATE_UNBIND,
    DICTATE_START_BINDING,
    DICTATE_SUBMAP_ENTER,
    DICTATE_STOP_BINDING,
    DICTATE_SUBMAP_LEAVE,
    DICTATE_SUBMAP,
    HANDSFREE_SUBMAP,
)
# Ask mode and the typed one-shot no longer exist. Any binding left over from
# an older install would now dead-end, so setup deletes it.
RETIRED_PATTERN = re.compile(r'omarvis (toggle|toggleMode) ask"|/bin/omarvis-text"')
PRE_SUBMAP_DICTATION_LINES = (
    re.compile(r'^hl\.unbind\("SUPER \+ J"\)'),
    re.compile(r'omarchy-shell omarvis dictate start"\)'),
    re.compile(r'omarchy-shell omarvis dictate stop", \{ release = true \}\)'),
)


@dataclass(frozen=True)
class BindingsPlan:
    missing: tuple[str, ...] = ()
    retired: bool = False
    rebind_dictation: bool = False

    @property
    def changes(self) -> bool:
        return bool(self.missing) or self.retired or self.rebind_dictation


def plan_bindings(text: str) -> BindingsPlan:
    missing: list[str] = []
    if 'omarchy-shell omarvis toggle")' not in text:
        missing.append(AGENT_BINDING)
    retired = RETIRED_PATTERN.search(text) is not None
    # The dictation keys are one unit: the press/release pair plus the submap
    # that makes the Space chord possible. Any install without the submap (or
    # with the pre-submap pair) gets the whole unit rewritten.
    rebind = 'hl.dsp.submap("omarvis-dictate")' not in text
    if rebind:
        missing.extend(DICTATION_UNIT)
    else:
        checks = (
            ('hl.define_submap("omarvis-handsfree"', HANDSFREE_SUBMAP),
            ('omarchy-shell omarvis dictate start")', DICTATE_START_BINDING),
            (
                'omarchy-shell omarvis dictate stop", { release = true, submap_universal = true })',
                DICTATE_STOP_BINDING,
            ),
            (
                'hl.dsp.submap("reset"), { release = true, submap_universal = true })',
                DICTATE_SUBMAP_LEAVE,
            ),
            ('hl.define_submap("omarvis-dictate"', DICTATE_SUBMAP),
        )
        for needle, binding in checks:
            if needle not in text:
                missing.append(binding)
    if 'omarchy-shell omarvis panel")' not in text:
        missing.append(PANEL_BINDING)
    if 'omarchy-shell omarvis toggleRemote")' not in text:
        missing.append(REMOTE_BINDING)
    return BindingsPlan(tuple(missing), retired, rebind)


def apply_plan(text: str, plan: BindingsPlan) -> str:
    lines = text.split("\n")
    if plan.rebind_dictation:
        lines = [
            line for line in lines
            if not any(pattern.search(line) for pattern in PRE_SUBMAP_DICTATION_LINES)
        ]
    if plan.retired:
        lines = [line for line in lines if not RETIRED_PATTERN.search(line)]
    updated = "\n".join(lines)
    if plan.missing:
        if not updated.endswith("\n"):
            updated += "\n"
        updated += "\n" + "\n".join(plan.missing) + "\n"
    return updated


def validate_bindings(
    path: Path, runner: Callable[[Sequence[str]], tuple[int | None, str]] = _run
) -> str | None:
    """Syntax-check with luac when available, then make Hyprland reload and report."""
    try:
        code, text = runner(["luac", "-p", str(path)])
    except ExecutableError:
        code, text = 0, ""
    if code != 0:
        return f"Lua syntax check failed: {text[:400]}"
    try:
        code, text = runner(["hyprctl", "reload"])
    except ExecutableError:
        return None  # not inside Hyprland: syntax is all that can be checked
    if code != 0:
        return f"hyprctl reload failed: {text[:400]}"
    try:
        code, text = runner(["hyprctl", "configerrors"])
    except ExecutableError:
        return None
    if text.strip():
        return f"Hyprland reported config errors: {text[:400]}"
    return None


def apply_bindings(
    path: Path = BINDINGS_PATH,
    *,
    journal: Path = JOURNAL_PATH,
    runner: Callable[[Sequence[str]], tuple[int | None, str]] = _run,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """Transactionally update the bindings file; returns the backup name."""
    with private_dir(path.parent, create=False, private=False) as dir_fd:
        try:
            with open_private_file(dir_fd, path.name, limit=BINDINGS_LIMIT, private=False) as handle:
                mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o777
                raw = handle.read()
        except PrivateFileError as error:
            raise SetupError(str(error)) from error
        text = raw.decode("utf-8", "replace")
        plan = plan_bindings(text)
        if not plan.changes:
            return None
        backup = f"{path.name}.{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}.bak"
        write_private_file(dir_fd, backup, raw, mode=mode)
        journal_add({"action": "bindings", "path": str(path), "backup": backup}, journal=journal)
        write_private_file(dir_fd, path.name, apply_plan(text, plan).encode(), mode=mode)
    problem = validate_bindings(path, runner)
    if problem is not None:
        restore_bindings(path, backup, runner=runner)
        raise SetupError(f"{problem}; restored {path} from {backup}")
    return backup


def restore_bindings(
    path: Path,
    backup: str,
    *,
    runner: Callable[[Sequence[str]], tuple[int | None, str]] = _run,
) -> str:
    with private_dir(path.parent, create=False, private=False) as dir_fd:
        with open_private_file(dir_fd, backup, limit=BINDINGS_LIMIT, private=False) as handle:
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o777
            raw = handle.read()
        write_private_file(dir_fd, path.name, raw, mode=mode)
    try:
        runner(["hyprctl", "reload"])
    except ExecutableError:
        pass
    return f"restored {path} from {backup}"


# ------------------------------------------------------------------------ CLI


def _read_stdin_secret() -> str:
    data = sys.stdin.buffer.read(MAX_SECRET_BYTES + 1)
    if len(data) > MAX_SECRET_BYTES:
        raise SetupError("stdin input is implausibly long for a key")
    return data.decode("utf-8", "strict").strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omarvis.setupfiles")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("log")
    commands.add_parser("check-key")
    commands.add_parser("store-key")
    merge = commands.add_parser("merge-config")
    merge.add_argument("--browser-mode", required=True)
    merge.add_argument("--agent-browser-path", required=True)
    merge.add_argument("--browser-executable-path", required=True)
    commands.add_parser("seed-profile")
    commands.add_parser("fetch-elevenlabs-client")
    commands.add_parser("fetch-agent-browser")
    bindings = commands.add_parser("bindings")
    bindings.add_argument("mode", choices=("show", "plan", "apply"))
    journal = commands.add_parser("journal")
    journal.add_argument("mode", choices=("add", "clear", "pending"))
    journal.add_argument("entry", nargs="?")
    commands.add_parser("recover")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    command = arguments.command
    try:
        if command == "prepare":
            prepare()
        elif command == "log":
            append_log(sys.stdin.buffer)
        elif command == "check-key":
            code, message = check_key(_read_stdin_secret())
            print(message)
            return code
        elif command == "store-key":
            store_key(_read_stdin_secret())
        elif command == "merge-config":
            merge_config(
                browser_mode=arguments.browser_mode,
                agent_browser_path=arguments.agent_browser_path,
                browser_executable_path=arguments.browser_executable_path,
            )
        elif command == "seed-profile":
            print("seeded" if seed_profile() else "present")
        elif command == "fetch-elevenlabs-client":
            install_artifact(ELEVENLABS_CLIENT)
        elif command == "fetch-agent-browser":
            install_artifact(agent_browser_artifact())
        elif command == "bindings":
            if arguments.mode == "show":
                print("\n".join(ALL_BINDINGS))
            elif arguments.mode == "plan":
                raw = read_private_path(BINDINGS_PATH, limit=BINDINGS_LIMIT, private=False)
                if raw is None:
                    print(json.dumps({"exists": False, "changes": False}))
                else:
                    plan = plan_bindings(raw.decode("utf-8", "replace"))
                    print(
                        json.dumps(
                            {
                                "exists": True,
                                "changes": plan.changes,
                                "missing": list(plan.missing),
                                "retired": plan.retired,
                                "rebind_dictation": plan.rebind_dictation,
                            }
                        )
                    )
            else:
                backup = apply_bindings()
                print(backup or "")
        elif command == "journal":
            if arguments.mode == "add":
                entry = json.loads(arguments.entry or "{}")
                if not isinstance(entry, dict) or "action" not in entry:
                    raise SetupError("journal entries need an action")
                journal_add(entry)
            elif arguments.mode == "clear":
                journal_clear()
            else:
                print("yes" if journal_entries() else "no")
        elif command == "recover":
            recover()
    except (SetupError, PrivateFileError, ExecutableError, OSError, ValueError) as error:
        print(f"omarvis-setup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
