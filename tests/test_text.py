import os
import subprocess
from pathlib import Path


def test_text_command_passes_menu_input_as_one_message(tmp_path):
    repo = Path(__file__).parent.parent
    fake_bin = tmp_path / "bin"
    fake_venv = tmp_path / "venv" / "bin"
    fake_bin.mkdir()
    fake_venv.mkdir(parents=True)
    log = tmp_path / "python-args"
    omarchy = fake_bin / "omarchy"
    omarchy.write_text("#!/bin/sh\nprintf '%s' 'close the window; not a shell command'\n")
    omarchy.chmod(0o755)
    python = fake_venv / "python"
    python.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" >\"$OMARVIS_TEST_LOG\"\n"
    )
    python.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "OMARVIS_VENV": str(fake_venv.parent),
            "OMARVIS_TEST_LOG": str(log),
        }
    )

    completed = subprocess.run(
        [repo / "bin" / "omarvis-text"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert log.read_text().splitlines() == [
        "-u",
        "-m",
        "omarvis.daemon",
        "--text-only",
        "--message",
        "close the window; not a shell command",
    ]
