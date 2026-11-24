"""rclone's pacer can leak its tokens and stall every uncached listing.

Seen 2026-11-01 after 7.4 days of uptime: 5 goroutines waiting in
pacer.beginCall, 0 inside a call, the bridge answering PROPFIND in 3ms.
Nothing on our side was wrong, so the only useful thing is to notice.
The dumps below use rclone's real frame names; if an upgrade renames them,
these tests are what should break first.
"""

from teledrive_webdav.windows import mountwatch
from teledrive_webdav.windows.mountwatch import MountWatch, count_pacer

IDLE = """goroutine 1 [chan receive, 92 minutes]:
github.com/rclone/rclone/cmd/mountlib.(*MountPoint).Wait(0xfc57748c380)
\tgithub.com/rclone/rclone/cmd/mountlib/mount.go:414 +0xfb

goroutine 70 [select]:
github.com/rclone/rclone/vfs/vfscache.(*Cache).cleaner(0xfc5772d8a00, {0x7ff775383890, 0xfc576ec8320})
\tgithub.com/rclone/rclone/vfs/vfscache/cache.go:857 +0x168
created by github.com/rclone/rclone/vfs/vfscache.New in goroutine 1
"""

WAITING = """goroutine {n} [chan receive, 41 minutes]:
github.com/rclone/rclone/lib/pacer.(*Pacer).beginCall(0xc000a2c000, 0x0)
\tgithub.com/rclone/rclone/lib/pacer/pacer.go:170 +0x6e
github.com/rclone/rclone/lib/pacer.(*Pacer).call(0xc000a2c000, 0xc0012f3e30, 0xa)
\tgithub.com/rclone/rclone/lib/pacer/pacer.go:227 +0x58
github.com/rclone/rclone/lib/pacer.(*Pacer).Call(0xc000a2c000, 0xc0012f3e30)
\tgithub.com/rclone/rclone/lib/pacer/pacer.go:250 +0x8a
github.com/rclone/rclone/backend/webdav.(*Fs).listAll(0xc0003e6000, {0x2b4c6a8, 0xc00011e000}, {0xc00129e1e0, 0x5}, 0x0, 0x0, 0x0, 0xc0012f3f60)
\tgithub.com/rclone/rclone/backend/webdav/webdav.go:879 +0x2c5
"""

ACTIVE = """goroutine {n} [IO wait]:
internal/poll.runtime_pollWait(0x1fa5485a600, 0x72)
\truntime/netpoll.go:351 +0x85
github.com/rclone/rclone/lib/rest.(*Client).Call(0xc0003f2000, {0x2b4c6a8, 0xc00011e000}, 0xc0012a8000)
\tgithub.com/rclone/rclone/lib/rest/rest.go:250 +0x5a5
github.com/rclone/rclone/lib/pacer.(*Pacer).call(0xc000a2c000, 0xc0012f3e30, 0xa)
\tgithub.com/rclone/rclone/lib/pacer/pacer.go:228 +0x7f
"""

REFILL = """goroutine {n} [sleep]:
time.Sleep(0x989680)
\truntime/time.go:315 +0xf2
github.com/rclone/rclone/lib/pacer.(*Pacer).beginCall.func1(0x989680)
\tgithub.com/rclone/rclone/lib/pacer/pacer.go:178 +0x25
created by github.com/rclone/rclone/lib/pacer.(*Pacer).beginCall in goroutine 812
"""


def dump(waiting=0, active=0, refill=0):
    parts = [IDLE]
    n = 100
    for template, count in ((WAITING, waiting), (ACTIVE, active), (REFILL, refill)):
        for _ in range(count):
            parts.append(template.replace("{n}", str(n)))
            n += 1
    return "\n".join(parts)


def test_counts_each_kind_of_pacer_goroutine():
    sample = count_pacer(dump(waiting=5, active=2, refill=1))
    assert (sample.waiting, sample.active, sample.refilling) == (5, 2, 1)


def test_created_by_line_does_not_make_a_refill_goroutine_look_like_a_waiter():
    assert count_pacer(dump(refill=1)).waiting == 0


def test_the_2026_11_01_shape_is_stalled_and_a_busy_or_idle_mount_is_not():
    assert count_pacer(dump(waiting=5)).stalled
    assert not count_pacer(dump(waiting=5, active=1)).stalled
    assert not count_pacer(dump(waiting=5, refill=1)).stalled  # a token is on its way
    assert not count_pacer(dump()).stalled


class Feed:
    def __init__(self, *texts):
        self.texts = list(texts)

    def __call__(self):
        return self.texts.pop(0)


def watch(*texts, auto_recover=False, recover=None):
    return MountWatch(fetch=Feed(*texts), auto_recover=auto_recover,
                      recover=recover or (lambda: (_ for _ in ()).throw(AssertionError("recovered"))))


def test_only_three_stalled_samples_in_a_row_count_as_exhausted(caplog):
    w = watch(dump(waiting=5), dump(waiting=5), dump(waiting=5))
    w.sample()
    w.sample()
    assert not w.status()["pacer_exhausted"]
    with caplog.at_level("ERROR", logger="mountwatch"):
        w.sample()
    assert w.status()["pacer_exhausted"]
    assert sum("pacer" in r.getMessage() for r in caplog.records) == 1


def test_one_healthy_sample_resets_the_streak():
    w = watch(dump(waiting=5), dump(waiting=5), dump(), dump(waiting=5), dump(waiting=5))
    for _ in range(5):
        w.sample()
    assert not w.status()["pacer_exhausted"]


def test_recovery_clears_the_flag_and_logs_once(caplog):
    w = watch(*([dump(waiting=5)] * 3), dump(active=1), dump())
    for _ in range(3):
        w.sample()
    with caplog.at_level("INFO", logger="mountwatch"):
        w.sample()
        w.sample()
    assert not w.status()["pacer_exhausted"]
    assert sum("recovered" in r.getMessage() for r in caplog.records) == 1


def test_unreachable_rclone_is_not_a_verdict():
    w = watch(dump(waiting=5), dump(waiting=5), None, dump(waiting=5))
    for _ in range(4):
        w.sample()
    assert not w.status()["pacer_exhausted"]


def test_auto_recover_off_never_touches_the_mount():
    w = watch(*([dump(waiting=5)] * 6))  # recover() would raise if called
    for _ in range(6):
        w.sample()
    assert w.status()["pacer_exhausted"]


def test_auto_recover_on_asks_once_per_episode():
    calls = []
    w = watch(*([dump(waiting=5)] * 5), auto_recover=True, recover=lambda: calls.append(1) or False)
    for _ in range(5):
        w.sample()
    assert calls == [1]


def test_status_carries_no_paths_or_credentials():
    w = watch(dump(waiting=5))
    w.sample()
    assert set(w.status()) == {"pacer_exhausted", "pacer_waiting", "pacer_active", "pacer_checked_at"}
