"""Upload visibility: progress in /rpc/status, what-is-this on the start line,
and a flood log that does not repeat itself every 17 seconds."""
import logging
import threading
import types
from pathlib import Path

import pytest

from teledrive_webdav.core import fileid
from teledrive_webdav.upload import upload_engine
from teledrive_webdav.upload.segment_scheduler import SegmentDescriptor, SegmentScheduler
from test_bridge_e2e import _upload_now, rig  # noqa: F401
from test_segment_scheduler import Clock, Pool
from teledrive_webdav.upload.transfer_models import TransferRequest
from teledrive_webdav.upload.upload_activity import AccountActivityRegistry, UploadSpeedTracker

MIB = 1 << 20


def _scheduler(sizes):
    clock = Clock()
    speed = UploadSpeedTracker(clock=clock, window=30, snapshot_ttl=300)
    activity = AccountActivityRegistry(clock=clock, speed_tracker=speed)
    pool = Pool(activity, account_ids=(1,))
    descriptors, offset = [], 0
    for index, size in enumerate(sizes):
        descriptors.append(SegmentDescriptor(index, offset, size))
        offset += size
    return SegmentScheduler("file", descriptors, pool=pool, activity=activity,
                            speed_tracker=speed, clock=clock)


# -- 1. progress in status ---------------------------------------------------


def test_segment_status_reports_size_position_and_percent():
    scheduler = _scheduler([100, 100])
    action = scheduler.select_next_action()
    scheduler.part_succeeded(action.lease, 0, 50)
    rows = scheduler.status()
    assert [r["segments_total"] for r in rows] == [2, 2]
    assert [r["size"] for r in rows] == [100, 100]
    done = next(r for r in rows if r["logical_uploaded_bytes"])
    assert done["percent"] == 50.0
    assert done["segment_number"] == done["segment_index"] + 1  # "1 of 2", not 0-based


def test_scheduler_metrics_report_total_and_overall_percent():
    scheduler = _scheduler([100, 100])
    action = scheduler.select_next_action()
    scheduler.part_succeeded(action.lease, 0, 100)
    metrics = scheduler.metrics_snapshot()
    assert metrics["total_bytes"] == 200
    assert metrics["percent"] == 50.0
    assert metrics["segments_total"] == 2 and metrics["segments_done"] == 0


def test_engine_scheduler_status_carries_the_file_id():
    scheduler = _scheduler([100])
    scheduler.log_tag = fileid.tag("a/b.wav")
    engine = object.__new__(upload_engine.UploadEngine)
    engine._scheduler_state_lock = threading.Lock()
    engine._active_schedulers = {1: scheduler}
    status = engine.scheduler_status()[0]
    assert status["id"] == "0x%016x" % fileid.path_id("a/b.wav")
    assert "a/b" not in str(status)


# -- 2. start line ------------------------------------------------------------


def _request(name, size, mime="application/octet-stream", album=True):
    return TransferRequest(source=Path(name), upload_name=name, mime_type=mime,
                           parent_id=None, logical_size=size, allow_album=album)


@pytest.mark.parametrize(
    "name,size,mime,album,expected",
    [
        ("x.WAV", 59_500_000, "audio/wav", True, "ext=.wav size=56.7MiB protocol=big parts=1"),
        ("y.bin", 5 * MIB, "application/octet-stream", True, "ext=.bin size=5.0MiB protocol=small parts=1"),
        ("p.jpg", 2 * MIB, "image/jpeg", True, "ext=.jpg size=2.0MiB protocol=album parts=1"),
        ("p.jpg", 2 * MIB, "image/jpeg", False, "ext=.jpg size=2.0MiB protocol=small parts=1"),
        ("z.mkv", 600 * MIB, "video/x-matroska", True, "ext=.mkv size=600.0MiB protocol=split parts=2"),
        ("noext", 100, "application/octet-stream", True, "ext=(none) size=100B protocol=small parts=1"),
    ],
)
def test_describe_upload_says_kind_size_and_protocol(name, size, mime, album, expected):
    assert upload_engine.describe_upload(_request(name, size, mime, album)) == expected


def test_uploading_line_has_the_description_and_no_name(rig, caplog):
    mark = "PRIVATE-start.bin"
    with caplog.at_level(logging.INFO, logger="uploadstage"):
        assert rig.request("PUT", f"/{mark}", data=b"w" * 4096).status_code == 201
        _upload_now(rig, mark)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("uploading "))
    assert fileid.tag(mark) in line and "ext=.bin" in line and "protocol=small" in line
    assert "PRIVATE" not in line


# -- 4. flood log -------------------------------------------------------------


def test_flood_gate_logs_first_then_summarises():
    gate = upload_engine.FloodLogGate(interval=60.0)
    assert gate.admit("k", 0.0) == (0, 0.0)  # first one: logged, nothing held
    assert gate.admit("k", 17.0) is None
    assert gate.admit("k", 34.0) is None
    assert gate.admit("k", 51.0) is None
    held, window = gate.admit("k", 61.0)  # next window: how many were held
    assert held == 3 and window == pytest.approx(61.0)
    assert gate.admit("k", 62.0) is None


def test_flood_gate_keys_are_independent():
    gate = upload_engine.FloodLogGate(interval=60.0)
    assert gate.admit("a", 0.0) is not None
    assert gate.admit("b", 1.0) is not None  # another file is not throttled by the first


def test_premium_flood_lines_collapse_per_file(caplog, monkeypatch):
    monkeypatch.setattr(upload_engine, "_FLOOD_LOG", upload_engine.FloodLogGate(interval=60.0))
    now = [0.0]
    monkeypatch.setattr(upload_engine.time, "monotonic", lambda: now[0])
    task = types.SimpleNamespace(size=100, logical_uploaded_bytes=10, index=0)
    scheduler = types.SimpleNamespace(
        log_tag=fileid.tag("f"), file_job_id="j", task=lambda task_id: task,
        speed_tracker=types.SimpleNamespace(live_speed=lambda lease: 1.0),
    )
    lease = types.SimpleNamespace(task_id="t", attempt_id=1, account_id=7)
    cycle = types.SimpleNamespace(
        lease=lease, wait_seconds=15, account_accepted_parts=1, account_accepted_bytes=2,
        task_accepted_parts=1, task_accepted_bytes=2)
    pacer = types.SimpleNamespace(mode="frozen", rate=1.0, paused_until=0.0)
    with caplog.at_level(logging.INFO, logger="upload_engine"):
        for t in (0, 1, 17, 18, 34, 61):
            now[0] = float(t)
            upload_engine.UploadEngine._log_scheduler_diagnostic(
                scheduler, "premium_flood", {"cycle": cycle, "pacer": pacer})
    lines = [r.getMessage() for r in caplog.records if "premium flood" in r.getMessage()]
    assert len(lines) == 2
    assert "more in the last" not in lines[0]
    assert "(+4 more in the last 61s)" in lines[1]
