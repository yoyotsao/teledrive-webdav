"""Offline tests for bot-challenge login.

The backend dropped ``POST /auth/login`` (which took a Telethon StringSession)
in favour of a bot-mediated challenge: ask for a nonce, DM it to the bot from
the account being authenticated, then trade the nonce for a JWT. The backend
never sees an auth_key that way -- which means the bridge cannot just hand over
``cfg.session`` any more, it has to *be* the Telegram client that sends the DM.

These pin that handshake without a backend or a Telegram connection.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tdapi import ApiError, TeleDriveClient  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for requests.Session, scripted per URL suffix."""

    def __init__(self):
        self.calls = []
        self.verify_replies = []

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append(("POST", url, json))
        if url.endswith("/auth/challenge"):
            return FakeResponse(200, {
                "nonce": "NONCE-1",
                "bot_username": "TDSessionVerifybot",
                "expires_in": 120,
            })
        if url.endswith("/auth/verify"):
            return self.verify_replies.pop(0)
        raise AssertionError(f"unexpected POST {url}")

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("params")))
        return FakeResponse(200, {"items": []})


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.session = "SESSION-STRING"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


def client(tmp_path, session):
    api = TeleDriveClient(Cfg(tmp_path))
    api._http_session = lambda: session
    return api


def test_login_dms_the_nonce_and_returns_the_jwt(tmp_path):
    sess = FakeSession()
    sess.verify_replies = [FakeResponse(200, {"token": "JWT-1", "user_id": 42})]
    api = client(tmp_path, sess)

    sent = []
    api.set_dm_sender(lambda username, text: sent.append((username, text)))

    assert api.login() == "JWT-1"
    # The nonce goes to the bot the challenge named, verbatim: the backend
    # matches on exact message text.
    assert sent == [("TDSessionVerifybot", "NONCE-1")]
    # And it is the challenge's nonce that is redeemed, not the session string.
    verify = [c for c in sess.calls if c[1].endswith("/auth/verify")]
    assert verify[0][2] == {"nonce": "NONCE-1"}
    assert not any("/auth/login" in c[1] for c in sess.calls)


def test_session_string_never_reaches_the_backend(tmp_path):
    """The whole point of the backend's change -- pin it so a future refactor
    cannot quietly reintroduce the old handshake."""
    sess = FakeSession()
    sess.verify_replies = [FakeResponse(200, {"token": "JWT-1"})]
    api = client(tmp_path, sess)
    api.set_dm_sender(lambda username, text: None)
    api.login()

    assert not any("SESSION-STRING" in repr(call) for call in sess.calls)


def test_verify_202_keeps_polling(tmp_path):
    """202 means the bot has not seen the DM yet -- that is normal, the update
    arrives via a long-poll on the backend's side."""
    sess = FakeSession()
    sess.verify_replies = [
        FakeResponse(202, {"status": "waiting"}),
        FakeResponse(202, {"status": "waiting"}),
        FakeResponse(200, {"token": "JWT-2"}),
    ]
    api = client(tmp_path, sess)
    api.set_dm_sender(lambda username, text: None)

    slept = []
    assert api.login(_sleep=slept.append) == "JWT-2"
    assert len(slept) == 2


def test_login_persists_and_reuses_the_token(tmp_path):
    sess = FakeSession()
    sess.verify_replies = [FakeResponse(200, {"token": "JWT-3"})]
    api = client(tmp_path, sess)
    api.set_dm_sender(lambda username, text: None)
    api.login()

    # A second bridge start must not DM the bot again: a 24h JWT survives a
    # restart, and every login leaves a nonce in the user's chat with the bot.
    again = client(tmp_path, FakeSession())
    assert again._token == "JWT-3"
    assert again.login() == "JWT-3"


def test_login_without_a_dm_sender_is_a_clear_error(tmp_path):
    """warmup.py's CLI builds the client too. Failing here with the reason beats
    hanging for two minutes on a nonce nobody will ever send."""
    api = client(tmp_path, FakeSession())
    with pytest.raises(RuntimeError, match="Telegram"):
        api.login()


def test_expired_challenge_surfaces_the_backend_error(tmp_path):
    sess = FakeSession()
    sess.verify_replies = [FakeResponse(401, None, text="Invalid or expired challenge")]
    api = client(tmp_path, sess)
    api.set_dm_sender(lambda username, text: None)
    with pytest.raises(ApiError):
        api.login(_sleep=lambda s: None)


def test_concurrent_logins_send_one_nonce(tmp_path):
    """_call retries on 401 from every rclone thread at once. Without the lock
    each one would DM the bot a nonce of its own."""
    sess = FakeSession()
    sess.verify_replies = [FakeResponse(200, {"token": "JWT-4"})] * 8
    api = client(tmp_path, sess)
    sent = []
    api.set_dm_sender(lambda username, text: sent.append(text))

    results = []
    threads = [threading.Thread(target=lambda: results.append(api.login())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["JWT-4"] * 8
    assert len(sent) == 1
