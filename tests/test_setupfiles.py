"""The setup helper: bounded downloads, secrets on stdin, hostile runtime paths, rollback."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from omarvis import setupfiles as sf
from omarvis.process import ExecutableError


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tarball(members: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            archive.addfile(info)
    return buffer.getvalue()


class _Response(io.BytesIO):
    status = 200


def _opener(body: bytes, calls: list):
    def opener(url, *, headers, timeout, max_redirects):
        calls.append((url, dict(headers), timeout, max_redirects))
        return _Response(body)

    return opener


def _artifact(tmp_path: Path, tar: bytes, payload: bytes, **overrides) -> sf.Artifact:
    fields = dict(
        name="test artifact",
        url="https://example.test/artifact.tgz",
        tarball_sha256=_sha256(tar),
        tarball_limit=1 << 20,
        member="package/dist/lib.iife.js",
        member_sha256=_sha256(payload),
        member_limit=1 << 20,
        destination=tmp_path / "vendor" / "lib.js",
        mode=0o644,
    )
    fields.update(overrides)
    return sf.Artifact(**fields)


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".")) if directory.exists() else []


def test_install_artifact_verifies_both_digests_and_is_idempotent(tmp_path: Path) -> None:
    payload = b"window.ElevenLabsClient = {};"
    tar = _tarball({"package/dist/lib.iife.js": payload, "package/README.md": b"# hi"})
    artifact = _artifact(tmp_path, tar, payload)
    calls: list = []
    reports: list[str] = []

    assert sf.install_artifact(
        artifact, state_dir=tmp_path / "state", opener=_opener(tar, calls), report=reports.append
    )
    assert artifact.destination.read_bytes() == payload
    assert stat.S_IMODE(artifact.destination.stat().st_mode) == 0o644
    assert calls == [("https://example.test/artifact.tgz", {}, sf.NETWORK_TIMEOUT_SECONDS, sf.MAX_REDIRECTS)]
    assert _leftovers(tmp_path / "state" / "tmp") == []
    assert _leftovers(artifact.destination.parent) == []
    assert reports and "verified sha256" in reports[-1]

    # Already present and verified: no network at all.
    calls.clear()
    assert not sf.install_artifact(
        artifact, state_dir=tmp_path / "state", opener=_opener(tar, calls), report=reports.append
    )
    assert calls == []


def test_install_artifact_refuses_wrong_digests_and_installs_nothing(tmp_path: Path) -> None:
    payload = b"payload"
    tar = _tarball({"package/dist/lib.iife.js": payload})

    wrong_tarball = _artifact(tmp_path, tar, payload, tarball_sha256="0" * 64)
    with pytest.raises(sf.SetupError, match="tarball sha256"):
        sf.install_artifact(wrong_tarball, state_dir=tmp_path / "state", opener=_opener(tar, []), report=lambda _m: None)
    assert not wrong_tarball.destination.exists()

    wrong_member = _artifact(tmp_path, tar, payload, member_sha256="0" * 64)
    with pytest.raises(sf.SetupError, match="lib.iife.js sha256"):
        sf.install_artifact(wrong_member, state_dir=tmp_path / "state", opener=_opener(tar, []), report=lambda _m: None)
    assert not wrong_member.destination.exists()
    assert _leftovers(tmp_path / "state" / "tmp") == []
    assert _leftovers(wrong_member.destination.parent) == []


def test_install_artifact_caps_sizes_and_rejects_non_regular_members(tmp_path: Path) -> None:
    payload = b"x" * 100
    tar = _tarball({"package/dist/lib.iife.js": payload})

    with pytest.raises(sf.SetupError, match="exceeded"):
        sf.install_artifact(
            _artifact(tmp_path, tar, payload, tarball_limit=10),
            state_dir=tmp_path / "state",
            opener=_opener(tar, []),
            report=lambda _m: None,
        )
    with pytest.raises(sf.SetupError, match="exceeds"):
        sf.install_artifact(
            _artifact(tmp_path, tar, payload, member_limit=10),
            state_dir=tmp_path / "state",
            opener=_opener(tar, []),
            report=lambda _m: None,
        )
    linked = _tarball({"package/other": payload}, symlink=("package/dist/lib.iife.js", "/etc/passwd"))
    with pytest.raises(sf.SetupError, match="not a regular file"):
        sf.install_artifact(
            _artifact(tmp_path, linked, payload),
            state_dir=tmp_path / "state",
            opener=_opener(linked, []),
            report=lambda _m: None,
        )
    missing = _tarball({"package/other": payload})
    with pytest.raises(sf.SetupError, match="not in the tarball"):
        sf.install_artifact(
            _artifact(tmp_path, missing, payload),
            state_dir=tmp_path / "state",
            opener=_opener(missing, []),
            report=lambda _m: None,
        )


def test_network_is_https_only() -> None:
    with pytest.raises(sf.SetupError, match="non-HTTPS"):
        sf.https_open("http://example.test/x", headers={}, timeout=1.0, max_redirects=0)
    with pytest.raises(sf.SetupError, match="non-HTTPS"):
        sf.check_key("k", url="http://api.elevenlabs.io/v1/convai/agents")
    assert sf.KEY_CHECK_URL.startswith("https://")
    assert sf.ELEVENLABS_CLIENT.url.startswith("https://")
    assert sf.AGENT_BROWSER_URL.startswith("https://")


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.example", code, "nope", Message(), io.BytesIO(body))


def test_check_key_sends_the_key_as_a_header_and_classifies_answers() -> None:
    calls: list = []

    def accepting(url, *, headers, timeout, max_redirects):
        calls.append((headers, max_redirects))
        return _Response(b"{}")

    assert sf.check_key("sk-good", opener=accepting)[0] == 0
    assert calls == [({"xi-api-key": "sk-good"}, 0)]

    def scope_rejecting(url, **_kwargs):
        raise _http_error(401, b'{"detail":{"status":"missing_permissions"}}')

    code, message = sf.check_key("sk-narrow", opener=scope_rejecting)
    assert code == 1 and message == sf.KEY_SCOPE_HINT

    def rejecting(url, **_kwargs):
        raise _http_error(401, b'{"detail":"bad"}')

    code, message = sf.check_key("sk-bad", opener=rejecting)
    assert code == 1 and "HTTP 401" in message

    def unreachable(url, **_kwargs):
        raise OSError("network is down")

    code, message = sf.check_key("sk-any", opener=unreachable)
    assert code == 2 and "network is down" in message
    assert sf.check_key("", opener=accepting)[0] == 1
    assert sf.check_key("with\nnewline", opener=accepting)[0] == 1


def test_cli_takes_secrets_on_stdin_never_as_arguments() -> None:
    parser = sf.build_parser()
    assert parser.parse_args(["check-key"]).command == "check-key"
    assert parser.parse_args(["store-key"]).command == "store-key"
    with pytest.raises(SystemExit):
        parser.parse_args(["check-key", "sk-leaks-in-argv"])
    with pytest.raises(SystemExit):
        parser.parse_args(["store-key", "sk-leaks-in-argv"])


def test_append_log_refuses_planted_links_and_fifos_and_rotates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    log = state / "setup.log"
    log.symlink_to(victim)
    with pytest.raises(OSError):
        sf.append_log(io.BytesIO(b"leak"), log_path=log)
    assert victim.read_text() == "untouched"
    log.unlink()

    os.mkfifo(log)
    with pytest.raises(OSError):
        sf.append_log(io.BytesIO(b"leak"), log_path=log)
    log.unlink()

    assert sf.append_log(io.BytesIO(b"hello\n"), log_path=log) == 6
    assert log.read_bytes() == b"hello\n"
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert sf.append_log(io.BytesIO(b"more\n"), log_path=log, limit=4) == 0
    assert log.read_bytes().startswith(b"-- log rotated")
    assert log.read_bytes().endswith(b"-- output truncated --\n")


def test_store_key_replaces_a_planted_link_without_following_it(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    key_path = config_dir / "api_key"
    key_path.symlink_to(victim)

    sf.store_key("sk-secret\n", path=key_path)

    assert victim.read_text() == "untouched"
    assert not key_path.is_symlink()
    assert key_path.read_bytes() == b"sk-secret\n"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    with pytest.raises(sf.SetupError):
        sf.store_key("   ", path=key_path)


def test_seed_profile_never_follows_a_planted_link(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    profile = config_dir / "profile.md"
    profile.symlink_to(victim)

    assert sf.seed_profile(profile) is False
    assert victim.read_text() == "untouched"

    profile.unlink()
    assert sf.seed_profile(profile) is True
    assert profile.read_text() == sf.PROFILE_TEMPLATE
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert sf.seed_profile(profile) is False


def test_merge_config_keeps_the_previous_file_and_recover_restores_it(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "config.json"
    journal = tmp_path / "state" / "setup.journal"
    before = {"agent_id": "agent-1", "herdr_announcements": False, "vision": {"old": True}}
    config.write_text(json.dumps(before))

    sf.merge_config(
        browser_mode="real-profile",
        agent_browser_path="/state/agent-browser",
        browser_executable_path="/usr/bin/chromium",
        path=config,
        journal=journal,
    )

    merged = json.loads(config.read_text())
    assert merged["agent_id"] == "agent-1"
    assert merged["herdr_announcements"] is False
    assert merged["browser_mode"] == "real-profile"
    assert merged["agent_browser_path"] == "/state/agent-browser"
    assert "vision" not in merged
    assert merged["dictation"]["model_id"] == "scribe_v2"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert sf.journal_entries(journal) == [
        {"action": "config", "path": str(config), "previous": "config.json.prev"}
    ]

    restored = sf.recover(journal=journal, runner=lambda argv: (0, ""), report=lambda _m: None)
    assert restored == [f"restored {config} from before setup"]
    assert json.loads(config.read_text()) == before
    assert sf.journal_entries(journal) == []

    # A config that did not exist before setup is removed again on recovery.
    fresh = config_dir / "fresh.json"
    sf.merge_config(
        browser_mode="unavailable",
        agent_browser_path="",
        browser_executable_path="",
        path=fresh,
        journal=journal,
    )
    assert fresh.exists()
    sf.recover(journal=journal, runner=lambda argv: (0, ""), report=lambda _m: None)
    assert not fresh.exists()


def test_merge_config_refuses_a_planted_link_at_the_config_path(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text('{"agent_id": "victim"}')
    config = config_dir / "config.json"
    config.symlink_to(victim)

    with pytest.raises(sf.PrivateFileError):
        sf.merge_config(
            browser_mode="unavailable",
            agent_browser_path="",
            browser_executable_path="",
            path=config,
            journal=tmp_path / "state" / "setup.journal",
        )
    assert victim.read_text() == '{"agent_id": "victim"}'


def _bindings_dir(tmp_path: Path, text: str = "-- user bindings\n") -> Path:
    hypr = tmp_path / "hypr"
    hypr.mkdir()
    bindings = hypr / "bindings.lua"
    bindings.write_text(text)
    bindings.chmod(0o644)
    return bindings


def test_bindings_apply_backs_up_validates_and_records_a_journal_entry(tmp_path: Path) -> None:
    bindings = _bindings_dir(tmp_path)
    journal = tmp_path / "state" / "setup.journal"
    calls: list[tuple[str, ...]] = []

    def runner(argv):
        calls.append(tuple(argv))
        return 0, ""

    backup = sf.apply_bindings(bindings, journal=journal, runner=runner, clock=lambda: 0.0)

    assert backup is not None and backup.startswith("bindings.lua.") and backup.endswith(".bak")
    assert (bindings.parent / backup).read_text() == "-- user bindings\n"
    text = bindings.read_text()
    assert text.startswith("-- user bindings\n")
    for binding in sf.ALL_BINDINGS:
        assert binding in text
    assert stat.S_IMODE(bindings.stat().st_mode) == 0o644
    assert ("luac", "-p", str(bindings)) in calls
    assert ("hyprctl", "reload") in calls
    assert ("hyprctl", "configerrors") in calls
    assert sf.journal_entries(journal) == [
        {"action": "bindings", "path": str(bindings), "backup": backup}
    ]
    assert not sf.plan_bindings(text).changes
    assert sf.apply_bindings(bindings, journal=journal, runner=runner) is None


def test_bindings_apply_rolls_back_when_hyprland_reports_errors(tmp_path: Path) -> None:
    bindings = _bindings_dir(tmp_path)
    journal = tmp_path / "state" / "setup.journal"
    calls: list[tuple[str, ...]] = []

    def runner(argv):
        calls.append(tuple(argv))
        if argv[:2] == ["hyprctl", "configerrors"]:
            return 0, "bindings.lua:12: unexpected symbol"
        return 0, ""

    with pytest.raises(sf.SetupError, match="config errors"):
        sf.apply_bindings(bindings, journal=journal, runner=runner, clock=lambda: 0.0)

    assert bindings.read_text() == "-- user bindings\n"
    assert calls.count(("hyprctl", "reload")) == 2, "the restored file is reloaded too"
    backups = list(bindings.parent.glob("bindings.lua.*.bak"))
    assert len(backups) == 1

    def bad_syntax(argv):
        if argv[0] == "luac":
            return 1, "luac: syntax error near 'end'"
        return 0, ""

    with pytest.raises(sf.SetupError, match="Lua syntax"):
        sf.apply_bindings(bindings, journal=journal, runner=bad_syntax, clock=lambda: 1.0)
    assert bindings.read_text() == "-- user bindings\n"


def test_bindings_validation_degrades_without_luac_or_hyprland(tmp_path: Path) -> None:
    bindings = _bindings_dir(tmp_path)

    def no_tools(argv):
        raise ExecutableError(f"no trusted executable for {argv[0]!r}")

    backup = sf.apply_bindings(
        bindings, journal=tmp_path / "state" / "setup.journal", runner=no_tools, clock=lambda: 0.0
    )
    assert backup is not None
    assert sf.AGENT_BINDING in bindings.read_text()


def test_bindings_apply_refuses_a_planted_link(tmp_path: Path) -> None:
    hypr = tmp_path / "hypr"
    hypr.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("-- victim\n")
    bindings = hypr / "bindings.lua"
    bindings.symlink_to(victim)

    with pytest.raises(sf.SetupError):
        sf.apply_bindings(bindings, journal=tmp_path / "state" / "setup.journal", runner=lambda a: (0, ""))
    assert victim.read_text() == "-- victim\n"


def test_recover_undoes_a_crashed_run_newest_first(tmp_path: Path) -> None:
    bindings = _bindings_dir(tmp_path)
    journal = tmp_path / "state" / "setup.journal"
    calls: list[tuple[str, ...]] = []

    def runner(argv):
        calls.append(tuple(argv))
        return 0, ""

    backup = sf.apply_bindings(bindings, journal=journal, runner=runner, clock=lambda: 0.0)
    sf.journal_add({"action": "plugin", "was_enabled": False}, journal=journal)
    assert sf.journal_entries(journal)[-1]["action"] == "plugin"
    calls.clear()

    restored = sf.recover(journal=journal, runner=runner, report=lambda _m: None)

    assert restored == ["disabled the plugin again", f"restored {bindings} from {backup}"]
    assert calls[0] == ("omarchy", "plugin", "disable", sf.PLUGIN_ID)
    assert ("hyprctl", "reload") in calls
    assert bindings.read_text() == "-- user bindings\n"
    assert not journal.exists()

    # A plugin that was already enabled before setup stays enabled.
    sf.journal_add({"action": "plugin", "was_enabled": True}, journal=journal)
    calls.clear()
    assert sf.recover(journal=journal, runner=runner, report=lambda _m: None) == []
    assert calls == []


def test_prepare_makes_both_runtime_directories_private(tmp_path: Path) -> None:
    state = tmp_path / "share" / "omarvis"
    config = tmp_path / "config" / "omarvis"
    config.mkdir(parents=True, mode=0o755)

    sf.prepare(state, config)

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o700
