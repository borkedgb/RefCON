"""
Parses an Arma Reforger server console log and reports player identities:
BE player number, persistent identityId (UUID), BE GUID, SteamID (PC only),
names used, and IPs seen.

Also groups connections by IP so accounts sharing an IP (a classic ban
evasion pattern: same box, different account) jump out immediately.

Usage:
    python altdetector.py console.log
    python altdetector.py console.log --csv out.csv
    python altdetector.py --selftest
"""
import argparse
import csv
import re

RE_AUTHENTICATING = re.compile(
    r"ServerImpl event: authenticating \(identity=0x(?P<slot>[0-9a-fA-F]+), "
    r"address=(?P<ip>[\d.]+):(?P<port>\d+)\)"
)
RE_AUTH = re.compile(
    r"Authenticated player: rplIdentity=0x(?P<slot>[0-9a-fA-F]+) "
    r"identityId=(?P<iid>[0-9a-fA-f-]+) name=(?P<name>.+)$"
)
RE_GUID = re.compile(
    r"Setting GUID for player identity=0x(?P<slot>[0-9a-fA-F]+), GUID=(?P<guid>.+)$"
)
RE_BEGUID = re.compile(r"BE GUID: (?P<guid>[0-9a-fA-F]{32})")
# stock BattlEye connect line, gives us a player number without needing the ServerAdminTools mod
RE_CONNECTED = re.compile(
    r"Player #(?P<num>\d+) .+? \((?P<ip>[\d.]+):(?P<port>\d+)\) connected"
)

CONSOLE_GUID_PLACEHOLDER = "[u8; 64]"


class Identity:
    def __init__(self, iid):
        self.iid = iid
        self.entity_id = None
        self.names = []
        self.be_guid = None
        self.steam_id = None
        self.connections = []  # (ip, port)

    def add_name(self, name):
        name = name.strip()
        if not self.names or self.names[-1] != name:
            self.names.append(name)

    def guid_display(self):
        return self.be_guid or "?"

    def ips(self):
        seen = []
        for ip, _port in self.connections:
            if ip not in seen:
                seen.append(ip)
        return seen


def parse(path):
    identities = {}
    pending_by_slot = {}
    pending_ip_by_slot = {}
    ident_by_connection = {}
    last_guid_ident = None

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_AUTHENTICATING.search(line)
            if m:
                pending_ip_by_slot[int(m["slot"], 16)] = (m["ip"], m["port"])
                continue

            m = RE_AUTH.search(line)
            if m:
                slot = int(m["slot"], 16)
                ident = identities.setdefault(m["iid"], Identity(m["iid"]))
                ident.add_name(m["name"])
                pending_by_slot[slot] = ident
                # matched by slot, not arrival order, two people joining at once can log out of order
                ip_port = pending_ip_by_slot.pop(slot, None)
                if ip_port:
                    ident.connections.append(ip_port)
                    ident_by_connection[ip_port] = ident
                continue

            m = RE_GUID.search(line)
            if m:
                slot = int(m["slot"], 16)
                last_guid_ident = pending_by_slot.get(slot)
                if last_guid_ident and m["guid"].strip() != CONSOLE_GUID_PLACEHOLDER:
                    last_guid_ident.steam_id = m["guid"].strip()
                continue

            m = RE_BEGUID.search(line)
            if m and last_guid_ident:  # no slot on this line, rides on whichever GUID line touched last
                last_guid_ident.be_guid = m["guid"]
                continue

            m = RE_CONNECTED.search(line)
            if m:
                ident = ident_by_connection.get((m["ip"], m["port"]))
                if ident:
                    ident.entity_id = m["num"]

    return identities


def shared_ip_groups(identities):
    by_ip = {}
    for ident in identities.values():
        for ip in ident.ips():
            by_ip.setdefault(ip, []).append(ident)
    return {ip: idents for ip, idents in by_ip.items() if len(idents) > 1}


def print_report(identities):
    rows = sorted(
        identities.values(), key=lambda i: int(i.entity_id or -1)
    )

    print("=== Player Identities ===")
    header = f"{'Player#':<7}| {'BE GUID':<34}| {'Names':<30}| {'IP(s)':<28}| identityId"
    print(header)
    print("-" * len(header))
    for ident in rows:
        print(
            f"{(ident.entity_id or '?'):<7}"
            f"| {ident.guid_display():<34}"
            f"| {', '.join(ident.names):<30}"
            f"| {', '.join(ident.ips()):<28}"
            f"| {ident.iid}"
        )

    groups = shared_ip_groups(identities)
    print("\n=== Shared-IP Groups (possible alts / ban evasion) ===")
    if not groups:
        print("None found.")
    for ip, idents in groups.items():
        print(f"\nIP {ip} used by {len(idents)} distinct accounts:")
        for ident in idents:
            print(
                f"  - Player# {ident.entity_id or '?'} "
                f"({', '.join(ident.names)}) "
                f"BE GUID: {ident.guid_display()} "
                f"identityId: {ident.iid}"
            )


def write_csv(identities, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entity_id", "identityId", "be_guid", "steam_id", "names", "ips"])
        for ident in identities.values():
            w.writerow(
                [
                    ident.entity_id or "",
                    ident.iid,
                    ident.be_guid or "",
                    ident.steam_id or "",
                    "; ".join(ident.names),
                    "; ".join(ident.ips()),
                ]
            )


def _parse_text(text):
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        return parse(tmp)
    finally:
        os.remove(tmp)


def _selftest():
    sample = """\
16:28:17.409 RPL          : ServerImpl event: authenticating (identity=0x00000000, address=1.2.3.4:1000)
16:28:17.573 BACKEND      : Authenticated player: rplIdentity=0x00000000 identityId=aaaa-1111 name=Alice
16:28:17.589  DEFAULT      : BattlEye Server: 'Player #0 Alice (1.2.3.4:1000) connected'
16:28:17.589  DEFAULT      : BattlEye Server: Setting GUID for player identity=0x00000000, GUID=76561198000000001
16:28:17.589  DEFAULT      : BattlEye Server: 'Player #0 Alice - BE GUID: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
16:34:34.800 RPL          : ServerImpl event: authenticating (identity=0x00000001, address=1.2.3.4:2000)
16:34:34.825 BACKEND      : Authenticated player: rplIdentity=0x00000001 identityId=bbbb-2222 name=Bob
16:34:34.857  DEFAULT      : BattlEye Server: 'Player #1 Bob (1.2.3.4:2000) connected'
16:34:34.857  DEFAULT      : BattlEye Server: Setting GUID for player identity=0x00000001, GUID=[u8; 64]
16:34:34.857  DEFAULT      : BattlEye Server: 'Player #1 Bob - BE GUID: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
"""
    identities = _parse_text(sample)
    assert len(identities) == 2
    alice = identities["aaaa-1111"]
    bob = identities["bbbb-2222"]
    assert alice.entity_id == "0"
    assert alice.names == ["Alice"]
    assert alice.steam_id == "76561198000000001"
    assert alice.be_guid == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert alice.ips() == ["1.2.3.4"]
    assert bob.entity_id == "1"
    assert bob.steam_id is None
    assert bob.be_guid == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    groups = shared_ip_groups(identities)
    assert "1.2.3.4" in groups and len(groups["1.2.3.4"]) == 2

    # this used to swap their IPs when the connect lines logged out of order
    interleaved = """\
16:28:17.400 RPL          : ServerImpl event: authenticating (identity=0x00000000, address=1.1.1.1:1000)
16:28:17.410 RPL          : ServerImpl event: authenticating (identity=0x00000001, address=2.2.2.2:2000)
16:28:17.500 BACKEND      : Authenticated player: rplIdentity=0x00000000 identityId=cccc-3333 name=Carol
16:28:17.510 BACKEND      : Authenticated player: rplIdentity=0x00000001 identityId=dddd-4444 name=Dave
16:28:17.589  DEFAULT      : BattlEye Server: 'Player #1 Dave (2.2.2.2:2000) connected'
16:28:17.590  DEFAULT      : BattlEye Server: 'Player #0 Carol (1.1.1.1:1000) connected'
"""
    identities = _parse_text(interleaved)
    assert identities["cccc-3333"].ips() == ["1.1.1.1"]
    assert identities["dddd-4444"].ips() == ["2.2.2.2"]

    print("selftest OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logfile", nargs="?", help="path to server console log")
    parser.add_argument("--csv", help="also write results to this CSV path")
    parser.add_argument("--selftest", action="store_true", help="run built-in self-check and exit")
    args = parser.parse_args()

    if args.selftest:
        _selftest()
        return

    if not args.logfile:
        parser.error("logfile is required unless --selftest is given")

    identities = parse(args.logfile)
    print_report(identities)
    if args.csv:
        write_csv(identities, args.csv)
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    main()
