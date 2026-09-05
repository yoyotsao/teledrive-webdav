"""The shell warm: what a batch reports, and what happens when it wedges.

Offline. warmshell.exe is never launched here — ``subprocess.Popen`` is replaced
— because what is being tested is the accounting around it, which is where this
layer went silently dead: for weeks every batch was killed at the flat 600s
timeout and logged "0 warmed", which says nothing about whether the mount was
gone, the handler was failing, or one file had the shell wedged. The sweep spent
ten minutes per hundred files that way and never put one entry into
thumbcache_*.db — the cache worth 274 previews a second.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import warmup  # noqa: E402


def _warmer(tmp_path, mount_drive="H:", exists=True):
    exe = tmp_path / "warmshell.exe"
    if exists:
        exe.write_bytes(b"stub")
    resolver = SimpleNamespace(
        cfg=SimpleNamespace(mount_drive=mount_drive),
        wait_for_quiet=lambda quiet: None,
    )
    return warmup.Warmer(resolver, quiet=0, shell_exe=exe)


class _FakePopen:
    """Stands in for warmshell.exe: canned output, optionally never finishing."""

    def __init__(self, stdout=b"", stderr=b"", timeout=False):
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self.killed = False
        self.inputs = []

    def __call__(self, argv, **kwargs):
        self.argv = argv
        return self

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.inputs.append(input)
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired(cmd="warmshell", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


# --------------------------------------------------------------------------- #
# the per-file report
# --------------------------------------------------------------------------- #


def test_report_reads_warmed_and_failed_lines():
    err = (
        "+ 41 H:\\pixiv\\a.jpg\n"
        "- 3 H:\\pixiv\\b.jpg\n"
        "+ 12 H:\\湊あくあ\\c.jpg\n"
    ).encode("utf-8")

    out = warmup._shell_report(err)

    assert out == {
        "H:\\pixiv\\a.jpg": True,
        "H:\\pixiv\\b.jpg": False,
        # Non-ASCII paths are most of them here, so the report is UTF-8 and not
        # the console codepage — that mistake is what turned 湊あくあ into
        # mojibake once already (see CLAUDE.md on URL escaping).
        "H:\\湊あくあ\\c.jpg": True,
    }


def test_report_ignores_anything_it_cannot_parse():
    err = b"warmshell: something on stderr\n+ 8 H:\\a.jpg\n+\n"

    assert warmup._shell_report(err) == {"H:\\a.jpg": True}


# --------------------------------------------------------------------------- #
# one batch
# --------------------------------------------------------------------------- #


def test_a_finished_batch_counts_from_the_exe(tmp_path, monkeypatch):
    warmer = _warmer(tmp_path)
    fake = _FakePopen(stdout=b"2\n", stderr=b"+ 5 H:\\a.jpg\n+ 6 H:\\b.jpg\n")
    monkeypatch.setattr(warmup.subprocess, "Popen", fake)

    warmed, wedged = warmer._warm_group(["H:\\a.jpg", "H:\\b.jpg"])

    assert (warmed, wedged) == (2, False)
    assert fake.inputs == [b"H:\\a.jpg\nH:\\b.jpg\n"]


def test_a_killed_batch_still_reports_what_it_warmed(tmp_path, monkeypatch, caplog):
    """The count on stdout never arrives; the flushed lines are all there is."""
    warmer = _warmer(tmp_path)
    fake = _FakePopen(stderr=b"+ 5 H:\\a.jpg\n- 4 H:\\b.jpg\n", timeout=True)
    monkeypatch.setattr(warmup.subprocess, "Popen", fake)

    with caplog.at_level("WARNING"):
        warmed, wedged = warmer._warm_group(["H:\\a.jpg", "H:\\b.jpg", "H:\\stuck.jpg"])

    assert (warmed, wedged) == (1, True)
    assert fake.killed
    # And it names what the shell was still holding, which is the one thing the
    # old flat-timeout message could never say.
    assert "H:\\stuck.jpg" in caplog.text


def test_a_wedged_batch_stops_the_pass(tmp_path, monkeypatch):
    """Whatever wedged this batch is still true for the next twenty-four."""
    warmer = _warmer(tmp_path)
    fake = _FakePopen(timeout=True)
    monkeypatch.setattr(warmup.subprocess, "Popen", fake)
    monkeypatch.setattr(warmup.os.path, "isdir", lambda path: True)
    calls = []
    real_group = warmer._warm_group
    warmer._warm_group = lambda group: (calls.append(group) or real_group(group))

    paths = [f"H:\\{i}.jpg" for i in range(warmup.SHELL_BATCH * 4)]
    assert warmer._run_warmshell(paths) == 0
    assert len(calls) == 1  # not four


def test_the_batch_budget_scales_with_the_batch(tmp_path, monkeypatch):
    """A flat ceiling made 24s a file look normal; the budget is per file."""
    warmer = _warmer(tmp_path)
    seen = {}

    class _Timeouts(_FakePopen):
        def communicate(self, input=None, timeout=None):
            seen["timeout"] = timeout
            return b"0\n", b""

    monkeypatch.setattr(warmup.subprocess, "Popen", _Timeouts())

    warmer._warm_group(["H:\\a.jpg"] * warmup.SHELL_BATCH)

    assert seen["timeout"] == pytest.approx(
        warmup.SHELL_SECONDS_PER_FILE * warmup.SHELL_BATCH
    )
    assert seen["timeout"] < 600  # the ceiling it replaces


# --------------------------------------------------------------------------- #
# no mount, nothing to do
# --------------------------------------------------------------------------- #


def test_no_mount_means_no_batches(tmp_path, monkeypatch):
    """A missing drive fails in microseconds, which reads exactly like success.

    ``SHCreateItemFromParsingName`` on a drive that is not there returns at once,
    so 25 files "produced nothing" instantly — indistinguishable in the log from
    a handler that answered badly, and this project's history has both shapes.
    """
    warmer = _warmer(tmp_path)
    monkeypatch.setattr(warmup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(
        warmup.subprocess, "Popen",
        lambda *a, **k: pytest.fail("asked the shell about an unmounted drive"),
    )

    assert warmer._run_warmshell(["H:\\a.jpg"]) == 0


def test_a_mounted_drive_is_asked_about(tmp_path, monkeypatch):
    warmer = _warmer(tmp_path)
    fake = _FakePopen(stdout=b"1\n", stderr=b"+ 5 H:\\a.jpg\n")
    monkeypatch.setattr(warmup.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(warmup.subprocess, "Popen", fake)

    assert warmer._run_warmshell(["H:\\a.jpg"]) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
