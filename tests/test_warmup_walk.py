"""One failing folder listing must not cost the whole warm-up pass.

Measured 2026-11-14: three passes in a row died on a single backend read
timeout somewhere in ~7,400 listings, so the shell warm never ran at all.
"""

from types import SimpleNamespace

import fileid
import warmup


def entry(name, file_id, is_dir=False, message_id=1):
    return SimpleNamespace(name=name, file_id=file_id, is_dir=is_dir,
                           message_id=None if is_dir else message_id)


class FakeApi:
    def __init__(self, tree, fail=None):
        self.tree = tree
        self.fail = dict(fail or {})  # parent_id -> how many calls fail first
        self.calls = []

    def list_dir(self, parent_id, *, fresh=False):
        self.calls.append(parent_id)
        if self.fail.get(parent_id, 0) > 0:
            self.fail[parent_id] -= 1
            raise TimeoutError("read timed out")
        return self.tree[parent_id]


TREE = {
    None: [entry("a", "A", is_dir=True), entry("b", "B", is_dir=True), entry("root.jpg", "r")],
    "A": [entry("a1.jpg", "a1")],
    "B": [entry("b1.jpg", "b1")],
}


def test_a_folder_that_fails_twice_is_skipped_and_the_rest_is_walked(caplog):
    api = FakeApi(TREE, fail={"A": 2})
    out = []
    with caplog.at_level("WARNING", logger="warmup"):
        warmup.walk(api, None, "", out)
    assert [p for p, _ in out] == ["/b/b1.jpg", "/root.jpg"]
    assert fileid.tag("a") in caplog.text


def test_a_single_transient_failure_is_retried():
    api = FakeApi(TREE, fail={"B": 1})
    out = []
    warmup.walk(api, None, "", out)
    assert sorted(p for p, _ in out) == ["/a/a1.jpg", "/b/b1.jpg", "/root.jpg"]
    assert api.calls.count("B") == 2


def test_stop_request_still_ends_the_walk():
    api = FakeApi(TREE)
    out = []
    warmup.walk(api, None, "", out, before=lambda: False)
    assert out == [] and api.calls == []
