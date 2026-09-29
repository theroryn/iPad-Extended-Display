#!/usr/bin/env python3
"""iPad Extended Display — headless virtual display + Sunshine, with a GUI.

Creates a headless virtual display via Mutter's ScreenCast API (sized to
whatever's picked in the UI, defaulting to the iPad Mini's native panel, and
placed left or right of your main display per the Position dropdown — via
`gdctl set` once Mutter has auto-added it, since ScreenCast itself has no
placement option), then (re)launches Sunshine (Flatpak). Start also brings
up the iPad's Wi-Fi hotspot (SSID/password editable in the window); Stop
tears it back down and reconnects to your normal Wi-Fi. Closing the window
stops everything first.

Note: Mutter's virtual displays have no real kernel DRM device behind them —
Sunshine's DEFAULT capture backend can't see them at all, only its "portal"
capture backend can. That backend caches its GNOME screen-share consent
(a restore token) and silently reuses whatever display was granted last
time. Since the virtual display is re-created from scratch on every Start,
Sunshine's cached token and Flatpak's stored consent for it are cleared
before each launch, so GNOME's picker is forced to prompt again and the
user can pick the current virtual display.
"""
import json
import os
import subprocess
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GLibUnix, Gst, Gtk

MUTTER_SC = "org.gnome.Mutter.ScreenCast"
MUTTER_DC = "org.gnome.Mutter.DisplayConfig"
VIRTUAL_PRODUCT = "Virtual remote monitor"
FPS = 60

CONFIG_DIR = os.path.expanduser("~/.config/ipad-extended-display")
SIZES_FILE = os.path.join(CONFIG_DIR, "sizes.json")
DEFAULT_SIZES = [
    {"label": "iPad Mini — Landscape", "width": 1024, "height": 768},
    {"label": "iPad Mini — Portrait", "width": 768, "height": 1024},
]

POSITION_FILE = os.path.join(CONFIG_DIR, "position.json")
DEFAULT_POSITION = "right"

SUNSHINE_APP_ID = "dev.lizardbyte.app.Sunshine"
SUNSHINE_PORTAL_TOKEN = os.path.expanduser(
    "~/.var/app/dev.lizardbyte.app.Sunshine/config/sunshine/portal_token")

HOTSPOT_FILE = os.path.join(CONFIG_DIR, "hotspot.json")
DEFAULT_HOTSPOT = {"enabled": True, "ssid": "iPad", "password": "Padnet123",
                   "interface": "wlp0s20f3"}
HOTSPOT_CON_NAME = "iPad"


def load_hotspot():
    try:
        with open(HOTSPOT_FILE) as f:
            return {**DEFAULT_HOTSPOT, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULT_HOTSPOT)


def save_hotspot(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(HOTSPOT_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def start_hotspot(cfg):
    iface, ssid, password = cfg["interface"], cfg["ssid"], cfg["password"]
    existing = subprocess.run(["nmcli", "-g", "NAME", "connection", "show"],
                              stdout=subprocess.PIPE, text=True).stdout.splitlines()
    subprocess.run(["nmcli", "device", "disconnect", iface],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if HOTSPOT_CON_NAME in existing:
        subprocess.run(["nmcli", "connection", "modify", HOTSPOT_CON_NAME,
                        "802-11-wireless.ssid", ssid, "wifi-sec.psk", password])
        subprocess.run(["nmcli", "connection", "up", HOTSPOT_CON_NAME])
    else:
        subprocess.run(["nmcli", "device", "wifi", "hotspot", "ifname", iface,
                        "con-name", HOTSPOT_CON_NAME, "ssid", ssid, "password", password])
        subprocess.run(["nmcli", "connection", "modify", HOTSPOT_CON_NAME,
                        "802-11-wireless-security.pmf", "1"])
        subprocess.run(["nmcli", "connection", "up", HOTSPOT_CON_NAME])
    # Never let NetworkManager bring the hotspot back up on its own — an
    # AP-mode profile is always "available", so with autoconnect on NM can
    # pick it again whenever the Wi-Fi device reconnects.
    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_CON_NAME,
                    "connection.autoconnect", "no"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _hotspot_active():
    active = subprocess.run(["nmcli", "-g", "NAME", "connection", "show", "--active"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True).stdout.splitlines()
    return HOTSPOT_CON_NAME in active


def stop_hotspot(cfg):
    """Take the hotspot connection down and hand the Wi-Fi device back to
    NetworkManager's normal autoconnect.

    The old approach (device disconnect + device connect) let NM choose
    "the best available connection" on reconnect, which was sometimes the
    hotspot profile itself — so the hotspot came straight back up. Instead,
    deactivate the hotspot profile by name and verify it's actually gone.
    """
    iface = cfg["interface"]
    subprocess.run(["nmcli", "connection", "modify", HOTSPOT_CON_NAME,
                    "connection.autoconnect", "no"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _attempt in range(5):
        subprocess.run(["nmcli", "connection", "down", HOTSPOT_CON_NAME],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _hotspot_active():
            break
        time.sleep(0.5)
    # Clear any autoconnect block left by the "device disconnect" in
    # start_hotspot so NM rejoins the usual Wi-Fi network by itself.
    subprocess.run(["nmcli", "device", "set", iface, "autoconnect", "yes"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return not _hotspot_active()


def load_sizes():
    try:
        with open(SIZES_FILE) as f:
            sizes = json.load(f)
        if isinstance(sizes, list) and all(
                isinstance(s, dict) and "label" in s and "width" in s and "height" in s
                for s in sizes):
            return sizes
    except (OSError, ValueError):
        pass
    return [dict(s) for s in DEFAULT_SIZES]


def save_sizes(sizes):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SIZES_FILE, "w") as f:
        json.dump(sizes, f, indent=2)


def load_position():
    try:
        with open(POSITION_FILE) as f:
            side = json.load(f).get("side")
        if side in ("left", "right"):
            return side
    except (OSError, ValueError):
        pass
    return DEFAULT_POSITION


def save_position(side):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(POSITION_FILE, "w") as f:
        json.dump({"side": side}, f, indent=2)


class DisplayController:
    """Owns the Mutter ScreenCast session and its GStreamer pipeline."""

    def __init__(self, on_state_change):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.on_state_change = on_state_change
        self.session = None
        self.pipeline = None
        self._sub_ids = []

    def _call(self, dest, path, iface, method, params=None, reply=None):
        return self.bus.call_sync(dest, path, iface, method, params,
                                  GLib.VariantType(reply) if reply else None,
                                  Gio.DBusCallFlags.NONE, -1, None)

    @property
    def running(self):
        return self.session is not None

    def _display_state(self):
        """(monitors, logical) — monitors: {connector: (product, width_px,
        height_px)} using each monitor's current mode; logical: [(x, y,
        scale, primary, connector)] of the current layout."""
        state = self._call(MUTTER_DC, "/org/gnome/Mutter/DisplayConfig", MUTTER_DC,
                           "GetCurrentState").unpack()
        monitors = {}
        for (connector, _vendor, product, _serial), modes, _props in state[1]:
            width = height = None
            for mode in modes:
                mode_props = mode[-1]
                if isinstance(mode_props, dict) and mode_props.get("is-current"):
                    width, height = mode[1], mode[2]
                    break
            monitors[connector] = (product, width, height)
        logical = []
        for x, y, scale, _transform, primary, mons, _props in state[2]:
            logical.append((x, y, scale, bool(primary), mons[0][0]))
        return monitors, logical

    def _reposition_virtual(self, before, anchor, side, deadline):
        """Poll until the new virtual monitor shows up in Mutter's display
        config, then place it left/right of `anchor` — Mutter has already
        auto-placed it somewhere (typically to the right) by the time it's
        visible here, so this moves it. Positions are computed by hand
        rather than via gdctl's own --left-of/--right-of: asking gdctl to
        place a monitor left of one already sitting at x=0 makes it compute
        a negative x, which Mutter flatly rejects ("Invalid logical monitor
        position") — confirmed against a live session. Shifting the whole
        existing layout right by the new monitor's width instead keeps
        everything at x >= 0."""
        if not anchor:
            return False
        monitors, logical = self._display_state()
        new = [c for c in monitors if c not in before and monitors[c][0] == VIRTUAL_PRODUCT]
        if not new:
            return time.monotonic() < deadline
        connector = new[0]

        by_connector = {con: (x, y, scale, primary) for x, y, scale, primary, con in logical}
        anchor_x, anchor_y, anchor_scale, _ = by_connector[anchor]
        _, _, v_scale, _ = by_connector[connector]
        # Logical width = physical pixels / scale — round to int, gdctl's --x
        # only accepts integers and GNOME logical coordinates are always
        # whole numbers anyway.
        anchor_w = round(monitors[anchor][1] / anchor_scale)
        v_w = round(monitors[connector][1] / v_scale)

        shift, new_v_x = (v_w, 0) if side == "left" else (0, anchor_x + anchor_w)

        args = ["gdctl", "set"]
        for con, (x, y, scale, primary) in by_connector.items():
            if con == connector:
                continue
            args += ["--logical-monitor", "--monitor", con,
                     "--x", str(round(x + shift)), "--y", str(y), "--scale", str(scale)]
            if primary:
                args.append("--primary")
        args += ["--logical-monitor", "--monitor", connector,
                 "--x", str(round(new_v_x)), "--y", str(anchor_y), "--scale", str(v_scale)]
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False

    def ensure_sunshine_running(self):
        # Always relaunch fresh so the permission reset below actually takes
        # effect (an already-running Sunshine won't re-check permissions)
        # and so it re-enumerates outputs and sees the new virtual display.
        if subprocess.run(["pgrep", "-x", "sunshine"],
                          stdout=subprocess.DEVNULL).returncode == 0:
            subprocess.run(["flatpak", "kill", SUNSHINE_APP_ID],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)

        try:
            os.remove(SUNSHINE_PORTAL_TOKEN)
        except FileNotFoundError:
            pass
        subprocess.run(["flatpak", "permission-reset", SUNSHINE_APP_ID],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.Popen(["flatpak", "run", SUNSHINE_APP_ID],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def stop_sunshine(self):
        subprocess.run(["flatpak", "kill", SUNSHINE_APP_ID],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):
            if subprocess.run(["pgrep", "-x", "sunshine"],
                              stdout=subprocess.DEVNULL).returncode != 0:
                break
            time.sleep(0.1)
        # xdg-desktop-portal tears down Sunshine's own (separate) portal
        # screen-cast session asynchronously after its bus connection drops.
        # Give that a moment to finish before we touch the virtual monitor.
        time.sleep(0.5)

    def start(self, width, height, position):
        if self.running:
            return
        self.ensure_sunshine_running()

        # Snapshot the pre-existing monitors/primary now, before the virtual
        # one exists, so _reposition_virtual can tell which one is new and
        # what to place it relative to.
        before_monitors, before_logical = self._display_state()
        before = set(before_monitors)
        anchor = next((con for _x, _y, _s, primary, con in before_logical if primary), None)

        session = self._call(MUTTER_SC, "/org/gnome/Mutter/ScreenCast",
                             MUTTER_SC, "CreateSession",
                             GLib.Variant("(a{sv})", ({},)), "(o)")[0]
        stream = self._call(MUTTER_SC, session, MUTTER_SC + ".Session", "RecordVirtual",
                            GLib.Variant("(a{sv})", ({"cursor-mode": GLib.Variant("u", 1),
                                                       "is-platform": GLib.Variant("b", True)},)),
                            "(o)")[0]

        Gst.init(None)

        def on_stream_added(*args):
            node = args[-1].unpack()[0]
            self.pipeline = Gst.parse_launch(
                f"pipewiresrc path={node} always-copy=false ! "
                f"video/x-raw,width={width},height={height},max-framerate={FPS}/1 ! "
                "fakesink sync=false")
            self.pipeline.set_state(Gst.State.PLAYING)
            self.on_state_change(True, f"Active — {width}×{height}@{FPS}")
            GLib.timeout_add(150, self._reposition_virtual, before, anchor, position,
                             time.monotonic() + 10)

        def on_session_closed(*_args):
            self._reset()
            self.stop_sunshine()
            self.on_state_change(False, "Stopped (session closed).")

        self._sub_ids.append(self.bus.signal_subscribe(
            MUTTER_SC, MUTTER_SC + ".Stream", "PipeWireStreamAdded", stream,
            None, Gio.DBusSignalFlags.NONE, on_stream_added))
        self._sub_ids.append(self.bus.signal_subscribe(
            MUTTER_SC, MUTTER_SC + ".Session", "Closed", session,
            None, Gio.DBusSignalFlags.NONE, on_session_closed))

        self._call(MUTTER_SC, session, MUTTER_SC + ".Session", "Start")
        self.session = session
        self.on_state_change(True, f"Starting {width}×{height}@{FPS}…")

    def stop(self):
        if not self.running:
            return
        # Sunshine captures the virtual monitor through its OWN, separate
        # portal screen-cast session (confirmed via a "remote-desktop"
        # permission grant referencing the virtual monitor, active right up
        # until the crash). Removing the virtual monitor while that portal
        # session is still attached to it crashes GNOME Shell, regardless of
        # how cleanly our own pipeline below is torn down. So: close
        # Sunshine and let its portal session tear down FIRST.
        self.stop_sunshine()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            # Block until pipewiresrc has actually detached from the
            # PipeWire stream too, for the same reason.
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        session = self.session
        self._reset()
        try:
            self._call(MUTTER_SC, session, MUTTER_SC + ".Session", "Stop")
        except GLib.Error:
            pass
        self.on_state_change(False, "Stopped — Sunshine closed.")

    def _reset(self):
        for sub_id in self._sub_ids:
            self.bus.signal_unsubscribe(sub_id)
        self._sub_ids = []
        self.session = None
        self.pipeline = None


class ManageSizesDialog(Gtk.Window):
    """Modal editor for the add/remove-able list of display sizes."""

    def __init__(self, parent, sizes, on_done):
        super().__init__(title="Edit display sizes", transient_for=parent, modal=True)
        self.set_default_size(340, 380)
        self.sizes = sizes
        self.on_done = on_done

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.set_child(box)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.listbox)
        box.append(scroller)
        self._rebuild_list()

        box.append(Gtk.Separator())

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.label_entry = Gtk.Entry(placeholder_text="Name", hexpand=True)
        self.new_width = Gtk.SpinButton.new_with_range(320, 7680, 2)
        self.new_width.set_value(1024)
        self.new_height = Gtk.SpinButton.new_with_range(320, 4320, 2)
        self.new_height.set_value(768)
        add_row.append(self.label_entry)
        add_row.append(Gtk.Label(label="W:"))
        add_row.append(self.new_width)
        add_row.append(Gtk.Label(label="H:"))
        add_row.append(self.new_height)
        box.append(add_row)

        add_button = Gtk.Button(label="Add")
        add_button.connect("clicked", self._on_add)
        box.append(add_button)

        done_button = Gtk.Button(label="Done")
        done_button.connect("clicked", lambda _b: self.close())
        box.append(done_button)

        self.connect("close-request", self._on_close)

    def _rebuild_list(self):
        child = self.listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        for i, s in enumerate(self.sizes):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
            row.append(Gtk.Label(label=f"{s['label']} ({s['width']}×{s['height']})",
                                 hexpand=True, xalign=0))
            remove_button = Gtk.Button(label="Remove")
            remove_button.connect("clicked", self._on_remove, i)
            row.append(remove_button)
            self.listbox.append(row)

    def _on_add(self, _button):
        width = int(self.new_width.get_value())
        height = int(self.new_height.get_value())
        label = self.label_entry.get_text().strip() or f"{width}×{height}"
        self.sizes.append({"label": label, "width": width, "height": height})
        self.label_entry.set_text("")
        self._rebuild_list()

    def _on_remove(self, _button, index):
        del self.sizes[index]
        self._rebuild_list()

    def _on_close(self, _window):
        self.on_done(self.sizes)
        return False


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="iPad Extended Display")
        self.set_default_size(360, -1)
        self.set_resizable(False)
        self.controller = DisplayController(self._on_state_change)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.set_child(box)

        self.hotspot = load_hotspot()
        self.hotspot_up = False

        self.hotspot_check = Gtk.CheckButton(label="Start a Wi-Fi hotspot",
                                             active=self.hotspot["enabled"])
        self.hotspot_check.connect("toggled", self._on_hotspot_toggled)
        box.append(self.hotspot_check)

        ssid_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ssid_row.append(Gtk.Label(label="SSID:"))
        self.ssid_entry = Gtk.Entry(text=self.hotspot["ssid"], hexpand=True)
        self.ssid_entry.connect("changed", self._on_hotspot_changed)
        ssid_row.append(self.ssid_entry)
        box.append(ssid_row)

        password_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        password_row.append(Gtk.Label(label="Password:"))
        self.password_entry = Gtk.PasswordEntry(text=self.hotspot["password"],
                                                hexpand=True, show_peek_icon=True)
        self.password_entry.connect("changed", self._on_hotspot_changed)
        password_row.append(self.password_entry)
        box.append(password_row)

        self.ssid_entry.set_sensitive(self.hotspot["enabled"])
        self.password_entry.set_sensitive(self.hotspot["enabled"])

        box.append(Gtk.Separator())

        box.append(Gtk.Label(label="Display size", xalign=0))

        self.sizes = load_sizes()

        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.preset_combo = Gtk.ComboBoxText(hexpand=True)
        size_row.append(self.preset_combo)
        manage_button = Gtk.Button(label="Edit sizes…")
        manage_button.connect("clicked", self._on_manage_sizes)
        size_row.append(manage_button)
        box.append(size_row)

        self._rebuild_combo()
        self.preset_combo.connect("changed", self._on_preset_changed)

        self.custom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.width_spin = Gtk.SpinButton.new_with_range(320, 7680, 2)
        self.width_spin.set_value(1024)
        self.height_spin = Gtk.SpinButton.new_with_range(320, 4320, 2)
        self.height_spin.set_value(768)
        self.custom_box.append(Gtk.Label(label="W:"))
        self.custom_box.append(self.width_spin)
        self.custom_box.append(Gtk.Label(label="H:"))
        self.custom_box.append(self.height_spin)
        self.custom_box.set_visible(False)
        box.append(self.custom_box)

        position_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        position_row.append(Gtk.Label(label="Position:"))
        self.position_combo = Gtk.ComboBoxText(hexpand=True)
        self.position_combo.append("right", "Right of main display")
        self.position_combo.append("left", "Left of main display")
        self.position_combo.set_active_id(load_position())
        self.position_combo.connect("changed", self._on_position_changed)
        position_row.append(self.position_combo)
        box.append(position_row)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                             homogeneous=True)
        self.start_button = Gtk.Button(label="Start")
        self.start_button.connect("clicked", self._on_start)
        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.connect("clicked", self._on_stop)
        self.stop_button.set_sensitive(False)
        button_box.append(self.start_button)
        button_box.append(self.stop_button)
        box.append(button_box)

        self.status_label = Gtk.Label(label="Stopped.", xalign=0, wrap=True)
        box.append(self.status_label)

        self.connect("close-request", self._on_close_request)

    def _rebuild_combo(self):
        self.preset_combo.remove_all()
        for s in self.sizes:
            self.preset_combo.append_text(f"{s['label']} ({s['width']}×{s['height']})")
        self.preset_combo.append_text("Custom")
        self.preset_combo.set_active(0)

    def _on_preset_changed(self, combo):
        idx = combo.get_active()
        self.custom_box.set_visible(not (0 <= idx < len(self.sizes)))

    def _selected_size(self):
        idx = self.preset_combo.get_active()
        if 0 <= idx < len(self.sizes):
            s = self.sizes[idx]
            return s["width"], s["height"]
        return int(self.width_spin.get_value()), int(self.height_spin.get_value())

    def _on_hotspot_changed(self, _widget):
        self.hotspot["ssid"] = self.ssid_entry.get_text() or DEFAULT_HOTSPOT["ssid"]
        self.hotspot["password"] = self.password_entry.get_text() or DEFAULT_HOTSPOT["password"]
        save_hotspot(self.hotspot)

    def _on_hotspot_toggled(self, check):
        self.hotspot["enabled"] = check.get_active()
        save_hotspot(self.hotspot)
        sensitive = self.hotspot["enabled"] and not self.controller.running
        self.ssid_entry.set_sensitive(sensitive)
        self.password_entry.set_sensitive(sensitive)

    def _on_manage_sizes(self, _button):
        ManageSizesDialog(self, list(self.sizes), self._on_sizes_updated).present()

    def _on_sizes_updated(self, new_sizes):
        self.sizes = new_sizes
        save_sizes(self.sizes)
        self._rebuild_combo()

    def _on_position_changed(self, combo):
        save_position(combo.get_active_id())

    def _on_start(self, _button):
        if self.hotspot["enabled"]:
            start_hotspot(self.hotspot)
            self.hotspot_up = True
        width, height = self._selected_size()
        try:
            self.controller.start(width, height, self.position_combo.get_active_id())
        except Exception as exc:
            self._teardown_hotspot()
            self.status_label.set_label(f"Failed to start: {exc}")
            raise

    def _on_stop(self, _button):
        self.controller.stop()
        # Covers the case where the controller wasn't "running" (so it
        # never fired a state change) but the hotspot was still started.
        self._teardown_hotspot()

    def _teardown_hotspot(self):
        """Stop the hotspot if we started it. Returns a status suffix."""
        if not self.hotspot_up:
            return ""
        self.hotspot_up = False
        if stop_hotspot(self.hotspot):
            return f" Wi-Fi '{self.hotspot['ssid']}' removed."
        return f" Wi-Fi '{self.hotspot['ssid']}' could NOT be stopped — check nmcli."

    def _on_state_change(self, running, status_text):
        self.start_button.set_sensitive(not running)
        self.stop_button.set_sensitive(running)
        self.preset_combo.set_sensitive(not running)
        self.custom_box.set_sensitive(not running)
        self.position_combo.set_sensitive(not running)
        self.hotspot_check.set_sensitive(not running)
        self.ssid_entry.set_sensitive(self.hotspot["enabled"] and not running)
        self.password_entry.set_sensitive(self.hotspot["enabled"] and not running)
        if not running:
            status_text += self._teardown_hotspot()
        elif self.hotspot_up:
            status_text += f" Wi-Fi '{self.hotspot['ssid']}' up."
        self.status_label.set_label(status_text)

    def _on_close_request(self, _window):
        self.controller.stop()
        self._teardown_hotspot()
        return False


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.rory.ipadextendeddisplay")

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()


def main():
    app = Application()

    def handle_signal():
        win = app.props.active_window
        win.close() if win else app.quit()
        return GLib.SOURCE_REMOVE

    for sig in (2, 15):  # SIGINT, SIGTERM
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, handle_signal)
    app.run(None)


if __name__ == "__main__":
    main()
