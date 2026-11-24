"""存档位置检测与附带存档导入（`/rpc/game/savedata/*`），规则对齐桌面版 detect.rs / import.rs。"""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from pathlib import Path, PureWindowsPath

RENPY_OPTIONS_MAX = 1024 * 1024
BUNDLED_SEARCH_DEPTH = 3
BUNDLED_COUNT_DEPTH = 4
BACKUPS_KEPT = 20
SAVE_NAME_MARKERS = ("save", "セーブ", "存档", "存檔")
HINT_BY_SUFFIX = {
    ".rpgsave": "rpg_maker_mv",
    ".rmmzsave": "rpg_maker_mz",
    ".rvdata2": "rpg_maker_vx_ace",
    ".ksd": "kirikiri",
    ".kdt": "kirikiri",
}
# 允许导入的目标根（除游戏包根目录外），环境变量形式，展开后比较。
ALLOWED_TARGET_BASES = (
    "%APPDATA%",
    "%LOCALAPPDATA%",
    "%USERPROFILE%\\AppData\\LocalLow",
    "%USERPROFILE%\\Documents",
    "%USERPROFILE%\\Saved Games",
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SavedataError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def is_link(path: Path) -> bool:
    """符号链接或 junction（任何 reparse point）都算链接。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def expand_env(value: str, env=None) -> str:
    """只展开 Windows 的 `%NAME%`（不分大小写）；未定义的变量原样保留。"""
    env = os.environ if env is None else env
    folded = {str(key).casefold(): str(val) for key, val in env.items()}
    return re.sub(r"%([^%]+)%", lambda m: folded.get(m.group(1).casefold(), m.group(0)), value)


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(str(path))).casefold()


def _contains(base, candidate, *, strict: bool = False) -> bool:
    base_key, candidate_key = _key(base), _key(candidate)
    if base_key == candidate_key:
        return not strict
    try:
        return os.path.commonpath((base_key, candidate_key)) == base_key
    except ValueError:
        return False


def _children(directory: Path) -> dict[str, Path]:
    try:
        return {child.name.casefold(): child for child in directory.iterdir()}
    except OSError:
        return {}


def _child(directory: Path, *names: str) -> Path | None:
    current = directory
    for name in names:
        current = _children(current).get(name.casefold())
        if current is None:
            return None
    return current


def _safe_segment(name: str | None) -> str | None:
    if not name or name in (".", "..") or any(ch in name for ch in "/\\:%"):
        return None
    return name


def renpy_save_directory(root: Path) -> str | None:
    game_dir = _child(root, "game")
    if game_dir is None or not game_dir.is_dir():
        return None
    scripts = sorted(
        (child for child in _children(game_dir).values() if child.suffix.casefold() == ".rpy" and child.is_file()),
        key=lambda item: (item.name.casefold() != "options.rpy", item.name.casefold()),
    )
    for script in scripts:
        try:
            if script.stat().st_size > RENPY_OPTIONS_MAX:
                continue
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, right = line.split("=", 1)
            if not left.strip().endswith("config.save_directory"):
                continue
            match = re.search(r"""(["'])(.*?)\1""", right)
            return _safe_segment(match.group(2)) if match else None
    return None


def unity_company_product(root: Path) -> tuple[str, str] | None:
    data_dirs = sorted(
        (child for child in _children(root).values() if child.name.casefold().endswith("_data") and child.is_dir()),
        key=lambda item: item.name.casefold(),
    )
    for data_dir in data_dirs:
        info = _child(data_dir, "app.info")
        if info is None or not info.is_file():
            continue
        try:
            lines = info.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if len(lines) < 2:
            continue
        company, product = _safe_segment(lines[0].strip()), _safe_segment(lines[1].strip())
        if company and product:
            return company, product
    return None


def detect_engine(root: Path) -> str | None:
    top = _children(root)
    names = list(top)

    def is_file(*parts):
        found = _child(root, *parts)
        return found is not None and found.is_file()

    if is_file("www", "js", "rpg_core.js"):
        return "rpg_maker_mv"
    if is_file("js", "rmmz_core.js"):
        return "rpg_maker_mz"
    if top.get("renpy") is not None and top["renpy"].is_dir() and top.get("game") is not None and top["game"].is_dir():
        return "ren_py"
    if "unityplayer.dll" in top:
        return "unity"
    if "scene.pck" in top or any(n.startswith("siglusengine") and n.endswith(".exe") for n in names):
        return "siglus"
    if any(n.endswith(".xp3") for n in names):
        return "kirikiri"
    if any(n.endswith(".rgss3a") for n in names):
        return "rpg_maker_vx_ace"
    return None


def candidates_for(engine: str | None, root: Path) -> list[str]:
    if engine == "rpg_maker_mv":
        return [str(root / "www" / "save")]
    if engine == "rpg_maker_mz":
        return [str(root / "save")]
    if engine == "ren_py":
        result = []
        name = renpy_save_directory(root)
        if name:
            result.append(f"%APPDATA%\\RenPy\\{name}")
        result.append(str(root / "game" / "saves"))
        return result
    if engine == "unity":
        names = unity_company_product(root)
        return [f"%USERPROFILE%\\AppData\\LocalLow\\{names[0]}\\{names[1]}"] if names else []
    if engine in ("siglus", "kirikiri"):
        return [str(root / "savedata")]
    # rpg_maker_vx_ace 的存档就在游戏根目录，不能把整个游戏目录当存档路径。
    return []


def _looks_like_save(name: str) -> bool:
    lowered = name.casefold()
    return any(marker in lowered for marker in SAVE_NAME_MARKERS)


def _files_under(directory: Path, max_depth: int) -> list[Path]:
    """深度 ≤ max_depth 的普通文件，按名称排序，不跟随链接。"""
    files: list[Path] = []
    pending = [(directory, 1)]
    while pending:
        current, depth = pending.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for child in children:
            if is_link(child):
                continue
            if child.is_file():
                files.append(child)
            elif child.is_dir() and depth < max_depth:
                pending.append((child, depth + 1))
    return files


def _engine_hint(files: list[Path]) -> str | None:
    for item in files:
        hint = HINT_BY_SUFFIX.get(item.suffix.casefold())
        if hint:
            return hint
    if any(item.name.casefold() == "persistent" for item in files) and any(
        item.suffix.casefold() == ".save" for item in files
    ):
        return "ren_py"
    return None


def find_bundled(package_root: Path) -> list[dict]:
    found = []
    pending = [(package_root, 1)]
    while pending:
        current, depth = pending.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for child in children:
            if is_link(child) or not child.is_dir():
                continue
            # 备份目录（如 MTool 的 Tool/gameSaveBackup）不是附带存档，连同其下层一起跳过。
            if "backup" in child.name.casefold():
                continue
            if not _looks_like_save(child.name):
                if depth < BUNDLED_SEARCH_DEPTH:
                    pending.append((child, depth + 1))
                continue
            target = child
            while True:
                try:
                    inner = list(target.iterdir())
                except OSError:
                    break
                if len(inner) != 1 or is_link(inner[0]) or not inner[0].is_dir():
                    break
                target = inner[0]
            files = _files_under(target, BUNDLED_COUNT_DEPTH)
            if files:
                found.append({
                    "path": target.relative_to(package_root).as_posix(),
                    "dir": target,
                    "file_count": len(files),
                    "engine_hint": _engine_hint(files),
                })
    return found


def _relative_parts(value: str) -> list[str] | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    windows = PureWindowsPath(value)
    if Path(value).is_absolute() or windows.is_absolute() or windows.drive or windows.root:
        return None
    parts = value.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return parts


def _walk_inside(root: Path, parts: list[str]) -> Path | None:
    """逐段走进 root，任一段是链接或越出 root 就返回 None。"""
    current = root
    for part in parts:
        current = current / part
        if is_link(current):
            return None
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    return resolved if _contains(root, resolved) else None


def detect(package_root: Path, exe_relpath: str | None = None, env=None) -> dict:
    package_root = package_root.resolve()
    detect_root = package_root
    if exe_relpath:
        parts = _relative_parts(exe_relpath)
        parent = _walk_inside(package_root, parts[:-1]) if parts else None
        if parent is None or not parent.is_dir() or is_link(package_root.joinpath(*parts)):
            raise SavedataError(400, "invalid_exe_relpath", "启动文件路径无效或越出游戏目录")
        detect_root = parent

    engine = detect_engine(detect_root)
    bundled = find_bundled(package_root)
    if engine is None:
        engine = next((item["engine_hint"] for item in bundled if item["engine_hint"]), None)
    candidate_paths = candidates_for(engine, detect_root)

    candidate_keys = set()
    candidates = []
    for path in candidate_paths:
        expanded = expand_env(path, env)
        candidates.append({"path": path, "exists": "%" not in expanded and os.path.exists(expanded)})
        candidate_keys.add(_key(expanded))
    return {
        "engine": engine,
        "candidates": candidates,
        "bundled": [
            {key: item[key] for key in ("path", "file_count", "engine_hint")}
            for item in bundled
            if _key(item["dir"]) not in candidate_keys
        ],
    }


def _source_tree(source: Path) -> tuple[list[str], list[str]]:
    """返回来源内所有目录与文件的相对路径；遇到链接即是冲突。"""
    dirs, files = [], []
    pending = [source]
    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SavedataError(409, "conflict", f"无法读取来源目录：{current.relative_to(source).as_posix() or '.'}") from exc
        for child in children:
            rel = child.relative_to(source).as_posix()
            if is_link(child):
                raise SavedataError(409, "conflict", f"来源内含链接：{rel}")
            if child.is_dir():
                dirs.append(rel)
                pending.append(child)
            elif child.is_file():
                files.append(rel)
    return dirs, files


def _prune_backups(game_dir: Path) -> None:
    backups = sorted((item for item in game_dir.iterdir() if item.is_dir()), key=lambda item: item.name)
    for stale in backups[:-BACKUPS_KEPT]:
        shutil.rmtree(stale, ignore_errors=True)


def import_savedata(package_root: Path, source_relpath: str, target: str, game_id: int,
                    backup_root: Path, env=None, clock=time.time) -> dict:
    package_root = package_root.resolve()
    parts = _relative_parts(source_relpath)
    source = _walk_inside(package_root, parts) if parts else None
    if source is None or not source.is_dir():
        raise SavedataError(400, "invalid_source", "附带存档来源无效")

    expanded = expand_env(target, env) if isinstance(target, str) else ""
    # Windows 上 `\foo` 也算 isabs，但它相对于当前磁盘，不是真正的绝对路径。
    needs_drive = os.name == "nt"
    if not expanded or "%" in expanded or "\x00" in expanded or not os.path.isabs(expanded) \
            or (needs_drive and not PureWindowsPath(expanded).drive):
        raise SavedataError(400, "invalid_target", "目标必须是绝对路径")
    target_path = Path(os.path.normpath(expanded))

    bases = [package_root] + [
        Path(base) for base in (expand_env(item, env) for item in ALLOWED_TARGET_BASES)
        if "%" not in base and os.path.isabs(base)
    ]
    if not any(_contains(base, target_path, strict=True) for base in bases):
        raise SavedataError(403, "target_not_allowed", "目标不在允许的存档位置内")

    if _contains(source, target_path) or _contains(target_path, source):
        raise SavedataError(400, "paths_overlap", "来源与目标重叠")

    target_exists = os.path.lexists(target_path)
    if target_exists and (is_link(target_path) or not target_path.is_dir()):
        raise SavedataError(409, "target_not_directory", "目标已存在且不是目录")

    dirs, files = _source_tree(source)
    if target_exists:
        for rel in dirs:
            existing = target_path / rel
            if os.path.lexists(existing) and (is_link(existing) or not existing.is_dir()):
                raise SavedataError(409, "conflict", f"目标中同名项不是目录：{rel}")
        for rel in files:
            existing = target_path / rel
            if os.path.lexists(existing) and (is_link(existing) or existing.is_dir()):
                raise SavedataError(409, "conflict", f"目标中同名项是目录：{rel}")

    backup_path = None
    if target_exists and any(target_path.iterdir()):
        game_dir = backup_root / str(game_id)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(clock()))
        backup = game_dir / stamp
        suffix = 1
        while backup.exists():
            backup = game_dir / f"{stamp}_{suffix}"
            suffix += 1
        try:
            game_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target_path, backup, symlinks=True)
        except OSError as exc:
            shutil.rmtree(backup, ignore_errors=True)
            raise SavedataError(500, "backup_failed", "备份现有存档失败，未导入") from exc
        backup_path = str(backup)
        _prune_backups(game_dir)

    target_path.mkdir(parents=True, exist_ok=True)
    for rel in dirs:
        (target_path / rel).mkdir(parents=True, exist_ok=True)
    for rel in files:
        shutil.copy2(source / rel, target_path / rel)
    return {"copied_files": len(files), "target_path": str(target_path), "backup_path": backup_path}
