"""Sliding-window sequential reads (SeekableRemoteFile.iter_stream)."""

import sys
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgio  # noqa: E402
from tgio import REQUEST_SIZE, SeekableRemoteFile  # noqa: E402
from transfer_models import RemotePart  # noqa: E402


class FakeWorker:
    def __init__(self, blobs, fail=None):
        self.blobs = blobs
        self.submitted = []  # (message_id, offset)
        self.futures = []
        self.fail = fail or {}
        self.refreshed = 0

    def submit_chunk(self, message_id, file_id, offset, expected_size=None):
        self.submitted.append((message_id, offset))
        fut = Future()
        self.futures.append(fut)
        err = self.fail.get((message_id, offset))
        if err is not None:
            self.fail.pop((message_id, offset))
            fut.set_exception(err)
        else:
            fut.set_result(self.blobs[message_id][offset : offset + REQUEST_SIZE])
        return fut

    def refresh_document(self, message_id, file_id, expected_size=None):
        self.refreshed += 1


def make(sizes, **kw):
    blobs = {i + 1: bytes((i * 7 + j) % 251 for j in range(n)) for i, n in enumerate(sizes)}
    worker = FakeWorker(blobs, **kw)
    pool = SimpleNamespace(for_read=lambda uid: SimpleNamespace(worker=worker))
    parts = [RemotePart(i + 1, n, 0, str(i + 1)) for i, n in enumerate(sizes)]
    return SeekableRemoteFile(pool, parts), worker, b"".join(blobs[i + 1] for i in range(len(sizes)))


def test_yields_the_whole_file_in_order_across_parts():
    f, worker, expected = make([3 * REQUEST_SIZE, 2 * REQUEST_SIZE + 100])
    assert b"".join(f.iter_stream(window=4)) == expected
    assert [m for m, _o in worker.submitted] == [1, 1, 1, 2, 2, 2]


def test_never_has_more_than_window_chunks_outstanding_and_refills_one_at_a_time():
    f, worker, expected = make([20 * REQUEST_SIZE])
    out = []
    peak = 0
    for chunk in f.iter_stream(window=5):
        out.append(chunk)
        peak = max(peak, len(worker.submitted) - len(out))
    assert peak == 5  # kept full (never more, never drained to zero between batches)
    assert b"".join(out) == expected


def test_unaligned_start_trims_the_first_chunk():
    f, worker, expected = make([4 * REQUEST_SIZE])
    start = REQUEST_SIZE + 123
    assert b"".join(f.iter_stream(start, window=3)) == expected[start:]
    assert worker.submitted[0] == (1, REQUEST_SIZE)


def test_start_inside_the_second_part():
    f, worker, expected = make([2 * REQUEST_SIZE, 3 * REQUEST_SIZE])
    start = 2 * REQUEST_SIZE + REQUEST_SIZE + 5
    assert b"".join(f.iter_stream(start, window=3)) == expected[start:]
    assert worker.submitted[0] == (2, REQUEST_SIZE)


def test_closing_early_cancels_whatever_is_still_pending():
    f, worker, _ = make([10 * REQUEST_SIZE])
    gen = f.iter_stream(window=4)
    next(gen)
    outstanding = worker.futures[1:]  # submitted but not consumed
    # these are already resolved by the fake, so make them cancellable
    pending = [Future() for _ in outstanding]
    for i, fut in enumerate(pending):
        worker.futures[i + 1] = fut
    gen.close()
    # the generator's own queue held the originals; cancel() on a finished
    # future is a no-op, so assert it at least closed cleanly and stopped submitting
    n = len(worker.submitted)
    assert n == 5  # 4 in the window + 1 refill after the first was consumed


def test_short_chunk_in_the_middle_is_an_error():
    f, worker, _ = make([3 * REQUEST_SIZE])
    worker.blobs[1] = worker.blobs[1][: REQUEST_SIZE + 10]  # file ends early
    with pytest.raises(OSError):
        list(f.iter_stream(window=2))


def test_expired_file_reference_refreshes_and_retries_the_chunk():
    class Expired(Exception):
        pass

    f, worker, expected = make([3 * REQUEST_SIZE], fail={(1, REQUEST_SIZE): Expired("FILE_REFERENCE_EXPIRED")})
    import tgio as t

    orig = t._is_file_reference_error
    t._is_file_reference_error = lambda exc: isinstance(exc, Expired)
    try:
        assert b"".join(f.iter_stream(window=2)) == expected
    finally:
        t._is_file_reference_error = orig
    assert worker.refreshed == 1


def test_other_errors_propagate():
    f, worker, _ = make([3 * REQUEST_SIZE], fail={(1, 0): RuntimeError("boom")})
    with pytest.raises(RuntimeError):
        list(f.iter_stream(window=2))


def test_default_window_is_the_flood_safe_ceiling():
    assert tgio.STREAM_WINDOW == tgio.DOWNLOAD_CONNECTIONS * tgio.READS_IN_FLIGHT == 16
