"""Offline tests for reported file sizes.

The backend stores ``filesize`` as the padded upload length (512 KB parts x N),
so it over-reports by up to one part. Advertising that padding makes clients read
past the end of the Telegram document, wait out a long timeout and get nothing
back — that is what stalls video players looking for a trailing moov atom.
``file_hash``'s ":<n>" suffix carries the real length; these tests pin the
clipping that follows from it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdapi import Entry, _clip_parts, _hash_size  # noqa: E402

PART = 512 * 1024
SEG = 1000 * PART  # 524288000


def entry(size, file_hash=None, *, is_split=False, group=None, message_id=7):
    return Entry(
        file_id="f1",
        name="clip.mp4",
        is_dir=False,
        size=size,
        mtime=0.0,
        message_id=message_id,
        is_split=is_split,
        split_group_id=group,
        file_hash=file_hash,
    )


# --------------------------------------------------------------------------- #
# _hash_size
# --------------------------------------------------------------------------- #


def test_hash_size_reads_the_suffix():
    assert _hash_size("24e90a12fa9e6ef1:2657828026") == 2657828026


def test_hash_size_none_without_a_suffix():
    assert _hash_size("24e90a12fa9e6ef1") is None
    assert _hash_size(None) is None
    assert _hash_size("") is None


def test_hash_size_none_when_suffix_is_not_a_number():
    assert _hash_size("abc:notanumber") is None


def test_real_size_is_exposed_on_entry():
    # observed row: filesize padded to 29 x 512 KB, hash carries the truth
    e = entry(15204352, "deadbeef:15089802")
    assert e.real_size == 15089802
    assert entry(15204352, "deadbeef").real_size is None


# --------------------------------------------------------------------------- #
# _clip_parts
# --------------------------------------------------------------------------- #


def test_clip_is_a_noop_without_a_real_size():
    parts = [(1, SEG), (2, SEG)]
    assert _clip_parts(parts, None) == parts


def test_clip_trims_only_the_tail():
    # 3 segments registered, real file ends 100 bytes into the third
    parts = [(1, SEG), (2, SEG), (3, SEG)]
    assert _clip_parts(parts, 2 * SEG + 100) == [(1, SEG), (2, SEG), (3, 100)]


def test_clip_drops_parts_beyond_the_real_end():
    parts = [(1, SEG), (2, SEG), (3, SEG)]
    assert _clip_parts(parts, SEG) == [(1, SEG)]


def test_clip_shrinks_a_single_padded_part():
    # the non-split case: one message, filesize rounded up to a 512 KB multiple
    assert _clip_parts([(7, 15204352)], 15089802) == [(7, 15089802)]


def test_clip_leaves_an_under_registered_file_alone():
    # upload stopped after part 0: the bytes are gone, nothing to trim
    assert _clip_parts([(1, SEG)], 6 * SEG) == [(1, SEG)]


def test_clip_sums_to_the_real_size():
    parts = [(i, SEG) for i in range(12)]
    real = 6291278706  # observed: 12 padded segments over-report by 177,294
    assert sum(s for _, s in _clip_parts(parts, real)) == real


def test_clip_handles_an_exact_multiple():
    parts = [(1, SEG), (2, SEG)]
    assert _clip_parts(parts, 2 * SEG) == parts


# --------------------------------------------------------------------------- #
# parts_for / total_size, with the HTTP layer stubbed out
# --------------------------------------------------------------------------- #


class FakeApi:
    """Just enough of TeleDriveClient to exercise parts_for/total_size."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = 0
        self._cache = {}

    # borrowed implementations under test
    from tdapi import TeleDriveClient

    parts_for = TeleDriveClient.parts_for
    total_size = TeleDriveClient.total_size

    @property
    def _split_cache(self):
        outer = self

        class _Store:
            def get(self, key):
                return outer._cache.get(key)

            def put(self, key, value, *, defer=False):
                outer._cache[key] = value

        return _Store()

    def _call(self, method, path, **kw):
        self.calls += 1
        return {"files": self.rows}


def test_total_size_clips_a_padded_single_message_file():
    api = FakeApi()
    assert api.total_size(entry(15204352, "h:15089802")) == 15089802


def test_total_size_keeps_filesize_when_no_hash_suffix():
    api = FakeApi()
    assert api.total_size(entry(15204352, "nohash")) == 15204352


def test_total_size_clips_a_split_file_tail():
    rows = [
        {"telegram_message_id": 100 + i, "filesize": SEG, "part_index": i}
        for i in range(12)
    ]
    api = FakeApi(rows)
    e = entry(SEG, "h:6291278706", is_split=True, group="g1")
    assert api.total_size(e) == 6291278706
    assert sum(s for _, s in api.parts_for(e)) == 6291278706


def test_total_size_reports_what_exists_for_an_under_registered_split():
    # the Aqua case: 1 of 6 parts registered, real length far larger
    rows = [{"telegram_message_id": 1859, "filesize": SEG, "part_index": 0}]
    api = FakeApi(rows)
    e = entry(SEG, "h:2657828026", is_split=True, group="g2")
    assert api.total_size(e) == SEG  # not 2657828026 — those bytes are gone
    assert api.parts_for(e) == [(1859, SEG)]


def test_clipping_survives_the_disk_cache():
    rows = [
        {"telegram_message_id": 100 + i, "filesize": SEG, "part_index": i}
        for i in range(3)
    ]
    api = FakeApi(rows)
    e = entry(SEG, "h:%d" % (2 * SEG + 100), is_split=True, group="g3")
    first = api.parts_for(e)
    second = api.parts_for(e)  # served from _split_cache this time
    assert api.calls == 1
    assert first == second == [(100, SEG), (101, SEG), (102, 100)]
