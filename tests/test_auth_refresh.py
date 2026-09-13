import threading
import time

from tdapi import TeleDriveClient


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = "https://backend.example/api/v1"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


class Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self.payload


class RefreshSession:
    def __init__(self, *, reject_refresh=False):
        self.lock = threading.Lock()
        self.refresh_calls = 0
        self.old_calls = 0
        self.reject_refresh = reject_refresh
        self.refresh_headers = []

    def request(self, method, url, **kwargs):
        auth = (kwargs.get("headers") or {}).get("Authorization")
        if url.endswith("/auth/refresh"):
            with self.lock:
                self.refresh_calls += 1
                self.refresh_headers.append(auth)
            # Keep the leader in refresh long enough for sibling 401s to queue
            # behind the condition instead of accidentally serializing the test.
            time.sleep(0.02)
            if self.reject_refresh:
                return Response(401, text="refresh expired")
            return Response(200, {"token": "JWT-NEW"})
        if auth == "Bearer JWT-OLD":
            with self.lock:
                self.old_calls += 1
            return Response(401, text="expired")
        return Response(200, {"ok": True})


def make_client(tmp_path, session):
    api = TeleDriveClient(Cfg(tmp_path))
    api._token = "JWT-OLD"
    api._http_session = lambda: session
    return api


def test_concurrent_401s_perform_exactly_one_refresh(tmp_path):
    session = RefreshSession()
    api = make_client(tmp_path, session)
    results = []
    errors = []

    def call():
        try:
            results.append(api._call("GET", "/files"))
        except Exception as exc:  # pragma: no cover - assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [{"ok": True}] * 8
    assert session.refresh_calls == 1
    assert session.refresh_headers == ["Bearer JWT-OLD"]
    assert api._token == "JWT-NEW"
    assert (tmp_path / "token.txt").read_text(encoding="utf-8") == "JWT-NEW"


def test_refresh_rejection_runs_one_challenge_fallback(tmp_path):
    session = RefreshSession(reject_refresh=True)
    api = make_client(tmp_path, session)
    login_calls = []

    def login(force=False, **_kwargs):
        login_calls.append(force)
        api._token = "JWT-LOGIN"
        return api._token

    api.login = login
    results = []
    threads = [threading.Thread(target=lambda: results.append(api._call("GET", "/files"))) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert session.refresh_calls == 1
    assert login_calls == [True]
    assert results == [{"ok": True}] * 6
