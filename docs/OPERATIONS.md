# Argus NVR — Operations Guide

Day-to-day operations, maintenance, and advanced configuration.
See `README.md` for initial installation and setup.

---

## Daily operations

### Checking system status
Open the **Health** tab in the UI. It shows:
- App uptime and platform info
- go2rtc process status (PID + restart count)
- **Camera health table** — per-camera recorder status, detector status, last recording time, and events in the last hour
- Per-camera recorder and detector processes (with restart counts)
- Storage usage bar (F:), write rate, projected retention days

Health auto-refreshes every 10 seconds. Click **Refresh** to force an immediate update.

### Viewing live cameras
- **Live** tab → select layout (1/4/6/9 cameras)
- On mobile: layout automatically collapses to single column
- Click a camera tile to expand to full-screen overlay
- **Sub-stream toggle** — each tile has a MAIN/SUB button to switch between the high-quality main stream and the low-bitrate sub-stream; useful on slow connections
- **Snapshot** — 📷 button downloads a JPEG of the current frame
- **Heatmap overlay** — HEAT button overlays the accumulated motion heatmap for that camera
- **PTZ controls** — if the camera has PTZ enabled, PTZ button shows a directional pad; click arrows to pan/tilt; the camera must have ONVIF PTZ configured in its camera settings
- **Camera cycle / kiosk mode** — Settings > Live View > Cycle cameras; rotates through all live cameras full-screen at the configured dwell time

### Reviewing events
- **Events** tab → filter by camera, date/time range
- New events appear live at the top (WebSocket push from the server)
- Thumbnail shows the best frame from the motion event
- Jump to recording: click the recording ID link

### Playback
- **Playback** tab → select camera + date
- 24h timeline bar: blue = recorded segments, colour dots = events
- **Hover over the timeline** to see a thumbnail preview of the footage at that point
- Click timeline or dots to seek; click event markers to jump to that moment
- Speed control: 1× / 2× / 4× / 8×
- **Export clip**: Save Clip button → set start/end times → optional watermark (burns camera name + timestamp into the video) → downloads MP4

### Smart search
- **Search** tab → filter by keyword, camera, event type, time range, detection score, and whether a recording exists
- Results show event thumbnail, camera name, type badge, detection score, and a **View** link that opens playback at the event timestamp
- Use keyword search to find events by object label or camera name

---

## Camera management

### Adding a camera
1. Go to **Cameras > Add Camera**
2. Enter host IP, credentials, and paths (or use auto-discovery)
3. Click **Test** to verify RTSP connectivity before saving
4. Select record mode (`motion-only` recommended for storage savings)

### PTZ configuration
To enable PTZ controls in the live view:
1. **Cameras** → edit the camera → check **PTZ capable**
2. Set **PTZ Config JSON** — minimum required fields:
   ```json
   {
     "onvif_url": "http://192.168.1.10/onvif/device_service",
     "profile_token": "Profile_1"
   }
   ```
3. Save. The PTZ pad will appear in the live view player for that camera.

Preset support: the PTZ API exposes `/api/cameras/{id}/ptz/presets` — presets are fetched from the camera via ONVIF. The current UI uses relative moves only; go-to-preset is available via the API.

### Privacy masks
Exclude regions from motion detection without blacking out the video:
1. **Cameras** → edit the camera → **Privacy masks JSON**
2. Format: array of rectangles (x, y, w, h all in 0–1 normalised coordinates):
   ```json
   [{"x": 0.0, "y": 0.0, "w": 0.3, "h": 0.2}]
   ```
3. The detector ignores motion within these regions.

### Arm schedule
Limit detection to specific hours (e.g., night-only):
1. **Cameras** → edit the camera → **Arm schedule JSON**
2. Format: array of time windows:
   ```json
   [{"days": [0,1,2,3,4,5,6], "start": "22:00", "end": "07:00"}]
   ```
   `days` uses 0=Monday … 6=Sunday. The camera records continuously but only fires events during armed windows.

### Hikvision substream configuration
For optimal motion detection:
1. Open the camera web interface (http://\<camera-ip\>)
2. Configuration > Video > Encoding Parameters > Sub Stream
3. Set: H.264 codec, 640x360 or 704x480, 5-10 fps, ~512 kbps bitrate
4. The main stream can remain at full quality (4MP/8MP, higher bitrate)

### Record modes explained

**`motion-only` (recommended default)**
- Recording starts when motion is detected, stops after the post-motion buffer
- Saves 70–90% of storage vs continuous for typical scenes
- Pre-motion buffer: keeps N seconds of footage before motion started (circular buffer)
- Post-motion buffer: continues recording N seconds after motion ends
- Configure: `MOTION_PRE_BUFFER_SECONDS`, `MOTION_POST_BUFFER_SECONDS` in `.env`

**`continuous`**
- FFmpeg records 24/7 in rolling segments
- Best for high-security areas where you need every frame
- Use the retention settings to manage storage

**`continuous+events`**
- 24/7 recording + separate event clips indexed in the database
- Highest storage use; events link to exact timestamps in the continuous footage

### Editing camera credentials
Cameras > click camera name > Edit. The RTSP password is re-encrypted on save.
Old encrypted value is replaced; the original plaintext is never stored.

### Removing a camera
Cameras > click camera > Delete. This stops the recorder/detector for that camera,
removes it from go2rtc's config, and soft-deletes the DB record.
Existing recordings on disk are NOT automatically deleted (purge manually if needed).

---

## User management

### Roles
| Role | Permissions |
|---|---|
| `admin` | Full access: manage users, view audit log, purge recordings, change all settings |
| `operator` | Live view, playback, events, search, clip export; cannot manage users or purge |
| `viewer` | Live view and playback only; read-only access |

### Creating users
**Users** tab (admin only) → **+ Add User**:
- Username and password (min 8 characters)
- Role: `admin`, `operator`, or `viewer`
- New users are created with `Must Change Password = Yes` — they must set a new password on first login

### Resetting a password
**Users** tab → click **Reset Pwd** next to the user → enter a new password (min 8 characters) → **Set Password**.

### Changing a user's role
**Users** tab → change the role dropdown → **Save**.

### Deleting a user
**Users** tab → **Delete** → confirm. The user's sessions are immediately invalidated.

---

## Audit log

The **Audit** tab (admin only) shows a full log of:
- Logins and password changes
- User create / update / delete / password reset
- Camera create / update / delete
- Purge operations

Each entry records: timestamp, username, action, target type + ID, detail, and client IP.

### Filtering
Use the action and username filter fields to narrow results. The log is paginated (100 entries per page).

### Purging
**Purge All** clears the entire audit log. The purge itself is not re-logged (it would be self-defeating).

### Retention
The audit log is stored indefinitely — it does not participate in the recording retention policy. Purge manually if the table grows too large.

---

## Storage management

### Monitoring
Health tab > Storage section shows:
- F: drive: total / used / free with percentage bar
- Recordings indexed in DB (bytes + segment count)
- 24h rolling write rate (GB/day)
- Projected days until the free-space floor is hit

### Per-camera retention
Each camera can override the global retention limit:
1. **Cameras** → edit camera → **Retention (days)** field
2. Leave blank to use the global `RETENTION_MAX_AGE_DAYS` setting

### Adjusting global retention
Edit `.env` and restart the app:
```
RETENTION_MAX_AGE_DAYS=14          # reduce to reclaim space faster
RETENTION_FREE_SPACE_FLOOR_PCT=15  # raise to keep more headroom
```

The storage manager runs every few minutes in the background. After changing
retention settings, it will prune on the next cycle automatically.

### Emergency space recovery
If F: fills up unexpectedly before the retention manager acts:
```powershell
# Check what's using space under recordings:
Get-ChildItem -Path "F:\sentinel\recordings" -Recurse |
    Sort-Object Length -Descending |
    Select-Object -First 20 FullName, Length

# Manually delete recordings for a specific date:
Remove-Item -Recurse -Force "F:\sentinel\recordings\*\2026-04-01"
```
After manual deletion, the DB will have orphaned recording rows. They will
be cleaned up by the storage manager on its next cycle (it reconciles DB vs disk).

### Estimating required F: capacity
Formula: `cameras × bitrate_Mbps × 10.8 GB/day × retention_days`

Example: 5 cameras at 3 Mbps average, 14 days retention:
`5 × 3 × 10.8 × 14 = 2,268 GB ≈ 2.2 TB`

With motion-only recording (typical 20-30% duty cycle):
`2,268 GB × 0.25 = ~570 GB` — a significant saving.

---

## Cloud backup

Argus can sync the recordings directory to any [rclone](https://rclone.org)-supported remote (S3, Backblaze B2, Google Drive, Dropbox, etc.).

### Setup
1. Install rclone on the NVR machine and configure a remote: `rclone config`
2. **Settings > Cloud Backup** → enable, set the remote name (e.g. `b2:my-nvr-bucket/recordings`)
3. Optionally set a cron-style schedule (e.g. `0 3 * * *` for 3am daily)
4. Click **Trigger Now** to run a manual sync and verify

### Monitoring
Settings > Cloud Backup > **History** shows the last N sync runs with status, bytes transferred, and duration.

### Notes
- rclone must be on the system PATH or its path set in `.env`
- The sync is additive (rclone `copy`, not `sync` — remote files are never deleted automatically)
- Only the recordings directory is backed up; the database is backed up separately via Settings > Backup

---

## Notifications tuning

### Cooldown
Prevents alert fatigue. Set the minimum seconds between notifications per camera:
```
Settings > Notifications > Cooldown seconds (default 60)
```

### Time-of-day filtering
Restrict notifications to active hours:
```
Settings > Notifications > Active hours (e.g. 22:00–07:00 for night-only)
```

### Testing a channel
Settings > Notifications > click **Test** next to a channel. A test message with
a placeholder thumbnail is sent immediately.

### Web Push (browser notifications)
1. **Settings > Web Push** → must be enabled first
2. Open the app in the browser you want to receive notifications on → browser will prompt for permission
3. Subscribe — the subscription is stored server-side; push is sent on every motion event
4. Requires HTTPS (or localhost for development); will not work over plain HTTP on a remote host

### Two-way audio
Settings > Two-way audio → enable. When enabled, the live view player requests microphone access from the browser and the audio stream is sent to the camera (browser MediaDevices API). Camera must support RTSP two-way audio. Disable if not needed — it causes unnecessary microphone permission prompts.

---

## ONVIF output

Settings > ONVIF Output → enable + set port (default 8090).

When enabled, Argus exposes its cameras as ONVIF-compatible sources. Third-party NVR software or cameras can discover and pull streams from Argus using the standard ONVIF protocol.

Note: ONVIF output is a pass-through to go2rtc's ONVIF server. Ensure the configured port is reachable on your LAN if needed.

---

## Log management

Logs rotate automatically when running as a service (NSSM rotates at 10 MB).

Manual log inspection:
```powershell
# Tail the main app log:
Get-Content "F:\sentinel\logs\sentinel.log" -Tail 50 -Wait

# Filter for errors:
Select-String -Path "F:\sentinel\logs\sentinel.log" -Pattern "ERROR|WARN"

# go2rtc log:
Get-Content "F:\sentinel\logs\go2rtc.log" -Tail 50
```

---

## Service management

The Windows service name is `ArgusNVR` (managed by NSSM).

```powershell
# Status
Get-Service ArgusNVR

# Start / stop / restart (requires elevated PowerShell)
Start-Service ArgusNVR
Stop-Service ArgusNVR
Restart-Service ArgusNVR

# View in NSSM GUI
& "F:\sentinel\tools\nssm.exe" edit ArgusNVR

# Check auto-start setting
Get-Service ArgusNVR | Select-Object Name, Status, StartType
```

If the service fails to start, check:
1. `F:\sentinel\logs\argus-stderr.log` for Python tracebacks
2. That `.env` exists with a valid `ARGUS_SECRET_KEY`
3. That the venv Python is at `F:\sentinel\app\.venv\Scripts\python.exe`

---

## Database operations

The SQLite database is at `F:\sentinel\data\sentinel.db`.

```powershell
$python = "F:\sentinel\app\.venv\Scripts\python.exe"

# Inspect tables
& $python -c "
import sqlite3
db = sqlite3.connect(r'F:\sentinel\data\sentinel.db')
for row in db.execute('SELECT name FROM sqlite_master WHERE type=''table'' ORDER BY name'):
    count = db.execute(f'SELECT COUNT(*) FROM {row[0]}').fetchone()[0]
    print(f'{row[0]:30s} {count:>8} rows')
db.close()
"

# Export settings
& $python -c "
import sqlite3, json
db = sqlite3.connect(r'F:\sentinel\data\sentinel.db')
settings = dict(db.execute('SELECT key, value FROM settings').fetchall())
print(json.dumps(settings, indent=2))
db.close()
"
```

### Manual backup
```powershell
# Trigger via API (WAL-safe, runs in background):
$r = Invoke-WebRequest -Uri "http://localhost:8080/api/auth/login" -Method POST -ContentType "application/json" -Body '{"username":"admin","password":"<password>"}' -UseBasicParsing
$tok = ($r.Content | ConvertFrom-Json).access_token
Invoke-WebRequest -Uri "http://localhost:8080/api/system/backup" -Method POST -Headers @{Authorization="Bearer $tok"} -UseBasicParsing

# Or via Python (WAL-safe copy):
$python = "F:\sentinel\app\.venv\Scripts\python.exe"
& $python -c "
import sqlite3
src = sqlite3.connect(r'F:\sentinel\data\sentinel.db')
dst = sqlite3.connect(r'F:\sentinel\data\sentinel.db.bak')
src.backup(dst)
dst.close(); src.close()
print('Backup complete.')
"
```

---

## Updating Argus NVR

1. Stop the service (elevated PowerShell): `Stop-Service ArgusNVR`
2. Pull the new code (git, or copy files)
3. Rebuild the frontend: `cd F:\sentinel\app\frontend; npm ci; npm run build`
4. Start the service: `Start-Service ArgusNVR`

Database migrations run automatically on startup. No manual migration steps are needed.

---

## Tailscale remote access — advanced

### Sharing with a family member
1. Tailscale admin console → invite the user or share the device.
2. On Argus: **Users** tab → create a viewer-role account for them.
3. Give them the Tailscale IP + port and their credentials.
4. Scan the **Settings > QR Code** with their phone for quick URL entry.

### MagicDNS
Enable MagicDNS in the Tailscale admin console. Then access Argus at
`http://argus-nvr:8080` instead of a numeric IP.

### Exit node (if you need to route all traffic through home)
Not typically needed for Argus. Standard device sharing is sufficient.

---

## Security hardening checklist

- [ ] Change the bootstrap admin password immediately after first login
- [ ] Set `ARGUS_SECRET_KEY` to a strong random value (the installer does this automatically)
- [ ] Create operator/viewer accounts for non-admin users — do not share the admin password
- [ ] Review the **Audit** log periodically for unexpected access
- [ ] Use Tailscale for remote access — never port-forward 8080 to the internet
- [ ] Create a dedicated camera user on the Hikvision DVR/camera (not admin)
- [ ] Enable Windows Firewall; block inbound 8080 from the internet on your router
- [ ] Enable Tailscale ACLs if sharing with other users
- [ ] Web Push requires HTTPS — if enabling for remote use, put Argus behind a reverse proxy with TLS

---

## Performance tuning

### CPU usage is high
- Check if AI detection is enabled (`DETECT_ENABLED=true` in `.env`). Disable it
  if you don't need object detection — frame-diff motion is much cheaper.
- Reduce `DETECT_FPS` (default 2) to lower detection CPU cost.
- Switch cameras from `continuous` to `motion-only` recording.
- Use the **sub-stream toggle** in live view to reduce browser decode load.

### Live view latency is high
- WebRTC normally delivers sub-second latency. High latency usually means:
  - UDP is blocked (try the MSE fallback in go2rtc)
  - The main stream bitrate is too high for your LAN/Tailscale bandwidth
  - Switch to sub-stream in the live view player

### Recordings are missing gaps
The recorder logs gaps in the `recordings` table (`has_gap=true`). Common causes:
- Camera disconnected (check camera power/network)
- FFmpeg crashed — watchdog should restart within seconds; check logs for the crash reason
- go2rtc restarted — recorders restart automatically after go2rtc comes back up

---

## go2rtc advanced configuration

go2rtc config is rendered from the camera database at
`F:\sentinel\app\config\go2rtc.yaml`. Do not edit this file manually — it is
overwritten on startup or when cameras are added/edited.

Each camera gets two go2rtc stream entries: `<slug>` (main stream) and `<slug>_sub` (sub-stream). Both are available for WebRTC, MSE, and HLS playback.

---

## Hikvision LAN discovery notes

The scanner sends:
1. **ONVIF WS-Discovery** — multicast to 239.255.255.250:3702 (standard ONVIF)
2. **Hikvision SADP** — UDP broadcast on port 37020 (Hikvision proprietary)
3. **Subnet port probe** — scans the local /24 subnet for open ports 554/80/8000

For best results:
- Run the scan from the same LAN as the cameras (not over Tailscale)
- Ensure Windows Firewall allows inbound UDP on 3702 and 37020 briefly during scan
- If cameras don't appear, add them manually using the known IP address
