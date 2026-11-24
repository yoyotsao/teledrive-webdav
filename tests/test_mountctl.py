import subprocess
from pathlib import Path
from types import SimpleNamespace

import mountctl


def cfg(tmp_path):
    return SimpleNamespace(mount_drive="H:", host="127.0.0.1", port=8081,
                           rclone_dir=tmp_path / "rclone", cache_dir=tmp_path,
                           vfs_cache_max_size="160G", vfs_cache_min_free_space="20G")


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


def test_mount_command_pins_the_volume_serial(tmp_path):
    # A serial that changes per mount would give every file a new identity to
    # the shell, orphaning Windows' thumbcache exactly like unstable file IDs.
    cmd = mountctl.mount_command(cfg(tmp_path), "rclone")
    at = cmd.index(f"VolumeSerialNumber={mountctl.VOLUME_SERIAL}")
    assert cmd[at - 1] == "-o"


def test_rclone_exe_prefers_the_stable_inode_build(monkeypatch, tmp_path):
    built = tmp_path / "rclone.exe"
    built.write_bytes(b"")
    monkeypatch.setattr(mountctl, "STABLE_RCLONE", built)
    monkeypatch.setattr(mountctl.shutil, "which", lambda n: "C:/stock/rclone.exe")
    assert mountctl.rclone_exe() == str(built)


def test_rclone_exe_falls_back_to_path_with_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(mountctl, "STABLE_RCLONE", tmp_path / "missing.exe")
    monkeypatch.setattr(mountctl.shutil, "which", lambda n: "C:/stock/rclone.exe")
    with caplog.at_level("WARNING", logger="mountctl"):
        assert mountctl.rclone_exe() == "C:/stock/rclone.exe"
    assert "thumbnails will not survive a remount" in caplog.text


def test_mount_command_uses_configured_cache_sizes(tmp_path):
    c = cfg(tmp_path)
    c.vfs_cache_max_size, c.vfs_cache_min_free_space = "300G", "50G"
    cmd = mountctl.mount_command(c, "rclone")
    assert cmd[cmd.index("--vfs-cache-max-size") + 1] == "300G"
    assert cmd[cmd.index("--vfs-cache-min-free-space") + 1] == "50G"


def test_rclone_size_rejects_typos():
    import pytest
    from config import ConfigError, rclone_size

    assert rclone_size("x", " 1.5T ") == "1.5T"
    for bad in ("160GB", "0G", "-5G", "big", ""):
        with pytest.raises(ConfigError):
            rclone_size("x", bad)
