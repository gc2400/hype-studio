# Hype Studio

Hype Radio TV's own playout (radio + TV) on the Hetzner server `88.99.105.186`.

## How updates work
- **`main`** = what runs on the server. The server's updater (`hype-updater.service`, `deploy/updater.py`) checks `main` every minute and installs new commits by itself:
  1. **Conflict check:** files changed on the server by hand → no install (`conflict`).
  2. **Lab test:** a `radio/radio.liq` change is tested in a lab first (`deploy/lab_radio.sh`).
  3. **Install:** backup to `/var/backups/hype/<time>/`, install, and restart only the services that changed (radio last).
  4. **Check after 45 s:** services active + guard OK + the radio not silent. Otherwise **rollback**.
- **`status`** = the server's report: `status.json` (the last update, the log, services, guard) and `history.log`.
- **`[night]`** in a commit message: install between 04:00 and 05:00 (a radio restart = ~10 s silence).
- **Changed something directly on the server?** Run `python3 /srv/hype-repo/deploy/export.py`, then commit + push from `/srv/hype-repo`.

## Layout
| Folder | What |
|---|---|
| `engine/` | `/opt/hype/*.py` – engine (playout, API), planner (clocks), live, tickers, indexer, guard |
| `radio/radio.liq` | Liquidsoap: mixing, sound processing, outputs |
| `tv/ladder.sh` | HLS ladder (1080/720/480) + the panel preview |
| `web/panel/` | Studio panel (`/panel/`) |
| `web/status/` | Status page |
| `web/overlay/` | OBS graphics pages |
| `web/guest/` | Guest page |
| `system/` | systemd units, nginx site |
| `deploy/` | `files.txt` (repo ↔ server paths + what to restart), updater, export, lab test |

No passwords or keys are stored here: `/etc/hype/*.json`, `mediamtx.yml` and OBS `service.json` stay on the server only.
