from tdapi import TeleDriveClient


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = self.base_url + "/api/v1"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


def client(tmp_path, target, accounts):
    api = TeleDriveClient(Cfg(tmp_path))
    api._token = "JWT"
    api._call = lambda method, path, **kwargs: target if path == "/storage-target" else {"accounts": accounts}
    return api


def test_freeze_saved_messages_uses_exact_primary_and_versions(tmp_path):
    api = client(tmp_path, {"storage_mode":"saved_messages","channel_id":None,"version":4,"accounts_version":8}, [
        {"telegram_user_id":2,"is_primary":0}, {"telegram_user_id":1,"is_primary":1}
    ])
    frozen = api.freeze_storage_target()
    assert frozen.storage_mode == "saved_messages"
    assert frozen.primary_account_id == 1
    assert frozen.linked_account_ids == (1, 2)
    assert frozen.target_peer_key == "me:1"
    assert (frozen.target_version, frozen.accounts_version) == (4, 8)


def test_freeze_channel_uses_raw_channel_peer_key(tmp_path):
    api = client(tmp_path, {"storage_mode":"channel","channel_id":"1234567890","version":5,"accounts_version":3}, [
        {"telegram_user_id":1,"is_primary":1}
    ])
    frozen = api.freeze_storage_target()
    assert frozen.channel_id == "1234567890"
    assert frozen.target_peer_key == "1234567890"
