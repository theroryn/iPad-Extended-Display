# Setup guide: Sunshine + Moonlight, host → iPad

Goal: connect an iPad to a Sunshine host over Wi-Fi via Moonlight, showing a
dedicated **extended** desktop (not a mirror) sized to the iPad's native
screen.

---

## 1. Give the host a second internet connection — optional

Most laptops have a single Wi-Fi radio, which can't run an access point and
stay connected to another network at the same time. If the host needs the
Wi-Fi hotspot from step 2 (because it isn't already on the same network as
the iPad) and also needs internet access itself, give it another source
first — for example, USB tethering from a phone:

1. Connect the phone to the host over USB.
2. On the phone, enable USB tethering (usually under
   **Settings → Network/Connections → Hotspot and Tethering**).
3. Confirm the host picked up the new wired network device and has internet
   through it:

```bash
nmcli device status
ip -4 addr show
```

You should see a new device (something like `enxXXXXXXXXXXXX` or `usb0`)
with an IP address, separate from the host's Wi-Fi interface.

---

## 2. Wi-Fi hotspot on the host — optional

Only needed when the host and iPad aren't already on the same network. If
they're both already on the same Wi-Fi, skip this — untick the hotspot
checkbox in the app and go straight to step 3.

The app manages this automatically: it brings the hotspot up before
creating the virtual display and tears it back down (reconnecting the host
to its normal Wi-Fi) when stopped. Settings live in
`~/.config/ipad-extended-display/hotspot.json`, created automatically on
first run.

**In the app:** the "Start a Wi-Fi hotspot" checkbox controls this — untick
it when the host and iPad already share a network, tick it and fill in an
SSID/password otherwise. Fields save automatically as you type, and an
SSID/password change applies immediately if the hotspot is already up,
otherwise on next start.

**On the iPad:** Settings → Wi-Fi → join the configured SSID.

If you need to do it by hand instead, this is what the app runs under the
hood (replace `<iface>` with your Wi-Fi interface name from
`nmcli device status`):

```bash
# pmf 1 disables 802.11w — some older Wi-Fi stacks can't join a hotspot
# that requires it, even though other devices connect fine without it
nmcli device disconnect <iface>
nmcli device wifi hotspot ifname <iface> con-name iPad ssid iPad password "<password>"
nmcli connection modify iPad 802-11-wireless-security.pmf 1
nmcli connection up iPad

# tear it down by device name (not connection name) and rejoin the normal network
nmcli device disconnect <iface>
nmcli device connect <iface>
```

Confirm it's up and get the host's IP on that hotspot:

```bash
nmcli connection show --active
ip -4 addr show <iface>
```

Note the IP (e.g. `10.42.0.1`) — needed if Moonlight doesn't auto-discover
the host.

---

## 3. Virtual display + Sunshine

Sunshine itself needs no custom configuration — stock config, no custom app
entries, nothing added to `sunshine.conf`/`apps.json`.

Run the AppImage (or a source build — see the main [README](../README.md)):

- **Display size**: a dropdown of presets (iPad Mini landscape/portrait by
  default; "Edit sizes…" to add/remove your own), or "Custom" for a one-off
  width/height.
- **Position**: right or left of the host's main display. Applied via
  `gdctl set` a moment after Start, once Mutter has registered the new
  monitor (Mutter auto-places new monitors on its own — usually to the
  right — before this repositions it).
- **Start**: brings up the Wi-Fi hotspot (if enabled), creates the headless
  virtual display, positions it, and (re)launches Sunshine — clearing its
  cached screen-share permission first so GNOME's picker prompts again and
  the new virtual display can be selected.
- **Stop** (or closing the window): closes Sunshine first, then tears down
  the virtual display and the Wi-Fi hotspot.

Once it's running, open Sunshine's web UI (`https://localhost:47990`; the
first time, set an admin username/password there, separate from the host's
login) — it should now offer the virtual display as selectable.

---

## 4. Pair Moonlight (on the iPad) to Sunshine

1. The iPad must be connected to the hotspot (or shared network) from step
   2.
2. Open **Moonlight** on the iPad — it should auto-discover the host. If
   not, "Add Host" → enter the host's IP manually.
3. Tap the host — Moonlight shows a 4-digit PIN.
4. Within about 30 seconds, in the Sunshine web UI's **PIN** tab, enter that
   code, name the device, and click **Send**.
5. Moonlight confirms pairing within a few seconds.

---

## 5. Connect

With the app still running (Started), select the **Desktop** tile in
Moonlight (the only one — stock Sunshine has no other app entries). If
Sunshine offers a display picker (a dialog on connect, or a setting in its
Configuration tab), choose the virtual display from step 3 instead of the
host's physical screen.

Match Moonlight's resolution to the virtual display's: gear/settings icon →
**Resolution and FPS** → set **Custom** to the same width×height, and the
same FPS.

(Black bars top/bottom usually mean this reverted to a 16:9 preset —
re-set it.)
