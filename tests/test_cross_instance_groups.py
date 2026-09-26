from operation_state import GroupBarrierStore


def test_group_is_hidden_until_every_child_is_durable_across_instances(tmp_path):
    first = GroupBarrierStore(tmp_path)
    second = GroupBarrierStore(tmp_path)
    first.create("group", ("a", "b"))

    assert first.mark_durable("group", "a") is False
    assert second.ready("group") is False
    assert second.mark_durable("group", "b") is True
    assert first.ready("group") is True


def test_competing_child_generation_cannot_satisfy_original_group(tmp_path):
    store = GroupBarrierStore(tmp_path)
    store.create("group", ("file@g1", "tail@g1"))

    store.mark_durable("group", "file@g2")
    store.mark_durable("group", "tail@g1")

    assert store.ready("group") is False
    assert store.durable_children("group") == ("tail@g1",)
