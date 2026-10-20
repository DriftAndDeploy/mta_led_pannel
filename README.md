# When Is The Train Coming

A 64×32 LED panel that shows the next catchable NYC subway train, configured
from a phone at **https://whenisthetraincoming.com**.

```
ESP32 + HUB75 panel  ──HTTPS──▶  Cloudflare tunnel  ──▶  Raspberry Pi (Flask)  ──▶  MTA GTFS-RT
   polls every 15 s                whenisthetraincoming.com     server/proxy_server.py
   pulls OTA updates  ◀─────────────────────────────────  server/firmware/firmware.bin
```

## Repo layout

| Path | What it is |
|---|---|
| `firmware/subway_display/subway_display.ino` | ESP32 sketch (open this folder in Arduino IDE) |
| `server/proxy_server.py` | Pi server: panel API, web app API, OTA endpoints |
| `server/templates/index.html` | The phone web app |
| `server/firmware/firmware.bin` | Published OTA image (created by the publish script) |
| `server/test_server.py` | Offline tests: `cd server && venv/bin/python -m unittest -v` |
| `deploy/install.sh` | One-time Pi setup / migration |
| `deploy/update.sh` | Pull from GitHub + restart right now (otherwise automatic every 5 min) |
| `deploy/auto_update.sh` | Run by the `mta-autoupdate` timer every 5 minutes |
| `tools/publish_firmware.ps1` / `.sh` | Copy an exported build into `server/firmware/` |

---

## One-time setup

### 1. Move the Pi onto this repo

The Pi currently runs from `~/code/mta_panel_server`. This switches the
`mta-proxy` service to run from the repo and copies your panel configs over
(the old folder is left untouched as a backup).

First give the Pi read-only access to the private repo with a **deploy key**:

```bash
# on the Pi
ssh-keygen -t ed25519 -f ~/.ssh/mta_deploy -N ""
cat ~/.ssh/mta_deploy.pub
# GitHub → DriftAndDeploy/mta_led_pannel → Settings → Deploy keys → Add key
# paste it, leave "Allow write access" OFF
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/mta_deploy
EOF
```

Then clone and install:

```bash
cd ~/code
git clone git@github.com:DriftAndDeploy/mta_led_pannel.git
cd mta_led_pannel
bash deploy/install.sh
```

If the old `tunnel-watcher` service still exists it is no longer needed (the
named tunnel is permanent): `sudo systemctl disable --now tunnel-watcher`.
Cloudflare's `config.yml` does not change — it still points at `localhost:5000`.

Check it: open whenisthetraincoming.com — the header should say **Online**
next to your panel within 15 seconds of the panel being on.

### 2. Flash the panel over USB one last time

OTA needs a partition layout with two app slots and room to spare (the
firmware is ~1.2 MB). In Arduino IDE:

1. **Tools → Board:** ESP32 Dev Module (esp32 core 3.x)
2. **Tools → Partition Scheme:** `Minimal SPIFFS (1.9MB APP with OTA/190KB SPIFFS)`
3. **Tools → Erase All Flash Before Sketch Upload:** Disabled (keeps WiFi credentials)
4. Libraries: ESP32-HUB75-MatrixPanel-I2S-DMA, Adafruit GFX, ArduinoJson 7.x, WiFiManager (tzapu)
5. Open `firmware/subway_display/` and upload.

Repeat for each panel. After this, never plug in again unless a panel is dead.

> The Panel ID is the last 6 hex digits of the chip's MAC — the same IDs
> v5.22 used. If a panel shows a different ID than the web app remembers,
> open **⚙ Settings → Switch panel** in the web app and enter the one on the screen.

---

## Everyday workflows

### Change the server or web app

```bash
git commit -am "..." && git push
```

That's it. The Pi checks GitHub every 5 minutes, pulls new commits and
restarts the server. If the new version fails its health check it rolls back
on its own and skips that commit until you push a fix.

> **Everything pushed to `main` goes live within 5 minutes**, including
> `server/firmware/firmware.bin`, which the panels then install. Put
> unfinished work on a branch.

Check what it did: `journalctl -u mta-autoupdate -n 30 --no-pager` (on the Pi).
To update immediately instead of waiting, run `deploy/update.sh` on the Pi.

### Release new panel firmware over the air

1. Edit the sketch and **bump `FW_BUILD`** (e.g. `600` → `601`) and `FW_VERSION`.
2. Arduino IDE: **Sketch → Export Compiled Binary** (same board + partition settings as above).
3. Publish it:
   ```powershell
   powershell -ExecutionPolicy Bypass -File tools\publish_firmware.ps1   # Windows
   ```
   ```bash
   ./tools/publish_firmware.sh                                           # WSL / Git Bash
   ```
   It refuses to publish unless the build number went up.
4. Commit and push:
   ```bash
   git add server/firmware/firmware.bin firmware/ && git commit -m "Firmware build 601" && git push
   ```

Within 5 minutes the Pi has it, and ~15 s later each panel shows **UPDATING** with a progress bar, reboots, and
comes back on the new build. **⚙ Settings → Firmware** in the web app shows
the running build (`Build 600 → 601 (updating)`, then `Build 601 · up to date`).

**Safety nets**
- The download is MD5-verified before the panel switches to it.
- A newly flashed build is "on probation" until it talks to the server
  successfully. If it crashes or reboots first, the bootloader automatically
  goes back to the previous build.
- A panel won't retry a build that failed until it reboots.
- To hold one panel back, set `"auto_update": false` in
  `server/data/config_<PANELID>.json` on the Pi.
- To stop a rollout, `git revert` the firmware commit and run `update.sh`.

Test risky changes on one panel over USB before publishing — rollback
protects against crashes, not against firmware that runs but misbehaves.

---

## What the panel shows

- **Normal:** next catchable train time (top), line bullet + scrolling text
  along the bottom in this order: "Leave by" (white), weather, service alert
  (yellow) — each only if it's turned on (and, for alerts, if there is one).
- **Minimalist:** bullet + time centred, with a 2×2 status dot in the top-left
  corner (green normal, yellow alerts/changes, red suspended).
- **Weather** (optional, **Display Options → Show Weather**): icon + temperature
  (°F) for the selected station, plus an **umbrella** icon when there's at least
  a 50% chance of rain in the next 12 hours (or it's raining now). Normal mode
  shows it beside the bullet, or at the front of the scrolling text; minimalist
  mode shows it centred under the time. Data from Open-Meteo (free, no key),
  refreshed every 15 min; if it can't be reached the panel just hides it.
- **Animations** (**Display Options → Animations**): when a train is due at your
  station within a minute, the panel shows the bullet + "ARRIVING" and a little
  two-car train in the line's colour pulls in, stops and leaves (~4 s), once per
  train — for both trains in two-train mode. **Preview train** plays it now.
  Also with Animations on: on holidays and on the solstices/equinoxes the whole
  panel plays a 10 s themed animation every 10 minutes — fireworks (New Year's
  Day/Eve, July 4th), hearts (Valentine's), shamrocks (St. Patrick's), eggs
  (Easter), jack-o'-lantern (Halloween), leaves (Thanksgiving, fall equinox),
  tree (Christmas Eve/Day), flag (Presidents, Memorial, Labor, Veterans Day),
  stars (MLK Day, Juneteenth), flowers / sun / snow (spring, summer, winter).
  **Preview a holiday** shows the next one each time you tap it. Solstice and
  equinox dates are calculated (accurate to about a minute, NYC time).
- **Night Schedule** (**Display Options → Night Schedule**): the panel goes dark
  between the **Off at** and **On at** times (default 1:00–6:00 AM, NYC time;
  can cross midnight). Tapping the power button during that window turns it
  back on until the window ends; the schedule resumes the next night. The power
  button shows blue while the panel is asleep on schedule.
- **Both Directions** (**Display Options → Both Directions**): the same two-row
  layout for your line and station, ↑ uptown (N) on top and ↓ downtown (S)
  below — a small arrow next to each bullet tells them apart. Turning it on
  turns Two Trains off and vice versa.
- **Two Trains** (**Display Options → Two Trains**): two rows, each a bullet
  and a big arrival time, right-aligned so the colons line up. A yellow/red dot
  after a time means delays/suspension on that line. Pick the second train with
  the **Train 1 / Train 2** tabs that appear above section 01 (sections 01–03
  and 05 edit whichever tab is selected). Scroll text, weather and minimalist
  mode are paused while it's on.
- **Demo** (**⚙ Settings → Demo Mode**): the server rotates a random line/station every 45 s — runs on the
  Pi, so it keeps going with the phone closed.
- Train time shows `--:--` if the panel hasn't heard from the server for 3 minutes.

On power-up the Panel ID shows for 15 s; it also stays on the WiFi setup
screen. With no known WiFi the panel opens the **PanelSetup** hotspot —
connect, and if no page appears browse to `http://192.168.4.1`.

If that page won't load, plug the panel into your PC instead: Arduino IDE →
**Tools → Serial Monitor**, set **115200 baud** and **Newline**, then type
`NetworkName|password` and press Enter. The panel saves it and connects.

## Changing WiFi (new router or password)

**Before** you swap routers or change the password, while the panels are still
online:

1. Open the web app → **⚙ Settings → Panel WiFi** → enter the new network name and
   password → **Send to panel**.
2. Within ~15 s it shows "✓ Panel saved …". The password is then deleted from
   the Pi; only the panel keeps it.
3. Repeat for each panel (**⚙ Settings → Switch panel**).
4. Swap the router. About 30 s after the old network disappears, each panel
   scans and joins the new one by itself.

Each panel remembers up to 4 networks, so it also moves between known networks
on its own. Easiest of all: give a new router the same name and password as the
old one, and nothing needs to change.

Forgot to do it in advance? Use the PanelSetup hotspot or the Serial Monitor
method above.

## Troubleshooting

| Symptom | Check |
|---|---|
| Web app says "Not seen yet" | Panel ID matches the one on the panel's boot screen? |
| Panel stuck on "Fetching trains" | Pi up? `curl https://whenisthetraincoming.com/healthz` |
| `--:--` everywhere | MTA feed down — `sudo journalctl -u mta-proxy -n 50 --no-pager` |
| OTA never starts | `curl https://whenisthetraincoming.com/firmware/info` shows the new build? Panel was flashed with the Minimal SPIFFS scheme? |
| "UPDATE FAIL" on panel | Serial monitor at 115200 prints the reason; usually a partition-scheme mismatch |
| PanelSetup page won't load | Use the Serial Monitor method above (USB cable) |
| Faint lines / glow next to lit pixels | **⚙ Settings → Ghosting Fix** → raise it (4 → 6 → 8) until it's gone |
| Pushed but nothing changed | Wait 5 min, then on the Pi: `journalctl -u mta-autoupdate -n 30 --no-pager` |
