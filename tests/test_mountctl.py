import subprocess
from pathlib import Path
from types import SimpleNamespace

import mountctl


def cfg(tmp_path):
    return SimpleNamespace(mount_drive="H:", host="127.0.0.1", port=8081,
                           rclone_dir=tmp_path / "rclone", cache_dir=tmp_path)


def test_ensure_is_noop_when_rclone_already_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(mountctl, "rclone_running", lambda: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert mountctl.ensure_mounted(cfg(tmp_path)) is True


def test_ensure_refuses_when_drive_exists_but_rclone_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(mountctl, "rclone_running", lambda: False)
    monkeypatch.setattr(mountctl.shutil, "which", lambda n: "rclone")
    monkeypatch.setattr(mountctl, "drive_present", lambda c: True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert mountctl.ensure_mounted(cfg(tmp_path)) is False


def test_unmount_never_kills_when_rclone_stays(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mountctl, "rclone_running", lambda: True)
    monkeypatch.setattr(mountctl, "_rc", lambda c, timeout=3.0: calls.append(c) or {})
    monkeypatch.setattr(mountctl, "drive_present", lambda c: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("taskkill")))
    assert mountctl.unmount(cfg(tmp_path), wait=0.6) is False
    assert calls == ["core/quit"]


def test_mount_command_has_rc_no_auth(tmp_path):
    cmd = mountctl.mount_command(cfg(tmp_path), "rclone")
    assert "--rc-no-auth" in cmd and cmd[3] == "H:"
