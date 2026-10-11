# Argus NVR

Self-hosted network video recorder for IP cameras. Motion-triggered recording, WebRTC live view, face recognition, multi-user RBAC, PTZ control, smart search, and a timeline scrubber — with build scripts for a Windows installer and a Linux tarball.

Built on FastAPI + React + go2rtc + FFmpeg + SQLite.

---

## Features

### Recording & Playback
- **Multi-camera live view** — WebRTC grid (1 / 4 / 6 / 9 layouts), sub-second latency, self-healing reconnect with exponential backoff
- **Sub-stream toggle** — switch each live tile between main stream (high quality) and sub-stream (low bitrate) without reloading
- **Motion-triggered recording** — per-camera detection zones, configurable sensitivity, pre/post-motion buffer
- **Continuous recording** — 24/7 fragmented MP4 via FFmpeg stream-copy (near-zero CPU; crash-safe segments — no footage lost on unexpected shutdown)
- **Timeline playback** — 24-hour scrubber with event markers, zoom levels (1h / 3h / 6h / 24h), and **hover thumbnail previews** at any point on the timeline
- **Clip export** — download MP4 clips (the export API also supports a burned-in camera name + timestamp watermark; not yet exposed in the UI)
- **Snapshot from live view** — one-click JPEG download from any live camera tile
- **Camera cycle / kiosk mode** — full-screen carousel through all live cameras (configurable dwell time)

### Detection & Analysis
- **Face recognition** — InsightFace buffalo\_sc (512-d ONNX, CPU); name and track persons across cameras
- **Heatmap overlay** — per-camera motion heatmap accumulated over time; toggle in the live view player; reset via API
- **Search** — search events by label or camera name; filter by camera, event type, time range, detection score, and whether a recording exists
- **Hikvision support** — ISAPI alert stream for hardware VMD events (faster than frame-diff)
- **Wyze support** — via RTSP firmware

### Camera Control
- **PTZ control** — ONVIF pan/tilt/zoom via in-player D-pad and stop; per-camera enable; operator or admin role required (preset list/go-to exists in the API, no UI yet)
- **Privacy masks** — define rectangular exclusion zones per camera (stored as JSON, applied in the detection pipeline)
- **Scheduled arming** — per-camera arm windows (days + start/end, overnight windows supported); detection only runs inside them

### Users & Access
- **Multi-user accounts** — admin / operator / viewer roles; admin creates and manages users via the Users page
- **Role-based access control** — sensitive operations (purge, user management, audit log) require admin role
- **Audit log** — logins, password changes, and user create/update/delete/reset are logged with username, IP, and timestamp; paginated viewer with filters and purge
- **Forced password change** — new users created with `must_change_password=1`; until it's changed, their token is only accepted by the password-change endpoint

### Notifications
- **Push notifications** — Telegram, ntfy, SMTP email, and HMAC-signed webhook; person-aware alerts. FCM sending is implemented in the backend for a companion mobile app (not included), configured via the `notify_fcm_service_account_json` setting
- **Web Push / PWA** — VAPID-based browser push notifications; subscribe from the Settings page; requires HTTPS or localhost

### Infrastructure
- **Cloud backup** — rclone-based sync of the recordings directory to any rclone-supported remote (S3, Backblaze, Google Drive, etc.); triggered manually from Settings (last run status shown; full history via `GET /api/cloud-backup/history`)
- **QR code** — Settings page shows a scannable QR code for the app URL; useful for onboarding mobile devices
- **Per-camera retention** — storage retention limit configurable per camera (overrides global `RETENTION_MAX_AGE_DAYS`)
- **Auto storage retention** — prunes oldest footage by age, free-space floor, and an optional total-size cap (`RETENTION_MAX_TOTAL_GB`)
- **Camera health dashboard** — per-camera recorder status, detector status, last recording time, and events-per-hour in the Health tab
- **Windows installer** — Inno Setup build with embedded Python 3.11 and service registration (NSSM)
- **Linux installer** — bash script, systemd unit, `argus-update` upgrade script
- **Progressive Web App** — installable on mobile (online only; the service worker doesn't cache)

---

## Installation

There are no prebuilt releases yet; build from source.

### Linux (Debian 12/13, Ubuntu 22.04/24.04)

```bash
git clone https://github.com/zer0contextlost/argus-nvr.git
cd argus-nvr
(cd frontend && npm ci && npm run build)   # needs Node.js 20+
sudo ./installer/linux/install.sh
```

Installs to `/opt/argus/`, creates an `argus` system user, writes `/etc/argus/.env` with a fresh secret key, and enables the `argus` systemd unit. You need FFmpeg (`apt install ffmpeg`) and a [go2rtc](https://github.com/AlexxIT/go2rtc/releases) binary at `/opt/argus/tools/go2rtc` (or set `GO2RTC_PATH`).

To upgrade from a tarball built with `installer/linux/build-tarball.sh`:
```bash
sudo argus-update --from dist/argus-1.0.0-linux-x86_64.tar.gz
```

### Windows

Build the installer (see below), then run `dist\Argus-Setup-x.x.x.exe`. It installs under Program Files, registers a Windows service, and opens TCP 8080 in the firewall.

### First login

**Default credentials:** `admin` / `Screenshot123!`. The API refuses everything except the password-change endpoint until you set a new password. Set `ARGUS_ADMIN_PASSWORD` before first start to skip the default entirely.

---

## Development setup

### Prerequisites

- Python 3.11+
- Node.js 20+
- [go2rtc](https://github.com/AlexxIT/go2rtc/releases) binary
- FFmpeg

### Backend

```bash
# from the repo root
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env   # then edit ARGUS_SECRET_KEY at minimum
uvicorn sentinel.main:app --reload --port 8080
```

### Frontend

```bash
cd frontend
npm ci
npm run dev   # http://localhost:5173 — proxies /api to :8080
```

### Build the Windows installer

```powershell
# One-time setup (Inno Setup + embedded Python + binaries):
.\installer\windows\setup-build-env.ps1

# Incremental build:
.\installer\windows\build-installer.ps1 -SkipFrontend -SkipBinaries -SkipPython

# Full build:
.\installer\windows\build-installer.ps1
# Output: dist\Argus-Setup-1.0.0.exe
```

### Build the Linux tarball

```bash
bash installer/linux/build-tarball.sh
# Output: dist/argus-1.0.0-linux-x86_64.tar.gz
```

---

## Configuration

Copy `.env.example` to `.env`. The following variables are recognised:

| Variable | Description | Default |
|---|---|---|
| `ARGUS_SECRET_KEY` | JWT signing + camera-password encryption key. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Changing it later makes stored camera passwords unreadable | **set this** (insecure placeholder otherwise) |
| `ARGUS_DATA_ROOT` | Root for recordings, events, database, logs | auto-inferred |
| `ARGUS_HOST` | Bind address | `0.0.0.0` |
| `ARGUS_PORT` | HTTP port | `8080` |
| `ARGUS_RECORDINGS_DIR` | Recording storage | `<data_root>/recordings` |
| `ARGUS_EVENTS_DIR` | Event thumbnails | `<data_root>/events` |
| `ARGUS_DB_PATH` | SQLite database | `<data_root>/data/sentinel.db` |
| `FFMPEG_PATH` | FFmpeg binary | auto-detected from `<data_root>/tools` |
| `GO2RTC_PATH` | go2rtc binary | auto-detected from `<data_root>/tools` |
| `SEGMENT_SECONDS` | Recording segment length | `120` |
| `RETENTION_MAX_AGE_DAYS` | Delete footage older than N days | `30` |
| `RETENTION_FREE_SPACE_FLOOR_PCT` | Keep at least N% free on the data drive | `10` |
| `DETECT_FPS` | Detection frame rate | `5` |
| `RETENTION_MAX_TOTAL_GB` | Hard cap on total recording size (0 = off) | `0` |

Notification settings (Telegram tokens, SMTP credentials, etc.) are stored in the database after first configuration via the Settings page — they do not need to be in `.env`.

---

## Adding cameras

### Auto-discovery

**Cameras > Scan Network** probes the LAN via ONVIF WS-Discovery, Hikvision SADP broadcast, and subnet port scan. Click a discovered device to pre-fill the form.

### Manual add

**Cameras > Add Camera**. Key fields:

| Field | Notes |
|---|---|
| Make / preset | Hikvision cameras get the ISAPI alert subscription; Wyze and generic RTSP presets fill in stream paths |
| Host | Camera IP address |
| RTSP user / password | Stored encrypted (Fernet) |
| Main path / Sub path | RTSP path suffixes — Hikvision defaults: `Streaming/Channels/101` / `102` |
| Record mode | `continuous`, `motion-only`, or `continuous+events` |
| PTZ capable | Enable to show PTZ controls in live view |
| Privacy masks | JSON array of `{x, y, w, h}` rectangles (0–1 normalised) to exclude from detection |
| Arm schedule | JSON array of `{days, start, end}` objects to limit detection hours |

After saving, use **Test** on the camera card to verify RTSP reachability.

### Hikvision substream recommendation

- **Resolution:** 640×360 or 704×480 (low-res is fine for detection)
- **Encoding:** H.264 (not H.265 — browser WebRTC compatibility)
- **Frame rate:** 5–10 fps

---

## Architecture

```
Cameras (RTSP)
      │
      ▼
  go2rtc  ──  one connection per camera, fans out internally
      │
  ┌───┴──────────────────────────────────────┐
  │                                          │
FFmpeg recorder                       Motion detector
(stream-copy, near-zero CPU)    (frame-diff + blob filter)
  │                                          │
  │                                 InsightFace (optional)
  │                                 face recognition
  │                                          │
  └──────────────┬───────────────────────────┘
                 │
        FastAPI backend (:8080)
        ├─ REST API + JWT auth (Bearer + ?token= for media)
        ├─ RBAC (admin / operator / viewer)
        ├─ go2rtc reverse proxy (HTTP + WebSocket)
        ├─ SQLite (WAL, auto-migrations)
        ├─ Audit log
        ├─ Storage retention manager
        ├─ Cloud backup (rclone)
        └─ Notifier (Telegram / ntfy / SMTP / webhook / FCM / Web Push)
                 │
        React SPA (built frontend/dist/)
        WebRTC live view · Timeline · Events · Persons · Smart Search
        Users · Audit · Settings
```

**go2rtc** is reverse-proxied through the app — browsers only ever connect to port 8080. go2rtc's internal ports (1984, 8554, 8555) stay on `127.0.0.1`.

---

## Remote access

### LAN

Argus binds to `0.0.0.0` by default. Open `http://<nvr-ip>:8080` from any device on your network. For WebRTC live view from other machines, also open UDP 8555:

```powershell
# Elevated PowerShell
netsh advfirewall firewall add rule name="Argus NVR Web" dir=in action=allow protocol=TCP localport=8080
netsh advfirewall firewall add rule name="Argus NVR WebRTC TCP" dir=in action=allow protocol=TCP localport=8555
netsh advfirewall firewall add rule name="Argus NVR WebRTC UDP" dir=in action=allow protocol=UDP localport=8555
```

The Windows installer's `service-install.ps1` only adds the TCP 8080 rule; add the 8555 rules yourself if you need WebRTC from other machines.

Use **Settings > QR Code** to scan the app URL onto a phone without typing.

### Remote / Tailscale (recommended)

Install [Tailscale](https://tailscale.com) on the NVR machine and your phone. Access via the Tailscale IP (`100.x.y.z:8080`) — zero port-forwarding, no public IP exposure.

---

## Stack

| Component | Technology |
|---|---|
| RTSP restreaming | go2rtc (single binary) |
| Recording | FFmpeg (stream-copy, fragmented MP4) |
| Motion detection | Frame-diff + scipy blob filter |
| Face recognition | InsightFace buffalo\_sc (ONNX, CPU) |
| PTZ control | ONVIF SOAP over httpx (no zeep) |
| Heatmap accumulation | Pillow (PNG overlay) |
| Cloud backup | rclone subprocess |
| Web Push | pywebpush (VAPID) |
| Backend | Python 3.11 + FastAPI + pydantic-settings |
| Database | SQLite (WAL, schema migrations) |
| Frontend | React 19 + TypeScript + Vite |
| Live view | WebRTC (trickle ICE via WebSocket) |
| Windows service | NSSM |
| Windows installer | Inno Setup 6 |

---

## Security

- All `/api/*` routes require JWT Bearer auth except `GET /api/health`
- Media endpoints (`/recordings/*/video`, thumbnails, heatmaps, snapshots) also accept `?token=` query param so browser `<video>` and `<img>` elements work without custom headers
- Camera passwords encrypted at rest (Fernet, key = `ARGUS_SECRET_KEY`)
- First-run default password (`Screenshot123!`) must be changed before the API accepts the session for anything else (enforced server-side via a token claim)
- **Role-based access control** — admin / operator / viewer; user management and audit log are admin-only; destructive operations (purge recordings) require admin
- **Audit log** — logins, password changes, and user management actions are recorded (action, user, IP, timestamp)
- Webhook notifications signed with HMAC-SHA256 (header `X-Argus-Signature`) — receivers should verify before acting
- Settings API whitelists recognised keys — arbitrary DB key injection via the settings endpoint is blocked
- Face embeddings stored as raw float32 bytes and never unpickled
- Do not expose port 8080 to the public internet — use Tailscale or a reverse proxy with TLS

---

## Known limitations

- **Two-way audio is not implemented.** The live player only opens a receive-only audio channel. A real implementation needs a manufacturer-specific back-channel (e.g. Hikvision's ISAPI `TwoWayAudio` endpoints) and won't work uniformly across mixed camera brands — Wyze, for example, uses its own proprietary protocol for talk-back rather than RTSP/ONVIF.
- **Hikvision ISAPI motion alerts require firmware support.** Some models/firmwares don't expose the `alertStream` endpoint at all; the subscriber detects this (persistent 404s) and backs off automatically. Frame-diff motion detection still runs normally on those cameras — you just lose the faster ISAPI-triggered recording start.
- **Not implemented yet** (settings exist but nothing reads them): ONVIF output (re-exposing cameras as ONVIF sources) and scheduled cloud backup.
- **No automated test suite** in this repo.
- **ONVIF PTZ/discovery** assumes reasonably standards-compliant camera firmware; very old devices may not respond correctly to all SOAP calls.

---

## License

MIT
