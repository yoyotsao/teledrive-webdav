"""瀏覽器遊戲 RPC 的來源限制、上游驗證與路由骨架。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gamelaunch import GameLauncher, LaunchError

log = logging.getLogger("bridge.game_rpc")


def _safe_download_error(message: str) -> str:
    """保留可顯示的失敗原因，同時移除 credentials 與本機絕對路徑。"""
    from upload_engine import redact

    message = str(message).strip()
    if message.startswith("ERROR "):
        message = message[6:].strip()
    message = redact(message)
    message = re.sub(
        r"(?i)(['\"])(?:[A-Z]:[\\/]|\\\\(?:(?:\?|\.)\\UNC\\)?[^\\/'\"]+\\[^\\/'\"]+|//(?:\?/UNC/)?[^/'\"]+/[^/'\"]+).*?\1",
        "[local path]",
        message,
    )
    # 未加引號時不可靠地切分含空格路徑；保守遮蔽絕對路徑起點之後的整行。
    message = re.sub(r"(?i)(?<![\w:])(?:[A-Z]:[\\/]|\\\\|//)[^\r\n]*$", "[local path]", message)
    message = re.sub(r"(?<![:\w])/(?:[^/\s:]+/)*[^/\s:]+", "[local path]", message)
    message = re.sub(
        r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "[redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message).strip()
    return message[:500] or "Download failed"


class GameStateError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class DownloadJob:
    def __init__(self, path: str, started_at: float):
        self.path = path
        self.started_at = started_at
        self.completed_bytes = 0
        self.total_bytes = 0
        self.error: str | None = None
        self.cancel = threading.Event()
        self.finished = threading.Event()
        self.thread: threading.Thread | None = None


class GameState:
    """管理 canonical 遊戲路徑的背景下載與本機完成狀態。"""

    COMPLETE_MARKER = ".reina-complete"

    def __init__(self, cfg, resolver, fetcher, running_provider=None):
        self.cfg = cfg
        self.resolver = resolver
        self.fetcher = fetcher
        self.running_provider = running_provider
        self.path_map_file = Path(cfg.cache_dir) / "reina-games.json"
        self._lock = threading.RLock()
        self._jobs: dict[str, DownloadJob] = {}
        self._roots: dict[str, str] = {}
        self.path_map_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            stored = json.loads(self.path_map_file.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._roots = {str(path): str(root) for path, root in stored.items() if isinstance(root, str)}
        except (OSError, ValueError, TypeError):
            self._roots = {}

    def canonical_game_segments(self, path: str) -> list[str]:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "\\"))
            or "\\" in path
            or "\x00" in path
        ):
            raise GameStateError(400, "invalid_game_path", "invalid game path")
        segments = path.split("/")
        if (
            len(segments) < 2
            or any(segment in ("", ".", "..") for segment in segments)
            or segments[0] != self.cfg.game_folder
        ):
            raise GameStateError(400, "invalid_game_path", "invalid game path")
        return segments

    def _root_for(self, path: str, segments: list[str]) -> Path:
        with self._lock:
            mapped = self._roots.get(path)
        if mapped:
            root = Path(mapped)
            try:
                root.resolve().relative_to(Path(self.cfg.local_dir).resolve())
                return root
            except (OSError, ValueError):
                pass
        return self.fetcher.destination_for(segments)

    def _is_running(self, path: str) -> bool:
        providers = [self.running_provider]
        launcher = getattr(self, "launcher", None)
        if launcher is not None and launcher is not self.running_provider:
            providers.append(launcher)
        for provider in providers:
            if provider is None:
                continue
            try:
                if callable(provider):
                    if provider(path):
                        return True
                elif provider.is_running(path):
                    return True
            except Exception:
                log.exception("running provider failed for a game path")
        return False

    def _state_for(self, path: str) -> dict:
        segments = self.canonical_game_segments(path)
        try:
            root = self._root_for(path, segments)
        except FileNotFoundError:
            # 遠端已刪除或改名：這是遊戲狀態（absent），不是 bridge 連線失敗
            root = None
        except Exception:
            log.exception("resolving the local game root failed")
            raise GameStateError(503, "bridge_backend_unavailable", "TeleDrive backend is unavailable") from None
        with self._lock:
            job = self._jobs.get(path)
            job_active = job is not None and not job.finished.is_set()
            if job_active:
                status = "downloading"
                completed = job.completed_bytes
                total = job.total_bytes
                elapsed = max(0, int(time.time() - job.started_at))
                error = job.error
            else:
                completed = 0
                total = 0
                elapsed = 0
                error = job.error if job is not None else None
        running = self._is_running(path)
        if running:
            status = "running"
            elapsed_provider = getattr(getattr(self, "launcher", None), "elapsed_seconds", None)
            if not callable(elapsed_provider):
                elapsed_provider = getattr(self.running_provider, "elapsed_seconds", None)
            if callable(elapsed_provider):
                try:
                    elapsed = max(0, int(elapsed_provider(path)))
                except Exception:
                    log.exception("running elapsed provider failed for a game path")
                    elapsed = 0
        elif not job_active and root is not None and (root / self.COMPLETE_MARKER).is_file():
            status = "ready"
        elif not job_active and root is not None and root.exists():
            status = "incomplete"
        elif not job_active:
            status = "absent"
        capability_provider = getattr(self, "launcher", None) or self.running_provider
        capability = getattr(capability_provider, "locale_emulator_available", None)
        if capability is None:
            configured_le = getattr(self.cfg, "reina_locale_emulator", "")
            try:
                locale_available = bool(configured_le and Path(configured_le).resolve(strict=True).is_file())
            except OSError:
                locale_available = False
        else:
            locale_available = bool(capability)
        return {
            "path": path,
            "status": status,
            "completed_bytes": completed,
            "total_bytes": total,
            "elapsed_seconds": elapsed,
            "error": error,
            "capabilities": {"locale_emulator": locale_available},
        }

    def states(self, paths: list[str]) -> list[dict]:
        return [self._state_for(path) for path in paths]

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _save_root(self, path: str, root: Path) -> None:
        with self._lock:
            self._roots[path] = str(root)
            self._atomic_write(
                self.path_map_file,
                json.dumps(self._roots, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )

    def fetch(self, path: str) -> dict:
        segments = self.canonical_game_segments(path)
        with self._lock:
            current = self._jobs.get(path)
            if current is not None and not current.finished.is_set():
                return self._job_state(current)
            job = DownloadJob(path, time.time())
            self._jobs[path] = job
            thread = threading.Thread(
                target=self._run_download,
                args=(job, segments),
                name="game-download",
                daemon=True,
            )
            job.thread = thread
            thread.start()
            return self._job_state(job)

    def _job_state(self, job: DownloadJob) -> dict:
        return {
            "path": job.path,
            "status": "downloading" if not job.finished.is_set() else "incomplete",
            "completed_bytes": job.completed_bytes,
            "total_bytes": job.total_bytes,
            "elapsed_seconds": max(0, int(time.time() - job.started_at)),
            "error": job.error,
        }

    def _run_download(self, job: DownloadJob, segments: list[str]) -> None:
        success = False
        try:
            root = self.fetcher.destination_for(segments)
            marker = root / self.COMPLETE_MARKER
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
            for line in self.fetcher.fetch_segments(segments, skip_existing=True, cancel=job.cancel):
                if line.startswith("PROGRESS "):
                    fields = line.split(" ", 3)
                    try:
                        with self._lock:
                            job.completed_bytes = int(fields[1])
                            job.total_bytes = int(fields[2])
                    except (IndexError, ValueError):
                        continue
                elif line.startswith("CANCELLED"):
                    break
                elif line.startswith("ERROR "):
                    with self._lock:
                        job.error = _safe_download_error(line)
                    break
                elif line.startswith("OK ") and not job.cancel.is_set():
                    success = True
            with self._lock:
                if success and not job.cancel.is_set():
                    self._save_root(job.path, root)
                    self._atomic_write(marker, f"{job.path}\n".encode("utf-8"))
                elif not job.cancel.is_set() and job.error is None:
                    job.error = "Download ended without a completion marker"
                job.finished.set()
        except Exception as exc:
            message = _safe_download_error(str(exc) or type(exc).__name__)
            with self._lock:
                job.error = message
                job.finished.set()
            log.warning("game download failed: %s", message)
        finally:
            with self._lock:
                job.finished.set()

    def cancel(self, path: str) -> dict:
        self.canonical_game_segments(path)
        with self._lock:
            job = self._jobs.get(path)
            if job is None or job.finished.is_set():
                raise GameStateError(409, "download_not_active", "download is not active")
            job.cancel.set()
            return self._job_state(job)


class GameRpcError(Exception):
    def __init__(self, status: int, message: str, code: str = "game_rpc_error"):
        self.status = status
        self.message = message
        self.code = code
        super().__init__(message)


class GameRpc:
    ALLOWED_METHODS = "GET, POST, DELETE, OPTIONS"
    ALLOWED_HEADERS = "Authorization, Content-Type"

    def __init__(self, cfg, resolver, fetcher=None, running_provider=None, game_launcher=None,
                 session_store=None, process_adapter=None):
        self.cfg = cfg
        self.resolver = resolver
        self.api = resolver.api
        self._token_cache: dict[str, tuple[int, float]] = {}
        self._cache_lock = threading.Lock()
        self.state = GameState(cfg, resolver, fetcher, running_provider) if fetcher is not None else None
        self.launcher = game_launcher
        if self.state is not None and self.launcher is None:
            self.launcher = GameLauncher(
                self.state,
                locale_emulator=cfg.reina_locale_emulator,
                store=session_store,
                process_adapter=process_adapter,
            )
        if self.launcher is not None and self.state is not None:
            self.state.launcher = self.launcher

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
            raise GameRpcError(403, "origin not allowed", "origin_not_allowed")
        if not token or len(token.split(".")) != 3:
            raise GameRpcError(401, "invalid bearer token", "invalid_bearer_token")

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
            raise GameRpcError(503, "authentication service unavailable", "auth_service_unavailable") from None

        if response.status_code == 401:
            raise GameRpcError(401, "invalid bearer token", "invalid_bearer_token")
        if response.status_code == 403:
            # token 有效但 TeleDrive 拒絕授權：不是登入失效，不能讓網頁端因此登出
            raise GameRpcError(403, "TeleDrive denied access", "teledrive_forbidden")
        if response.status_code != 200:
            raise GameRpcError(503, "authentication service unavailable", "auth_service_unavailable")

        try:
            payload_segment = token.split(".")[1]
            payload_bytes = base64.urlsafe_b64decode(
                payload_segment + "=" * (-len(payload_segment) % 4)
            )
            claims = json.loads(payload_bytes)
            user_id = int(claims["user_id"])
            exp = int(claims["exp"])
        except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError):
            raise GameRpcError(401, "invalid bearer token", "invalid_bearer_token") from None

        ttl = min(300, exp - now)
        if ttl > 0:
            with self._cache_lock:
                self._token_cache[token_hash] = (user_id, now + ttl)
        return user_id

    def handle(self, environ, start_response):
        origin = environ.get("HTTP_ORIGIN", "")
        if not self._configured_origin():
            return self._response(start_response, 503, {"code": "game_rpc_disabled", "error": "browser game RPC is disabled"})
        if not self._origin_allowed(origin):
            return self._response(start_response, 403, {"code": "origin_not_allowed", "error": "origin not allowed"})

        cors = self._cors_headers(environ, origin)
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method == "OPTIONS":
            requested_method = environ.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "").upper()
            if requested_method and requested_method not in {"GET", "POST", "DELETE"}:
                return self._response(start_response, 403, {"code": "method_not_allowed", "error": "method not allowed"})
            return self._response(start_response, 204, None, cors)

        authorization = environ.get("HTTP_AUTHORIZATION", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or " " in token:
            return self._response(start_response, 401, {"code": "bearer_token_required", "error": "bearer token required"}, cors)

        try:
            owner_id = self.verify_browser_token(token, origin)
            expected_id = int(self.resolver.pool.primary.worker.user_id)
            if owner_id != expected_id:
                raise GameRpcError(403, "user does not own this bridge", "bridge_owner_mismatch")
        except GameRpcError as exc:
            return self._response(start_response, exc.status, {"code": exc.code, "error": exc.message}, cors)

        try:
            return self._route(environ, start_response, method, cors)
        except Exception:
            # 任何未預期例外都要回帶 CORS 的 JSON，否則瀏覽器只會看到連線錯誤
            log.exception("unexpected game RPC failure")
            return self._response(start_response, 500, {"code": "internal_error", "error": "internal error"}, cors)

    def _route(self, environ, start_response, method, cors):
        route = environ.get("PATH_INFO", "")
        if route == "/rpc/game/state":
            if method != "GET":
                return self._response(start_response, 405, {"code": "method_not_allowed", "error": "method not allowed"}, cors)
            if self.state is None:
                return self._response(start_response, 501, {"code": "not_implemented", "error": "game RPC is not implemented"}, cors)
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            try:
                games = self.state.states(query.get("paths", []))
            except GameStateError as exc:
                return self._response(start_response, exc.status, {"code": exc.code, "error": exc.message}, cors)
            return self._response(start_response, 200, {"games": games}, cors)
        if route == "/rpc/game/fetch":
            if self.state is None:
                return self._response(start_response, 501, {"code": "not_implemented", "error": "game RPC is not implemented"}, cors)
            query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            try:
                if method == "POST":
                    try:
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        payload = json.loads(environ["wsgi.input"].read(length))
                    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                        raise GameStateError(400, "invalid_json", "invalid JSON body") from None
                    if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
                        raise GameStateError(400, "invalid_game_path", "invalid game path")
                    result = self.state.fetch(payload["path"])
                    return self._response(start_response, 202, result, cors)
                if method == "DELETE":
                    values = query.get("path", [])
                    if len(values) != 1:
                        raise GameStateError(400, "invalid_query", "exactly one path parameter is required")
                    result = self.state.cancel(values[0])
                    return self._response(start_response, 202, result, cors)
                return self._response(start_response, 405, {"code": "method_not_allowed", "error": "method not allowed"}, cors)
            except GameStateError as exc:
                return self._response(start_response, exc.status, {"code": exc.code, "error": exc.message}, cors)
        if route in ("/rpc/game/exes", "/rpc/game/launch"):
            if self.state is not None:
                try:
                    if route == "/rpc/game/exes":
                        values = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).get("path", [])
                        if method != "GET":
                            raise GameStateError(405, "method_not_allowed", "method not allowed")
                        if len(values) != 1:
                            raise GameStateError(400, "invalid_query", "exactly one path parameter is required")
                        self.state.canonical_game_segments(values[0])
                        if self.launcher is None:
                            return self._response(start_response, 501, {"code": "not_implemented", "error": "game executable listing is not implemented"}, cors)
                        return self._response(start_response, 200, {"exes": self.launcher.list_exes(values[0])}, cors)
                    else:
                        if method != "POST":
                            raise GameStateError(405, "method_not_allowed", "method not allowed")
                        length = int(environ.get("CONTENT_LENGTH") or "0")
                        payload = json.loads(environ["wsgi.input"].read(length))
                        if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
                            raise GameStateError(400, "invalid_game_path", "invalid game path")
                        self.state.canonical_game_segments(payload["path"])
                        if not isinstance(payload.get("exe_relpath"), str):
                            raise GameStateError(400, "bridge_exe_invalid", "selected executable path is required")
                        if not isinstance(payload.get("game_id"), int) or isinstance(payload.get("game_id"), bool):
                            raise GameStateError(400, "invalid_game_id", "game id must be an integer")
                        if not isinstance(payload.get("locale_emulator", False), bool):
                            raise GameStateError(400, "invalid_locale_emulator", "locale_emulator must be boolean")
                        if self.launcher is None:
                            return self._response(start_response, 501, {"code": "not_implemented", "error": "game launch is not implemented"}, cors)
                        session = self.launcher.launch(
                            payload["path"], payload["exe_relpath"], payload["game_id"],
                            payload.get("locale_emulator", False),
                        )
                        return self._response(start_response, 200, {"session_id": session.session_id}, cors)
                except LaunchError as exc:
                    return self._response(start_response, exc.status, {"code": exc.code, "error": exc.message}, cors)
                except GameStateError as exc:
                    return self._response(start_response, exc.status, {"code": exc.code, "error": exc.message}, cors)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    return self._response(start_response, 400, {"code": "invalid_json", "error": "invalid JSON body"}, cors)
            return self._response(start_response, 501, {"code": "not_implemented", "error": "game RPC is not implemented"}, cors)
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
            200: "OK",
            202: "Accepted",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            405: "Method Not Allowed",
            404: "Not Found",
            409: "Conflict",
            501: "Not Implemented",
            503: "Service Unavailable",
            500: "Internal Server Error",
        }
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        response_headers = list(headers)
        if body is not None:
            response_headers.append(("Content-Type", "application/json; charset=utf-8"))
        response_headers.append(("Content-Length", str(len(payload))))
        start_response(f"{status} {reasons[status]}", response_headers)
        return [payload]
