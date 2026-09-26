"""The backend drops idle keep-alive connections; the bridge must not turn that
into a 500.

`requests.Session` pools connections to the backend. uvicorn closes an idle one
after a few seconds, so any gap between metadata calls leaves a dead socket in
the pool and the next request raises RemoteDisconnected before the server has
seen a single byte. Without a retry that surfaces as a 500 on /rpc/thumb -- and
a 500 there is not a slow thumbnail, it is the shell falling back to the built-in
handler, which reads the *whole* original off Telegram. One dropped socket
measured as a 6.8 MB sequential download, and enough of them starve the
connection pool into FLOOD_WAIT.
"""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tdapi  # noqa: E402
from tdapi import ApiError, TeleDriveClient  # noqa: E402


class Reply:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = b"{}"

    def json(self):
        return self._payload


class Session:
    """Scripted transport: each entry is either an exception to raise or a Reply."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append(("POST", url))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.session = "S"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


def client(tmp_path, script):
    api = TeleDriveClient(Cfg(tmp_path))
    session = Session(script)
    api._http_session = lambda: session
    api._token = "JWT"  # skip the challenge; this file is about the transport
    return api, session


def dropped():
    return requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
    )


def test_a_dropped_idle_connection_is_retried(tmp_path):
    api, session = client(tmp_path, [dropped(), Reply(200, {"items": [1]})])
    assert api._call("GET", "/files") == {"items": [1]}
    assert len(session.calls) == 2


def test_the_retry_is_not_infinite(tmp_path):
    """A backend that is actually down must fail fast, not hold the shell."""
    api, session = client(tmp_path, [dropped(), dropped()])
    with pytest.raises(requests.exceptions.ConnectionError):
        api._call("GET", "/files")
    assert len(session.calls) == 2


def test_a_write_is_retried_too(tmp_path):
    """Safe by construction: RemoteDisconnected here means the request never
    reached the app, so nothing was registered twice."""
    api, session = client(tmp_path, [dropped(), Reply(200, {"file_id": "f1"})])
    assert api._call("POST", "/files/register", payload={"n": 1}) == {"file_id": "f1"}
    assert len(session.calls) == 2


def test_the_401_relogin_still_works_alongside_it(tmp_path):
    api, _ = client(tmp_path, [
        Reply(401, text="Authentication required"),
        Reply(401, text="refresh grace rejected"),                                # refresh
        Reply(200, {"nonce": "n", "bot_username": "b", "expires_in": 120}),  # challenge
        Reply(200, {"token": "JWT2"}),                                          # verify
        Reply(200, {"items": []}),
    ])
    api.set_dm_sender(lambda u, t: None)
    assert api._call("GET", "/files") == {"items": []}


def test_a_real_http_error_is_not_retried(tmp_path):
    """403 is an answer, not a dropped socket -- retrying only doubles the cost."""
    api, session = client(tmp_path, [Reply(403, text="nope")])
    with pytest.raises(ApiError):
        api._call("GET", "/files")
    assert len(session.calls) == 1


def test_a_dropped_socket_does_not_spend_the_relogin_budget(tmp_path):
    """The connection retry, refresh and challenge budgets are independent."""
    api, _ = client(tmp_path, [
        dropped(),                                                            # dead pooled socket
        Reply(401, text="Authentication required"),                           # fresh socket, stale JWT
        Reply(401, text="refresh grace rejected"),                           # refresh
        Reply(200, {"nonce": "n", "bot_username": "b", "expires_in": 120}),
        Reply(200, {"token": "JWT2"}),
        Reply(200, {"items": ["ok"]}),
    ])
    api.set_dm_sender(lambda u, t: None)
    assert api._call("GET", "/files") == {"items": ["ok"]}


def test_a_relogin_does_not_spend_the_connection_budget(tmp_path):
    """Re-authenticating must not consume the independent socket retry."""
    api, _ = client(tmp_path, [
        Reply(401, text="Authentication required"),
        Reply(401, text="refresh grace rejected"),
        Reply(200, {"nonce": "n", "bot_username": "b", "expires_in": 120}),
        Reply(200, {"token": "JWT2"}),
        dropped(),                        # the pool handed out another dead one
        Reply(200, {"items": ["ok"]}),
    ])
    api.set_dm_sender(lambda u, t: None)
    assert api._call("GET", "/files") == {"items": ["ok"]}


def test_each_thread_gets_its_own_http_session(tmp_path, monkeypatch):
    sessions = []

    class NewSession:
        pass

    def create_session():
        session = NewSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(tdapi.requests, "Session", create_session)
    api = TeleDriveClient(Cfg(tmp_path))
    main_session = api._http_session()
    from_thread = []

    import threading

    thread = threading.Thread(target=lambda: from_thread.append(api._http_session()))
    thread.start()
    thread.join()

    assert from_thread == [sessions[1]]
    assert main_session is sessions[0]
    assert from_thread[0] is not main_session
