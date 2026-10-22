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

- **New panel:** nothing is assumed about where it lives. Until a line and
  station are picked in the web app it shows `--:--` and scrolls "Pick your
  line and station at whenisthetraincoming.com".
- **Normal:** next catchable train time (top), line bullet + scrolling text
  along the bottom in this order: Arrive By's "Leave no later than …" (green),
  "Leave by" (white), weather, service alert (yellow) — each only if it's
  turned on (and, for alerts, if there is one).
- **Minimalist** is now a preset in **07 Customize Layout** (bullet + time
  centred, 2×2 status dot: green normal, yellow alerts/changes, red suspended).
  Panels that had the old Minimalist Mode toggle on were switched to that
  layout automatically, with a scrolling line if leave-by/alerts were on.
- **Weather** (optional, **Display Options → Show Weather**): icon + temperature
  (°F) for the selected station, plus an **umbrella** icon when there's at least
  a 50% chance of rain in the next 12 hours (or it's raining now). Normal mode
  shows it beside the bullet, or at the front of the scrolling text. Data from Open-Meteo (free, no key),
  refreshed every 15 min; if it can't be reached the panel just hides it.
- **Arrive By** (**Display Options → Arrive By**): pick a destination station
  and the time you need to be there. Starting from the panel's station (02)
  plus its walk time (05), the Pi uses the live feeds of every line (direct,
  or one transfer — any two stations within ~300 m count, with walking time
  added) to find the latest you can leave. In the 3 hours before the deadline
  the panel scrolls e.g. "Leave no later than 8:12 to reach 14 St-Union Sq by
  9:00". Live trips only reach about an hour ahead, so until they cover the
  deadline the time is an estimate, shown with a "~" (fastest trip right now +
  8 min for waiting). If nothing gets there in time: "Leave now: you'd reach
  … at 9:07 (7 min late)". The toggle's hint shows what it would say right
  now and which train it's based on. It scrolls along the bottom of the normal
  screen; with Use My Layout on, it scrolls in the Scrolling text piece. The
  built-in Two Trains / Both Directions screens have no room for it.
- **Live Scores** (**Display Options → Live Scores**, with **MLB** / **NBA**
  sub-toggles) turns the panel into a scoreboard: it shows games *instead of*
  trains, and the train options are locked in the web app until it's turned off
  (Night Schedule still works). Team abbreviations in team colours, big scores,
  inning/quarter. A Yankees/Mets/Knicks/Nets game always wins; several take turns
  every 20 s; with none, the closest games do. When a game it was showing ends,
  the final stays up for 5 minutes with confetti and "NYK WIN!" (no celebration
  if a New York team lost). No game on: "NEXT GAME" with the next start time.
  **Preview game** / **Preview win** show samples. Data: ESPN's public
  scoreboard, every 30 s.
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
- **Text color** (section 06): the train time's color. Turn on **Use for All
  Text** to also use it for leave-by, weather, service alerts and "ARRIVING"
  (line bullets and holiday greetings keep their own colors).
- **Two Trains** (**Display Options → Two Trains**): two rows, each a bullet
  and a big arrival time, right-aligned so the colons line up. A yellow/red dot
  after a time means delays/suspension on that line. Pick the second train with
  the **Train 1 / Train 2** tabs that appear above section 01 (sections 01–03
  and 05 edit whichever tab is selected). Scroll text and weather are paused
  while it's on (unless your custom "Two rows" layout places them).
- **Custom layout** (section **07 Customize Layout**, collapsed until you tap
  it): **Open Layout Editor** shows a pixel-exact simulation of the 64×32
  panel. Drag pieces around (they snap to LED pixels), tap one to change it:
  **A− / A+** font size for anything with text (Tiny 3×5, Small 5×7, Big,
  Bigger, Huge; the scrolling text goes Tiny to Bigger, weather Small to
  Bigger), bullet size, alignment and colour, nudge it with the arrows, or
  remove it. **Add** offers the train time, line bullet, leave-by time,
  scrolling text (Arrive By message + service alerts, plus weather if ticked;
  leave-by is always its own piece), weather, status dot, clock and a short
  custom label. The **One train** tab is the normal screen;
  the **Two rows** tab is used whenever Two Trains or Both Directions is on and
  adds train-2 pieces. Presets (Standard, Minimalist, Two rows) are starting
  points. **Send to Panel** saves both layouts and turns **Use My Layout** on;
  the panel picks it up on its next update (~15 s). Turning the toggle off goes
  back to the built-in screens without losing your layout. Live
  Scores, animations and the night schedule still take over as usual; Leave By /
  Alerts / Weather show whenever their pieces are placed.
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
