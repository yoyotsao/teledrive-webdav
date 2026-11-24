import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import launch_bridge  # noqa: E402


def test_bridge_is_launched_without_a_window_and_without_blocking_stdio(monkeypatch):
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    launch_bridge.launch("C:/repo", python="py.exe")

    assert seen["cmd"] == ["py.exe", "-m", "teledrive_webdav.dav.bridge"]
    assert seen["kw"]["cwd"] == "C:/repo"
    assert seen["kw"]["creationflags"] & launch_bridge.CREATE_NO_WINDOW
    for stream in ("stdin", "stdout", "stderr"):
        assert seen["kw"][stream] == subprocess.DEVNULL
