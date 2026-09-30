from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from wsgiref.util import setup_testing_defaults

import pytest

from config import Config, load_config
import gamestate
from gamelaunch import GameLauncher, MemorySessionStore, ProcessInfo, ProcessRef
from gamestate import GameRpc


ALLOWED_ORIGIN = "https://teledrive.example"


class Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


class Session:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


def token(user_id=17, exp=None, *, payload=None):
    claims = payload or {"user_id": user_id, "exp": exp or int(time.time()) + 600}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


@pytest.fixture
def game_rpc():
    cfg = Config(
        api_id=1, api_hash="hash", primary_user_id=17, session_dir=Path("."),
        base_url="https://teledrive.example", reina_allowed_origin=ALLOWED_ORIGIN,
        reina_server_url="https://teledrive.example",
    )
    session = Session()
    api = SimpleNamespace(_http_session=lambda: session)
    resolver = SimpleNamespace(cfg=cfg, api=api, pool=SimpleNamespace(
        primary=SimpleNamespace(worker=SimpleNamespace(user_id=17)),
    ))
    return GameRpc(cfg, resolver), session


def request(app, method="GET", path="/rpc/game/state", *, origin=ALLOWED_ORIGIN,
            authorization=None, headers=None, query="", body=b""):
    environ = {}
    setup_testing_defaults(environ)
    environ.update({
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "HTTP_ORIGIN": origin,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    })
    if authorization is not None:
        environ["HTTP_AUTHORIZATION"] = authorization
    if headers:
        for name, value in headers.items():
            environ["HTTP_" + name.upper().replace("-", "_")] = value
    result = {}

    def start_response(status, response_headers, exc_info=None):
        result["status"] = int(status.split(" ", 1)[0])
        result["headers"] = dict(response_headers)

    result["body"] = b"".join(app.handle(environ, start_response))
    return result


def test_preflight_requires_exact_allowed_origin(game_rpc):
    app, session = game_rpc
    response = request(app, "OPTIONS", origin="https://evil.example", headers={
        "access-control-request-method": "GET",
        "access-control-request-headers": "authorization,content-type",
    })
    assert response["status"] == 403
    assert "Access-Control-Allow-Origin" not in response["headers"]
    assert session.calls == []


def test_preflight_does_not_require_bearer(game_rpc):
    app, session = game_rpc
    response = request(app, "OPTIONS", headers={
        "access-control-request-method": "GET",
        "access-control-request-headers": "authorization",
    })
    assert response["status"] == 204
    assert response["headers"]["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
    assert "DELETE" in response["headers"]["Access-Control-Allow-Methods"]
    assert "Authorization" in response["headers"]["Access-Control-Allow-Headers"]
    assert session.calls == []


@pytest.mark.parametrize("origin", ["http://teledrive.example", "https://teledrive.example:444"])
def test_preflight_rejects_scheme_or_port_difference(game_rpc, origin):
    response = request(game_rpc[0], "OPTIONS", origin=origin)
    assert response["status"] == 403


def test_private_network_preflight_is_scoped_to_allowed_origin(game_rpc):
    app = game_rpc[0]
    allowed = request(app, "OPTIONS", headers={
        "access-control-request-method": "GET",
        "access-control-request-private-network": "true",
    })
    denied = request(app, "OPTIONS", origin="https://evil.example", headers={
        "access-control-request-private-network": "true",
    })
    assert allowed["headers"]["Access-Control-Allow-Private-Network"] == "true"
    assert denied["status"] == 403
    assert "Access-Control-Allow-Private-Network" not in denied["headers"]


def test_missing_or_malformed_bearer_is_unauthorized_without_logging_token(game_rpc, caplog):
    app, session = game_rpc
    assert request(app, authorization=None)["status"] == 401
    secretish = "malformed-secret-token"
    with caplog.at_level(logging.DEBUG):
        response = request(app, authorization=f"Bearer {secretish}")
    assert response["status"] == 401
    assert secretish not in caplog.text
    assert session.calls == []


def test_auth_checks_browser_token_upstream_then_accepts_matching_owner(game_rpc):
    app, session = game_rpc
    response = request(app, authorization=f"Bearer {token()}")
    assert response["status"] == 501
    assert session.calls[0][0:2] == ("GET", "https://teledrive.example/api/v1/folders")
    assert session.calls[0][2]["headers"]["Authorization"] == f"Bearer {token()}"
    assert session.calls[0][2]["timeout"] == 10
    assert response["headers"]["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN


def test_wrong_user_is_forbidden_only_after_upstream_success(game_rpc):
    app, session = game_rpc
    response = request(app, authorization=f"Bearer {token(user_id=999)}")
    assert response["status"] == 403
    assert len(session.calls) == 1
    session.response = Response(401)
    app._token_cache.clear()
    response = request(app, authorization=f"Bearer {token(user_id=999)}")
    assert response["status"] == 401


def test_token_cache_is_hashed_and_reuses_upstream_validation(game_rpc):
    app, session = game_rpc
    browser_token = token()
    for _ in range(2):
        assert request(app, authorization=f"Bearer {browser_token}")["status"] == 501
    assert len(session.calls) == 1
    assert browser_token not in app._token_cache


def test_token_cache_lifetime_never_exceeds_jwt_exp(game_rpc, monkeypatch):
    app, _ = game_rpc
    monkeypatch.setattr(gamestate.time, "time", lambda: 100.25)
    browser_token = token(exp=103)
    assert request(app, authorization=f"Bearer {browser_token}")["status"] == 501
    expiry = next(iter(app._token_cache.values()))[1]
    assert expiry <= 103
    assert expiry - 100.25 <= 3


def test_upstream_failure_maps_to_503_with_cors(game_rpc):
    app, session = game_rpc
    session.error = TimeoutError("upstream timed out")
    response = request(app, authorization=f"Bearer {token()}")
    assert response["status"] == 503
    assert response["headers"]["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN


def test_upstream_server_error_maps_to_503(game_rpc):
    app, session = game_rpc
    session.response = Response(502)
    response = request(app, authorization=f"Bearer {token()}")
    assert response["status"] == 503
    assert response["headers"]["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN


def test_disallowed_origin_is_rejected_before_auth(game_rpc):
    app, session = game_rpc
    response = request(app, origin="https://evil.example", authorization="Bearer junk")
    assert response["status"] == 403
    assert session.calls == []


def test_unconfigured_browser_rpc_is_disabled(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[telegram]\napi_id=123\napi_hash=hash\nprimary_user_id=123\nsession_dir=sessions\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    cfg = load_config(config_path)
    assert cfg.reina_allowed_origin == ""
    assert cfg.reina_server_url == ""
    assert cfg.reina_locale_emulator == ""


def test_config_origin_trailing_slash_is_normalized(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[telegram]\napi_id=123\napi_hash=hash\nprimary_user_id=123\nsession_dir=sessions\n"
        "[reina]\nallowed_origin=https://teledrive.example/\n"
        "server_url=https://server.example/\nlocale_emulator=\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    cfg = load_config(config_path)
    assert cfg.reina_allowed_origin == "https://teledrive.example"
    assert cfg.reina_server_url == "https://server.example/"
    assert cfg.reina_locale_emulator == ""


def test_game_rpc_repeated_paths_and_background_fetch_cancel(tmp_path):
    cfg = Config(
        api_id=1, api_hash="hash", primary_user_id=17, session_dir=tmp_path,
        base_url="https://teledrive.example", reina_allowed_origin=ALLOWED_ORIGIN,
        reina_server_url="https://teledrive.example", game_folder="game",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
    )
    session = Session()
    api = SimpleNamespace(_http_session=lambda: session)
    resolver = SimpleNamespace(cfg=cfg, api=api, pool=SimpleNamespace(
        primary=SimpleNamespace(worker=SimpleNamespace(user_id=17)),
    ))
    started = threading.Event()
    release = threading.Event()
    root = cfg.local_dir / "A,B"

    class Fetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, _segments, *, skip_existing, cancel):
            assert skip_existing
            started.set()
            yield "PROGRESS 4 9 1/1 progress"
            release.wait(2)
            if cancel.is_set():
                yield "CANCELLED download cancelled"
            else:
                root.mkdir(parents=True, exist_ok=True)
                yield f"OK {root}"

    app = GameRpc(cfg, resolver, Fetcher())
    auth = f"Bearer {token()}"
    paths = request(
        app, authorization=auth,
        query="paths=game%2FA%2CB&paths=game%2F%E9%81%8A%E6%88%B2",
    )
    assert paths["status"] == 200
    assert [game["path"] for game in json.loads(paths["body"])["games"]] == ["game/A,B", "game/遊戲"]

    started_request = request(
        app, "POST", "/rpc/game/fetch", authorization=auth,
        body=json.dumps({"path": "game/A,B"}).encode(),
    )
    assert started_request["status"] == 202
    assert started.wait(2)
    duplicate = request(
        app, "POST", "/rpc/game/fetch", authorization=auth,
        body=json.dumps({"path": "game/A,B"}).encode(),
    )
    assert duplicate["status"] == 202
    assert app.state._jobs["game/A,B"].thread is not None
    canceled = request(app, "DELETE", "/rpc/game/fetch", authorization=auth, query="path=game%2FA%2CB")
    assert canceled["status"] == 202
    release.set()
    assert app.state._jobs["game/A,B"].finished.wait(2)

    invalid = request(app, authorization=auth, query="paths=game%2F..%2Fsecret")
    assert invalid["status"] == 400
    assert json.loads(invalid["body"])["code"] == "invalid_game_path"


def test_game_exe_and_launch_rpc_use_relative_paths_and_return_session_id(tmp_path):
    cfg = Config(
        api_id=1, api_hash="hash", primary_user_id=17, session_dir=tmp_path,
        base_url="https://teledrive.example", reina_allowed_origin=ALLOWED_ORIGIN,
        reina_server_url="https://teledrive.example", game_folder="game",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
        reina_locale_emulator=str(tmp_path / "LEProc.exe"),
    )
    Path(cfg.reina_locale_emulator).write_bytes(b"launcher")
    root = cfg.local_dir / "GameA"
    (root / "bin").mkdir(parents=True)
    (root / ".reina-complete").write_text("ready", encoding="utf-8")
    (root / "bin" / "game.exe").write_bytes(b"exe")
    session = Session()
    api = SimpleNamespace(_http_session=lambda: session)
    resolver = SimpleNamespace(cfg=cfg, api=api, pool=SimpleNamespace(
        primary=SimpleNamespace(worker=SimpleNamespace(user_id=17)),
    ))

    class Fetcher:
        def destination_for(self, _segments):
            return root

        def fetch_segments(self, *_args, **_kwargs):
            return iter(())

    class Adapter:
        def __init__(self):
            self.items = []

        def snapshot_all(self):
            return list(self.items)

        def process_ref(self, pid):
            return next((item.ref for item in self.items if item.ref.pid == pid), None)

        def is_alive(self, ref):
            return any(item.ref == ref for item in self.items)

        def exe_path(self, pid):
            return next((item.exe_path for item in self.items if item.ref.pid == pid), None)

        def parent_pid(self, pid):
            return next((item.parent_pid for item in self.items if item.ref.pid == pid), None)

        def spawn(self, command, cwd):
            self.items.append(ProcessInfo(ProcessRef(222, 2000.0), command[-1], 1))
            return SimpleNamespace(pid=222)

    adapter = Adapter()
    state = gamestate.GameState(cfg, resolver, Fetcher())
    launcher = GameLauncher(
        state, locale_emulator=cfg.reina_locale_emulator,
        store=MemorySessionStore(), process_adapter=adapter,
        clock=lambda: 2000.0, start_monitor=False,
    )
    app = GameRpc(cfg, resolver, Fetcher(), game_launcher=launcher)
    auth = f"Bearer {token()}"

    exes = request(app, authorization=auth, path="/rpc/game/exes", query="path=game%2FGameA")
    assert exes["status"] == 200
    assert json.loads(exes["body"]) == {"exes": ["bin/game.exe"]}

    launched = request(
        app, "POST", "/rpc/game/launch", authorization=auth,
        body=json.dumps({
            "path": "game/GameA", "exe_relpath": "bin/game.exe",
            "game_id": 81, "locale_emulator": False,
        }).encode(),
    )
    assert launched["status"] == 200
    result = json.loads(launched["body"])
    assert result["session_id"]

    state_response = request(app, authorization=auth, query="paths=game%2FGameA")
    game = json.loads(state_response["body"])["games"][0]
    assert game["status"] == "running"
    assert game["elapsed_seconds"] == 0
    assert game["capabilities"] == {"locale_emulator": True}
