"""
Operations on a single server's player history dict (identityId -> entry).
Persistence itself lives in servers.py, which owns one such dict per server;
this module just knows how to fold new identities into one and query it for
alts, so an IP overlap gets caught even when the two accounts never appeared
together in the same single log.
"""


def merge(history, identities):
    """Fold freshly-parsed altdetector.Identity objects into history, in place."""
    for ident in identities.values():
        entry = history.setdefault(
            ident.iid,
            {"entity_id": None, "names": [], "be_guid": None, "steam_id": None, "ips": []},
        )
        for name in ident.names:
            if name not in entry["names"]:
                entry["names"].append(name)
        for ip in ident.ips():
            if ip not in entry["ips"]:
                entry["ips"].append(ip)
        if ident.entity_id:
            entry["entity_id"] = ident.entity_id
        if ident.be_guid:
            entry["be_guid"] = ident.be_guid
        if ident.steam_id:
            entry["steam_id"] = ident.steam_id
    return history


def merge_live(history, player_rows):
    """Keys entries by BE GUID since RCon never gives an identityId; returns whether anything changed."""
    by_guid = {entry["be_guid"]: key for key, entry in history.items() if entry["be_guid"]}
    changed = False
    for num, ip, _port, _ping, guid, name, _status in player_rows:
        key = by_guid.get(guid)
        if key is None:
            key = guid
            history[key] = {
                "identityId": None,
                "entity_id": None,
                "names": [],
                "be_guid": guid,
                "steam_id": None,
                "ips": [],
            }
            by_guid[guid] = key
            changed = True
        entry = history[key]
        if entry["entity_id"] != num:
            entry["entity_id"] = num
            changed = True
        if name not in entry["names"]:
            entry["names"].append(name)
            changed = True
        if ip not in entry["ips"]:
            entry["ips"].append(ip)
            changed = True
    return changed


def shared_ip_iids(history):
    by_ip = {}
    for iid, entry in history.items():
        for ip in entry["ips"]:
            by_ip.setdefault(ip, []).append(iid)

    flagged = set()
    for iids in by_ip.values():
        if len(iids) > 1:
            flagged.update(iids)
    return flagged


def alt_names(history, iid):
    #O(n^2) over the whole history, fine until this hits a LOT of accounts
    entry = history[iid]
    names = set()
    for ip in entry["ips"]:
        for other_iid, other in history.items():
            if other_iid != iid and ip in other["ips"]:
                names.update(other["names"])
    return names


def _selftest():
    hist = {
        "aaaa": {"names": ["Alice"], "ips": ["1.2.3.4"]},
        "bbbb": {"names": ["Bob"], "ips": ["1.2.3.4"]},
        "cccc": {"names": ["Carol"], "ips": ["9.9.9.9"]},
    }
    assert shared_ip_iids(hist) == {"aaaa", "bbbb"}
    assert alt_names(hist, "aaaa") == {"Bob"}
    assert alt_names(hist, "bbbb") == {"Alice"}
    assert alt_names(hist, "cccc") == set()

    from altdetector import Identity

    dave = Identity("dddd")
    dave.entity_id = "1"
    dave.names = ["Dave"]
    dave.be_guid = "d" * 32
    dave.connections = [("5.5.5.5", "1111")]
    merge(hist, {"dddd": dave})
    assert hist["dddd"]["ips"] == ["5.5.5.5"]
    assert hist["dddd"]["be_guid"] == "d" * 32

    merge(hist, {"dddd": dave})
    assert hist["dddd"]["ips"] == ["5.5.5.5"]
    assert hist["dddd"]["names"] == ["Dave"]

    live_hist = {}
    row = ("2", "6.6.6.6", "2302", "20", "e" * 32, "Eve", "")
    assert merge_live(live_hist, [row]) is True
    assert live_hist["e" * 32] == {
        "identityId": None,
        "entity_id": "2",
        "names": ["Eve"],
        "be_guid": "e" * 32,
        "steam_id": None,
        "ips": ["6.6.6.6"],
    }
    assert merge_live(live_hist, [row]) is False  # same row again, nothing new to learn

    print("selftest OK")


if __name__ == "__main__":
    _selftest()
