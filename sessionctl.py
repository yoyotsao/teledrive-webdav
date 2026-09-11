"""Create and migrate private Telethon SQLite session files."""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Protocol

from telegram_sessions import SessionDirectoryLock, SessionLockError


class SessionCtlError(RuntimeError):
    pass


class SessionExistsError(SessionCtlError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    primary_user_id: int
    session_dir: Path
    account_ids: tuple[int, ...]
    obsolete_sources: tuple[str, ...]


class SessionPermissionPolicy(Protocol):
    def prepare_directory(self, path: Path) -> None: ...

    def verify_staged_file(self, path: Path) -> None: ...


class PosixSessionPermissionPolicy:
    def prepare_directory(self, path: Path) -> None:
        path = Path(path)
        failure = False
        try:
            if path.exists():
                if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
                    failure = True
            else:
                path.mkdir(parents=True, mode=0o700)
                os.chmod(path, 0o700)
        except (OSError, RuntimeError):
            failure = True
        if failure:
            raise SessionCtlError(
                "Telegram session directory permissions are not private"
            ) from None

    def verify_staged_file(self, path: Path) -> None:
        failure = False
        try:
            os.chmod(path, 0o600)
            mode = stat.S_IMODE(Path(path).stat().st_mode)
            if mode & 0o077:
                failure = True
        except (OSError, RuntimeError):
            failure = True
        if failure:
            raise SessionCtlError(
                "Telegram session file permissions are not private"
            ) from None


class WindowsSessionPermissionPolicy:
    """Apply a fail-closed DACL using Windows' built-in PowerShell/.NET APIs."""

    _SYSTEM = "S-1-5-18"
    _ADMINS = "S-1-5-32-544"

    def _run(self, script: str, path: Path) -> None:
        try:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            proc = None
        if proc is None or proc.returncode != 0:
            raise SessionCtlError(
                "Telegram session permissions could not be secured"
            ) from None

    @classmethod
    def _directory_script(cls) -> str:
        return rf"""
$p = $args[0]
if (-not (Test-Path -LiteralPath $p)) {{ New-Item -ItemType Directory -Path $p -Force | Out-Null }}
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$allowed = @($me.Value, '{cls._SYSTEM}', '{cls._ADMINS}')
$acl = Get-Acl -LiteralPath $p
foreach ($ace in $acl.Access) {{
  if ($ace.AccessControlType -eq 'Allow') {{
    try {{ $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }} catch {{ exit 12 }}
    if ($allowed -notcontains $sid) {{ exit 13 }}
  }}
}}
$acl.SetAccessRuleProtection($true, $false)
foreach ($ace in @($acl.Access)) {{ $acl.RemoveAccessRuleAll($ace) | Out-Null }}
foreach ($sidText in $allowed) {{
  $sid = New-Object System.Security.Principal.SecurityIdentifier($sidText)
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
  $acl.AddAccessRule($rule)
}}
Set-Acl -LiteralPath $p -AclObject $acl
"""

    @classmethod
    def _file_script(cls) -> str:
        return rf"""
$p = $args[0]
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$allowed = @($me.Value, '{cls._SYSTEM}', '{cls._ADMINS}')
$acl = New-Object System.Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($sidText in $allowed) {{
  $sid = New-Object System.Security.Principal.SecurityIdentifier($sidText)
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
  $acl.AddAccessRule($rule)
}}
Set-Acl -LiteralPath $p -AclObject $acl
"""

    def prepare_directory(self, path: Path) -> None:
        self._run(self._directory_script(), path)

    def verify_staged_file(self, path: Path) -> None:
        self._run(self._file_script(), path)


def default_permission_policy() -> SessionPermissionPolicy:
    return WindowsSessionPermissionPolicy() if os.name == "nt" else PosixSessionPermissionPolicy()


def resolve_session_dir_for_cli(config_path: Path, session_dir: Path) -> Path:
    config_path = Path(config_path).resolve()
    raw = Path(session_dir)
    return (raw if raw.is_absolute() else config_path.parent / raw).resolve()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_bootstrap_credentials(config_path: Path) -> tuple[int, str]:
    parser = configparser.ConfigParser()
    try:
        parser.read([Path(config_path)], encoding="utf-8")
    except (OSError, configparser.Error):
        parser = configparser.ConfigParser()
    env_file = parser.get("env", "env_file", fallback="").strip()
    env_values: dict[str, str] = {}
    if env_file:
        env_path = Path(env_file)
        if not env_path.is_absolute():
            env_path = Path(config_path).resolve().parent / env_path
        env_values = _read_env_file(env_path)

    raw_id = parser.get("telegram", "api_id", fallback="").strip()
    api_hash = parser.get("telegram", "api_hash", fallback="").strip()
    if not raw_id:
        raw_id = (os.environ.get("TELEGRAM_API_ID") or env_values.get("TELEGRAM_API_ID") or "").strip()
    if not api_hash:
        api_hash = (os.environ.get("TELEGRAM_API_HASH") or env_values.get("TELEGRAM_API_HASH") or "").strip()
    if not raw_id.isdigit() or int(raw_id) <= 0 or not api_hash:
        raise SessionCtlError("Telegram API credentials are missing or invalid") from None
    return int(raw_id), api_hash


def _default_client_factory(path: str, api_id: int, api_hash: str):
    from telethon import TelegramClient

    return TelegramClient(path, api_id, api_hash, receive_updates=False)


async def _login_to_staging(
    temporary: Path, api_id: int, api_hash: str, client_factory
) -> int:
    client = client_factory(str(temporary), api_id, api_hash)
    primary_error = None
    try:
        await client.start()
        return int((await client.get_me()).id)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await client.disconnect()
        except BaseException:
            if primary_error is None:
                raise


def _promote_no_replace(temporary: Path, destination: Path) -> None:
    promotion_error = None
    try:
        os.link(temporary, destination)
    except FileExistsError:
        promotion_error = SessionExistsError(
            f"Telegram session for account {destination.stem} already exists"
        )
    except OSError:
        promotion_error = SessionCtlError("could not finalize Telegram session")
    if promotion_error is not None:
        raise promotion_error from None
    cleanup_failed = False
    try:
        temporary.unlink()
    except (OSError, RuntimeError):
        cleanup_failed = True
    if cleanup_failed:
        raise SessionCtlError(
            "Telegram session finalized; staging cleanup failed"
        ) from None


def _login_session_impl(
    config_path: Path,
    session_dir: Path,
    *,
    client_factory,
    permission_policy: SessionPermissionPolicy,
) -> Path:
    api_id, api_hash = load_bootstrap_credentials(config_path)
    directory = resolve_session_dir_for_cli(config_path, session_dir)
    permission_policy.prepare_directory(directory)
    with SessionDirectoryLock(directory), TemporaryDirectory(
        dir=directory, prefix=".teledrive-sessionctl-"
    ) as staging:
        temporary = Path(staging) / "pending.session"
        login_error = None
        try:
            user_id = asyncio.run(
                _login_to_staging(temporary, api_id, api_hash, client_factory)
            )
        except BaseException as exc:
            login_error = SessionCtlError(
                f"Telegram login failed ({type(exc).__name__})"
            )
        if login_error is not None:
            raise login_error from None
        destination = directory / f"{user_id}.session"
        permission_policy.verify_staged_file(temporary)
        _promote_no_replace(temporary, destination)
        return destination


def login_session(
    config_path: Path,
    session_dir: Path,
    *,
    client_factory=None,
    permission_policy: SessionPermissionPolicy | None = None,
) -> Path:
    failure = None
    try:
        return _login_session_impl(
            config_path,
            session_dir,
            client_factory=client_factory or _default_client_factory,
            permission_policy=permission_policy or default_permission_policy(),
        )
    except SessionExistsError as exc:
        failure = SessionExistsError(str(exc))
    except SessionLockError as exc:
        failure = SessionCtlError(str(exc))
    except SessionCtlError as exc:
        failure = SessionCtlError(str(exc))
    except BaseException as exc:
        failure = SessionCtlError(
            f"Telegram session operation failed ({type(exc).__name__})"
        )
    raise failure from None



@dataclass(frozen=True)
class _LegacyInput:
    expected_user_id: int | None
    raw: str
    source_name: str


def _legacy_session_env_key() -> str:
    return "TELEGRAM_" + "SESSION_STRING"


def _load_legacy_inputs(config_path: Path) -> tuple[list[_LegacyInput], tuple[str, ...]]:
    parser = configparser.ConfigParser()
    try:
        parser.read([Path(config_path)], encoding="utf-8")
    except (OSError, configparser.Error):
        raise SessionCtlError("legacy Telegram configuration cannot be read") from None
    base = Path(config_path).resolve().parent
    configured = parser.get("telegram", "session", fallback="").strip()
    accounts_raw = parser.get("telegram", "accounts_file", fallback="").strip()
    env_file_raw = parser.get("env", "env_file", fallback="").strip()
    env_values: dict[str, str] = {}
    if env_file_raw:
        env_path = Path(env_file_raw)
        if not env_path.is_absolute():
            env_path = base / env_path
        env_values = _read_env_file(env_path)
    key = _legacy_session_env_key()
    env_value = (os.environ.get(key) or "").strip()
    file_value = (env_values.get(key) or "").strip()
    obsolete: list[str] = []

    if accounts_raw:
        accounts_path = Path(accounts_raw)
        if not accounts_path.is_absolute():
            accounts_path = base / accounts_path
        try:
            payload = json.loads(accounts_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SessionCtlError("legacy accounts file cannot be read") from None
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise SessionCtlError("legacy accounts file has no accounts") from None
        inputs: list[_LegacyInput] = []
        seen: set[int] = set()
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise SessionCtlError(f"legacy account {index} is invalid") from None
            user_id = row.get("telegram_user_id")
            raw = row.get("session")
            if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
                raise SessionCtlError(f"legacy account {index} has invalid user ID") from None
            if user_id in seen:
                raise SessionCtlError(f"duplicate legacy account {user_id}") from None
            if not isinstance(raw, str) or not raw:
                raise SessionCtlError(f"legacy account {user_id} has no session") from None
            seen.add(user_id)
            inputs.append(_LegacyInput(user_id, raw, "accounts_file"))
        if configured:
            obsolete.append("[telegram].session")
        if env_value:
            obsolete.append(key)
        if file_value:
            obsolete.append("env_file:" + key)
        return inputs, tuple(obsolete)

    candidates = [
        ("[telegram].session", configured),
        (key, env_value),
        ("env_file:" + key, file_value),
    ]
    chosen = next(((name, raw) for name, raw in candidates if raw), None)
    if chosen is None:
        raise SessionCtlError("no legacy Telegram session source is configured") from None
    chosen_name, chosen_raw = chosen
    obsolete.extend(name for name, raw in candidates if raw and name != chosen_name)
    return [_LegacyInput(None, chosen_raw, chosen_name)], tuple(obsolete)


def copy_string_session_to_sqlite(raw: str, destination: Path) -> None:
    from telethon.sessions import SQLiteSession, StringSession

    source = StringSession(raw)
    target = SQLiteSession(str(destination))
    try:
        target.set_dc(source.dc_id, source.server_address, source.port)
        target.auth_key = source.auth_key
        target.save()
    finally:
        target.close()


def _default_existing_matcher(existing: Path, raw: str) -> bool:
    from telethon.sessions import SQLiteSession, StringSession

    source = StringSession(raw)
    target = SQLiteSession(str(existing))
    try:
        source_key = getattr(source.auth_key, "key", None)
        target_key = getattr(target.auth_key, "key", None)
        return (
            source.dc_id == target.dc_id
            and source.server_address == target.server_address
            and source.port == target.port
            and source_key == target_key
        )
    finally:
        target.close()


async def _validate_migrated_session(
    path: Path,
    api_id: int,
    api_hash: str,
    client_factory,
) -> int:
    client = client_factory(str(path), api_id, api_hash)
    primary_error = None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SessionCtlError("legacy Telegram session is not authorized")
        return int((await client.get_me()).id)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            await client.disconnect()
        except BaseException:
            if primary_error is None:
                raise


def _rewrite_runtime_config(
    config_path: Path, *, primary_user_id: int, session_dir: Path
) -> None:
    parser = configparser.ConfigParser()
    try:
        parser.read([Path(config_path)], encoding="utf-8")
        if not parser.has_section("telegram"):
            parser.add_section("telegram")
        parser.remove_option("telegram", "session")
        parser.remove_option("telegram", "accounts_file")
        parser.set("telegram", "primary_user_id", str(primary_user_id))
        parser.set("telegram", "session_dir", str(Path(session_dir).resolve()))
        config_dir = Path(config_path).resolve().parent
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            prefix=".teledrive-config-",
            suffix=".tmp",
            dir=config_dir,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            parser.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, config_path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
    except SessionCtlError:
        raise
    except BaseException:
        raise SessionCtlError("runtime configuration could not be replaced") from None


def _migrate_legacy_sessions_impl(
    config_path: Path,
    session_dir: Path,
    *,
    client_factory,
    session_copier,
    existing_matcher,
    permission_policy: SessionPermissionPolicy,
) -> MigrationResult:
    directory = resolve_session_dir_for_cli(config_path, session_dir)
    permission_policy.prepare_directory(directory)
    with SessionDirectoryLock(directory), TemporaryDirectory(
        dir=directory, prefix=".teledrive-migrate-"
    ) as staging:
        api_id, api_hash = load_bootstrap_credentials(config_path)
        inputs, obsolete = _load_legacy_inputs(config_path)
        staged: list[tuple[_LegacyInput, Path, int]] = []
        for index, item in enumerate(inputs, 1):
            temporary = Path(staging) / f"pending-{index}.session"
            try:
                session_copier(item.raw, temporary)
                permission_policy.verify_staged_file(temporary)
                actual = asyncio.run(
                    _validate_migrated_session(
                        temporary, api_id, api_hash, client_factory
                    )
                )
            except BaseException as exc:
                account = item.expected_user_id or index
                raise SessionCtlError(
                    f"legacy account {account} validation failed ({type(exc).__name__})"
                ) from None
            staged.append((item, temporary, actual))

        actual_ids = [actual for _item, _temporary, actual in staged]
        duplicate = next(
            (actual for actual in actual_ids if actual_ids.count(actual) > 1), None
        )
        if duplicate is not None:
            raise SessionCtlError(
                f"duplicate migrated Telegram user ID {duplicate}"
            ) from None
        for item, _temporary, actual in staged:
            if item.expected_user_id is not None and actual != item.expected_user_id:
                raise SessionCtlError(
                    f"legacy account {item.expected_user_id} identity mismatch: got {actual}"
                ) from None

        for item, temporary, actual in staged:
            destination = directory / f"{actual}.session"
            if destination.exists():
                matches = False
                try:
                    matches = bool(existing_matcher(destination, item.raw))
                except BaseException:
                    matches = False
                if not matches:
                    raise SessionCtlError(
                        f"legacy account {actual} destination conflict"
                    ) from None
                try:
                    existing_actual = asyncio.run(
                        _validate_migrated_session(
                            destination, api_id, api_hash, client_factory
                        )
                    )
                except BaseException as exc:
                    raise SessionCtlError(
                        f"legacy account {actual} existing session validation failed ({type(exc).__name__})"
                    ) from None
                if existing_actual != actual:
                    raise SessionCtlError(
                        f"legacy account {actual} existing session identity mismatch"
                    ) from None
                try:
                    temporary.unlink()
                except OSError:
                    raise SessionCtlError(
                        f"legacy account {actual} staging cleanup failed"
                    ) from None
            else:
                try:
                    _promote_no_replace(temporary, destination)
                except SessionExistsError:
                    raise SessionCtlError(
                        f"legacy account {actual} destination conflict"
                    ) from None

        primary = staged[0][2]
        account_ids = tuple(actual for _item, _temporary, actual in staged)
        _rewrite_runtime_config(
            config_path, primary_user_id=primary, session_dir=directory
        )
        return MigrationResult(primary, directory, account_ids, obsolete)


def migrate_legacy_sessions(
    config_path: Path,
    session_dir: Path,
    *,
    client_factory=None,
    session_copier=None,
    existing_matcher=None,
    permission_policy: SessionPermissionPolicy | None = None,
) -> MigrationResult:
    failure = None
    try:
        return _migrate_legacy_sessions_impl(
            config_path,
            session_dir,
            client_factory=client_factory or _default_client_factory,
            session_copier=session_copier or copy_string_session_to_sqlite,
            existing_matcher=existing_matcher or _default_existing_matcher,
            permission_policy=permission_policy or default_permission_policy(),
        )
    except SessionLockError as exc:
        failure = SessionCtlError(str(exc))
    except SessionCtlError as exc:
        failure = SessionCtlError(str(exc))
    except BaseException as exc:
        failure = SessionCtlError(
            f"Telegram session migration failed ({type(exc).__name__})"
        )
    raise failure from None

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage private Telegram SQLite sessions")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="create a fresh Telegram SQLite session")
    login.add_argument("--config", type=Path, default=Path("config.ini"))
    login.add_argument("--session-dir", type=Path, required=True)
    migrate = sub.add_parser("migrate", help="convert legacy plaintext sessions")
    migrate.add_argument("--config", type=Path, default=Path("config.ini"))
    migrate.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "login":
            result = login_session(args.config, args.session_dir)
            print(f"Telegram session created for account {result.stem}: {result}")
            return 0
        if args.command == "migrate":
            result = migrate_legacy_sessions(args.config, args.session_dir)
            print(f"Migrated Telegram accounts {','.join(map(str, result.account_ids))} to {result.session_dir}")
            if result.obsolete_sources:
                print("Remove obsolete plaintext sources: " + ", ".join(result.obsolete_sources), file=sys.stderr)
            return 0
    except SessionCtlError as exc:
        print(f"sessionctl: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
