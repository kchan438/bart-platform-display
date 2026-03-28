# bart-platform-display

A standalone pygame app for a Raspberry Pi Zero W + 3.5" SPI TFT display (480x320).
Displays real-time BART Platform 2 departures for a configured station.

## Hardware

- Raspberry Pi Zero W
- 3.5" SPI TFT LCD (480x320, GPIO HAT, driver mapped to `/dev/fb1`)
- 32-bit Raspberry Pi OS Lite

SSH remains enabled so you can modify the project remotely at any time.

---

## Project structure

```
bart-platform-display/
├── main.py                         # pygame app
├── config.json                     # station, platform, API key
├── requirements.txt
├── fonts/
│   └── PressStart2P-Regular.ttf    # must be downloaded (see below)
└── bart-platform-display.service   # systemd unit for auto-start
```

---

## 1 — Configure your TFT display

Before running the app your display driver must be loaded so the screen appears
as `/dev/fb1`. How to do this depends on your display model.

**Waveshare 3.5" (Type A/B, ILI9486):**
Follow the official Waveshare wiki driver install instructions for your model.
After install, reboot and confirm `/dev/fb1` exists:

```bash
ls /dev/fb*
```

You should see both `/dev/fb0` (HDMI) and `/dev/fb1` (TFT).

---

## 2 — Copy the project to the Pi

From your dev machine:

```bash
scp -r bart-platform-display pi@<PI_IP>:/home/pi/
```

Or clone/copy however you prefer.

---

## 3 — Download the font

The app requires **Press Start 2P** (a free, open-source pixel font).

1. Go to Google Fonts and search "Press Start 2P"
2. Download the font ZIP
3. Extract `PressStart2P-Regular.ttf`
4. Place it at `fonts/PressStart2P-Regular.ttf` inside the project folder

```bash
# On the Pi, from inside the project directory:
ls fonts/PressStart2P-Regular.ttf   # should exist
```

---

## 4 — Install dependencies

SSH into the Pi, then:

```bash
cd /home/pi/bart-platform-display

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

pygame's pip install builds from source on 32-bit Pi OS — it will take a few
minutes. If it fails, install the system package instead:

```bash
sudo apt install python3-pygame
```

Then remove `pygame` from `requirements.txt` and create the venv with
`--system-site-packages`:

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install requests
```

---

## 5 — Test manually

```bash
cd /home/pi/bart-platform-display
source venv/bin/activate
python main.py
```

The TFT should show the departure board. Press `ESC` (if a keyboard is attached)
or `Ctrl+C` in the terminal to exit.

**Desktop/dev testing** (no TFT): override the SDL driver before running:

```bash
SDL_VIDEODRIVER='' SDL_FBDEV='' python main.py
```

This opens a normal pygame window on your desktop.

---

## 6 — Enable auto-start at boot

```bash
# Copy the service file
sudo cp /home/pi/bart-platform-display/bart-platform-display.service \
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
  "platform": "2",
  "api_key": "YOUR_BART_API_KEY",
  "refresh_interval": 30
}
```

| Key                | Description                                      |
|--------------------|--------------------------------------------------|
| `station`          | BART station abbreviation (e.g. `MONT`, `EMBR`)  |
| `platform`         | Platform number to display (`"1"` or `"2"`)      |
| `api_key`          | Your BART API key                                |
| `refresh_interval` | Seconds between API polls (default `30`)         |

After editing, restart the service:

```bash
sudo systemctl restart bart-platform-display
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Black screen on TFT | Check `/dev/fb1` exists; confirm display driver is installed |
| `Font not found` error | Place `PressStart2P-Regular.ttf` in `fonts/` |
| `pygame.error: No available video device` | Confirm `SDL_VIDEODRIVER=fbcon` and `SDL_FBDEV=/dev/fb1` are set |
| `LOADING...` stays forever | Check internet connectivity; run `journalctl -u bart-platform-display -f` for errors |
| pip pygame build fails | Use system pygame: `sudo apt install python3-pygame` and create venv with `--system-site-packages` |
