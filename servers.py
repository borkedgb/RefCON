"""Per-server state (RCon details, nickname, player history), keyed by host IP. See README.md."""
import json
import os
import sys

# frozen __file__ points into a temp folder PyInstaller deletes on exit, use the exe's own path instead
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PATH = os.path.join(_BASE_DIR, "data.json")

_LEGACY_HISTORY_PATH = os.path.join(_BASE_DIR, "history.json")
_LEGACY_RCON_PATH = os.path.join(_BASE_DIR, "rcon_config.json")


def _migrate_legacy_files():
    """one-off: folds the old single-server history.json/rcon_config.json into data.json"""
    if not os.path.exists(_LEGACY_HISTORY_PATH):
        return {}

    with open(_LEGACY_HISTORY_PATH, encoding="utf-8") as f:
        identities = json.load(f)

    host, port, password = "unknown", "2302", ""
    if os.path.exists(_LEGACY_RCON_PATH):
        with open(_LEGACY_RCON_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        host = cfg.get("host") or host
        port = cfg.get("port") or port
        password = cfg.get("password") or password

    servers = {host: {"nickname": host, "port": port, "password": password, "identities": identities}}
    save(servers)

    # verify the write landed before deleting the only other copy of this data
    with open(PATH, encoding="utf-8") as f:
        written_back = json.load(f)
    if written_back != servers:
        raise RuntimeError(
            f"Migration to {PATH} did not verify correctly, leaving {_LEGACY_HISTORY_PATH} "
            "and its config in place. Check data.json by hand before retrying."
        )

    os.remove(_LEGACY_HISTORY_PATH)
    if os.path.exists(_LEGACY_RCON_PATH):
        os.remove(_LEGACY_RCON_PATH)
    return servers


def load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return _migrate_legacy_files()


def save(servers):
    # write-then-rename so a crash mid-write can't truncate the only copy of the data
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)
    os.replace(tmp, PATH)


def get_or_create(servers, host):
    return servers.setdefault(
        host,
        {"nickname": host, "port": "2302", "password": "", "identities": {}},
    )


def display_name(host, server):
    nickname = server.get("nickname") or host
    if nickname == host:
        return host
    return f"{nickname} ({host})"


def _selftest():
    import shutil
    import tempfile

    global PATH, _LEGACY_HISTORY_PATH, _LEGACY_RCON_PATH
    orig_paths = (PATH, _LEGACY_HISTORY_PATH, _LEGACY_RCON_PATH)
    tmp_dir = tempfile.mkdtemp()
    PATH = os.path.join(tmp_dir, "data.json")
    _LEGACY_HISTORY_PATH = os.path.join(tmp_dir, "history.json")
    _LEGACY_RCON_PATH = os.path.join(tmp_dir, "rcon_config.json")

    try:
        servers = {}
        server = get_or_create(servers, "1.2.3.4")
        assert server == {"nickname": "1.2.3.4", "port": "2302", "password": "", "identities": {}}
        assert display_name("1.2.3.4", server) == "1.2.3.4"
        server["nickname"] = "My Server"
        assert display_name("1.2.3.4", server) == "My Server (1.2.3.4)"

        with open(_LEGACY_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump({"iid1": {"names": ["Bob"]}}, f)
        with open(_LEGACY_RCON_PATH, "w", encoding="utf-8") as f:
            json.dump({"host": "9.9.9.9", "port": "4041", "password": "secret"}, f)

        migrated = load()
        assert migrated["9.9.9.9"]["port"] == "4041"
        assert migrated["9.9.9.9"]["password"] == "secret"
        assert migrated["9.9.9.9"]["identities"] == {"iid1": {"names": ["Bob"]}}
        assert not os.path.exists(_LEGACY_HISTORY_PATH)
        assert not os.path.exists(_LEGACY_RCON_PATH)
        assert os.path.exists(PATH)

        assert load() == migrated
    finally:
        PATH, _LEGACY_HISTORY_PATH, _LEGACY_RCON_PATH = orig_paths
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
