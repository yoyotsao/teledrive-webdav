from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import gamestate
import savedata
from config import Config
from gamestate import GameRpc
from savedata import SavedataError, detect, import_savedata
from test_game_rpc import ALLOWED_ORIGIN, Session, request, token


def write(path: Path, data: bytes | str = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    values = {
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "USERPROFILE": str(home),
    }
    for value in values.values():
        Path(value).mkdir(parents=True, exist_ok=True)
    return values


@pytest.fixture
def root(tmp_path):
    game = tmp_path / "local" / "123"
    game.mkdir(parents=True)
    return game


# ---- 引擎识别 ----

def test_kirikiri_with_bundled_save(root, env):
    write(root / "data.xp3")
    write(root / "save" / "Save" / "data0.kdt")
    result = detect(root, env=env)
    assert result["engine"] == "kirikiri"
    assert result["candidates"] == [{"path": str(root.resolve() / "savedata"), "exists": False}]
    assert result["bundled"] == [{"path": "save/Save", "file_count": 1, "engine_hint": "kirikiri"}]


@pytest.mark.parametrize("files, engine, candidate", [
    (["www/js/rpg_core.js", "data.xp3"], "rpg_maker_mv", ["www", "save"]),
    (["js/rmmz_core.js"], "rpg_maker_mz", ["save"]),
    (["Scene.pck"], "siglus", ["savedata"]),
    (["SiglusEngine_CHS.exe"], "siglus", ["savedata"]),
    (["Game.rgss3a"], "rpg_maker_vx_ace", None),
    (["readme.txt"], None, None),
])
def test_engine_order_and_candidates(root, env, files, engine, candidate):
    for name in files:
        write(root / name)
    result = detect(root, env=env)
    assert result["engine"] == engine
    expected = [] if candidate is None else [str(root.resolve().joinpath(*candidate))]
    assert [item["path"] for item in result["candidates"]] == expected


def test_renpy_reads_save_directory_from_options(root, env):
    (root / "renpy").mkdir()
    write(root / "game" / "script.rpy", 'define config.save_directory = "Wrong-1"\n')
    write(root / "game" / "options.rpy", "# define config.save_directory = 'x'\ndefine config.save_directory = \"Foo-123\"\n")
    (Path(env["APPDATA"]) / "RenPy" / "Foo-123").mkdir(parents=True)
    result = detect(root, env=env)
    assert result["engine"] == "ren_py"
    assert result["candidates"] == [
        {"path": "%APPDATA%\\RenPy\\Foo-123", "exists": True},
        {"path": str(root.resolve() / "game" / "saves"), "exists": False},
    ]


def test_renpy_unsafe_save_directory_is_ignored(root, env):
    (root / "renpy").mkdir()
    write(root / "game" / "options.rpy", 'define config.save_directory = "../evil"\n')
    paths = [item["path"] for item in detect(root, env=env)["candidates"]]
    assert paths == [str(root.resolve() / "game" / "saves")]


def test_unity_reads_app_info(root, env):
    write(root / "UnityPlayer.dll")
    write(root / "Foo_Data" / "app.info", " Acme \n Product X \n")
    result = detect(root, env=env)
    assert result["engine"] == "unity"
    assert result["candidates"] == [{"path": "%USERPROFILE%\\AppData\\LocalLow\\Acme\\Product X", "exists": False}]


def test_exe_relpath_sets_detection_root(root, env):
    write(root / "Game" / "data.xp3")
    write(root / "Game" / "game.exe")
    result = detect(root, "Game/game.exe", env=env)
    assert result["candidates"][0]["path"] == str(root.resolve() / "Game" / "savedata")


@pytest.mark.parametrize("bad", ["../x.exe", "C:\\x.exe", "/x.exe", "a/../../x.exe", "missing/dir/x.exe"])
def test_exe_relpath_must_stay_inside(root, env, bad):
    with pytest.raises(SavedataError) as exc:
        detect(root, bad, env=env)
    assert exc.value.code == "invalid_exe_relpath"


# ---- 附带存档 ----

def test_bundled_hint_fills_engine_and_existing_candidate_is_excluded(root, env):
    write(root / "savedata" / "data0.ksd")
    write(root / "extras" / "存档" / "persistent")
    write(root / "extras" / "存档" / "1-1-LT1.save")
    write(root / "deep" / "a" / "b" / "c" / "save" / "x.dat")  # 深度 4，超出搜索范围
    result = detect(root, env=env)
    assert result["engine"] == "kirikiri"
    assert result["candidates"] == [{"path": str(root.resolve() / "savedata"), "exists": True}]
    assert result["bundled"] == [{"path": "extras/存档", "file_count": 2, "engine_hint": "ren_py"}]


def test_backup_dirs_are_excluded(root, env):
    write(root / "Tool" / "gameSaveBackup" / "file1.rmmzsave")
    write(root / "SaveBackup" / "inner" / "save" / "a.kdt")
    write(root / "save" / "a.kdt")
    assert [item["path"] for item in detect(root, env=env)["bundled"]] == ["save"]


def test_empty_save_dir_is_dropped(root, env):
    (root / "Save" / "inner").mkdir(parents=True)
    assert detect(root, env=env)["bundled"] == []


# ---- 导入 ----

def backups(tmp_path):
    return tmp_path / "cache" / "savedata-backups"


def test_import_merges_and_backs_up(root, env, tmp_path):
    write(root / "save" / "Save" / "data0.kdt", b"new")
    write(root / "save" / "Save" / "sub" / "a.kdt", b"a")
    target = root / "savedata"
    first = import_savedata(root, "save/Save", str(target), 42, backups(tmp_path), env=env)
    assert first == {"copied_files": 2, "target_path": str(target.resolve()), "backup_path": None}
    write(target / "extra.kdt", b"keep")
    write(target / "data0.kdt", b"old")
    second = import_savedata(root, "save/Save", str(target), 42, backups(tmp_path), env=env, clock=lambda: 0)
    assert (target / "data0.kdt").read_bytes() == b"new"
    assert (target / "extra.kdt").read_bytes() == b"keep"
    backup = Path(second["backup_path"])
    assert backup.parent == backups(tmp_path) / "42"
    assert (backup / "data0.kdt").read_bytes() == b"old"


def test_import_keeps_only_20_backups(root, env, tmp_path):
    write(root / "save" / "a.kdt")
    target = Path(env["APPDATA"]) / "Foo"
    write(target / "old.kdt")
    game_dir = backups(tmp_path) / "7"
    for index in range(25):
        (game_dir / f"20000101_0000{index:02d}").mkdir(parents=True)
    import_savedata(root, "save", str(target), 7, backups(tmp_path), env=env)
    assert len(list(game_dir.iterdir())) == 20


def test_import_env_target(root, env, tmp_path):
    write(root / "save" / "a.save")
    result = import_savedata(root, "save", "%APPDATA%\\RenPy\\Foo-123", 1, backups(tmp_path), env=env)
    assert Path(result["target_path"]) == Path(env["APPDATA"]) / "RenPy" / "Foo-123"
    assert (Path(env["APPDATA"]) / "RenPy" / "Foo-123" / "a.save").is_file()


@pytest.mark.parametrize("source", ["../../x", "C:\\x", "/x", "", "missing", "save/a.kdt"])
def test_invalid_source(root, env, tmp_path, source):
    write(root / "save" / "a.kdt")
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, source, str(root / "savedata"), 1, backups(tmp_path), env=env)
    assert exc.value.code == "invalid_source"


@pytest.mark.parametrize("target, code", [
    ("relative\\dir", "invalid_target"),
    ("%NOPE%\\x", "invalid_target"),
    ("C:\\Windows\\Temp\\x", "target_not_allowed"),
    ("%APPDATA%", "target_not_allowed"),
    ("%USERPROFILE%\\Desktop\\x", "target_not_allowed"),
    ("%APPDATA%\\..\\..\\..\\x", "target_not_allowed"),
])
def test_invalid_target_writes_nothing(root, env, tmp_path, target, code):
    write(root / "save" / "a.kdt")
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "save", target, 1, backups(tmp_path), env=env)
    assert exc.value.code == code
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_overlap(root, env, tmp_path):
    write(root / "save" / "a.kdt")
    for target in (root / "save", root / "save" / "inner"):
        with pytest.raises(SavedataError) as exc:
            import_savedata(root, "save", str(target), 1, backups(tmp_path), env=env)
        assert exc.value.code == "paths_overlap"


def test_target_must_be_directory_and_types_must_match(root, env, tmp_path):
    write(root / "save" / "a.kdt")
    write(root / "save" / "d" / "b.kdt")
    write(root / "savefile")
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "save", str(root / "savefile"), 1, backups(tmp_path), env=env)
    assert exc.value.code == "target_not_directory"

    target = root / "savedata"
    (target / "a.kdt").mkdir(parents=True)
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "save", str(target), 1, backups(tmp_path), env=env)
    assert exc.value.code == "conflict" and "a.kdt" in exc.value.message
    assert not backups(tmp_path).exists()


def test_backup_failure_aborts_import(root, env, tmp_path, monkeypatch):
    write(root / "save" / "a.kdt", b"new")
    target = root / "savedata"
    write(target / "a.kdt", b"old")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(savedata.shutil, "copytree", boom)
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "save", str(target), 1, backups(tmp_path), env=env)
    assert exc.value.code == "backup_failed"
    assert (target / "a.kdt").read_bytes() == b"old"


# ---- RPC ----

@pytest.fixture
def rpc(tmp_path, env, monkeypatch):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg = Config(
        api_id=1, api_hash="hash", primary_user_id=17, session_dir=tmp_path,
        base_url="https://teledrive.example", reina_allowed_origin=ALLOWED_ORIGIN,
        reina_server_url="https://teledrive.example", game_folder="game",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
    )
    game_root = cfg.local_dir / "123"
    game_root.mkdir(parents=True)
    session = Session()
    resolver = SimpleNamespace(cfg=cfg, api=SimpleNamespace(_http_session=lambda: session), pool=SimpleNamespace(
        primary=SimpleNamespace(worker=SimpleNamespace(user_id=17)),
    ))
    running = set()

    class Fetcher:
        def destination_for(self, _segments):
            return game_root

    launcher = SimpleNamespace(is_running=lambda path: path in running)
    app = GameRpc(cfg, resolver, Fetcher(), game_launcher=launcher)
    return app, game_root, running, cfg


def test_rpc_detect_and_import(rpc):
    app, game_root, running, cfg = rpc
    auth = f"Bearer {token()}"
    write(game_root / "data.xp3")
    write(game_root / "save" / "Save" / "data0.kdt")

    not_ready = request(app, authorization=auth, path="/rpc/game/savedata/detect", query="path=game%2FA")
    assert not_ready["status"] == 409
    assert json.loads(not_ready["body"])["code"] == "game_not_ready"
    assert not_ready["headers"]["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN

    write(game_root / ".reina-complete", "ok")
    detected = request(app, authorization=auth, path="/rpc/game/savedata/detect", query="path=game%2FA")
    assert detected["status"] == 200
    body = json.loads(detected["body"])
    assert body["engine"] == "kirikiri"
    assert body["bundled"][0]["path"] == "save/Save"

    payload = {"path": "game/A", "source_relpath": "save/Save",
               "target": body["candidates"][0]["path"], "game_id": 42}
    running.add("game/A")
    busy = request(app, "POST", "/rpc/game/savedata/import", authorization=auth, body=json.dumps(payload).encode())
    assert busy["status"] == 409 and json.loads(busy["body"])["code"] == "game_running"
    running.clear()

    imported = request(app, "POST", "/rpc/game/savedata/import", authorization=auth, body=json.dumps(payload).encode())
    assert imported["status"] == 200
    assert json.loads(imported["body"])["copied_files"] == 1
    again = request(app, "POST", "/rpc/game/savedata/import", authorization=auth, body=json.dumps(payload).encode())
    backup = json.loads(again["body"])["backup_path"]
    assert Path(backup).parent == Path(cfg.cache_dir) / "savedata-backups" / "42"

    denied = request(app, "POST", "/rpc/game/savedata/import", authorization=auth, body=json.dumps(
        {**payload, "target": "C:\\Windows\\Temp\\x"}).encode())
    assert denied["status"] == 403 and json.loads(denied["body"])["code"] == "target_not_allowed"


@pytest.mark.skipif(os.name != "nt", reason="junction 只在 Windows")
def test_junctions_are_rejected(root, env, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    write(outside / "a.kdt")
    _winapi.CreateJunction(str(outside), str(root / "save"))
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "save", str(root / "savedata"), 1, backups(tmp_path), env=env)
    assert exc.value.code == "invalid_source"
    assert detect(root, env=env)["bundled"] == []

    write(root / "real" / "b.kdt")
    _winapi.CreateJunction(str(outside), str(root / "real" / "link"))
    with pytest.raises(SavedataError) as exc:
        import_savedata(root, "real", str(root / "savedata"), 1, backups(tmp_path), env=env)
    assert exc.value.code == "conflict" and "link" in exc.value.message
