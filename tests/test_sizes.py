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

import tdapi  # noqa: E402
from tdapi import Entry, _clip_parts, _hash_size  # noqa: E402
from transfer_models import RemotePart  # noqa: E402

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
        {"file_id": f"f{100 + i}", "telegram_message_id": 100 + i,
         "filesize": SEG, "part_index": i}
        for i in range(12)
    ]
    api = FakeApi(rows)
    e = entry(SEG, "h:6291278706", is_split=True, group="g1")
    assert api.total_size(e) == 6291278706
    assert sum(part.size for part in api.parts_for(e)) == 6291278706


def test_total_size_reports_what_exists_for_an_under_registered_split():
    # the Aqua case: 1 of 6 parts registered, real length far larger
    rows = [{"file_id": "f1859", "telegram_message_id": 1859,
             "filesize": SEG, "part_index": 0}]
    api = FakeApi(rows)
    e = entry(SEG, "h:2657828026", is_split=True, group="g2")
    assert api.total_size(e) == SEG  # not 2657828026 — those bytes are gone
    assert api.parts_for(e) == [RemotePart(1859, SEG, 0, "f1859")]


def test_clipping_survives_the_disk_cache():
    rows = [
        {"file_id": f"f{100 + i}", "telegram_message_id": 100 + i,
         "filesize": SEG, "part_index": i}
        for i in range(3)
    ]
    api = FakeApi(rows)
    e = entry(SEG, "h:%d" % (2 * SEG + 100), is_split=True, group="g3")
    first = api.parts_for(e)
    second = api.parts_for(e)  # served from _split_cache this time
    assert api.calls == 1
    assert first == second == [RemotePart(100, SEG, 0, "f100"),
                               RemotePart(101, SEG, 0, "f101"),
                               RemotePart(102, 100, 0, "f102")]


# --------------------------------------------------------------------------- #
# JsonStore concurrency
#
# Two processes hold these caches at once — the bridge while a warm-up run
# fills them. A whole-file write loses whatever the other one added: measured,
# 21,228 media-property entries went back down to 2,371 that way.
# --------------------------------------------------------------------------- #


def test_flush_keeps_entries_another_writer_added(tmp_path):
    from tdapi import JsonStore

    path = tmp_path / "shared.json"
    first = JsonStore(path)
    first.put("a", 1)

    # A second holder of the same file, as a separate process would be.
    second = JsonStore(path)
    second.put("b", 2)

    # The first one writes again from its own, older view.
    first.put("c", 3)

    import json

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"a": 1, "b": 2, "c": 3}


def test_flush_is_a_noop_when_nothing_changed(tmp_path):
    from tdapi import JsonStore

    path = tmp_path / "quiet.json"
    store = JsonStore(path)
    store.flush()
    assert not path.exists()  # nothing written, nothing to merge


def test_later_value_wins_over_the_file(tmp_path):
    from tdapi import JsonStore

    path = tmp_path / "shared.json"
    JsonStore(path).put("k", "old")
    store = JsonStore(path)
    store.put("k", "new")

    import json

    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "new"}


# --------------------------------------------------------------------------- #
# ShardedJsonStore: what a write costs when the values are big
# --------------------------------------------------------------------------- #


def test_a_sharded_store_writes_only_the_key_that_changed(tmp_path):
    """One archive's tree must not rewrite every other archive's.

    zip_dirs.json reached 132 MB across 276 archives on the live drive, and
    because it was one shared JSON, listing /game re-serialised the whole file
    once per archive -- about 18 GB of writes for one listing, which never
    finished and took the mount down with it.
    """
    store = tdapi.ShardedJsonStore(tmp_path / "zips")
    store.put("a", {"tree": ["x"] * 100})
    before = (tmp_path / "zips" / "a.json").stat().st_mtime_ns

    store.put("b", {"tree": ["y"] * 100})

    assert (tmp_path / "zips" / "a.json").stat().st_mtime_ns == before
    assert sorted(p.name for p in (tmp_path / "zips").glob("*.json")) == ["a.json", "b.json"]


def test_a_sharded_store_reads_back_across_processes(tmp_path):
    tdapi.ShardedJsonStore(tmp_path / "zips").put("key", {"v": 1})

    assert tdapi.ShardedJsonStore(tmp_path / "zips").get("key") == {"v": 1}
    assert tdapi.ShardedJsonStore(tmp_path / "zips").get("missing") is None


def test_a_sharded_store_makes_a_safe_filename_from_any_key(tmp_path):
    store = tdapi.ShardedJsonStore(tmp_path / "zips")
    store.put("../../escape", {"v": 1})

    files = list((tmp_path / "zips").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path / "zips"
    assert store.get("../../escape") == {"v": 1}


def test_a_sharded_store_leaves_no_temp_file_behind(tmp_path):
    store = tdapi.ShardedJsonStore(tmp_path / "zips")
    store.put("a", {"v": 1})
    store.put("a", {"v": 2})

    assert list((tmp_path / "zips").glob("*.tmp")) == []
    assert store.get("a") == {"v": 2}


def test_json_store_put_during_flush_neither_crashes_nor_drops(tmp_path, monkeypatch):
    """A put from another worker thread while flush() is mid-write.

    flush() used to hand the live dict to json.dump outside the lock, so a
    concurrent put raised "dictionary changed size during iteration" and
    /rpc/props answered 500 -- which the shell treats like "no properties" and
    reads the whole file instead. A put landing between the snapshot and the
    merge was also dropped from memory for good.
    """
    from tdapi import JsonStore

    path = tmp_path / "props.json"
    store = JsonStore(path)
    store.put("a", 1)

    real_read = Path.read_text

    def read_then_race(self, *args, **kwargs):
        text = real_read(self, *args, **kwargs)
        if self == path:
            store._data["during-merge"] = 2  # what a racing put() leaves behind
            store._dirty = True
        return text

    real_dump = tdapi.json.dump

    def dump_then_race(obj, fh, *args, **kwargs):
        store.put("during-dump", 3, defer=True)
        return real_dump(obj, fh, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_then_race)
    monkeypatch.setattr(tdapi.json, "dump", dump_then_race)
    store.put("b", 4)  # flushes

    assert store.get("during-merge") == 2
    assert store.get("during-dump") == 3
    monkeypatch.undo()
    store.flush()
    reloaded = JsonStore(path)
    assert {k: reloaded.get(k) for k in ("a", "b", "during-merge", "during-dump")} == {
        "a": 1, "b": 4, "during-merge": 2, "during-dump": 3,
    }
