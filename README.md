# Cubby

A left-edge dock for X11 desktops where **every app gets its own workspace**.

Open an app and it moves to a fresh workspace and gets a button in the dock. Click the button and you jump to that app, on the workspace where it lives. Close it and the empty workspace goes away.

Cubby is a single Python file (GTK 3 + libwnck). It was built for XFCE on a GoBox hotspot machine reached over xrdp, but it should work on other X11 desktops with an EWMH-compliant window manager (see [Compatibility](#compatibility)).

## Features

- **App drawer** on the top button: every installed app, type to filter, Enter to launch.
- **One workspace per app.** New windows move to the first empty workspace, or a new one is created. Workspace 1 is a clean "home" desktop, and the bottom of the app list has a button to return to it. Trailing empty workspaces are trimmed, and never below the count you started with.
- **Per-app opt-out.** Right-click any app and untick **Open on its own workspace** to keep it on the current workspace. It is on by default, and ticking it again moves the app's open windows immediately.
- **Pins.** Right-click an app, then **Pin to dock**. Pinned apps stay in the dock when closed and light up when running.
- **Folders.** Group pinned apps into a folder that opens a small popout of icons.
- **Desktop launchers.** `.desktop` files in `~/Desktop` show up in the drawer and can be pinned like any installed app.
- **Terminal launchers.** Launchers with `Terminal=true` open in their own `xfce4-terminal` window (falling back to `xterm`) with a unique window class. Each one gets its own button, pin and workspace instead of merging into one Terminal button.
- **Bottom panel:** network indicator (click for details), clock (click for a calendar), and a user menu (lock, suspend, log out, restart, shut down).
- **Panel options** on right-click: 12/24-hour clock, seconds, date, network indicator, icon size and dock width.
- **Reserved screen edge.** The dock reserves its strip with `_NET_WM_STRUT_PARTIAL`, so maximized windows don't cover it.
- **Single instance** per display.

## Requirements

| | Debian / Ubuntu | Fedora | Arch |
|---|---|---|---|
| Python 3 + GTK 3 bindings | `python3-gi gir1.2-gtk-3.0` | `python3-gobject gtk3` | `python-gobject gtk3` |
| libwnck 3 | `gir1.2-wnck-3.0` | `libwnck3` | `libwnck3` |
| `xprop` | `x11-utils` | `xorg-x11-utils` | `xorg-xprop` |

Optional:
- `xfce4-terminal` or `xterm` to run `Terminal=true` launchers.
- `nm-connection-editor` for the network popup's edit button.
- `xfce4-session` (`xfce4-session-logout`, `xflock4`) for the user menu's lock, suspend, log out, restart and shut down. These actions are XFCE-specific; on other desktops those entries may not work.

## Install and run

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 x11-utils
python3 cubby.py
```

Options:

```
--width PX         dock width (default 56)
--icon PX          icon size
--config FILE      config file (default ~/.config/cubby/dock.json)
--respect-top      start below existing top panels instead of the screen top
--respect-bottom   end above existing bottom panels instead of the screen bottom
```

Without `--respect-top` and `--respect-bottom` the dock spans the full screen height. Panel options set from the right-click menu are saved and override the defaults.

### Autostart

Create `~/.config/autostart/cubby.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Cubby
Comment=Left-edge dock with app drawer, pins, folders and per-app workspaces
Exec=sh -c "sleep 3 && exec python3 /path/to/cubby.py"
Terminal=false
X-GNOME-Autostart-enabled=true
```

The short sleep lets the window manager come up first.

## Usage

| Action | Result |
|---|---|
| Click top button | Open the app drawer |
| Click an app button | Switch to its workspace and focus it (launch it if it's a closed pin) |
| Click a folder | Open its popout |
| Right-click an app or drawer entry | Pin/unpin, add to folder, move, close, own-workspace toggle |
| Click clock | Calendar |
| Click network icon | Connection details |
| Click user button | Lock, suspend, log out, restart, shut down |
| Right-click clock, network, user button or empty dock space | Panel options |

Windows already open when Cubby starts are listed but not moved.

## Configuration

Everything is stored in `~/.config/cubby/dock.json`:

- `items`: pinned apps and folders, in order. Launchers outside the standard app directories (for example in `~/Desktop`) are stored by file path.
- `options`: clock format, seconds, date, network indicator, icon size, dock width.
- `workspace`: per-app overrides of the own-workspace setting.

## Compatibility

- **Works on:** X11 sessions with a window manager that supports the EWMH hints `_NET_WM_STRUT_PARTIAL` and `_NET_WM_DESKTOP`. Developed and tested on Debian with XFCE (xfwm4), including over xrdp. The display manager doesn't matter, only the session type.
- **Does not work on:** Wayland sessions (libwnck and the strut mechanism don't exist there). GNOME and KDE manage workspaces themselves and will conflict.
- **Untested or likely awkward:** tiling window managers, which manage workspaces on their own.

## Matching windows to launchers

Cubby decides which launcher a window belongs to in two tiers. A window class (`StartupWMClass`) or desktop-id match wins, and a bare executable-name match is only a fallback. If an app's window shows up as a separate button instead of lighting up its pin, add `StartupWMClass=<class>` to its `.desktop` file. Find the class by running `xprop WM_CLASS` and clicking the window.

## Known limitations

- X11 only; tested on XFCE only.
- The power and lock actions assume XFCE's session tools.
- Terminal launchers started from desktop icons or elsewhere, rather than from Cubby, open as ordinary terminals and appear under the generic Terminal button.

## License

MIT License, Copyright (c) 2026 Jordan Webb. See [LICENSE](LICENSE).

This software comes with no warranty. Use it at your own risk.
