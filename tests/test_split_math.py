"""Offline tests for split-file offset math and the seekable remote reader.

No Telegram, no network: a fake reader stands in for MTProto and serves bytes
from in-memory part buffers.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tgio import (  # noqa: E402
    SEGMENT_SIZE,
    SeekableRemoteFile,
    SlicedReader,
    build_part_table,
    map_range,
    plan_segments,
)


# --------------------------------------------------------------------------- #
# build_part_table
# --------------------------------------------------------------------------- #


def test_part_table_accumulates_offsets():
    table, total = build_part_table([(11, 100), (22, 50), (33, 7)])
    assert total == 157
    assert [(p.message_id, p.start, p.size) for p in table] == [(11, 0, 100), (22, 100, 50), (33, 150, 7)]


def test_part_table_drops_empty_parts():
    table, total = build_part_table([(11, 10), (22, 0), (33, 5)])
    assert total == 15
    assert [p.message_id for p in table] == [11, 33]


def test_part_table_empty():
    table, total = build_part_table([])
    assert table == [] and total == 0


# --------------------------------------------------------------------------- #
# map_range
# --------------------------------------------------------------------------- #


@pytest.fixture
def table():
    return build_part_table([(1, 100), (2, 100), (3, 30)])


def test_map_inside_one_part(table):
    parts, total = table
    assert map_range(parts, total, 10, 20) == [(0, 10, 20)]


def test_map_spans_two_parts(table):
    parts, total = table
    assert map_range(parts, total, 90, 20) == [(0, 90, 10), (1, 0, 10)]


def test_map_spans_three_parts(table):
    parts, total = table
    assert map_range(parts, total, 50, 180) == [(0, 50, 50), (1, 0, 100), (2, 0, 30)]


def test_map_exact_part_boundary(table):
    parts, total = table
    assert map_range(parts, total, 100, 100) == [(1, 0, 100)]


def test_map_clips_at_end_of_file(table):
    parts, total = table
    assert map_range(parts, total, 220, 999) == [(2, 20, 10)]


def test_map_beyond_eof_is_empty(table):
    parts, total = table
    assert map_range(parts, total, 230, 10) == []
    assert map_range(parts, total, 1_000_000, 10) == []


def test_map_zero_or_negative_is_empty(table):
    parts, total = table
    assert map_range(parts, total, 0, 0) == []
    assert map_range(parts, total, 10, -5) == []
    assert map_range(parts, total, -1, 10) == []


def test_map_covers_every_byte_exactly_once(table):
    parts, total = table
    covered = []
    for index, inner, count in map_range(parts, total, 0, total):
        base = parts[index].start
        covered.extend(range(base + inner, base + inner + count))
    assert covered == list(range(total))


# --------------------------------------------------------------------------- #
# plan_segments
# --------------------------------------------------------------------------- #


def test_plan_segments_single():
    assert plan_segments(100, 512) == [(0, 100)]


def test_plan_segments_exact_multiple():
    assert plan_segments(1024, 512) == [(0, 512), (512, 512)]


def test_plan_segments_with_remainder():
    assert plan_segments(1100, 512) == [(0, 512), (512, 512), (1024, 76)]


def test_plan_segments_default_boundary_matches_frontend():
    # 1000 parts x 512 KB, same boundary as frontend/src/lib/gramjs.ts:502
    assert SEGMENT_SIZE == 1000 * 512 * 1024
    plan = plan_segments(SEGMENT_SIZE + 1)
    assert plan == [(0, SEGMENT_SIZE), (SEGMENT_SIZE, 1)]
    assert plan_segments(SEGMENT_SIZE) == [(0, SEGMENT_SIZE)]


def test_plan_segments_covers_the_whole_file():
    total = 3 * SEGMENT_SIZE + 12345
    plan = plan_segments(total)
    assert sum(size for _, size in plan) == total
    assert [offset for offset, _ in plan] == [0, SEGMENT_SIZE, 2 * SEGMENT_SIZE, 3 * SEGMENT_SIZE]


def test_plan_segments_zero_and_invalid():
    assert plan_segments(0) == [(0, 0)]
    with pytest.raises(ValueError):
        plan_segments(-1)
    with pytest.raises(ValueError):
        plan_segments(10, 0)


# --------------------------------------------------------------------------- #
# SeekableRemoteFile over a fake transport
# --------------------------------------------------------------------------- #


class FakeReader:
    """Stands in for TelegramWorker.read(), counting requests."""

    def __init__(self, parts):
        self.parts = parts  # message_id -> bytes
        self.calls = []

    def read(self, message_id, offset, length):
        self.calls.append((message_id, offset, length))
        return self.parts[message_id][offset : offset + length]


@pytest.fixture
def remote():
    blobs = {
        1: bytes((i % 251) for i in range(300)),
        2: bytes(((i + 7) % 251) for i in range(300)),
        3: bytes(((i + 19) % 251) for i in range(40)),
    }
    reader = FakeReader(blobs)
    whole = blobs[1] + blobs[2] + blobs[3]
    return reader, whole


def _open(reader, block_size=64):
    return SeekableRemoteFile(reader, [(1, 300), (2, 300), (3, 40)], name="x.bin", block_size=block_size)


def test_remote_file_size_is_the_sum_of_parts(remote):
    reader, whole = remote
    fh = _open(reader)
    assert fh.size == len(whole) == 640
    assert fh.seek(0, 2) == 640


def test_remote_file_sequential_read_matches(remote):
    reader, whole = remote
    fh = _open(reader)
    assert fh.read() == whole


def test_remote_file_random_ranges_match(remote):
    reader, whole = remote
    fh = _open(reader)
    for start, length in [(0, 1), (5, 10), (295, 20), (299, 2), (600, 40), (639, 5), (0, 640), (640, 10)]:
        fh.seek(start)
        assert fh.read(length) == whole[start : start + length], (start, length)


def test_remote_file_read_never_crosses_a_part_in_one_call(remote):
    reader, _ = remote
    fh = _open(reader, block_size=1024)
    fh.seek(280)
    fh.read(80)  # spans part 1 -> part 2
    assert len(reader.calls) >= 2
    for message_id, offset, length in reader.calls:
        assert offset + length <= len(reader.parts[message_id])


def test_remote_file_block_cache_avoids_refetching(remote):
    reader, whole = remote
    fh = _open(reader, block_size=128)
    fh.seek(0)
    fh.read(10)
    first = len(reader.calls)
    for _ in range(20):
        fh.seek(0)
        fh.read(10)
    assert len(reader.calls) == first  # served entirely from the cache


def test_remote_file_readinto_and_tell(remote):
    reader, whole = remote
    fh = _open(reader)
    buf = bytearray(16)
    fh.seek(100)
    assert fh.readinto(buf) == 16
    assert bytes(buf) == whole[100:116]
    assert fh.tell() == 116


def test_remote_file_negative_seek_rejected(remote):
    reader, _ = remote
    fh = _open(reader)
    with pytest.raises(OSError):
        fh.seek(-1)


def test_sliced_reader_maps_onto_the_window(remote):
    reader, whole = remote
    fh = _open(reader)
    view = SlicedReader(fh, 310, 50, name="inner")
    assert view.size == 50
    assert view.read() == whole[310:360]
    view.seek(10)
    assert view.read(5) == whole[320:325]
    assert view.seek(0, 2) == 50


# --------------------------------------------------------------------------- #
# batched block fetching
#
# The block size is the rounding applied to every read, so it stays small to
# keep a tiny read from dragging megabytes behind it. Width has to come from
# somewhere else: a read spanning many blocks fetches them in one batch.
# --------------------------------------------------------------------------- #


def test_contiguous_groups_runs():
    from tgio import _contiguous

    assert list(_contiguous([])) == []
    assert list(_contiguous([3])) == [(3, 3)]
    assert list(_contiguous([0, 1, 2])) == [(0, 2)]
    assert list(_contiguous([0, 1, 5, 6, 9])) == [(0, 1), (5, 6), (9, 9)]


def test_small_read_only_fetches_one_block(remote):
    reader, _ = remote
    fh = _open(reader, block_size=64)
    fh.seek(0)
    fh.read(4)
    # 4 bytes asked for, at most one block fetched — not a large rounding
    assert sum(length for _, _, length in reader.calls) <= 64


def test_wide_read_is_one_call_per_part_not_per_block(remote):
    reader, whole = remote
    fh = _open(reader, block_size=64)
    fh.seek(0)
    assert fh.read(256) == whole[:256]
    # 4 blocks of 64, all inside part 1 (300 bytes): one batched request
    assert len(reader.calls) == 1


def test_wide_read_splits_only_at_part_boundaries(remote):
    reader, whole = remote
    fh = _open(reader, block_size=64)
    fh.seek(0)
    assert fh.read(320) == whole[:320]
    # blocks 0..4 cross out of part 1 at byte 300 — one call per part, no more
    assert len(reader.calls) == 2


def test_batch_skips_blocks_already_cached(remote):
    reader, whole = remote
    fh = _open(reader, block_size=64)
    fh.seek(64)
    fh.read(64)  # block 1 only
    calls_after_warm = len(reader.calls)
    fh.seek(0)
    assert fh.read(192) == whole[:192]  # blocks 0,1,2 — block 1 is cached
    # blocks 0 and 2 are not adjacent once 1 is skipped, so two runs
    assert len(reader.calls) == calls_after_warm + 2


def test_read_wider_than_the_cache_still_returns_everything(remote):
    reader, whole = remote
    # 8 blocks cached, but the read spans 10 — assembly must not lose a block
    fh = SeekableRemoteFile(
        reader, [(1, 300), (2, 300), (3, 40)], block_size=64, blocks_cached=8
    )
    assert fh.read() == whole


# --------------------------------------------------------------------------- #
# cached head: the first bytes served from disk instead of Telegram
# --------------------------------------------------------------------------- #


def test_head_serves_the_front_without_touching_the_reader(remote):
    reader, whole = remote
    fh = SeekableRemoteFile(
        reader, [(1, 300), (2, 300), (3, 40)], block_size=64, head=whole[:128]
    )
    assert fh.read(128) == whole[:128]
    assert reader.calls == []


def test_head_shorter_than_the_read_falls_through_for_the_rest(remote):
    reader, whole = remote
    fh = SeekableRemoteFile(
        reader, [(1, 300), (2, 300), (3, 40)], block_size=64, head=whole[:100]
    )
    # 100 bytes of head plus the remainder, which must be whole and correct
    assert fh.read(250) == whole[:250]
    assert reader.calls, "the tail past the head still has to be fetched"


def test_head_does_not_disturb_reads_past_it(remote):
    reader, whole = remote
    fh = SeekableRemoteFile(
        reader, [(1, 300), (2, 300), (3, 40)], block_size=64, head=whole[:128]
    )
    fh.seek(400)
    assert fh.read(100) == whole[400:500]


def test_head_longer_than_the_file_is_clipped(remote):
    reader, _ = remote
    small = SeekableRemoteFile(reader, [(3, 40)], block_size=64, head=b"\x00" * 4096)
    assert small.size == 40
    assert len(small.read()) == 40


def test_head_survives_seek_and_reread(remote):
    reader, whole = remote
    fh = SeekableRemoteFile(
        reader, [(1, 300), (2, 300), (3, 40)], block_size=64, head=whole[:128]
    )
    assert fh.read() == whole
    fh.seek(0)
    assert fh.read(64) == whole[:64]


def test_a_full_width_streamed_read_stays_in_the_cache():
    """The block cache has to hold one whole streamed read.

    ``_blocks_for`` caches the batch it just fetched and then trims to
    ``blocks_cached``. Hold fewer blocks than a read is wide and it throws away
    the front of the batch it has only just filled, so the next caller — rclone
    asking for the same region in smaller pieces, zipfile seeking backwards —
    pays for the network a second time. Uses the real constants: this is a
    relationship between two of them, not a number.
    """
    import tgio

    width = tgio.STREAM_BLOCK_SIZE
    blob = bytes((i % 251) for i in range(width * 2))
    reader = FakeReader({1: blob})
    fh = SeekableRemoteFile(reader, [(1, len(blob))], name="video.mp4")

    assert fh.read(width) == blob[:width]
    fetched = len(reader.calls)
    fh.seek(0)
    assert fh.read(width) == blob[:width]
    assert len(reader.calls) == fetched
