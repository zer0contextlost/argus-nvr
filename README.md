# Argus NVR

A self-hosted network video recorder for IP cameras. Argus pulls RTSP streams through [go2rtc](https://github.com/AlexxIT/go2rtc), records with FFmpeg stream-copy, detects motion on the CPU, and serves a React web app with WebRTC live view and a timeline for playback. Everything lives in one SQLite database and one recordings folder. It needs no GPU, no cloud account and no Docker.

It's a one-person project, built for and run on a single home setup of Hikvision and Wyze cameras. Expect rough edges on other cameras and platforms.

## What it does

**Live view.** WebRTC grid in 1, 4, 6 or 9 tiles. Each tile can switch between main and sub stream, take a JPEG snapshot, or show a motion heatmap. Double-click a tile for fullscreen. A cycle mode rotates through the cameras for a wall display.

**Recording.** Per camera: `motion-only`, `continuous`, or `continuous+events`. Recording is FFmpeg stream-copy into 2-minute fragmented MP4 segments, so CPU stays low and a segment cut off by a crash or power loss is still playable. Motion clips start with 5 seconds of pre-roll from a rolling buffer.

**Motion detection.** Frame differencing on the sub stream with blob filtering. You can set detection zones, privacy masks, sensitivity, and arm schedules (overnight windows work). On Hikvision cameras Argus also subscribes to the ISAPI alert stream, which can start a recording sooner than frame diffing.

**Playback.** 24-hour scrubber with event markers, 1h/3h/6h/24h zoom and thumbnail previews on hover. Segments are double-buffered, so playback doesn't stall at file boundaries. You can export any range as an MP4.

**Events and search.** Event list with thumbnails. Filter by camera, type, time range, score, and whether a recording exists. Optional face recognition (InsightFace on CPU) lets you name people and get person-aware alerts.

**Notifications.** Telegram, ntfy, SMTP, HMAC-signed webhooks, and browser Web Push. A backend for FCM is included for a companion mobile app, but no app ships in this repo.

**Users.** Admin, operator and viewer roles, an audit log of logins and user changes, and a forced password change on first login that the server enforces.

**Storage.** Retention by age, by a free-space floor, by an optional total-size cap, and per camera. The Health page shows recorder and detector status per camera, disk usage, and how many days of footage fit.

**Smaller things.** ONVIF network scan for adding cameras, an ONVIF PTZ pad in live view, manual rclone sync of recordings to any rclone remote, an installable PWA, and a QR code in Settings for opening the app on a phone.

## Install

There are no prebuilt releases yet. Build from source.

### Linux (Debian 12/13, Ubuntu 22.04/24.04)

```bash
sudo apt install ffmpeg
git clone https://github.com/zer0contextlost/argus-nvr.git
cd argus-nvr
(cd frontend && npm ci && npm run build)   # Node.js 20+
sudo ./installer/linux/install.sh
```

The installer puts the app in `/opt/argus/`, creates an `argus` system user, writes `/etc/argus/.env` with a fresh secret key, and enables the `argus` systemd unit. Put a [go2rtc](https://github.com/AlexxIT/go2rtc/releases) binary at `/opt/argus/tools/go2rtc` (or set `GO2RTC_PATH`), then open `http://<host>:8080`.

To upgrade from a tarball built with `installer/linux/build-tarball.sh`:

```bash
sudo argus-update --from dist/argus-1.0.0-linux-x86_64.tar.gz
```

### Windows

Build the installer (see [Development setup](#development-setup)) and run `dist\Argus-Setup-x.x.x.exe`. It installs under Program Files, registers a Windows service, and opens TCP 8080 in the firewall.

### First login

Username `admin`, password `Screenshot123!`. The API refuses everything except the password-change endpoint until you set a new one. To skip the default entirely, set `ARGUS_ADMIN_PASSWORD` before the first start.

### Hardware

Detection runs on the CPU at 5 fps per camera by default (`DETECT_FPS`). A home setup with a handful of cameras runs comfortably in a 4-core, 4 GB LXC container. Face recognition is optional (`pip install -r requirements-face.txt`, about 500 MB of packages).

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
npm run dev   # http://localhost:5173, proxies /api to :8080
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

Notification settings (Telegram tokens, SMTP credentials, etc.) are stored in the database after first configuration via the Settings page, so they don't need to be in `.env`.

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
| Main path / Sub path | RTSP path suffixes. Hikvision defaults: `Streaming/Channels/101` / `102` |
| Record mode | `continuous`, `motion-only`, or `continuous+events` |
| PTZ capable | Enable to show PTZ controls in live view |
| Privacy masks | JSON array of `{x, y, w, h}` rectangles (0–1 normalised) to exclude from detection |
| Arm schedule | JSON array of `{days, start, end}` objects to limit detection hours |

After saving, use **Test** on the camera card to verify RTSP reachability.

### Hikvision substream recommendation

- **Resolution:** 640×360 or 704×480 (low-res is fine for detection)
- **Encoding:** H.264. Browsers can't play H.265 over WebRTC
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

The app reverse-proxies go2rtc, so browsers only talk to port 8080 for HTTP and signalling. go2rtc's API (1984) and RTSP (8554) ports bind to `127.0.0.1`; its WebRTC media port (8555) has to be reachable by clients.

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

Install [Tailscale](https://tailscale.com) on the NVR machine and your phone. Access via the Tailscale IP (`100.x.y.z:8080`). No port forwarding, nothing exposed publicly.

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
- Roles: admin, operator, viewer; user management and audit log are admin-only; destructive operations (purge recordings) require admin
- Audit log: logins, password changes, and user management actions are recorded (action, user, IP, timestamp)
- Webhook notifications signed with HMAC-SHA256 (header `X-Argus-Signature`); receivers should verify it
- Settings API only accepts known keys
- Face embeddings stored as raw float32 bytes and never unpickled
- Don't expose port 8080 to the internet. Use Tailscale, or a reverse proxy with TLS

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
