from transfer_models import (
    DurableOperationIdentity,
    FrozenStorageTarget,
    GroupSendManifest,
    QueueStage,
    StagingIdentity,
)


def test_recovery_queue_stages_are_explicit():
    assert QueueStage.RECOVERING.value == "recovering"
    assert QueueStage.UNCERTAIN.value == "uncertain"


def test_same_logical_key_generations_have_distinct_transfer_and_source_identity():
    first = StagingIdentity("/docs/a.bin", "t-1", 1, "/stage/a.bin.1.t-1")
    second = StagingIdentity("/docs/a.bin", "t-2", 2, "/stage/a.bin.2.t-2")

    assert first.logical_key == second.logical_key
    assert first.transfer_id != second.transfer_id
    assert first.staging_generation != second.staging_generation
    assert first.source_path != second.source_path


def test_group_manifest_preserves_child_order_and_frozen_target():
    target = FrozenStorageTarget(
        storage_mode="channel",
        channel_id="-100123",
        target_peer_key="channel:-100123",
        target_version=4,
        accounts_version=8,
        primary_account_id=1,
        linked_account_ids=(1, 2),
    )
    children = (
        DurableOperationIdentity("op-b", 102, 2, "messages.sendMedia", part_index=1),
        DurableOperationIdentity("op-a", 101, 1, "messages.sendMedia", part_index=0),
    )
    manifest = GroupSendManifest("group-1", target, children)

    assert tuple(child.operation_id for child in manifest.children) == ("op-b", "op-a")
    assert tuple(child.random_id for child in manifest.children) == (102, 101)
    assert tuple(child.uploader_id for child in manifest.children) == (2, 1)
    assert manifest.target is target
    assert manifest.send_armed is False
    assert manifest.send_started is False
