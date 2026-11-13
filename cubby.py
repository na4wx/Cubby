#!/usr/bin/env python3
"""Cubby: left-edge dock for X11/XFCE: app drawer on top, one workspace per app, pins and folders.

Usage: ./cubby.py [--width PX] [--icon PX] [--config FILE] [--respect-top] [--respect-bottom]
  top button      opens the app drawer (all installed apps; type to filter, Enter launches)
  new app window  is moved to its own workspace and gets a button in the dock
  dock button     switches to that app's workspace and focuses it
                  (pinned app that isn't running: launches it)
  folder button   pops out the folder's apps; click one to launch or switch to it
  right-click     on any app, folder or drawer entry: pin/unpin, add to folder, move, close...
  bottom button   back to workspace 1, the empty "home" desktop

The bottom of the dock has a network indicator, a clock (click: calendar) and a user menu
(lock, suspend, log out, restart, shut down). Right-click any of them, or empty dock space,
for panel options (clock format, icon size, dock width, network indicator).

Pins, folders and panel options are saved in ~/.config/cubby/dock.json.
Windows already open when the dock starts are listed but not moved.
Needs python3-gi, gir1.2-gtk-3.0, gir1.2-wnck-3.0 and xprop.

Copyright (c) 2026 Jordan Webb. MIT License (see LICENSE). No warranty; use at your own risk.
"""
import argparse
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import warnings
from types import SimpleNamespace

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk, Wnck  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)  # Wnck class-group getters

DRAWER_WIDTH = 420
POPOUT_COLS = 4
CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                           "cubby", "dock.json")
GENERIC_EXEC = {"env", "sh", "bash", "python", "python3", "flatpak", "gtk-launch", "xdg-open"}
DEFAULT_OPTIONS = {"clock24": True, "seconds": False, "date": True, "network": True, "icon": 32, "width": 56}
OPTION_CHOICES = {"icon": (24, 32, 40, 48), "width": (48, 56, 64, 72)}

CSS = b"""
#dock, #drawer, #popout { background: #1b1d23; }
#dock, #drawer, #drawer label, #popout label { color: #e6e8ee; }
#dock button { color: #e6e8ee; min-width: 0; min-height: 0; margin: 3px 4px 3px 0; padding: 6px 4px 6px 7px;
               border: none; border-left: 3px solid transparent; border-radius: 8px;
               background: transparent; box-shadow: none; }
#dock button.running { border-left-color: #596070; }
#dock button:hover { background: #2c303a; }
#dock button.focused { background: #2f3b52; border-left-color: #6cb6ff; }
#dock button.minimized image { opacity: 0.5; }
#dock .foldericon { background: #333845; border-radius: 8px; padding: 2px; }
#drawer entry { margin: 10px; background: #262932; color: #e6e8ee; border: 1px solid #3a3f4b; }
#drawer scrolledwindow, #drawer viewport, #drawer list { background: transparent; }
#drawer row { padding: 6px 10px; background: transparent; }
#drawer row:hover { background: #262932; }
#drawer row:selected { background: #2f3b52; }
#drawer label.dim { opacity: 0.6; font-size: 90%; }
#popout label.title { font-weight: bold; opacity: 0.8; }
#popout button { color: #e6e8ee; padding: 6px; border: none; border-bottom: 2px solid transparent;
                 border-radius: 8px; background: transparent; box-shadow: none; }
#popout button:hover { background: #2c303a; }
#popout button.running { border-bottom-color: #6cb6ff; }
#dock button.clock { margin: 3px 4px; padding: 4px 0; border-left-width: 0; }
#dock .clock .time { font-weight: bold; font-size: 90%; }
#dock .clock .small { font-size: 70%; opacity: 0.75; }
#calpop, #netpop { background: #1b1d23; }
#calpop label, #netpop label { color: #e6e8ee; }
#calpop calendar { background: transparent; color: #e6e8ee; border: 1px solid #3a3f4b; }
#calpop calendar:selected { background: #2f3b52; }
#netpop label.dim { opacity: 0.6; font-size: 90%; }
#netpop button { color: #e6e8ee; background: #262932; border: 1px solid #3a3f4b; border-radius: 6px;
                 box-shadow: none; padding: 4px 10px; }
"""


# ---------------------------------------------------------------- helpers

TIME_WINDOW = []  # the dock's GdkWindow, once mapped; used to ask the X server for the time


def now():
    """Timestamp for activation requests; Wnck/WMs reject 0 (CurrentTime)."""
    t = Gtk.get_current_event_time()
    if not t and TIME_WINDOW:
        t = GdkX11.x11_get_server_time(TIME_WINDOW[0])
    return t


def icon_image(names, size):
    """Image for the first icon name the theme has (last name is the fallback)."""
    theme = Gtk.IconTheme.get_default()
    name = next((n for n in names if theme.has_icon(n)), names[-1])
    img = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DND)
    img.set_pixel_size(size)
    return img


def app_image(app, size):
    gicon = app.get_icon() if app else None
    img = Gtk.Image.new_from_gicon(gicon or Gio.ThemedIcon.new("application-x-executable"), Gtk.IconSize.DND)
    img.set_pixel_size(size)
    return img


def is_app_window(win):
    return (win.get_window_type() == Wnck.WindowType.NORMAL and not win.is_skip_tasklist()
            and win.get_transient() is None and win.get_pid() != os.getpid())


def group_key(win):
    """Windows of one app share a class group (dialogs included)."""
    cg = win.get_class_group()
    return (cg.get_res_class() if cg else "") or win.get_application().get_name() or str(win.get_pid())


def window_image(win, size):
    cg = win.get_class_group()
    names = [n.lower() for n in ((cg.get_res_class(), cg.get_name()) if cg else ()) if n]
    theme = Gtk.IconTheme.get_default()
    for n in names:
        if theme.has_icon(n):
            return icon_image([n], size)
    pix = win.get_icon()
    if pix:
        return Gtk.Image.new_from_pixbuf(pix.scale_simple(size, size, 2))  # 2 = BILINEAR
    return icon_image(["application-x-executable"], size)


def win_cands(win):
    """Lowercase names a window identifies itself by, to match it to a launcher."""
    cg = win.get_class_group()
    out = set()
    if cg:
        out |= {(cg.get_res_class() or "").lower(), (cg.get_name() or "").lower()}
    inst = getattr(win, "get_class_instance_name", lambda: None)()
    if inst:
        out.add(inst.lower())
    out.discard("")
    # a terminal we started for a launcher also carries the generic "xfce4-terminal" instance name; ignore it
    own = {c for c in out if c.startswith("cubby-")}
    return own or out


def app_ident(app):
    """Stable id for a launcher: its desktop id, or the file path for launchers outside the app dirs."""
    ident = app.get_id()
    try:  # some ids (desktop-folder launchers) don't resolve; use the path then
        if ident and Gio.DesktopAppInfo.new(ident):
            return ident
    except TypeError:
        pass
    return app.get_filename() or ident or ""


def terminal_class(ident):
    """Window class given to the terminal we start for a Terminal=true launcher, unique per launcher."""
    stem = os.path.basename(ident)
    stem = stem[:-8] if stem.endswith(".desktop") else stem
    return "cubby-" + (re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-") or "app")


def all_apps():
    """Installed launchers plus the .desktop files lying on the desktop (minus duplicates of installed ones)."""
    apps = [a for a in Gio.AppInfo.get_all() if a.should_show()]
    seen = {(a.get_name().lower(), os.path.basename(a.get_executable() or "")) for a in apps}
    folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
    for path in sorted(glob.glob(os.path.join(folder or "", "*.desktop"))):
        app = Gio.DesktopAppInfo.new_from_filename(path)
        if app and app.should_show() and (app.get_name().lower(), os.path.basename(app.get_executable() or "")) not in seen:
            apps.append(app)
    return apps


def app_cands(app):
    """(strong, weak) lowercase names a launcher is known by.

    Strong: StartupWMClass and desktop id. Weak: the executable name, only when the launcher
    runs it with no arguments (so "xfce4-terminal --preferences" doesn't claim terminal windows).
    """
    strong, weak = set(), set()
    wm = app.get_startup_wm_class() if hasattr(app, "get_startup_wm_class") else None
    if wm:
        strong.add(wm.lower())
    ident = app_ident(app)
    stem = os.path.basename(ident)
    strong.add((stem[:-8] if stem.endswith(".desktop") else stem).lower())
    if app.get_boolean("Terminal"):
        strong.add(terminal_class(ident).lower())
    argv = [t for t in (app.get_commandline() or "").split() if not t.startswith("%")]
    exe = os.path.basename(argv[0]).lower() if len(argv) == 1 else ""
    if exe and exe not in GENERIC_EXEC:
        weak.add(exe)
    strong.discard("")
    return strong, weak


def launch_in_terminal(app):
    """Run a Terminal=true launcher in its own terminal window, classed per launcher so the dock can tell
    it from other terminals. Returns False if no suitable terminal is installed."""
    argv = [t for t in shlex.split(app.get_commandline() or "") if not t.startswith("%")]
    if not argv:
        return False
    cwd = app.get_string("Path") or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    cls, title = terminal_class(app_ident(app)), app.get_name()
    if shutil.which("xfce4-terminal"):
        cmd = ["xfce4-terminal", "--disable-server", f"--class={cls}", f"--title={title}",
               f"--working-directory={cwd}", "-x"] + argv
    elif shutil.which("xterm"):
        cmd = ["xterm", "-class", cls, "-T", title, "-e"] + argv
    else:
        return False
    try:
        subprocess.Popen(cmd, cwd=cwd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"could not start {title}: {e}", file=sys.stderr)
    return True


def launch_app(app):
    if app.get_boolean("Terminal") and launch_in_terminal(app):
        return
    try:
        ctx = Gdk.Display.get_default().get_app_launch_context()
        ctx.set_timestamp(now())
        app.launch([], ctx)
    except GLib.Error as e:
        print(f"could not launch {app.get_name()}: {e.message}", file=sys.stderr)


def popup_menu(menu, owner, ev, widget=None, bottom=False):
    """Show a menu; flyouts stay open while it is up and close if focus didn't come back."""
    owner.menu_open = True

    def done(_m):
        def finish():
            owner.menu_open = False
            if isinstance(owner, Flyout) and owner.get_visible() and not owner.has_toplevel_focus():
                owner.close_flyout()
            return False
        GLib.timeout_add(150, finish)
    menu.connect("deactivate", done)
    menu.show_all()
    if widget and bottom:  # grow upward from the widget's bottom edge
        menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_EAST, Gdk.Gravity.SOUTH_WEST, ev)
    elif widget:
        menu.popup_at_widget(widget, Gdk.Gravity.EAST, Gdk.Gravity.NORTH_WEST, ev)
    else:
        menu.popup_at_pointer(ev)


def add_item(menu, label, callback=None, sensitive=True, submenu=None):
    item = Gtk.MenuItem(label=label)
    item.set_sensitive(sensitive)
    if submenu:
        item.set_submenu(submenu)
    elif callback:
        item.connect("activate", lambda _i: callback())
    menu.append(item)
    return item


# ---------------------------------------------------------------- network status

def default_route_dev():
    """Interface carrying the default route (lowest metric), from /proc/net/route."""
    best = None
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) > 6 and p[1] == "00000000" and int(p[3], 16) & 2 and (best is None or int(p[6]) < best[0]):
                    best = (int(p[6]), p[0])
    except (OSError, ValueError, StopIteration):
        pass
    return best[1] if best else None


def wifi_dbm(dev):
    """Signal level in dBm if dev is a Wi-Fi station, else None."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f.readlines()[2:]:
                name, rest = line.split(":", 1)
                if name.strip() == dev:
                    return float(rest.split()[2].rstrip("."))
    except (OSError, ValueError, IndexError):
        pass
    return None


def network_state():
    """(icon names to try, tooltip) for the panel's network indicator."""
    dev = default_route_dev()
    if not dev:
        return ["network-offline-symbolic", "network-error-symbolic"], "No network connection"
    dbm = wifi_dbm(dev)
    if dbm is None:
        return ["network-wired-symbolic"], f"Connected via {dev}"
    tier = "excellent" if dbm >= -55 else "good" if dbm >= -67 else "ok" if dbm >= -75 else "weak"
    return [f"network-wireless-signal-{tier}-symbolic", "network-wireless-symbolic"], \
        f"Wi-Fi on {dev}: {int(dbm)} dBm"


def nm_devices():
    """[(device, type, state, connection, ipv4)] from NetworkManager, minus loopback."""
    def fields(line):
        return [x.replace("\\:", ":") for x in re.split(r"(?<!\\):", line)]
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
                             capture_output=True, text=True, timeout=5).stdout
        addrs = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True,
                               timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    ips = {}
    for line in addrs.splitlines():
        f = line.split()
        if len(f) > 3 and f[2] == "inet":
            ips.setdefault(f[1], f[3])
    rows = [fields(l) + [""] * 4 for l in out.splitlines() if l]
    rows = [(r[0], r[1], r[2], r[3], ips.get(r[0], "")) for r in rows if r[1] != "loopback"]
    return sorted(rows, key=lambda r: (not r[2].startswith("connected"), r[0]))


# ---------------------------------------------------------------- saved pins and folders

class Config:
    """items: [{"t": "app", "id": desktop-id} | {"t": "folder", "name": str, "apps": [desktop-id]}]"""

    def __init__(self, path):
        self.path, self.items = path, []
        self.options = dict(DEFAULT_OPTIONS)
        self.workspace = {}  # app id / class key -> own-workspace on/off; absent means the default (on)
        try:
            with open(path) as f:
                data = json.load(f)
            for k, v in (data.get("options") or {}).items():
                if k in DEFAULT_OPTIONS and type(v) is type(DEFAULT_OPTIONS[k]) \
                        and (k not in OPTION_CHOICES or v in OPTION_CHOICES[k]):
                    self.options[k] = v
            self.workspace = {k: v for k, v in (data.get("workspace") or {}).items()
                              if isinstance(k, str) and isinstance(v, bool)}
            raw = data.get("items", [])
            for it in raw:
                if it.get("t") == "app" and isinstance(it.get("id"), str):
                    self.items.append({"t": "app", "id": it["id"]})
                elif it.get("t") == "folder" and isinstance(it.get("apps"), list):
                    apps = [a for a in it["apps"] if isinstance(a, str)]
                    self.items.append({"t": "folder", "name": str(it.get("name") or "Folder"), "apps": apps})
        except (OSError, ValueError, AttributeError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"items": self.items, "options": self.options, "workspace": self.workspace}, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def ids(self):
        out = set()
        for it in self.items:
            out |= {it["id"]} if it["t"] == "app" else set(it["apps"])
        return out

    def folders(self):
        return [it for it in self.items if it["t"] == "folder"]

    def folder_of(self, app_id):
        return next((f for f in self.folders() if app_id in f["apps"]), None)

    def _index(self, item):
        return next(i for i, it in enumerate(self.items) if it is item)

    def _detach(self, app_id):
        """Remove the app from wherever it is; an emptied folder disappears."""
        for i, it in enumerate(self.items):
            if it["t"] == "app" and it["id"] == app_id:
                del self.items[i]
                return
            if it["t"] == "folder" and app_id in it["apps"]:
                it["apps"].remove(app_id)
                if not it["apps"]:
                    del self.items[i]
                return

    def pin(self, app_id):
        if app_id not in self.ids():
            self.items.append({"t": "app", "id": app_id})

    def unpin(self, app_id):
        self._detach(app_id)

    def add_to_folder(self, app_id, folder):
        if app_id in folder["apps"]:
            return
        self._detach(app_id)
        folder["apps"].append(app_id)

    def new_folder(self, name, app_id):
        self._detach(app_id)
        self.items.append({"t": "folder", "name": name, "apps": [app_id]})

    def move_out(self, app_id):
        folder = self.folder_of(app_id)
        if not folder:
            return
        i = self._index(folder)
        folder["apps"].remove(app_id)
        entry = {"t": "app", "id": app_id}
        if folder["apps"]:
            self.items.insert(i + 1, entry)
        else:
            self.items[i] = entry

    def delete_folder(self, folder):
        i = self._index(folder)
        self.items[i:i + 1] = [{"t": "app", "id": a} for a in folder["apps"]]

    def move(self, item, delta):
        i = self._index(item)
        j = max(0, min(len(self.items) - 1, i + delta))
        self.items.insert(j, self.items.pop(i))

    def app_item(self, app_id):
        return next((it for it in self.items if it["t"] == "app" and it["id"] == app_id), None)


# ---------------------------------------------------------------- flyouts (drawer, folder popout)

class Flyout(Gtk.Window):
    """Undecorated panel next to the dock that closes when it loses focus."""

    def __init__(self, name):
        super().__init__()
        self.hidden_at = 0
        self.menu_open = False
        self.set_name(name)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.stick()
        self.connect("focus-out-event", self.on_focus_out)
        self.connect("key-press-event", self.on_key)

    def on_focus_out(self, *_):
        if not self.menu_open:
            self.close_flyout()

    def on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.close_flyout()
            return True
        return False

    def close_flyout(self):
        if self.get_visible():
            self.hidden_at = GLib.get_monotonic_time()
            self.hide()

    def recently_closed(self):
        """True for a moment after focus loss closed us: the click that did it shouldn't reopen us."""
        return GLib.get_monotonic_time() - self.hidden_at < 250000


class Drawer(Flyout):
    """Full-height launcher listing every installed app."""

    def __init__(self, dock):
        super().__init__("drawer")
        self.dock = dock
        self.dirty = True
        self.rows = {}  # ListBoxRow -> (AppInfo, lowercase search text)
        self.set_default_size(DRAWER_WIDTH, 600)

        self.entry = Gtk.SearchEntry(placeholder_text="Search applications")
        self.list = Gtk.ListBox()
        self.list.set_filter_func(self.matches)
        self.list.connect("row-activated", lambda _l, row: self.launch(self.rows[row][0]))
        self.list.connect("button-press-event", self.on_list_click)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.list)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(self.entry, False, False, 0)
        box.pack_start(sw, True, True, 0)
        self.add(box)

        self.entry.connect("search-changed", self.on_search)
        self.entry.connect("activate", self.on_enter)
        Gio.AppInfoMonitor.get().connect("changed", lambda *_: setattr(self, "dirty", True))

    def populate(self):
        for row in self.list.get_children():
            row.destroy()
        self.rows.clear()
        apps = sorted(all_apps(), key=lambda a: a.get_name().lower())
        for app in apps:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(spacing=10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            text.pack_start(Gtk.Label(label=app.get_name(), xalign=0), False, False, 0)
            desc = app.get_description()
            if desc:
                d = Gtk.Label(label=desc, xalign=0, ellipsize=3)  # 3 = END
                d.get_style_context().add_class("dim")
                text.pack_start(d, False, False, 0)
            box.pack_start(app_image(app, 32), False, False, 0)
            box.pack_start(text, True, True, 0)
            row.add(box)
            extra = []
            if hasattr(app, "get_keywords"):
                extra = list(app.get_keywords() or [])
            if hasattr(app, "get_generic_name"):
                extra.append(app.get_generic_name() or "")
            self.rows[row] = (app, " ".join([app.get_name(), desc or ""] + extra).lower())
            self.list.add(row)
        self.list.show_all()
        self.dirty = False

    def matches(self, row):
        words = self.entry.get_text().lower().split()
        hay = self.rows[row][1]
        return all(w in hay for w in words)

    def first_match(self):
        return next((r for r in self.list.get_children() if self.matches(r)), None)

    def select_first(self):
        row = self.first_match()
        if row:
            self.list.select_row(row)

    def on_search(self, _entry):
        self.list.invalidate_filter()
        self.select_first()

    def on_enter(self, _entry):
        row = self.list.get_selected_row() or self.first_match()
        if row:
            self.launch(self.rows[row][0])

    def on_list_click(self, _list, ev):
        if ev.button != 3:
            return False
        row = self.list.get_row_at_y(int(ev.y))
        if not row:
            return False
        self.list.select_row(row)
        self.dock.app_menu(app_ident(self.rows[row][0]), None, ev, owner=self)
        return True

    def on_key(self, w, ev):
        if super().on_key(w, ev):
            return True
        if ev.keyval == Gdk.KEY_Down and self.entry.is_focus():
            row = self.list.get_selected_row() or self.first_match()
            if row:
                row.grab_focus()
            return True
        if not self.entry.is_focus() and Gdk.keyval_to_unicode(ev.keyval) > 31:
            self.entry.grab_focus_without_selecting()  # let the keystroke reach the entry
        return False

    def launch(self, app):
        self.close_flyout()
        launch_app(app)

    def open_at(self, area, dock_width):
        if self.dirty:
            self.populate()
        self.entry.set_text("")
        self.resize(DRAWER_WIDTH, area.height)
        self.move(area.x + dock_width, area.y)
        self.show_all()
        self.move(area.x + dock_width, area.y)
        self.present()
        self.entry.grab_focus()
        self.select_first()


class Popout(Flyout):
    """Small icon grid showing the apps inside one dock folder."""

    def __init__(self, dock):
        super().__init__("popout")
        self.dock, self.folder = dock, None
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("title")
        self.grid = Gtk.Grid(row_spacing=4, column_spacing=4)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        box.pack_start(self.title, False, False, 0)
        box.pack_start(self.grid, False, False, 0)
        self.add(box)

    def open_for(self, folder, anchor_y):
        self.folder = folder
        for c in self.grid.get_children():
            c.destroy()
        self.title.set_text(folder["name"])
        n = 0
        for app_id in folder["apps"]:
            app = self.dock.lookup(app_id)
            if not app:
                continue
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_tooltip_text(app.get_name())
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cell.pack_start(app_image(app, 40), False, False, 0)
            cell.pack_start(Gtk.Label(label=app.get_name(), ellipsize=3, width_chars=9, max_width_chars=9),
                            False, False, 0)
            btn.add(cell)
            if self.dock.app_wins.get(app_id):
                btn.get_style_context().add_class("running")
            btn.connect("button-press-event", self.on_click, app_id)
            self.grid.attach(btn, n % POPOUT_COLS, n // POPOUT_COLS, 1, 1)
            n += 1
        self.resize(1, 1)
        self.show_all()
        _, h = self.get_preferred_height()
        a = self.dock.area
        y = max(a.y, min(anchor_y, a.y + a.height - h))
        self.move(a.x + self.dock.width, y)
        self.present()
        self.move(a.x + self.dock.width, y)

    def on_click(self, btn, ev, app_id):
        if ev.button == 1:
            self.close_flyout()
            self.dock.activate_app(app_id)
        elif ev.button == 3:
            self.dock.app_menu(app_id, self.dock.app_wins.get(app_id), ev, owner=self, widget=btn)
        return True


class CalPop(Flyout):
    """Big clock, full date and a month calendar, opened from the panel clock."""

    def __init__(self, dock):
        super().__init__("calpop")
        self.dock = dock
        self.time, self.date, self.cal = Gtk.Label(), Gtk.Label(), Gtk.Calendar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        for w in (self.time, self.date, self.cal):
            box.pack_start(w, False, False, 0)
        self.add(box)

    def open_for(self, btn):
        t = time.localtime()
        fmt = "%H:%M:%S" if self.dock.cfg.options["clock24"] else "%-I:%M:%S %p"
        self.time.set_markup(f'<span size="xx-large" weight="bold">{time.strftime(fmt, t)}</span>')
        self.date.set_text(time.strftime("%A, %B %-d, %Y", t))
        self.cal.select_month(t.tm_mon - 1, t.tm_year)
        self.cal.select_day(t.tm_mday)
        self.dock.show_flyout(self, btn)


class NetPop(Flyout):
    """NetworkManager's devices and their connections, opened from the network indicator."""

    def __init__(self, dock):
        super().__init__("netpop")
        self.dock = dock
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        self.add(self.box)

    def open_for(self, btn):
        for c in self.box.get_children():
            c.destroy()
        title = Gtk.Label(label="Network", xalign=0)
        title.get_style_context().add_class("title")
        self.box.pack_start(title, False, False, 0)
        rows = nm_devices()
        if not rows:
            self.box.pack_start(Gtk.Label(label="NetworkManager isn't available", xalign=0), False, False, 0)
        for dev, kind, state, conn, ip in rows:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            row.pack_start(Gtk.Label(label=conn if conn and conn != "--" else dev, xalign=0), False, False, 0)
            detail = Gtk.Label(label=" · ".join(x for x in (dev, kind, state, ip) if x), xalign=0)
            detail.get_style_context().add_class("dim")
            row.pack_start(detail, False, False, 0)
            self.box.pack_start(row, False, False, 4)
        if shutil.which("nm-connection-editor"):
            edit = Gtk.Button(label="Network settings…")
            edit.connect("clicked", lambda _b: (self.close_flyout(), self.dock.spawn(["nm-connection-editor"])))
            self.box.pack_start(edit, False, False, 4)
        self.dock.show_flyout(self, btn)


# ---------------------------------------------------------------- dock

class Dock(Gtk.Window):
    def __init__(self, width, icon, cfg, respect_top=False, respect_bottom=False):
        super().__init__()
        self.width, self.icon, self.cfg = width, icon, cfg
        self.menu_open = False
        self.wnck = Wnck.Screen.get_default()
        self.wnck.force_update()
        self.base = self.wnck.get_workspace_count()  # never shrink below the user's own count
        self.specs = {}      # button -> ("app", id) | ("folder", folder) | ("run", class key)
        self.order = []      # class keys of running, unpinned apps in the order they appeared
        self.claims = {}     # class key -> workspace number
        self.app_wins = {}   # pinned desktop id -> its windows (bottom to top)
        self.app_cache, self.cand_cache, self.cand_index = {}, {}, None
        self.layout_sig = None
        self.refresh_id = 0
        self.drawer = Drawer(self)
        self.popout = Popout(self)
        self.calpop = CalPop(self)
        self.netpop = NetPop(self)

        self.set_name("dock")
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.stick()

        self.menu_btn = self.make_button(
            icon_image(["view-app-grid-symbolic", "view-grid-symbolic", "applications-other",
                        "start-here"], icon), "Applications", self.toggle_drawer)
        self.net_img = Gtk.Image()
        self.net_icon_name = None
        self.net_btn = self.panel_button(self.net_img, "Network", self.toggle_net)
        self.clock_time, self.clock_sub, self.clock_date = Gtk.Label(), Gtk.Label(), Gtk.Label()
        clock_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for lbl, cls in ((self.clock_time, "time"), (self.clock_sub, "small"), (self.clock_date, "small")):
            lbl.get_style_context().add_class(cls)
            lbl.set_no_show_all(True)
            clock_box.pack_start(lbl, False, False, 0)
        self.clock_time.show()
        self.clock_btn = self.panel_button(clock_box, "", self.toggle_clock)
        self.clock_btn.get_style_context().add_class("clock")
        self.user_img = icon_image(["avatar-default-symbolic", "system-users-symbolic",
                                    "system-shutdown-symbolic"], self.small())
        self.user_btn = self.panel_button(self.user_img, GLib.get_user_name(), self.show_user_menu)
        self.net_btn.set_no_show_all(True)  # shown or hidden by the "network indicator" option
        self.net_img.show()
        self.items_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        sw.add(self.items_box)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(self.menu_btn, False, False, 6)
        box.pack_start(Gtk.Separator(), False, False, 2)
        box.pack_start(sw, True, True, 4)
        for w in (self.user_btn, self.clock_btn, self.net_btn):  # bottom-up: user, clock, network above
            box.pack_end(w, False, False, 2)
        box.pack_end(Gtk.Separator(), False, False, 2)
        self.add(box)
        self.connect("button-press-event", self.on_dock_click)

        disp = Gdk.Display.get_default()
        mon = disp.get_primary_monitor() or disp.get_monitor(0)
        geo, wa = mon.get_geometry(), mon.get_workarea()
        # Full screen height by default (a top bar that starts to the right of the dock doesn't overlap it).
        top = wa.y if respect_top else geo.y
        bottom = wa.y + wa.height if respect_bottom else geo.y + geo.height
        self.area = SimpleNamespace(x=geo.x, y=top, width=geo.width, height=bottom - top)
        self.set_size_request(width, self.area.height)
        self.resize(width, self.area.height)
        self.move(self.area.x, self.area.y)
        self.connect("map-event", lambda *_: self.set_struts())

        for win in self.wnck.get_windows():
            self.watch(win)
        self.wnck.connect("window-opened", self.on_opened)
        self.wnck.connect("window-closed", self.on_closed)
        self.wnck.connect("active-window-changed", self.queue_refresh)
        self.wnck.connect("active-workspace-changed", self.queue_refresh)
        Gio.AppInfoMonitor.get().connect("changed", self.on_apps_changed)
        self.refresh()
        self.update_clock()
        self.update_network()
        self.net_btn.set_visible(self.cfg.options["network"])
        GLib.timeout_add(1000, self.update_clock)
        GLib.timeout_add_seconds(3, self.update_network)

    def set_struts(self):
        """Reserve the dock's strip so maximized windows don't cover it."""
        a = self.area
        left = a.x + self.width
        vals = f"{left},0,0,0,{a.y},{a.y + a.height - 1},0,0,0,0,0,0"
        TIME_WINDOW[:] = [self.get_window()]
        xid = str(self.get_window().get_xid())
        try:
            for prop, v in (("_NET_WM_STRUT_PARTIAL", vals), ("_NET_WM_STRUT", f"{left},0,0,0")):
                subprocess.run(["xprop", "-id", xid, "-f", prop, "32c", "-set", prop, v],
                               check=False, capture_output=True)
        except OSError:
            print("xprop not found: the dock will overlap maximized windows", file=sys.stderr)

    def make_button(self, image, tip, on_click):
        b = Gtk.Button()
        b.add(image)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_tooltip_text(tip)
        b.connect("clicked", on_click)
        return b

    def app_windows(self):
        return [w for w in self.wnck.get_windows_stacked() if is_app_window(w)]

    # ----- installed apps and matching windows to launchers

    def lookup(self, app_id):
        if app_id not in self.app_cache:
            app = None
            try:
                app = (Gio.DesktopAppInfo.new_from_filename(app_id) if os.path.isabs(app_id)
                       else Gio.DesktopAppInfo.new(app_id))
            except TypeError:  # constructor returned NULL: id no longer resolves
                pass
            self.app_cache[app_id] = app
        return self.app_cache[app_id]

    def cands(self, app_id):
        if app_id not in self.cand_cache:
            app = self.lookup(app_id)
            self.cand_cache[app_id] = app_cands(app) if app else (set(), set())
        return self.cand_cache[app_id]

    def find_app_id(self, win):
        """Desktop id of the installed launcher a window belongs to, if any.

        Visible launchers only, and a WM-class/desktop-id match beats an executable-name match.
        """
        if self.cand_index is None:
            self.cand_index = ({}, {})
            for app in all_apps():
                for tier, names in zip(self.cand_index, app_cands(app)):
                    for c in names:
                        tier.setdefault(c, app_ident(app))
        wc = win_cands(win)
        for tier in self.cand_index:
            hit = next((tier[c] for c in sorted(wc) if c in tier), None)
            if hit:
                return hit
        return None

    def pinned_id_for(self, win, pinned):
        wc = win_cands(win)
        for rank in (0, 1):  # strong matches across all pins first, then executable-name matches
            hit = next((i for i in sorted(pinned) if wc & self.cands(i)[rank]), None)
            if hit:
                return hit
        return None

    def on_apps_changed(self, *_):
        self.app_cache, self.cand_cache, self.cand_index = {}, {}, None
        self.relayout()

    # ----- buttons

    def queue_refresh(self, *_):
        if not self.refresh_id:
            self.refresh_id = GLib.idle_add(self.refresh)

    def relayout(self):
        """Pins/folders changed: close the popout and rebuild the button list."""
        self.layout_sig = None
        self.popout.close_flyout()
        self.queue_refresh()

    def layout_key(self):
        keys = []
        for it in self.cfg.items:
            if it["t"] == "app" and self.lookup(it["id"]):
                keys.append(("app", it["id"]))
            elif it["t"] == "folder" and any(self.lookup(i) for i in it["apps"]):
                keys.append(("folder", it["name"], tuple(it["apps"])))
        return tuple(keys)

    def refresh(self):
        self.refresh_id = 0
        pinned = self.cfg.ids()
        self.app_wins, groups, live = {}, {}, set()
        for w in self.app_windows():
            live.add(group_key(w))
            pid = self.pinned_id_for(w, pinned)
            if pid:
                self.app_wins.setdefault(pid, []).append(w)
            else:
                groups.setdefault(group_key(w), []).append(w)
        self.order = [k for k in self.order if k in groups] + [k for k in groups if k not in self.order]
        for k in [k for k in self.claims if k not in live]:
            del self.claims[k]

        sig = (self.layout_key(), tuple(self.order))
        if sig != self.layout_sig:
            self.rebuild(groups)
            self.layout_sig = sig
        self.update_styles(groups)
        return False

    def make_item_button(self, image, spec):
        b = Gtk.Button()
        b.add(image)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.connect("button-press-event", self.on_item_click, spec)
        self.specs[b] = spec
        self.items_box.pack_start(b, False, False, 0)
        return b

    def folder_icon(self, folder):
        grid = Gtk.Grid(row_spacing=2, column_spacing=2, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        size = (self.icon - 8) // 2
        shown = [i for i in folder["apps"] if self.lookup(i)][:4]
        for n, app_id in enumerate(shown):
            grid.attach(app_image(self.lookup(app_id), size), n % 2, n // 2, 1, 1)
        frame = Gtk.Box()
        frame.get_style_context().add_class("foldericon")
        frame.set_size_request(self.icon, self.icon)
        frame.add(grid)
        return frame

    def rebuild(self, groups):
        for c in self.items_box.get_children():
            c.destroy()
        self.specs = {}
        shown = 0
        for it in self.cfg.items:
            if it["t"] == "app":
                app = self.lookup(it["id"])
                if app:
                    self.make_item_button(app_image(app, self.icon), ("app", it["id"]))
                    shown += 1
            elif any(self.lookup(i) for i in it["apps"]):
                self.make_item_button(self.folder_icon(it), ("folder", it))
                shown += 1
        if shown and self.order:
            self.items_box.pack_start(Gtk.Separator(), False, False, 4)
        for key in self.order:
            top = groups[key][-1]
            aid = self.find_app_id(top)
            app = self.lookup(aid) if aid else None
            self.make_item_button(app_image(app, self.icon) if app and app.get_icon()
                                  else window_image(top, self.icon), ("run", key))
        self.items_box.show_all()

    def update_styles(self, groups):
        active = self.wnck.get_active_window()
        akey = group_key(active) if active else None
        for btn, spec in self.specs.items():
            kind = spec[0]
            if kind == "run":
                wins = groups.get(spec[1], [])
                tip = wins[-1].get_name() + (f"  (+{len(wins) - 1})" if len(wins) > 1 else "") if wins else ""
            elif kind == "app":
                wins = self.app_wins.get(spec[1], [])
                app = self.lookup(spec[1])
                tip = app.get_name() if app else spec[1]
            else:
                wins = [w for i in spec[1]["apps"] for w in self.app_wins.get(i, [])]
                tip = spec[1]["name"]
            ctx = btn.get_style_context()
            states = (("running", bool(wins)), ("focused", any(group_key(w) == akey for w in wins)),
                      ("minimized", bool(wins) and all(w.is_minimized() for w in wins)))
            for cls, on in states:
                (ctx.add_class if on else ctx.remove_class)(cls)
            btn.set_tooltip_text(tip)

    # ----- clicks and menus

    def on_item_click(self, btn, ev, spec):
        kind = spec[0]
        if ev.button == 1:
            if kind == "run":
                self.focus_windows(self.group(spec[1]))
            elif kind == "app":
                self.activate_app(spec[1])
            else:
                self.toggle_folder(btn, spec[1])
        elif ev.button == 3:
            if kind == "run":
                wins = self.group(spec[1])
                self.app_menu(self.find_app_id(wins[0]) if wins else None, wins, ev, widget=btn)
            elif kind == "app":
                self.app_menu(spec[1], self.app_wins.get(spec[1], []), ev, widget=btn)
            else:
                self.folder_menu(spec[1], ev, btn)
        return True

    def group(self, key):
        return [w for w in self.app_windows() if group_key(w) == key]

    def focus_windows(self, wins):
        if not wins:
            return
        win = wins[-1]  # topmost
        ws = win.get_workspace()
        if ws:
            ws.activate(now())
        if win.is_minimized():
            win.unminimize(now())
        win.activate(now())

    def activate_app(self, app_id):
        wins = self.app_wins.get(app_id)
        if wins:
            self.focus_windows(wins)
        elif self.lookup(app_id):
            launch_app(self.lookup(app_id))

    def toggle_folder(self, btn, folder):
        p = self.popout
        if p.get_visible() and p.folder is folder:
            p.close_flyout()
        elif not (p.folder is folder and p.recently_closed()):
            pos = btn.translate_coordinates(self, 0, 0)
            p.open_for(folder, self.area.y + (pos[1] if pos else 0))

    def toggle_drawer(self, *_):
        d = self.drawer
        if d.get_visible():
            d.close_flyout()
        elif not d.recently_closed():
            d.open_at(self.area, self.width)

    def go_home(self, *_):
        self.wnck.get_workspace(0).activate(now())

    # ----- bottom panel: network, clock, user menu, options

    def small(self):
        return max(16, int(self.icon * 0.75))

    def panel_button(self, child, tip, on_left):
        b = Gtk.Button()
        b.add(child)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_tooltip_text(tip)
        b.connect("button-press-event", self.on_panel_click, on_left)
        return b

    def on_panel_click(self, btn, ev, on_left):
        if ev.button == 1:
            on_left(btn, ev)
        elif ev.button == 3:
            popup_menu(self.options_menu(), self, ev, btn, bottom=True)
        return True

    def on_dock_click(self, _w, ev):
        if ev.button == 3:  # empty part of the dock
            popup_menu(self.options_menu(), self, ev)
            return True
        return False

    def flyout_y(self, btn, height):
        """Top y for a flyout of this height whose bottom lines up with the button's bottom."""
        pos = btn.translate_coordinates(self, 0, 0)
        bottom = self.area.y + (pos[1] if pos else 0) + btn.get_allocated_height()
        return max(self.area.y, min(bottom - height, self.area.y + self.area.height - height))

    def show_flyout(self, win, btn):
        win.resize(1, 1)
        win.show_all()
        _, h = win.get_preferred_height()
        x, y = self.area.x + self.width, self.flyout_y(btn, h)
        win.move(x, y)
        win.present()
        win.move(x, y)

    def toggle_flyout(self, win, btn):
        if win.get_visible():
            win.close_flyout()
        elif not win.recently_closed():
            win.open_for(btn)

    def toggle_clock(self, btn, _ev):
        self.toggle_flyout(self.calpop, btn)

    def toggle_net(self, btn, _ev):
        self.toggle_flyout(self.netpop, btn)

    def spawn(self, cmd):
        try:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"could not run {cmd[0]}: {e}", file=sys.stderr)

    def update_clock(self):
        o, t = self.cfg.options, time.localtime()
        self.clock_time.set_text(time.strftime("%H:%M" if o["clock24"] else "%-I:%M", t))
        sub = ((time.strftime(":%S", t) if o["seconds"] else "")
               + ("" if o["clock24"] else time.strftime(" %p", t))).strip()
        self.clock_sub.set_text(sub)
        self.clock_sub.set_visible(bool(sub))
        self.clock_date.set_text(time.strftime("%b %-d", t))
        self.clock_date.set_visible(o["date"])
        self.clock_btn.set_tooltip_text(time.strftime("%A, %B %-d, %Y", t))
        return True

    def update_network(self):
        names, tip = network_state()
        theme = Gtk.IconTheme.get_default()
        name = next((n for n in names if theme.has_icon(n)), names[-1])
        if name != self.net_icon_name:
            self.net_icon_name = name
            self.net_img.set_from_icon_name(name, Gtk.IconSize.DND)
            self.net_img.set_pixel_size(self.small())
        self.net_btn.set_tooltip_text(tip)
        return True

    def confirm(self, text, action):
        dlg = Gtk.MessageDialog(text=text, message_type=Gtk.MessageType.QUESTION, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, action, Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        dlg.show_all()
        dlg.present()
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        return ok

    def power_action(self, action, cmd, warning=None):
        if warning and not self.confirm(warning, action):
            return
        self.spawn(cmd)

    def show_user_menu(self, btn, ev):
        menu = Gtk.Menu()
        add_item(menu, f"{GLib.get_user_name()}@{GLib.get_host_name()}", sensitive=False)
        menu.append(Gtk.SeparatorMenuItem())
        have = bool(shutil.which("xfce4-session-logout"))
        loss = " Network services and remote sessions on it will stop."
        entries = (
            ("Lock screen", ["xflock4"], None, bool(shutil.which("xflock4"))),
            ("Suspend", ["xfce4-session-logout", "--suspend"], None, have),
            ("Log out", ["xfce4-session-logout", "--logout"], "Log out of this session?", have),
            ("Restart", ["xfce4-session-logout", "--reboot"], "Restart this computer?" + loss, have),
            ("Shut down", ["xfce4-session-logout", "--halt"], "Shut down this computer?" + loss, have),
        )
        for label, cmd, warning, ok in entries:
            add_item(menu, label, lambda a=label, c=cmd, w=warning: self.power_action(a, c, w), sensitive=ok)
        menu.append(Gtk.SeparatorMenuItem())
        add_item(menu, "Panel options", submenu=self.options_menu())
        popup_menu(menu, self, ev, btn, bottom=True)

    def set_option(self, key, value):
        self.cfg.options[key] = value
        self.cfg.save()
        if key == "icon":
            self.icon = value
            self.menu_btn.get_child().set_pixel_size(value)
            for img in (self.net_img, self.user_img):
                img.set_pixel_size(self.small())
            self.relayout()
        elif key == "width":
            self.width = value
            self.set_size_request(value, self.area.height)
            self.resize(value, self.area.height)
            self.set_struts()
        self.update_clock()
        self.net_btn.set_visible(self.cfg.options["network"])

    def options_menu(self):
        menu, o = Gtk.Menu(), self.cfg.options

        def check(parent, label, key):
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(o[key])
            item.connect("toggled", lambda i: self.set_option(key, i.get_active()))
            parent.append(item)

        def radio(parent, key, current):
            group = None
            for v in OPTION_CHOICES[key]:
                item = Gtk.RadioMenuItem.new_with_label_from_widget(group, f"{v} px")
                group = group or item
                item.set_active(v == current)
                item.connect("toggled", lambda i, v=v: i.get_active() and self.set_option(key, v))
                parent.append(item)

        clock = Gtk.Menu()
        check(clock, "24-hour time", "clock24")
        check(clock, "Show seconds", "seconds")
        check(clock, "Show date", "date")
        add_item(menu, "Clock", submenu=clock)
        check(menu, "Show network indicator", "network")
        icons, widths = Gtk.Menu(), Gtk.Menu()
        radio(icons, "icon", self.icon)
        radio(widths, "width", self.width)
        add_item(menu, "Icon size", submenu=icons)
        add_item(menu, "Dock width", submenu=widths)
        return menu

    def change(self, fn):
        fn()
        self.cfg.save()
        self.relayout()

    def ask_name(self, title, initial=""):
        dlg = Gtk.Dialog(title=title, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.set_position(Gtk.WindowPosition.CENTER)
        dlg.set_keep_above(True)
        entry = Gtk.Entry(text=initial, activates_default=True, margin=12, width_chars=28)
        dlg.get_content_area().add(entry)
        dlg.show_all()
        dlg.present()
        ok = dlg.run() == Gtk.ResponseType.OK
        text = entry.get_text().strip()
        dlg.destroy()
        return text if ok and text else None

    def new_folder_for(self, app_id):
        name = self.ask_name("New folder")
        if name:
            self.change(lambda: self.cfg.new_folder(name, app_id))

    def folder_submenu(self, app_id):
        sub = Gtk.Menu()
        for f in self.cfg.folders():
            if app_id not in f["apps"]:
                add_item(sub, f["name"], lambda f=f: self.change(lambda: self.cfg.add_to_folder(app_id, f)))
        if sub.get_children():
            sub.append(Gtk.SeparatorMenuItem())
        add_item(sub, "New folder…", lambda: self.new_folder_for(app_id))
        return sub

    def app_menu(self, app_id, wins, ev, owner=None, widget=None):
        """Context menu for an app: wins = its open windows (None: don't offer Close)."""
        menu = Gtk.Menu()
        if wins:
            add_item(menu, "Close", lambda: [w.close(now()) for w in wins])
        if not app_id or not self.lookup(app_id):
            add_item(menu, "Pin to dock (no launcher found)", sensitive=False)
        else:
            cfg = self.cfg
            if app_id not in cfg.ids():
                add_item(menu, "Pin to dock", lambda: self.change(lambda: cfg.pin(app_id)))
            else:
                folder = cfg.folder_of(app_id)
                if folder:
                    add_item(menu, "Move out of folder", lambda: self.change(lambda: cfg.move_out(app_id)))
                else:
                    item = cfg.app_item(app_id)
                    add_item(menu, "Move up", lambda: self.change(lambda: cfg.move(item, -1)))
                    add_item(menu, "Move down", lambda: self.change(lambda: cfg.move(item, 1)))
                add_item(menu, "Unpin", lambda: self.change(lambda: cfg.unpin(app_id)))
            add_item(menu, "Add to folder", submenu=self.folder_submenu(app_id))
        ws_key = app_id or (group_key(wins[0]) if wins else None)
        if ws_key:
            menu.append(Gtk.SeparatorMenuItem())
            own = Gtk.CheckMenuItem(label="Open on its own workspace")
            own.set_active(self.wants_own_ws(ws_key))
            own.connect("toggled", lambda i: self.set_own_ws(ws_key, i.get_active(), wins))
            menu.append(own)
        popup_menu(menu, owner or self, ev, widget)

    def folder_menu(self, folder, ev, btn):
        menu = Gtk.Menu()

        def rename():
            name = self.ask_name("Rename folder", folder["name"])
            if name:
                self.change(lambda: folder.__setitem__("name", name))
        add_item(menu, "Rename…", rename)
        add_item(menu, "Move up", lambda: self.change(lambda: self.cfg.move(folder, -1)))
        add_item(menu, "Move down", lambda: self.change(lambda: self.cfg.move(folder, 1)))
        add_item(menu, "Delete folder (keep apps pinned)",
                 lambda: self.change(lambda: self.cfg.delete_folder(folder)))
        popup_menu(menu, self, ev, btn)

    # ----- workspace per app

    def watch(self, win):
        for sig in ("name-changed", "state-changed", "icon-changed"):
            win.connect(sig, self.queue_refresh)

    def on_opened(self, _screen, win):
        self.watch(win)
        self.queue_refresh()
        follow = win.get_workspace() is not None and win.get_workspace() == self.wnck.get_active_workspace()
        GLib.timeout_add(250, self.place, win, follow)  # let class/type hints settle first

    def on_closed(self, _screen, win):
        ws, active = win.get_workspace(), self.wnck.get_active_workspace()
        gohome = bool(ws and active and 0 < ws.get_number() == active.get_number())
        self.queue_refresh()
        GLib.timeout_add(300, self.tidy, gohome)

    def free_workspace(self, win):
        """First workspace after home with no app on it, else a new one at the end."""
        taken = set(self.claims.values())
        for w in self.app_windows():
            if w.get_xid() != win.get_xid() and w.get_workspace():
                taken.add(w.get_workspace().get_number())
        count = self.wnck.get_workspace_count()
        return next((n for n in range(1, count) if n not in taken), count)

    def ws_key(self, win):
        """Key the own-workspace choice is stored under: the app's launcher id, else its window class."""
        return self.pinned_id_for(win, self.cfg.ids()) or self.find_app_id(win) or group_key(win)

    def wants_own_ws(self, key):
        return self.cfg.workspace.get(key, True)

    def set_own_ws(self, key, on, wins):
        self.cfg.workspace[key] = on
        self.cfg.save()
        if on:  # turned on for an app that is already open: move it over now
            for w in wins or []:
                self.place(w, True)

    def place(self, win, follow):
        if win not in self.wnck.get_windows() or not is_app_window(win) or win.is_pinned():
            return False
        if not self.wants_own_ws(self.ws_key(win)):
            return False
        key = group_key(win)
        n = next((w.get_workspace().get_number() for w in self.group(key)
                  if w.get_xid() != win.get_xid() and w.get_workspace()), None)
        if n is None:
            n = self.claims.get(key)
        if n is None:
            n = self.free_workspace(win)
            if n >= self.wnck.get_workspace_count():
                self.wnck.change_workspace_count(n + 1)
        self.claims[key] = n
        self.move_when_ready(win, n, follow, 0)
        return False

    def move_when_ready(self, win, n, follow, tries):
        ws = self.wnck.get_workspace(n)  # a new workspace appears once the WM has processed the resize
        if ws is None:
            if tries < 25:
                GLib.timeout_add(40, self.move_when_ready, win, n, follow, tries + 1)
            return False
        if not win.get_workspace() or win.get_workspace().get_number() != n:
            win.move_to_workspace(ws)
        if follow:
            ws.activate(now())
            win.activate(now())
        return False

    def tidy(self, gohome):
        occupied = {w.get_workspace().get_number() for w in self.app_windows() if w.get_workspace()}
        active = self.wnck.get_active_workspace()
        if gohome and active and active.get_number() not in occupied:
            self.go_home()
        used = occupied | set(self.claims.values())
        count = n = self.wnck.get_workspace_count()
        while n > max(self.base, 1) and (n - 1) not in used:
            n -= 1
        if n != count:
            self.wnck.change_workspace_count(n)
        return False


# ---------------------------------------------------------------- entry

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=int, help="dock width in pixels (default: saved option, 56)")
    ap.add_argument("--icon", type=int, help="icon size in pixels (default: saved option, 32)")
    ap.add_argument("--config", default=CONFIG_PATH, help=f"pins/folders file (default {CONFIG_PATH})")
    ap.add_argument("--respect-top", action="store_true",
                    help="start below a top bar/panel instead of at the screen's top edge")
    ap.add_argument("--respect-bottom", action="store_true",
                    help="stop above bottom docks/panels instead of running to the screen's bottom edge")
    args = ap.parse_args()

    display = os.environ.get("DISPLAY")
    if not display:
        sys.exit("No X display (DISPLAY is unset).")
    lock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                             f"cubby-{os.getuid()}-{re.sub(r'[^0-9A-Za-z]', '_', display)}.lock")
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Cubby is already running on this display.")

    Wnck.set_client_type(Wnck.ClientType.PAGER)  # so the WM lets us switch focus and workspaces
    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    cfg = Config(args.config)
    dock = Dock(args.width or cfg.options["width"], args.icon or cfg.options["icon"], cfg, args.respect_top, args.respect_bottom)
    dock.show_all()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)
    Gtk.main()
    del lock


if __name__ == "__main__":
    main()
