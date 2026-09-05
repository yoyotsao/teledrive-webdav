"""Exact, account-aware deduplication for staged uploads."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from upload_engine import (  # noqa: E402
    CoverageError,
    FingerprintClaims,
    UploadedPart,
    assert_parts_cover_file,
    canonical_existing_parts,
)
import gamestage  # noqa: E402


def row(*, group="g", index=0, size=10, account=1, message=None, file_id=None, split=True):
    """A complete check-hash row with independently chosen storage identity."""
    return {
        "file_id": file_id or f"file-{group}-{index}-{account}",
        "filesize": size,
        "mime_type": "application/octet-stream",
        "telegram_message_id": message if message is not None else 100 + index,
        "access_hash": f"access-{index}-{account}",
        "telegram_user_id": account,
        "is_split_file": split,
        "split_group_id": group if split else None,
        "part_index": index,
        "has_thumbnail": False,
    }


def test_incomplete_duplicate_is_rejected():
    """Dropping a part must not make a shorter, corrupt duplicate reusable."""
    rows = [row(group="g", index=0, size=500), row(group="g", index=2, size=500)]

    assert canonical_existing_parts(rows, original_size=1000) == []


def test_exact_complete_duplicate_keeps_each_storage_account():
    """Each canonical segment retains the account that owns its Telegram message."""
    rows = [row(group="g", index=1, size=4, account=2), row(group="g", index=0, size=6, account=1)]

    parts = canonical_existing_parts(rows, original_size=10)

    assert [(p.index, p.size, p.telegram_user_id) for p in parts] == [(0, 6, 1), (1, 4, 2)]


def test_duplicate_db_aliases_do_not_over_count_a_candidate():
    """Repeated metadata registrations point at one real Telegram part."""
    rows = [
        row(group="g", index=0, size=6, file_id="old-alias", message=11),
        row(group="g", index=0, size=6, file_id="new-alias", message=11),
        row(group="g", index=1, size=4, message=12),
    ]

    parts = canonical_existing_parts(rows, original_size=10)

    assert [(part.index, part.message_id, part.file_id) for part in parts] == [
        (0, 11, "new-alias"),
        (1, 12, "file-g-1-1"),
    ]


def test_duplicate_telegram_message_cannot_fill_two_part_slots():
    """A mistakenly repeated Telegram message is not two segments of a file."""
    rows = [row(index=0, size=5, message=11), row(index=1, size=5, message=11)]

    assert canonical_existing_parts(rows, original_size=10) == []


def test_deterministically_prefers_the_same_complete_candidate():
    """Response ordering cannot make dedup choose a different upload generation."""
    first = [row(group="100", index=0, size=10, message=31)]
    second = [row(group="200", index=0, size=10, message=41)]

    assert canonical_existing_parts(second + first, original_size=10)[0].message_id == 31
    assert canonical_existing_parts(first + second, original_size=10)[0].message_id == 31


def test_over_counted_candidate_is_rejected_even_when_indices_are_contiguous():
    """A contiguous candidate with padded/excess rows must match the real bytes exactly."""
    rows = [row(index=0, size=6), row(index=1, size=5)]

    assert canonical_existing_parts(rows, original_size=10) == []


def test_complete_unsplit_row_is_considered_after_split_candidates():
    """A corrupt split group must not hide an exact one-message duplicate."""
    rows = [row(index=0, size=5), row(index=2, size=5), row(size=10, split=False, message=99)]

    parts = canonical_existing_parts(rows, original_size=10)

    assert [(part.index, part.message_id, part.size) for part in parts] == [(0, 99, 10)]


def test_coverage_check_rejects_a_short_result():
    """Registration must fail loudly instead of advertising missing bytes."""
    parts = [UploadedPart(0, 11, "f", None, 9, 1)]

    with pytest.raises(CoverageError, match="cover 9 bytes, expected 10"):
        assert_parts_cover_file(parts, 10)


def test_two_batch_aliases_run_one_producer():
    """Concurrent aliases for one fingerprint share a physical upload."""
    claims = FingerprintClaims()
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def producer():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return [UploadedPart(0, 77, "file", None, 10, 1)]

    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(claims.run, "same:10", producer)
        assert started.wait(timeout=2)
        second = executor.submit(claims.run, "same:10", producer)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]

    assert calls == 1
    assert results[0] == results[1]


def test_failed_claim_wakes_joined_follower_and_retry_claims_fresh_producer():
    """A failed owner wakes followers, then a later batch attempt is allowed."""
    claims = FingerprintClaims()
    started = threading.Event()
    follower_claimed = threading.Event()
    release = threading.Event()
    calls = 0

    claim = claims._claim

    def observe_follower(key):
        future, owner = claim(key)
        if not owner:
            follower_claimed.set()
        return future, owner

    claims._claim = observe_follower

    def failing():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        raise RuntimeError("telegram unavailable")

    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(claims.run, "same:10", failing)
        assert started.wait(timeout=2)
        second = executor.submit(claims.run, "same:10", failing)
        assert follower_claimed.wait(timeout=2)
        release.set()
        for future in (first, second):
            with pytest.raises(RuntimeError, match="telegram unavailable"):
                future.result(timeout=2)

    expected = [UploadedPart(0, 88, "retry", None, 10, 1)]
    assert claims.run("same:10", lambda: expected) == expected
    assert calls == 1


class _DedupApi:
    def __init__(self, rows):
        self.rows = rows
        self.registered = []

    def check_hash(self, _file_hash):
        return {"found": True, "files": self.rows}

    def register(self, **registered):
        self.registered.append(registered)

    def invalidate(self, _parent_id=None):
        pass


def test_dedup_registration_forwards_reused_part_storage_account(tmp_path):
    """A reused secondary-account message must remain routed to that account."""
    archive = tmp_path / "reused.bin"
    archive.write_bytes(b"0123456789")
    api = _DedupApi([row(size=10, account=42, split=False, message=77)])

    gamestage.upload_and_register(
        api, None, archive, "reused.bin", "parent", "application/octet-stream"
    )

    assert api.registered[0]["telegram_user_id"] == 42
