from tdapi import TeleDriveClient


class Cfg:
    def __init__(self, tmp_path):
        self.base_url = "https://backend.example"
        self.api_base = self.base_url + "/api/v1"
        self.cache_dir = tmp_path
        self.dir_cache_seconds = 60


def test_durable_operation_methods_preserve_backend_paths_and_payloads(tmp_path):
    api = TeleDriveClient(Cfg(tmp_path))
    api._token = "JWT"
    calls = []
    def call(method, path, **kwargs):
        calls.append((method, path, kwargs.get("params"), kwargs.get("payload")))
        if path == "/telegram-operations":
            return {"operations": []} if method == "GET" else {"operation_id": "op"}
        return {"ok": True}
    api._call = call
    payload = {"operation_id":"op","random_id":"1"}
    api.create_telegram_operation(payload)
    api.list_telegram_operations(True)
    api.get_telegram_operation("op")
    api.patch_telegram_operation("op", {"expected_operation_version":0,"state":"sending"})
    api.reconcile_telegram_operation_result("op", {"expected_operation_version":1,"mapping":{},"media_identity":{}})
    api.register_telegram_operation("op")
    api.register_telegram_operation_group("g")
    api.switch_file_location("f", {"expected_location_version":1,"operation_id":"op","result_version":1})
    api.switch_file_location_group([{"file_id":"f","expected_location_version":1,"operation_id":"op","result_version":1}])
    assert calls[0] == ("POST", "/telegram-operations", None, payload)
    assert calls[1][0:2] == ("GET", "/telegram-operations")
    assert calls[1][2] == {"include_terminal":"true"}
    assert calls[-1][1:] == ("/file-location-groups/switch", None, {"parts":[{"file_id":"f","expected_location_version":1,"operation_id":"op","result_version":1}]})
