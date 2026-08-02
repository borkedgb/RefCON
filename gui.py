"""
RefCON's GUI. See README.md for the full walkthrough.

Needs: pip install tkinterdnd2

Usage:
    python gui.py
    python gui.py --selftest
"""
import os
import queue
import re
import struct
import threading
import webbrowser

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

import be_rcon
import history
import servers as servers_module
from altdetector import parse

COLUMNS = ("entity", "guid", "names", "ips", "identityId", "shares")
HEADINGS = ("Player#", "BE GUID", "Names", "IP(s)", "identityId", "Shares IP With")
WIDTHS = (60, 260, 200, 160, 220, 200)

HISTORIC_COLUMNS = COLUMNS + ("banned",)
HISTORIC_HEADINGS = HEADINGS + ("Status",)
HISTORIC_WIDTHS = WIDTHS + (70,)

BAN_COLUMNS = ("num", "type", "target", "duration", "reason")
BAN_HEADINGS = ("#", "Type", "Target", "Duration", "Reason")
BAN_WIDTHS = (40, 60, 280, 100, 300)

PLAYERS_COLUMNS = ("num", "ip", "port", "ping", "guid", "name", "status")
PLAYERS_HEADINGS = ("#", "IP", "Port", "Ping", "GUID", "Name", "Status")
PLAYERS_WIDTHS = (40, 130, 60, 50, 260, 220, 70)

RE_RCON_PORT = re.compile(r"Config entry: RConPort (?P<port>\d+)")
RE_RCON_PASSWORD = re.compile(r"Config entry: RConPassword (?P<password>[^\s']+)")
RE_SERVER_ADDRESS = re.compile(r"Server registered with address: (?P<host>[\d.]+):\d+")

#formatted from BE docs, confirmed against one real bans reply
BAN_LINE = re.compile(r"^\s*(?P<num>\d+)\s+(?P<target>\S+)\s+(?P<duration>perm|\d+)\s*(?P<reason>.*)$")

# "(?)" after the GUID is BE's verification flag, not something we need
PLAYER_LINE = re.compile(
    r"^\s*(?P<num>\d+)\s+(?P<ip>[\d.]+):(?P<port>\d+)\s+(?P<ping>-?\d+)\s+"
    r"(?P<guid>[0-9a-fA-F]+)\s*(?:\([^)]*\))?\s+(?P<name>.+?)\s*$"
)

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


def load_ico_entry(path, size):
    """pulls the exact size's image out of a multi-res .ico, tk.PhotoImage can't read .ico directly"""
    with open(path, "rb") as f:
        data = f.read()
    count = struct.unpack_from("<H", data, 4)[0]
    entries = [struct.unpack_from("<BBBBHHII", data, 6 + i * 16) for i in range(count)]
    width, _h, _c, _r, _planes, _bpp, entry_size, offset = next(e for e in entries if (e[0] or 256) == size)
    return tk.PhotoImage(data=data[offset : offset + entry_size])


def extract_rcon_config(path):
    port = password = host = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if port is None:
                m = RE_RCON_PORT.search(line)
                if m:
                    port = m["port"]
            if password is None:
                m = RE_RCON_PASSWORD.search(line)
                if m:
                    password = m["password"]
            if host is None:
                m = RE_SERVER_ADDRESS.search(line)
                if m:
                    host = m["host"]
            if port and password and host:
                break
    return host, port, password


def build_rows(hist, iids, banned_guids=None):
    """banned_guids adds a Status column, matched on BE GUID since that's all the ban list gives us."""
    flagged = history.shared_ip_iids(hist)
    rows = sorted(iids, key=lambda i: int(hist[i]["entity_id"] or -1))
    result = []
    for iid in rows:
        values = (
            hist[iid]["entity_id"] or "?",
            hist[iid]["be_guid"] or "?",
            ", ".join(hist[iid]["names"]),
            ", ".join(hist[iid]["ips"]),
            iid,
            ", ".join(sorted(history.alt_names(hist, iid))),
        )
        if banned_guids is not None:
            values += ("Banned" if hist[iid]["be_guid"] in banned_guids else "",)
        result.append((values, iid in flagged))
    return result


def matches_query(values, query):
    query = query.strip().lower()
    if not query:
        return True
    return any(query in str(v).lower() for v in values)


def _is_noise_line(line):
    low = line.lower()
    return (
        line.startswith("[#]")
        or set(line) == {"-"}
        or low.startswith("guid bans")
        or low.startswith("ip bans")
        or low.startswith("players on server")
        or (line.startswith("(") and line.endswith(")") and "player" in low)
    )


def parse_bans(text):
    """rows plus anything unrecognised, so a format drift shows up instead of vanishing"""
    rows = []
    skipped = []
    section = "GUID"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("guid bans"):
            section = "GUID"
            continue
        if low.startswith("ip bans"):
            section = "IP"
            continue
        if _is_noise_line(line):
            continue
        m = BAN_LINE.match(line)
        if m:
            rows.append((m["num"], section, m["target"], m["duration"], m["reason"].strip()))
        else:
            skipped.append(line)
    return rows, skipped


def parse_players(text, banned_guids=None):
    """banned_guids flags anyone connected who's also banned, which shouldn't happen but does"""
    banned_guids = banned_guids or set()
    rows = []
    skipped = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _is_noise_line(line):
            continue
        m = PLAYER_LINE.match(line)
        if m:
            status = "Banned" if m["guid"] in banned_guids else ""
            rows.append((m["num"], m["ip"], m["port"], m["ping"], m["guid"], m["name"], status))
        else:
            skipped.append(line)
    return rows, skipped


def fill_player_table(tree, hist, iids, banned_guids=None, query=""):
    tree.delete(*tree.get_children())
    for values, is_alt in build_rows(hist, iids, banned_guids):
        if matches_query(values, query):
            tree.insert("", "end", values=values, tags=("alt",) if is_alt else ())


def _sort_key(value):
    try:
        return (0, float(value))
    except ValueError:
        return (1, value.lower())


def sort_treeview(tree, col, reverse):
    """re-binds itself with the direction flipped, so the next click reverses it"""
    items = [(tree.set(k, col), k) for k in tree.get_children("")]
    items.sort(key=lambda pair: _sort_key(pair[0]), reverse=reverse)
    for index, (_value, k) in enumerate(items):
        tree.move(k, "", index)
    tree.heading(col, command=lambda: sort_treeview(tree, col, not reverse))


def make_player_table(parent, columns=COLUMNS, headings=HEADINGS, widths=WIDTHS):
    tree = ttk.Treeview(parent, columns=columns, show="headings")
    for col, heading, width in zip(columns, headings, widths):
        tree.heading(col, text=heading, command=lambda t=tree, c=col: sort_treeview(t, c, False))
        tree.column(col, width=width)
    tree.tag_configure("alt", background="#ffb3b3")
    tree.pack(fill="both", expand=True)
    return tree


class BanDialog(simpledialog.Dialog):
    """asks for reason and duration together, duration used to be silently hardcoded to permanent"""

    def __init__(self, parent, names, guid):
        self.names = names
        self.guid = guid
        self.reason = None
        self.duration = None
        super().__init__(parent, title="Ban via RCon")

    def body(self, master):
        tk.Label(master, text=f"Ban {self.names}\nGUID: {self.guid}", justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        tk.Label(master, text="Reason:").grid(row=1, column=0, sticky="e")
        self.reason_entry = tk.Entry(master, width=30)
        self.reason_entry.insert(0, "Ban evasion")
        self.reason_entry.grid(row=1, column=1)

        tk.Label(master, text="Duration (minutes, 0 = permanent):").grid(row=2, column=0, sticky="e")
        self.duration_entry = tk.Entry(master, width=30)
        self.duration_entry.insert(0, "0")
        self.duration_entry.grid(row=2, column=1)

        return self.reason_entry

    def validate(self):
        try:
            int(self.duration_entry.get().strip())
        except ValueError:
            messagebox.showerror("Ban via RCon", "Duration must be a whole number of minutes.")
            return False
        return True

    def apply(self):
        self.reason = self.reason_entry.get().strip() or "Ban evasion"
        self.duration = int(self.duration_entry.get().strip())


class AddServerDialog(simpledialog.Dialog):
    """for setting up RCon without ever dropping a log in first"""

    def __init__(self, parent):
        self.host = None
        self.port = None
        self.password = None
        self.nickname = None
        super().__init__(parent, title="Add Server")

    def body(self, master):
        tk.Label(master, text="Host / IP:").grid(row=0, column=0, sticky="e")
        self.host_entry = tk.Entry(master, width=30)
        self.host_entry.grid(row=0, column=1)

        tk.Label(master, text="RCon port:").grid(row=1, column=0, sticky="e")
        self.port_entry = tk.Entry(master, width=30)
        self.port_entry.insert(0, "2302")
        self.port_entry.grid(row=1, column=1)

        tk.Label(master, text="RCon password:").grid(row=2, column=0, sticky="e")
        self.password_entry = tk.Entry(master, width=30, show="*")
        self.password_entry.grid(row=2, column=1)

        tk.Label(master, text="Nickname (optional):").grid(row=3, column=0, sticky="e")
        self.nickname_entry = tk.Entry(master, width=30)
        self.nickname_entry.grid(row=3, column=1)

        return self.host_entry

    def validate(self):
        if not self.host_entry.get().strip():
            messagebox.showerror("Add Server", "Host is required.")
            return False
        try:
            int(self.port_entry.get().strip())
        except ValueError:
            messagebox.showerror("Add Server", "Port must be a number.")
            return False
        return True

    def apply(self):
        self.host = self.host_entry.get().strip()
        self.port = self.port_entry.get().strip()
        self.password = self.password_entry.get()
        self.nickname = self.nickname_entry.get().strip() or self.host


def main():
    servers = servers_module.load()
    current_host = None
    current_log_host = None  # which server current_log_iids belongs to
    current_log_iids = []
    display_to_host = {}
    banned_guids_by_host = {}
    ban_rows_by_host = {}
    player_rows_by_host = {}
    auto_refresh_interval_ms = 5000

    root = TkinterDnD.Tk()
    root.title("RefCON")
    root.geometry("1150x500")
    icon_path = os.path.join(ASSETS_DIR, "refcon_icon.ico")
    root.iconbitmap(icon_path)

    # --- Menu bar ---
    def show_about():
        messagebox.showinfo(
            "About RefCON",
            "RefCON\n"
            "A lightweight, portable RCON tool for Arma Reforger.\n\n"
            "Copyright (C) 2026 borked.gb\n\n"
            "This program comes with ABSOLUTELY NO WARRANTY.\n"
            "This is free software, and you are welcome to redistribute it\n"
            "under certain conditions. See LICENSE (GNU GPL v3) for details.",
        )

    def set_auto_refresh_interval():
        nonlocal auto_refresh_interval_ms
        seconds = simpledialog.askinteger(
            "Auto-refresh interval",
            "Refresh Live Players every N seconds:",
            initialvalue=auto_refresh_interval_ms // 1000,
            minvalue=1,
        )
        if seconds:
            auto_refresh_interval_ms = seconds * 1000

    menu_bar = tk.Menu(root)
    settings_menu = tk.Menu(menu_bar, tearoff=0)
    settings_menu.add_command(label="Live Players auto-refresh interval...", command=set_auto_refresh_interval)
    menu_bar.add_cascade(label="Settings", menu=settings_menu)
    help_menu = tk.Menu(menu_bar, tearoff=0)
    help_menu.add_command(label="About", command=show_about)
    menu_bar.add_cascade(label="Help", menu=help_menu)
    root.config(menu=menu_bar)

    # --- Server picker + RCon settings ---
    settings = tk.Frame(root)
    settings.pack(fill="x", padx=4, pady=4)

    logo_img = load_ico_entry(icon_path, 32)
    logo_label = tk.Label(settings, image=logo_img, cursor="hand2")
    logo_label.image = logo_img  # tkinter drops the image if nothing keeps a reference to it
    logo_label.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/borkedgb/RefCON"))
    logo_label.pack(side="right", padx=(0, 8))

    tk.Button(settings, text="+ Add Server", command=lambda: add_server()).pack(side="left", padx=(0, 8))

    tk.Label(settings, text="Server:").pack(side="left")
    server_combo = ttk.Combobox(settings, width=28, state="readonly")
    server_combo.pack(side="left", padx=(0, 8))

    tk.Label(settings, text="nickname:").pack(side="left")
    nickname_entry = tk.Entry(settings, width=16)
    nickname_entry.pack(side="left", padx=(0, 8))

    tk.Label(settings, text="port:").pack(side="left")
    port_entry = tk.Entry(settings, width=6)
    port_entry.pack(side="left", padx=(0, 8))

    tk.Label(settings, text="password:").pack(side="left")
    password_entry = tk.Entry(settings, width=16, show="*")
    password_entry.pack(side="left")

    status = tk.Label(root, text="Drop a console log file here", bg="#eee", anchor="w")
    status.pack(fill="x")

    def copy_value(value):
        root.clipboard_clear()
        root.clipboard_append(value)
        status.config(text=f"Copied: {value}")

    # --- Tabs ---
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)

    def add_filter_box(toolbar, on_change):
        entry = tk.Entry(toolbar, width=20)
        entry.pack(side="right", padx=(2, 4))
        tk.Label(toolbar, text="Filter:").pack(side="right")
        entry.bind("<KeyRelease>", lambda e: on_change())
        return entry

    current_tab = tk.Frame(notebook)
    historic_tab = tk.Frame(notebook)
    players_tab = tk.Frame(notebook)
    bans_tab = tk.Frame(notebook)
    notebook.add(current_tab, text="Current Log")
    notebook.add(historic_tab, text="Historic Logs")
    notebook.add(players_tab, text="Live Players")
    notebook.add(bans_tab, text="Ban List")

    current_toolbar = tk.Frame(current_tab)
    current_toolbar.pack(fill="x")
    current_search_entry = add_filter_box(current_toolbar, lambda: render_current())
    drop_label = tk.Label(current_tab, text="Drop a console log file here", bg="#ddd")
    drop_label.pack(fill="x")
    current_tree = make_player_table(current_tab)

    historic_toolbar = tk.Frame(historic_tab)
    historic_toolbar.pack(fill="x")
    tk.Button(historic_toolbar, text="Refresh", command=lambda: refresh_historic()).pack(side="left")
    tk.Button(historic_toolbar, text="Clear Historic Logs", command=lambda: clear_historic()).pack(side="right")
    historic_search_entry = add_filter_box(historic_toolbar, lambda: refresh_historic())
    historic_tree = make_player_table(historic_tab, HISTORIC_COLUMNS, HISTORIC_HEADINGS, HISTORIC_WIDTHS)

    players_toolbar = tk.Frame(players_tab)
    players_toolbar.pack(fill="x")
    players_search_entry = add_filter_box(players_toolbar, lambda: render_players())
    players_tree = ttk.Treeview(players_tab, columns=PLAYERS_COLUMNS, show="headings")
    for col, heading, width in zip(PLAYERS_COLUMNS, PLAYERS_HEADINGS, PLAYERS_WIDTHS):
        players_tree.heading(col, text=heading, command=lambda t=players_tree, c=col: sort_treeview(t, c, False))
        players_tree.column(col, width=width)
    # orange, not the Historic Logs red, different severity: banned but still connected (this should never happen)
    players_tree.tag_configure("alt", background="#ffcc66")
    players_tree.pack(fill="both", expand=True)

    ban_toolbar = tk.Frame(bans_tab)
    ban_toolbar.pack(fill="x")
    bans_search_entry = add_filter_box(ban_toolbar, lambda: render_bans())
    ban_tree = ttk.Treeview(bans_tab, columns=BAN_COLUMNS, show="headings")
    for col, heading, width in zip(BAN_COLUMNS, BAN_HEADINGS, BAN_WIDTHS):
        ban_tree.heading(col, text=heading, command=lambda t=ban_tree, c=col: sort_treeview(t, c, False))
        ban_tree.column(col, width=width)
    ban_tree.pack(fill="both", expand=True)

    # --- Server switching ---
    def refresh_server_dropdown():
        display_to_host.clear()
        displays = []
        for host, server in servers.items():
            d = servers_module.display_name(host, server)
            display_to_host[d] = host
            displays.append(d)
        server_combo["values"] = displays
        if current_host is not None:
            server_combo.set(servers_module.display_name(current_host, servers[current_host]))

    def refresh_historic():
        if current_host is None:
            fill_player_table(historic_tree, {}, [])
            return
        server = servers[current_host]
        fill_player_table(
            historic_tree,
            server["identities"],
            server["identities"].keys(),
            banned_guids_by_host.get(current_host),
            query=historic_search_entry.get(),
        )

    def render_current():
        if current_host is None or current_log_host != current_host:
            fill_player_table(current_tree, {}, [])
            return
        server = servers[current_host]
        fill_player_table(current_tree, server["identities"], current_log_iids, query=current_search_entry.get())

    def render_players():
        rows = player_rows_by_host.get(current_host, [])
        query = players_search_entry.get()
        players_tree.delete(*players_tree.get_children())
        for row in rows:
            if matches_query(row, query):
                players_tree.insert("", "end", values=row, tags=("alt",) if row[6] == "Banned" else ())

    def render_bans():
        rows = ban_rows_by_host.get(current_host, [])
        query = bans_search_entry.get()
        ban_tree.delete(*ban_tree.get_children())
        for row in rows:
            if matches_query(row, query):
                ban_tree.insert("", "end", values=row)

    def clear_historic():
        if current_host is None:
            messagebox.showerror("Historic Logs", "No server selected.")
            return
        display = servers_module.display_name(current_host, servers[current_host])
        if not messagebox.askyesno(
            "Clear historic logs",
            f"Clear all historic player data for {display}?\n\n"
            "This removes every account/IP/name it has learned from past logs "
            "for this server. It cannot be undone.",
        ):
            return
        servers[current_host]["identities"] = {}
        servers_module.save(servers)
        refresh_historic()
        status.config(text=f"Cleared historic logs for {display}")

    def add_server():
        dialog = AddServerDialog(root)
        if dialog.host is None:
            return
        server = servers_module.get_or_create(servers, dialog.host)
        server["port"] = dialog.port
        server["password"] = dialog.password
        server["nickname"] = dialog.nickname
        servers_module.save(servers)
        select_server(dialog.host)
        status.config(text=f"Added server {servers_module.display_name(dialog.host, server)}")

    def select_server(host):
        nonlocal current_host
        current_host = host
        server = servers_module.get_or_create(servers, host)

        nickname_entry.delete(0, "end")
        nickname_entry.insert(0, server["nickname"])
        port_entry.delete(0, "end")
        port_entry.insert(0, server["port"])
        password_entry.delete(0, "end")
        password_entry.insert(0, server["password"])

        refresh_server_dropdown()
        refresh_historic()
        render_current()
        render_bans()
        render_players()

    def on_server_selected(event=None):
        host = display_to_host.get(server_combo.get())
        if host and host != current_host:
            select_server(host)

    server_combo.bind("<<ComboboxSelected>>", on_server_selected)

    def on_nickname_change(event=None):
        if current_host is None:
            return
        servers[current_host]["nickname"] = nickname_entry.get().strip() or current_host
        servers_module.save(servers)
        refresh_server_dropdown()

    nickname_entry.bind("<FocusOut>", on_nickname_change)
    nickname_entry.bind("<Return>", on_nickname_change)

    def rcon_creds():
        """also saves whatever's currently typed in the port/password fields"""
        port = int(port_entry.get().strip())
        password = password_entry.get()
        servers[current_host]["port"] = port_entry.get().strip()
        servers[current_host]["password"] = password
        servers_module.save(servers)
        return current_host, port, password

    rcon_queue = queue.Queue()

    def poll_rcon_queue():
        try:
            while True:
                on_done, host, ok, payload = rcon_queue.get_nowait()
                on_done(host, ok, payload)
        except queue.Empty:
            pass
        root.after(100, poll_rcon_queue)

    root.after(100, poll_rcon_queue)

    def run_rcon(command, on_done, silent=False):
        """runs on a background thread so a slow server doesn't freeze the window
        (silent skips the setup error popups, for the auto-refresh tick)"""
        if current_host is None:
            if not silent:
                messagebox.showerror("RCon", "No server selected, drop a log first.")
            return
        try:
            host, port, password = rcon_creds()
        except ValueError:
            if not silent:
                messagebox.showerror("RCon", "Port must be a number.")
            return

        def worker():
            try:
                response = be_rcon.send_command(host, port, password, command)
                rcon_queue.put((on_done, host, True, response))
            except Exception as e:
                rcon_queue.put((on_done, host, False, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    # --- Ban via RCon ---
    def ban_via_rcon(guid, names):
        if not guid or guid == "?":
            messagebox.showerror("RCon", "No BE GUID known for this row.")
            return
        dialog = BanDialog(root, names, guid)
        if dialog.reason is None:
            return
        reason, duration = dialog.reason, dialog.duration
        duration_text = "permanent" if duration == 0 else f"{duration} minute(s)"
        if not messagebox.askyesno(
            "Confirm ban",
            f"Send addBan for {names}\nGUID: {guid}\nDuration: {duration_text}\nReason: {reason}\n\n"
            "This bans them on the live server right now. Continue?",
        ):
            return

        def on_done(host, ok, payload):
            if ok:
                status.config(text=f"RCon: {payload.strip() or 'ban sent, no reply text'}")
            else:
                messagebox.showerror("RCon error", payload)

        status.config(text="Sending ban via RCon...")
        run_rcon(f"addBan {guid} {duration} {reason}", on_done)

    def attach_player_context_menu(tree, headings=HEADINGS, guid_index=1, name_index=2):
        menu = tk.Menu(root, tearoff=0)

        def on_right_click(event):
            row = tree.identify_row(event.y)
            if not row:
                return
            tree.selection_set(row)
            values = tree.item(row, "values")
            menu.delete(0, "end")
            for heading, value in zip(headings, values):
                menu.add_command(label=f"Copy {heading}", command=lambda v=value: copy_value(v))
            menu.add_separator()
            guid, names = values[guid_index], values[name_index]
            menu.add_command(label="Ban via RCon...", command=lambda: ban_via_rcon(guid, names))
            menu.post(event.x_root, event.y_root)

        tree.bind("<Button-3>", on_right_click)

    attach_player_context_menu(current_tree)
    attach_player_context_menu(historic_tree, HISTORIC_HEADINGS)
    attach_player_context_menu(players_tree, PLAYERS_HEADINGS, guid_index=4, name_index=5)

    def on_drop(event):
        nonlocal current_log_host, current_log_iids
        paths = root.tk.splitlist(event.data)
        if not paths:
            return
        path = paths[0]
        try:
            identities = parse(path)
            host, port, password = extract_rcon_config(path)

            if host is None:
                if current_host is None:
                    status.config(
                        text=f"Could not identify which server {path} belongs to "
                        "(no 'Server registered with address' line found in it), and no "
                        "server is selected to fall back to. Nothing was loaded."
                    )
                    return
                current_display = servers_module.display_name(current_host, servers[current_host])
                if not messagebox.askyesno(
                    "Unknown server",
                    f"Couldn't find which server {path} belongs to (no 'Server "
                    "registered with address' line in it).\n\n"
                    f"Merge it into the currently selected server ({current_display}) anyway?",
                ):
                    status.config(text=f"Skipped {path}: could not identify its server.")
                    return
                host = current_host

            server = servers_module.get_or_create(servers, host)
            if port:
                server["port"] = port
            if password:
                server["password"] = password
            history.merge(server["identities"], identities)
            servers_module.save(servers)

            current_log_host = host
            current_log_iids = list(identities.keys())
            select_server(host)

            status.config(text=f"Loaded {path} for {servers_module.display_name(host, server)}")
        except Exception as e:
            status.config(text=f"Error reading {path}: {e}")

    root.drop_target_register(DND_FILES)
    drop_label.drop_target_register(DND_FILES)
    root.dnd_bind("<<Drop>>", on_drop)
    drop_label.dnd_bind("<<Drop>>", on_drop)

    # --- Ban list tab ---
    def refresh_bans():
        def on_done(host, ok, payload):
            if not ok:
                messagebox.showerror("RCon error", payload)
                return
            rows, skipped = parse_bans(payload)
            ban_rows_by_host[host] = rows
            banned_guids_by_host[host] = {target for _num, kind, target, _dur, _reason in rows if kind == "GUID"}
            if host == current_host:
                render_bans()
                refresh_historic()
            msg = f"Fetched {len(rows)} ban(s) from RCon"
            if skipped:
                msg += f", {len(skipped)} line(s) didn't match and were skipped, see console"
                for line in skipped:
                    print(f"[RefCON] Unparsed bans line: {line!r}")
            status.config(text=msg)

        status.config(text="Fetching ban list...")
        run_rcon("bans", on_done)

    tk.Button(ban_toolbar, text="Refresh from RCon", command=refresh_bans).pack(side="left")

    # --- Live players tab ---
    def refresh_players(auto=False):
        def on_done(host, ok, payload):
            if not ok:
                if auto:
                    status.config(text=f"Auto-refresh error: {payload}")
                else:
                    messagebox.showerror("RCon error", payload)
                return
            rows, skipped = parse_players(payload, banned_guids_by_host.get(host))
            player_rows_by_host[host] = rows
            if host == current_host:
                render_players()
            msg = f"Fetched {len(rows)} live player(s) from RCon"
            if skipped:
                msg += f", {len(skipped)} line(s) didn't match and were skipped, see console"
                for line in skipped:
                    print(f"[RefCON] Unparsed players line: {line!r}")
            status.config(text=msg)

        status.config(text="Auto-refreshing live players..." if auto else "Fetching live players...")
        run_rcon("players", on_done, silent=auto)

    tk.Button(players_toolbar, text="Refresh from RCon", command=refresh_players).pack(side="left")

    auto_refresh_var = tk.BooleanVar(value=False)
    tk.Checkbutton(players_toolbar, text="Auto-refresh", variable=auto_refresh_var).pack(side="left", padx=(8, 0))

    def auto_refresh_tick():
        if auto_refresh_var.get() and current_host is not None:
            refresh_players(auto=True)
        root.after(auto_refresh_interval_ms, auto_refresh_tick)

    root.after(auto_refresh_interval_ms, auto_refresh_tick)

    ban_menu = tk.Menu(root, tearoff=0)

    def unban(ban_num, target):
        if not messagebox.askyesno("Confirm unban", f"Remove ban #{ban_num} ({target})?"):
            return

        def on_done(host, ok, payload):
            if not ok:
                messagebox.showerror("RCon error", payload)
                return
            status.config(text=f"RCon: {payload.strip() or 'unban sent, no reply text'}")
            refresh_bans()

        status.config(text="Sending unban...")
        run_rcon(f"removeBan {ban_num}", on_done)

    def on_ban_right_click(event):
        row = ban_tree.identify_row(event.y)
        if not row:
            return
        ban_tree.selection_set(row)
        values = ban_tree.item(row, "values")
        num, _type, target = values[0], values[1], values[2]
        ban_menu.delete(0, "end")
        ban_menu.add_command(label="Copy Target", command=lambda: copy_value(target))
        ban_menu.add_command(label="Unban", command=lambda: unban(num, target))
        ban_menu.post(event.x_root, event.y_root)

    ban_tree.bind("<Button-3>", on_ban_right_click)

    refresh_server_dropdown()
    if servers:
        select_server(next(iter(servers)))

    root.mainloop()


def _selftest():
    hist = {
        "aaaa": {"entity_id": "1", "names": ["Alice"], "be_guid": "a" * 32, "steam_id": None, "ips": ["1.2.3.4"]},
        "bbbb": {"entity_id": "2", "names": ["Bob"], "be_guid": "b" * 32, "steam_id": None, "ips": ["1.2.3.4"]},
        "cccc": {"entity_id": "3", "names": ["Carol"], "be_guid": "c" * 32, "steam_id": None, "ips": ["9.9.9.9"]},
    }
    rows = build_rows(hist, hist.keys())
    flags = {values[0]: is_alt for values, is_alt in rows}
    assert flags == {"1": True, "2": True, "3": False}

    shares = {values[0]: values[5] for values, _ in rows}
    assert shares["1"] == "Bob"
    assert shares["2"] == "Alice"
    assert shares["3"] == ""

    banned_rows = build_rows(hist, hist.keys(), banned_guids={"a" * 32})
    banned_status = {values[0]: values[6] for values, _ in banned_rows}
    assert banned_status == {"1": "Banned", "2": "", "3": ""}

    from altdetector import Identity

    dave = Identity("dddd")
    dave.entity_id = "4"
    dave.names = ["Dave"]
    dave.be_guid = "d" * 32
    dave.connections = [("5.5.5.5", "1111")]
    history.merge(hist, {"dddd": dave})
    assert hist["dddd"]["ips"] == ["5.5.5.5"]
    assert hist["dddd"]["names"] == ["Dave"]

    ban_text = """\
GUID Bans:
[#] [GUID] [Minutes left] [Reason]
----------------------------------------
0 beguid0000000000000000000000000 perm cheating

IP Bans:
[#] [IP Address] [Minutes left] [Reason]
----------------------------------------------
1 1.2.3.4 1432 alt account
"""
    bans, skipped = parse_bans(ban_text)
    assert bans == [
        ("0", "GUID", "beguid0000000000000000000000000", "perm", "cheating"),
        ("1", "IP", "1.2.3.4", "1432", "alt account"),
    ]
    assert skipped == []

    bad_ban_text = ban_text + "this is not a real ban line\n"
    _bans, skipped = parse_bans(bad_ban_text)
    assert skipped == ["this is not a real ban line"]

    players_text = """\
Players on server:
[#] [IP Address]:[Port] [Ping] [GUID] [Name]
--------------------------------------------------
5   1.2.3.4:64333   -1   aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa(?)  Alice (Lobby)
3   9.9.9.9:59602   38   bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb(?)  Bob (Lobby)
(2 players in total)
"""
    players, skipped = parse_players(players_text, banned_guids={"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})
    assert players == [
        ("5", "1.2.3.4", "64333", "-1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Alice (Lobby)", ""),
        ("3", "9.9.9.9", "59602", "38", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Bob (Lobby)", "Banned"),
    ]
    assert skipped == []

    # an unparseable line (ping "?" here) used to vanish silently instead of showing up as skipped
    _players, skipped = parse_players(players_text + "9   1.2.3.4:1000   ?   deadbeefdeadbeefdeadbeefdeadbeef(?)  Weird\n")
    assert len(skipped) == 1 and "Weird" in skipped[0]

    servers = {}
    server = servers_module.get_or_create(servers, "1.2.3.4")
    assert server == {"nickname": "1.2.3.4", "port": "2302", "password": "", "identities": {}}
    assert servers_module.display_name("1.2.3.4", server) == "1.2.3.4"
    server["nickname"] = "My Server"
    assert servers_module.display_name("1.2.3.4", server) == "My Server (1.2.3.4)"

    assert sorted(["10", "2", "1"], key=_sort_key) == ["1", "2", "10"]
    assert sorted(["Bob", "alice", "Carol"], key=_sort_key) == ["alice", "Bob", "Carol"]
    assert sorted(["10", "abc"], key=_sort_key) == ["10", "abc"]

    row = ("1", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Alice", "1.2.3.4", "iid-1", "")
    assert matches_query(row, "") is True
    assert matches_query(row, "alice") is True
    assert matches_query(row, "ALICE") is True
    assert matches_query(row, "1.2.3.4") is True
    assert matches_query(row, "nobody") is False

    print("selftest OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
