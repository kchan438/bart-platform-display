# bart-platform-display

A standalone pygame app for a Raspberry Pi Zero W + 3.5" SPI TFT display (480x320).
Displays real-time BART Platform 2 departures for a configured station.

## Hardware

- Raspberry Pi Zero W
- 3.5" SPI TFT LCD (480x320, GPIO HAT, ILI9486 driver, XPT2046 touch, mapped to `/dev/fb0`)
- Raspbian GNU/Linux 13 (trixie), 32-bit

SSH remains enabled so you can modify the project remotely at any time.

---

## Project structure

```
bart-platform-display/
├── main.py                         # entry point: render loop + input dispatch
├── bartdisplay/                    # application package
│   ├── config.py                   # config.json load/save (thread-safe)
│   ├── display.py                  # pygame + framebuffer output, fonts, palette
│   ├── departures.py               # BART ETD fetch (background thread)
│   ├── board.py                    # main departure-board render
│   ├── touch.py                    # XPT2046 evdev reader + gesture recognizer
│   ├── wifi.py                     # nmcli wrapper (scan/connect/disconnect/info)
│   ├── updater.py                  # safe async origin/main fast-forward updates
│   ├── system_control.py           # async systemd-logind D-Bus reboot request
│   └── ui/                         # swipe-down settings panel
│       ├── widgets.py              # buttons + icon helpers
│       ├── keyboard.py             # on-screen keyboard
│       └── settings.py            # Wi-Fi, API-key, and System panel
├── deploy/
│   └── polkit/                    # minimal NetworkManager/reboot authorization
├── docs/product/                  # implementation-ready product requirements
├── tests/                         # Wi-Fi lifecycle regression tests
├── config.json                     # station, platform, API key
├── requirements.txt
├── fonts/
│   └── PressStart2P-Regular.ttf    # must be downloaded (see below)
└── bart-platform-display.service   # systemd unit for auto-start
```

## On-device settings panel

The device is configured entirely from the touchscreen — no SSH needed:

- **Swipe down** starting in the top 120 pixels to open the settings shade;
  **swipe up** from any shade screen to return to the departure board. Override
  the opening zone with `touch_tuning.panel_open_start_max_y` in `config.json`.
- **Wi-Fi**: scan and list networks with signal strength, encryption, a *SAVED*
  tag for known networks, and the connected network marked *ONLINE*. Tap a
  network to Connect / Disconnect or view its info. Password-protected networks
  prompt for a password via the on-screen keyboard, with a large **Show/Hide**
  password button and a separate **Close** keyboard button.
  Opening and navigating the list never refreshes it; only **Rescan** starts a
  scan session. One tap runs two verified hardware passes, with a bounded third
  pass only when results collapse. It unions BSSIDs across the session, then
  shows one active/strongest row per SSID. Saved networks that are not currently
  visible remain labeled **NOT DETECTED** without stale signal/security claims.
  Saved networks are kept at the top, and the right-side scrollbar supports
  up/down taps plus thumb dragging.
  Live connection, authentication, IP-assignment, and disconnect status is
  shown. Recent results remain visible when a scan temporarily fails. Networks
  are saved by NetworkManager and auto-reconnect on boot. A background
  supervisor gives NetworkManager 20 seconds to recover an unexpected drop,
  then makes at most two exact-profile retries for transient failures. It never
  retries a user disconnect or authentication failure.
- **BART API Key**: shows the current key; **Modify** edits it with the keyboard
  and **Save** writes it to `config.json`. A new key applies on the next poll
  (~30 s) without a restart. After saving, **Open System Restart** links to the
  shared service restart control when an immediate reload is wanted.
- **System**: **Update App** downloads the latest `origin/main` only while
  Wi-Fi is connected. The operation runs in the background and reports whether
  the app changed, was already current, or failed with a short explanation.
  The same screen offers two deliberately separate, confirmation-protected
  actions: **Restart Display Service** cleanly exits only this app so systemd
  relaunches it, while **Restart Raspberry Pi** requests a normal full-device
  reboot. Duplicate taps are disabled while an update or restart is active,
  and immediate failures remain visible on screen.

### On-device app updates (Git)

**Settings → System → Update App** runs this narrow update from the application
checkout:

```bash
git pull --ff-only origin main
```

The updater:

- requires the device checkout to be on `main` with no tracked source changes;
- permits only the normal unstaged `config.json` change created by touchscreen
  settings, while relying on Git to refuse an update that would overwrite it;
- uses the existing `origin` remote and never embeds a token or credential;
- disables Git terminal and credential-manager prompts;
- refuses merge commits, diverged history, stashing, resets, cleans, and force
  operations;
- verifies the running branch exactly matches the fetched `origin/main` commit;
- runs off the pygame thread with a bounded timeout; and
- reports **Already up to date**, a successful update requiring a display
  restart, or a short categorized error.

Downloaded Python files do not replace modules already loaded by the running
process. After a changed update, press **Restart Display to Apply** and confirm
the service-only restart. The Raspberry Pi does not need to reboot.

No PolicyKit or `sudo` permission is needed. Git runs as the existing
`kevinchan` service user. This repository's public HTTPS remote needs no
credentials for read access. If the repository becomes private, configure a
non-interactive Git credential for that same service user; an interactive
`gh auth login` alone must not be assumed to make systemd Git operations work.

Validate the deployed checkout and non-interactive remote access before relying
on the touchscreen update:

```bash
sudo -u kevinchan git -C /home/kevinchan/bart-platform-display \
  status --short --branch
sudo -u kevinchan env GIT_TERMINAL_PROMPT=0 \
  git -C /home/kevinchan/bart-platform-display \
  ls-remote origin refs/heads/main
```

### Wi-Fi permissions (NetworkManager)

Wi-Fi control uses `nmcli`. The app runs as the `kevinchan` user in a headless
systemd service, so PolicyKit cannot show an interactive authentication prompt.
Install the repository's service-user-scoped rule:

```bash
sudo install -o root -g root -m 0644 \
  deploy/polkit/50-bart-platform-display-networkmanager.rules \
  /etc/polkit-1/rules.d/
sudo systemctl restart polkit
nmcli general permissions
```

The rule grants Wi-Fi scanning plus NetworkManager's `network-control` and
system-profile modification action classes to `kevinchan`. The relevant
permissions should report `yes`. The app remains non-root and is not authorized
to toggle the Wi-Fi radio. PolicyKit applies these grants to every process
running as `kevinchan` and cannot limit them to this application or to Wi-Fi
profiles, so use a dedicated service account if that broader per-user boundary
is not acceptable.

To roll the permission change back:

```bash
sudo rm /etc/polkit-1/rules.d/50-bart-platform-display-networkmanager.rules
sudo systemctl restart polkit
```

### System restart permissions (systemd-logind)

Restarting only the display service does not need elevated permission. The app
exits cleanly and the existing `Restart=always` systemd policy relaunches it
after five seconds.

A full Raspberry Pi reboot calls systemd-logind's `Reboot(false)` method over
the system D-Bus using `busctl`. The `false` argument disables interactive
authentication, which is unavailable to the headless service. Install the
separate reboot-only PolicyKit rule:

```bash
sudo install -o root -g root -m 0644 \
  deploy/polkit/51-bart-platform-display-reboot.rules \
  /etc/polkit-1/rules.d/
sudo systemctl restart polkit
```

The rule authorizes only the normal and multiple-session logind reboot actions,
and only when the request comes from the `kevinchan` process running in
`bart-platform-display.service`. It does not grant power-off, suspend,
ignore-inhibit, arbitrary service management, a root shell, or general `sudo`.

Confirm the running service is the authorization subject:

```bash
SERVICE_PID="$(systemctl show --property MainPID --value bart-platform-display)"
sudo pkcheck \
  --action-id org.freedesktop.login1.reboot \
  --process "$SERVICE_PID"
```

An exit status of `0` means the service process is authorized. Because the rule
is intentionally scoped to the systemd unit, calling the same logind method
from an ordinary SSH shell as `kevinchan` is not expected to receive this grant.

For end-to-end device validation, record both values before testing:

```bash
cat /proc/sys/kernel/random/boot_id
systemctl show --property MainPID --value bart-platform-display
```

After **Restart Display Service**, the main PID must change while the boot ID
stays the same. After **Restart Raspberry Pi**, reconnect over SSH and confirm
the boot ID changed and `systemctl is-active bart-platform-display` reports
`active`. Repeat the device reboot once while another SSH session is open to
exercise the multiple-session authorization.

To roll the reboot permission back:

```bash
sudo rm /etc/polkit-1/rules.d/51-bart-platform-display-reboot.rules
sudo systemctl restart polkit
```

---

## 1 — Configure your TFT display

The display driver must be loaded before running the app so the screen appears as `/dev/fb0`.

### Install lcd-show

Clone the lcd-show driver repo:

```bash
git clone https://github.com/goodtft/LCD-show.git ~/LCD-show
cd ~/LCD-show
sudo ./LCD35-show
```

The Pi reboots automatically. After reboot the driver will have written `dtoverlay=waveshare35a`
to `/boot/config.txt` — but on Raspbian trixie this is **the wrong file**. See the note below.

### Important: Raspbian trixie uses a different config path

On Raspbian 13 (trixie) the firmware reads from `/boot/firmware/config.txt`, not `/boot/config.txt`.
The lcd-show script doesn't know this and edits the wrong file, so the overlay never loads.

After running lcd-show and rebooting, check if `dtoverlay=waveshare35a` is in the right place:

```bash
grep waveshare /boot/firmware/config.txt
```

If it's missing, add it manually:

```bash
sudo nano /boot/firmware/config.txt
```

Add `dtoverlay=waveshare35a` at the bottom (keep `dtparam=spi=on` — it should already be there),
then reboot.

After reboot confirm the framebuffer exists:

```bash
ls /dev/fb*
```

You should see `/dev/fb0` (the TFT). On trixie there is no HDMI framebuffer — the TFT takes `fb0`.

### Disable the framebuffer console on the TFT

By default the Linux console (login prompt) renders to `fb0`, which conflicts with the app.
Disable it by adding `fbcon=map:10` to the kernel command line:

```bash
sudo nano /boot/firmware/cmdline.txt
```

Append `fbcon=map:10` to the end of the existing single line (do not add a new line), then reboot.
After this the TFT will show a blank screen at boot — that is correct.

---

## 2 — Copy the project to the Pi

```bash
git clone <this repo> ~/bart-platform-display
```

Or SCP from your dev machine:

```bash
scp -r bart-platform-display <user>@<PI_IP>:/home/<user>/
```

---

## 3 — Download the font

The app requires **Press Start 2P** (a free, open-source pixel font).

1. Go to Google Fonts and search "Press Start 2P"
2. Download the font ZIP
3. Extract `PressStart2P-Regular.ttf`
4. Place it at `fonts/PressStart2P-Regular.ttf` inside the project folder

```bash
ls fonts/PressStart2P-Regular.ttf   # should exist
```

---

## 4 — Install dependencies

```bash
cd ~/bart-platform-display
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

pygame installs via piwheels (pre-built wheel) so it should be fast. If it fails, install the
system package instead:

```bash
sudo apt install python3-pygame
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install requests numpy
```

---

## 5 — Test manually

```bash
cd ~/bart-platform-display
source venv/bin/activate
python main.py
```

The TFT should show the departure board. Press `Ctrl+C` to exit.

### Develop on a desktop (no Pi)

Set `BART_DEV=1` to run in a normal window instead of the framebuffer, with the
mouse standing in for touch (click = tap, click-drag = swipe/scroll). Wi-Fi and
touch hardware are mocked, so the settings panel UI can be built and tested off
the Pi. Full-device restart is disabled in this mode so exercising the System
view cannot reboot the development computer:

```bash
BART_DEV=1 python main.py
```

---

## 6 — Enable auto-start at boot

```bash
# Copy the service file
sudo cp ~/bart-platform-display/bart-platform-display.service \
        /etc/systemd/system/

# Reload systemd and enable the service
sudo systemctl daemon-reload
sudo systemctl enable bart-platform-display
sudo systemctl start bart-platform-display

# Check status
sudo systemctl status bart-platform-display
```

The display will now start automatically on every boot. SSH is unaffected.

**View logs:**

```bash
journalctl -u bart-platform-display -f
```

---

## 7 — Configuration

Edit `config.json` to change the station or other settings:

```json
{
  "station": "MONT",
  "station_name": "Montgomery St.",
  "platform": "2",
  "api_key": "YOUR_BART_API_KEY",
  "refresh_interval": 30
}
```

| Key                | Description                                      |
|--------------------|--------------------------------------------------|
| `station`          | BART station abbreviation (e.g. `MONT`, `EMBR`)  |
| `station_name`     | Display name shown in the top-left corner        |
| `platform`         | Platform number to display (`"1"` or `"2"`)      |
| `api_key`          | Your BART API key                                |
| `refresh_interval` | Seconds between API polls (default `30`)         |

After editing, use **Settings → System → Restart Display Service** on the
touchscreen, or restart the service over SSH:

```bash
sudo systemctl restart bart-platform-display
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `/dev/fb0` doesn't exist after reboot | lcd-show edited the wrong config file — manually add `dtoverlay=waveshare35a` to `/boot/firmware/config.txt` (not `/boot/config.txt`) and reboot |
| Display shows login prompt / console text | Add `fbcon=map:10` to `/boot/firmware/cmdline.txt` and reboot |
| `pygame.error: fbcon not available` | Expected on Raspbian trixie — SDL2 is built without fbcon/fbdev. The app uses `SDL_VIDEODRIVER=offscreen` and writes frames directly to `/dev/fb0` via mmap; no action needed |
| Display stays white, `dd if=/dev/zero of=/dev/fb0` has no effect | fbcon is still active and overwriting the framebuffer — confirm `fbcon=map:10` is in `/boot/firmware/cmdline.txt` |
| `Font not found` error | Place `PressStart2P-Regular.ttf` in `fonts/` |
| `LOADING...` stays forever | Check internet; run `journalctl -u bart-platform-display -f` for errors |
| Update App is disabled | Connect to Wi-Fi. Updates are intentionally unavailable in desktop development mode. |
| Update reports `Update setup is incomplete` | Confirm Git is installed and `/home/kevinchan/bart-platform-display` is on `main` with an `origin` remote and remote `main` branch. |
| Update reports `GitHub authentication failed` | Verify non-interactive read access as the `kevinchan` service user; do not place a token in the remote URL. |
| Update reports local changes or cannot fast-forward | Inspect the checkout over SSH. Commit or intentionally remove the device-only work elsewhere; the touchscreen updater never stashes, resets, cleans, or resolves history. |
| Wi-Fi action shows `Not authorized` | Reinstall the scoped PolicyKit rule above and confirm the required `nmcli general permissions` rows report `yes` |
| A correct Wi-Fi password still times out | Run `journalctl -u bart-platform-display -u NetworkManager --since "-5 minutes" --no-pager` and inspect the terminal NetworkManager reason |
| Wi-Fi disconnects after a successful connection | Run `journalctl -u bart-platform-display -u NetworkManager --since "-30 minutes" --no-pager`. The continuous supervisor records sanitized `phase`, error `code`, and NetworkManager reason number; manual scans also record `[wifi] device=... state=... reason=... active_profile=... autoconnect_off=...`. These lines exclude SSIDs, profile names, UUIDs, and passwords. |
| pip pygame build fails | Use system pygame: `sudo apt install python3-pygame` and create venv with `--system-site-packages` |

---

## How the framebuffer rendering works (Raspbian trixie)

On Raspbian trixie, SDL2 is compiled without the legacy `fbcon`/`fbdev` video backends, so the
standard approach of pointing SDL at the TFT with `SDL_VIDEODRIVER=fbcon` does not work.

Instead the app uses `SDL_VIDEODRIVER=offscreen` to render into a memory surface, then after each
frame converts the pixels from RGB24 to RGB565 (the TFT's native 16-bit format) using numpy and
writes them directly to `/dev/fb0` via `mmap`. This bypasses SDL's display stack entirely and
works regardless of which SDL2 backends are compiled in.
