"""GameStager: a unit with a PUT still in flight is never packed."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import gamestage
from config import Config
from gamestage import WRITER_STALE_SECONDS


class _Api:
    pass


class _Engine:
    pass


@pytest.fixture
def stager(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    cfg = Config(
        api_id=1, api_hash="hash", primary_user_id=1, session_dir=session_dir,
        base_url="http://backend", game_folder="game", dir_cache_seconds=60.0,
        host="127.0.0.1", port=0, mount_drive="E:", log_level="WARNING",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
        staging_dir=tmp_path / "staging", upload_dir=tmp_path / "uploads",
        debounce_minutes=0.0, seven_zip="7z-test",
    )
    for path in (cfg.cache_dir, cfg.local_dir, cfg.staging_dir, cfg.pack_dir, cfg.upload_dir):
        path.mkdir(parents=True, exist_ok=True)
    (cfg.staging_dir / "Game").mkdir()
    return gamestage.GameStager(cfg, _Api(), _Engine())


def test_a_unit_with_a_put_in_flight_is_not_due(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    assert "Game" not in stager._due(0.0)
    stager.end_write("Game", stager.cfg.staging_dir / "Game" / "a.bin", ok=True)
    assert "Game" in stager._due(0.0)


def test_one_finished_file_does_not_release_a_unit_whose_other_file_is_still_writing(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    stager.begin_write("Game")
    stager.end_write("Game", stager.cfg.staging_dir / "Game" / "a.bin", ok=True)
    assert "Game" not in stager._due(0.0)


def test_a_failed_put_removes_only_its_own_partial_file(stager):
    folder = stager.cfg.staging_dir / "Game"
    keep, torn = folder / "keep.bin", folder / "torn.bin"
    keep.write_bytes(b"whole")
    torn.write_bytes(b"par")
    stager.touch("Game")
    stager.begin_write("Game")
    stager.end_write("Game", torn, ok=False)
    assert keep.exists() and not torn.exists()


def test_a_stuck_writer_stops_blocking_the_unit_after_the_stale_limit(stager):
    stager.touch("Game")
    stager.begin_write("Game")
    stager._units["Game"].writer_started -= WRITER_STALE_SECONDS + 1
    assert "Game" in stager._due(0.0)
