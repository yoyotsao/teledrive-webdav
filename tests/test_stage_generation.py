from pathlib import Path

from operation_state import StageGenerationStore


def test_old_generation_cleanup_cannot_delete_newer_generation(tmp_path):
    store = StageGenerationStore(tmp_path)
    old = store.begin("folder/file.bin", "file.bin")
    store.source(old).write_bytes(b"old")
    new = store.begin("folder/file.bin", "file.bin")
    store.source(new).write_bytes(b"new")

    assert store.cleanup(old) is False
    assert store.source(new).read_bytes() == b"new"
    assert store.active("folder/file.bin") == new


def test_stale_close_handle_is_generation_scoped(tmp_path):
    store = StageGenerationStore(tmp_path)
    first = store.begin("x.bin", "x.bin")
    second = store.begin("x.bin", "x.bin")

    assert store.finish(first) is False
    assert store.active("x.bin") == second
    assert store.finish(second) is True


def test_startup_recovery_purges_partial_generation_but_keeps_durable_one(tmp_path):
    store = StageGenerationStore(tmp_path)
    partial = store.begin("partial.bin", "partial.bin")
    store.source(partial).write_bytes(b"partial")
    ready = store.begin("ready.bin", "ready.bin")
    store.source(ready).write_bytes(b"ready")
    store.mark_durable(ready)

    recovered = StageGenerationStore(tmp_path).recover()

    assert ready in recovered
    assert not store.source(partial).exists()
    assert store.source(ready).read_bytes() == b"ready"
