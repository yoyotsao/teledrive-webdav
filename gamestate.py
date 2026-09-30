"""瀏覽器遊戲 RPC 的來源限制、上游驗證與路由骨架。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger("bridge.game_rpc")


class GameRpcError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class GameRpc:
    ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"
    ALLOWED_HEADERS = "Authorization, Content-Type"

    def __init__(self, cfg, resolver):
        self.cfg = cfg
        self.resolver = resolver
        self.api = resolver.api
        self._token_cache: dict[str, tuple[int, float]] = {}
        self._cache_lock = threading.Lock()

    def _configured_origin(self) -> str:
        origin = self.cfg.reina_allowed_origin
        try:
            parsed = urlsplit(origin)
            _port = parsed.port
        except ValueError:
            return ""
        if (
            not self.cfg.reina_server_url
            or not origin
            or "*" in origin
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return ""
        return origin

    def _origin_allowed(self, origin: str) -> bool:
        configured = self._configured_origin()
        return bool(configured and origin == configured)

    def _validate_with_teledrive(self, token):
        return self.api._http_session().request(
            "GET",
            f"{self.cfg.api_base}/folders",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            allow_redirects=False,
        )

    def verify_browser_token(self, token: str, origin: str) -> int:
        if not self._origin_allowed(origin):
            raise GameRpcError(403, "origin not allowed")
        if not token or len(token.split(".")) != 3:
            raise GameRpcError(401, "invalid bearer token")

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._cache_lock:
            cached = self._token_cache.get(token_hash)
            if cached and cached[1] > now:
                return cached[0]
            self._token_cache.pop(token_hash, None)

        try:
            response = self._validate_with_teledrive(token)
        except Exception as exc:
            log.warning("browser token validation unavailable: %s", type(exc).__name__)
            raise GameRpcError(503, "authentication service unavailable") from None

        if response.status_code in (401, 403):
            raise GameRpcError(401, "invalid bearer token")
        if response.status_code != 200:
            raise GameRpcError(503, "authentication service unavailable")

        try:
            payload_segment = token.split(".")[1]
            payload_bytes = base64.urlsafe_b64decode(
                payload_segment + "=" * (-len(payload_segment) % 4)
            )
            claims = json.loads(payload_bytes)
            user_id = int(claims["user_id"])
            exp = int(claims["exp"])
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
            raise GameRpcError(401, "invalid bearer token") from None

        ttl = min(300, exp - now)
        if ttl > 0:
            with self._cache_lock:
                self._token_cache[token_hash] = (user_id, now + ttl)
        return user_id

    def handle(self, environ, start_response):
        origin = environ.get("HTTP_ORIGIN", "")
        if not self._configured_origin():
            return self._response(start_response, 503, {"error": "browser game RPC is disabled"})
        if not self._origin_allowed(origin):
            return self._response(start_response, 403, {"error": "origin not allowed"})

        cors = self._cors_headers(environ, origin)
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method == "OPTIONS":
            requested_method = environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "").upper()
            if requested_method and requested_method not in {"GET", "POST", "DELETE"}:
                return self._response(start_response, 403, {"error": "method not allowed"})
            return self._response(start_response, 204, None, cors)

        authorization = environ.get("HTTP_AUTHORIZATION", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or " " in token:
            return self._response(start_response, 401, {"error": "bearer token required"}, cors)

        try:
            owner_id = self.verify_browser_token(token, origin)
            expected_id = int(self.resolver.pool.primary.worker.user_id)
            if owner_id != expected_id:
                raise GameRpcError(403, "user does not own this bridge")
        except GameRpcError as exc:
            return self._response(start_response, exc.status, {"error": exc.message}, cors)

        route = environ.get("PATH_INFO", "")
        if route in ("/rpc/game/state", "/rpc/game/fetch", "/rpc/game/exes", "/rpc/game/launch"):
            return self._response(start_response, 501, {"error": "game RPC is not implemented"}, cors)
        return self._response(start_response, 404, {"error": "no such game RPC"}, cors)

    def _cors_headers(self, environ, origin):
        headers = [
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Methods", self.ALLOWED_METHODS),
            ("Access-Control-Allow-Headers", self.ALLOWED_HEADERS),
            ("Access-Control-Max-Age", "600"),
            ("Vary", "Origin"),
        ]
        if environ.get("HTTP_ACCESS_CONTROL_REQUEST_PRIVATE_NETWORK", "").lower() == "true":
            headers.append(("Access-Control-Allow-Private-Network", "true"))
        return headers

    def _response(self, start_response, status, body, headers=()):
        reasons = {
            204: "No Content",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            501: "Not Implemented",
            503: "Service Unavailable",
        }
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        response_headers = list(headers)
        if body is not None:
            response_headers.append(("Content-Type", "application/json; charset=utf-8"))
        response_headers.append(("Content-Length", str(len(payload))))
        start_response(f"{status} {reasons[status]}", response_headers)
        return [payload]
