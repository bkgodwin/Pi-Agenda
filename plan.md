# Pi-Agenda — Full Implementation Plan

## 1. Product Definition

**App name:** Pi-Agenda

Pi-Agenda is a self-hosted classroom digital-signage system for a Raspberry Pi Zero 2W or Raspberry Pi 3 Model B connected to an HDMI display. It continuously rotates class agendas, presentations, photos, websites, Microsoft 365 PowerPoint presentations, videos, and announcements through named playlists and layered teacher-defined schedules. A mobile-friendly management interface is available on the local network.

This plan covers the complete desired product. It is not an MVP plan, and the database, APIs, services, installer, player, and administration interface should be designed for all content types from the beginning.

### 1.1 Primary workflow

1. The teacher runs one command, `sudo ./start.sh`, on a prepared Raspberry Pi OS installation.
2. The script installs all system and Python dependencies, creates the application account and directories, asks the teacher to set the management password, configures boot services, starts the application, and prints the management URL.
3. On every Pi boot, the display shows the management hostname, current IP address, and port for approximately five seconds, then enters the fullscreen signage player without requiring an interactive sign-in.
4. The teacher uses a phone or computer on the LAN to add, schedule, preview, refresh, reorder, or remove content.
5. The Pi keeps displaying the last verified content and observing schedules when internet access is unavailable.

### 1.2 Success criteria

- All seven content types—PowerPoint file, PDF deck, Microsoft 365 PowerPoint link, image, website, video, and text announcement—can participate in one or more named playlists.
- Non-default playlists have independent schedules. An item is eligible only when both its own schedule and at least one selected playlist schedule are active.
- Exactly one unscheduled default playlist supplies fallback content whenever the global display schedule is on and no scheduled playlist is active.
- PowerPoint for the web edits appear through the live embed automatically. Downloadable cloud presentations can also be refreshed, converted, and retained for offline playback.
- Local presentation uploads are replaced through the management interface when their source changes; they are not described as automatically synchronized.
- A network or source failure never deletes the last verified playable generation.
- The kiosk starts after power-on without a desktop login prompt.
- Installation and later repair use the same idempotent `start.sh` entry point.
- The product remains usable within the Pi Zero 2W's 512 MB RAM constraint and takes advantage of the Pi 3 Model B's additional memory without requiring a separate build.

---

## 2. Complete Feature Scope

### 2.1 Content types

| Type | Source | Live rendering | Offline rendering | Timing |
|---|---|---|---|---|
| `ppt_file` | Uploaded `.pptx` | None | LibreOffice to PDF to verified PNG sequence | `slide_sec` per slide |
| `pdf_deck` | Uploaded `.pdf` | None | Verified PNG sequence | `slide_sec` per page |
| `ppt_link` | Supported OneDrive/SharePoint embed or share link | Sandboxed Microsoft 365 iframe | Download-and-convert generation when permitted; screenshot otherwise | Per-slide when converted; item duration for opaque embed |
| `image` | Uploaded JPG, PNG, WebP, or GIF | Local optimized image | Same local image | `duration_sec` |
| `url` | HTTP(S) page | Sandboxed iframe when the source permits embedding | Separate-origin static archive when safe and usable; screenshot fallback | `duration_sec` |
| `video` | Uploaded MP4 or supported input converted to H.264/AAC MP4 | Local HTML video | Same local video | Full length or configured duration |
| `announcement` | Text entered in the management interface | Native player text slide | Same local text slide | `duration_sec` |

All types support a name, enabled state, per-playlist sort order, days of week, optional time window, preview, and health information. Media types support volume and expanded fit modes; live web content uses a separate zoom control. Announcements expose background, text color, size, and alignment instead of irrelevant media controls.

### 2.2 Management interface

The management site is server-rendered Jinja, vanilla JavaScript, and lightweight CSS with no frontend build step. It must be practical on phones and older school laptops.

It provides:

- Drag-and-drop uploads with progress and conversion-job status.
- Microsoft 365 link and general website entry with validation and preview.
- Named playlist creation, editing, scheduling, default selection, enable/disable, per-playlist drag reorder, item membership, duplicate, preview, and delete.
- Per-item days, time window, duration or slide timing, volume, fit mode, rendering mode, and fallback status.
- Weekly global screen schedule with always-on, timed-window, and off-all-day modes.
- Manual screen override with a visible expiration time.
- Screen-blanking test controls plus timed holiday mode and early cancellation.
- Authenticated one-click update from the latest GitHub `main` branch, including an already-current result, repair installation, and reboot.
- Resolution selection: 720p default and 1080p optional.
- Refresh Now, Refresh All, and conversion progress.
- CPU load, memory, temperature, throttling state, disk space, IP address, uptime, worker state, and current display state.
- Logs, consistent backup download, restore workflow, password change, logout, restart services, and reboot.
- Clear limitations for Microsoft 365 controls, iframe restrictions, converted presentation fidelity, and audio.

---

## 3. Target Hardware and Operating System

### 3.1 Hardware

- Raspberry Pi Zero 2W, 512 MB RAM, or Raspberry Pi 3 Model B, 1 GB RAM.
- Reliable microSD card sized for the configured content quota; 32 GB or greater recommended.
- Quality Raspberry Pi-compatible power supply.
- HDMI television or monitor.
- External RTC module strongly recommended if schedules must remain correct after a cold boot without network access. Neither supported board has a battery-backed onboard RTC.
- Optional USB Ethernet adapter when school Wi-Fi is unreliable.

### 3.2 Operating system

- Primary target: current Raspberry Pi OS Lite 32-bit.
- The installer detects the Debian/Raspberry Pi OS release and explicitly supports the tested Trixie and Bookworm package layouts.
- X11 plus Openbox is installed as a minimal kiosk session; the full desktop image is not required.
- Python uses a virtual environment. Docker and heavyweight frontend or ORM stacks are excluded.
- The exact validated OS image, Chromium version, and package versions are recorded in the release documentation after the hardware-validation phase.

### 3.3 Resource policy

- One Chromium kiosk window and one active display item.
- At most one item preloaded.
- One conversion/refresh worker and one resource-heavy subprocess at a time.
- zram enabled. Disk swap is optional and conservative to limit SD-card wear.
- Conversions run with lower CPU and I/O priority and explicit memory, duration, and output-size limits.
- 720p is the supported default. 1080p remains selectable but is marked experimental until it passes the target-device soak tests.
- Chromium flags are chosen from measurements on the target board. Flags that disable memory-pressure behavior or GPU compositing are not enabled without evidence that they improve stability.

---

## 4. Security and Privacy Model

This system can contain student names, classroom schedules, and class photos. Being on a LAN is not treated as an adequate security boundary.

### 4.1 Endpoint access

- `/admin`, administration pages, previews, backups, logs, and all mutating APIs require an authenticated session.
- `/player`, `/startup`, `/api/playlist-now`, and full-resolution media routes accept loopback requests only by default because the kiosk browser runs on the Pi.
- Authenticated management previews use dedicated authenticated preview routes or short-lived signed media URLs.
- `/api/health/player` exposes only the minimum information required by the local player.
- A setting may deliberately allow remote player access, but it is off by default and requires a high-entropy device token. The token is submitted through `/player/connect` in a POST body and stored in a dedicated `HttpOnly` device cookie, never placed in a logged URL.

### 4.2 Authentication

- `start.sh` sets the first management password before the web service is exposed.
- The password is entered twice through a hidden terminal prompt, must be at least eight characters, and is stored only as a bcrypt hash.
- There is no remotely claimable first-run setup wizard.
- If installation is non-interactive, the installer generates a one-time random password, prints it once, and requires it to be changed at first login.
- Password recovery is performed over SSH with `sudo ./start.sh --reset-password`.
- Flask's secret key is randomly generated at install time, stored outside the repository, readable only by the service account, and preserved by idempotent reinstalls.
- Session cookies are `HttpOnly` and `SameSite=Lax`, sessions have a finite lifetime, and login rotates session state.
- All modifying forms and JSON requests require CSRF validation.
- Login attempts are rate-limited and security-relevant events are logged without storing passwords or session values.
- HTTP is the simple LAN default. Documentation warns that an untrusted shared LAN can observe HTTP traffic and recommends an isolated VLAN or a separately configured HTTPS reverse proxy.

### 4.3 Untrusted files and URLs

- Uploaded filenames are never used as storage paths. Content is identified by database ID and generation ID.
- File signatures, extensions, MIME type, size, generated page count, pixel dimensions, and decoded output are validated.
- LibreOffice, Poppler, Chromium capture, `wget`, and FFmpeg run as the unprivileged service account with timeouts and resource limits.
- URL fetching permits HTTP and HTTPS only, limits redirects, validates every redirect target, limits response and archive size, and rejects credentials embedded in URLs.
- Loopback, link-local, metadata, and private-network targets are blocked by default. Required school intranet hosts can be added to an explicit allowlist.
- Remote or archived HTML never runs under the management origin.

### 4.4 Archived-content isolation

- The main application listens on port `8000`.
- Archived website content is served by a cookie-free static service bound to `127.0.0.1:8002`, creating a separate browser origin.
- Archived content is rendered inside a restrictive sandboxed iframe with top navigation, popups, downloads, forms, and access to the Pi-Agenda origin disabled.
- The screenshot fallback is preferred whenever an archive is incomplete, script-heavy, login-dependent, or cannot be validated safely.

### 4.5 Privileged operations

- Flask and workers never run as root.
- Display power and reboot actions call narrowly scoped root-owned helpers or systemd units through explicit sudoers rules.
- No route accepts an arbitrary shell command, service name, path, or command argument.

---

## 5. Architecture

```text
[Teacher phone/laptop] -- LAN --> [Supported Raspberry Pi :8000]
                                      Waitress + Flask
                                      Admin UI and authenticated APIs
                                      SQLite
                                            |
                  +-------------------------+--------------------------+
                  |                         |                          |
          lightweight worker        local cache server         X11/Openbox/Chromium
          refresh/conversion         127.0.0.1:8002             127.0.0.1:8000/startup
          schedule/power                     |                          |
                  |                          +---- sandboxed archive ---+
                  +---- immutable media generations ----> HDMI display
```

### 5.1 Processes and systemd units

- `pi-agenda-web.service`: Flask application served by Waitress as the dedicated `pi-agenda` user.
- `pi-agenda-worker.service`: durable job runner, periodic scheduling, health checks, and display-state decisions.
- `pi-agenda-cache.service`: minimal static server for isolated archived website content, loopback only.
- `pi-agenda-kiosk.service`: X11/Openbox/Chromium kiosk on the physical display, without an interactive desktop login.
- The kiosk is enabled by `multi-user.target`, owns `tty1`, and suppresses the graphical login manager during boot. Escape schedules a clean kiosk stop and starts the detected desktop manager, or the Lite console when no desktop is installed. Reboot returns to kiosk mode.
- Each service has restart limits, health logging, least-privilege ownership, and explicit ordering.

The worker is separate from Waitress so web-server reloads, threads, or future worker-count changes cannot duplicate scheduled jobs. The worker remains lightweight and imports conversion-specific libraries only while a job needs them.

### 5.2 Filesystem layout

```text
/opt/pi-agenda/                         # installed application, root-owned
/var/lib/pi-agenda/
  db.sqlite
  secrets/flask-secret
  uploads/<item-id>/<original-file>
  generations/<item-id>/<generation-id>/
  thumbs/<item-id>/<generation-id>.png
  archives/<item-id>/<generation-id>/
  staging/<job-id>/
  backups/
/var/log/pi-agenda/                     # or journald-only deployment
```

- Application code is not writable by the service account.
- Mutable data never lives inside the Git checkout or application directory.
- Temporary and staging paths are on the same filesystem as their final generation so publication can be atomic.

---

## 6. Database and Migrations

SQLite is used through Python's `sqlite3` module with one connection per request or worker thread, foreign keys enabled, `busy_timeout`, short transactions, and a tested journal/synchronous policy. A migration runner and `schema_version` table exist from the first build.

### 6.1 Core schema

```sql
CREATE TABLE schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE media_items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK(type IN
    ('ppt_file','pdf_deck','ppt_link','image','url','video','announcement')),
  source TEXT NOT NULL,
  embed_url TEXT,
  render_mode TEXT NOT NULL DEFAULT 'auto'
    CHECK(render_mode IN ('auto','live','converted','archive','screenshot')),
  duration_sec INTEGER NOT NULL DEFAULT 20 CHECK(duration_sec >= 0),
  slide_sec INTEGER NOT NULL DEFAULT 10 CHECK(slide_sec > 0),
  volume INTEGER NOT NULL DEFAULT 80 CHECK(volume BETWEEN 0 AND 100),
  fit_mode TEXT NOT NULL DEFAULT 'contain'
    CHECK(fit_mode IN ('contain','cover','stretch','width','height','native')),
  web_zoom INTEGER NOT NULL DEFAULT 100 CHECK(web_zoom BETWEEN 50 AND 200),
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK(days_mask BETWEEN 1 AND 127),
  start_time TEXT,
  end_time TEXT,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  sort_order INTEGER NOT NULL DEFAULT 0,
  active_generation_id INTEGER,
  last_checked TEXT,
  last_good_at TEXT,
  last_status TEXT NOT NULL DEFAULT 'never'
    CHECK(last_status IN ('never','queued','refreshing','ok','stale','error')),
  last_error TEXT,
  background_color TEXT NOT NULL DEFAULT '#12372a',
  text_color TEXT NOT NULL DEFAULT '#ffffff',
  text_size INTEGER NOT NULL DEFAULT 64,
  text_align TEXT NOT NULL DEFAULT 'center',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(active_generation_id) REFERENCES media_generations(id)
);

CREATE TABLE media_generations (
  id INTEGER PRIMARY KEY,
  media_item_id INTEGER NOT NULL,
  generation_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  slide_count INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  content_hash TEXT NOT NULL,
  source_etag TEXT,
  source_last_modified TEXT,
  created_at TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  retire_after TEXT,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
);

CREATE TABLE display_schedule (
  weekday INTEGER PRIMARY KEY CHECK(weekday BETWEEN 0 AND 6),
  mode TEXT NOT NULL DEFAULT 'off'
    CHECK(mode IN ('off','always_on','window')),
  on_time TEXT,
  off_time TEXT
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  media_item_id INTEGER,
  state TEXT NOT NULL CHECK(state IN
    ('queued','running','succeeded','failed','cancelled')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  attempt INTEGER NOT NULL DEFAULT 0,
  not_before TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(media_item_id) REFERENCES media_items(id) ON DELETE CASCADE
);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE manual_display_override (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  state TEXT NOT NULL CHECK(state IN ('on','off')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE playlists (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL COLLATE NOCASE UNIQUE,
  is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
  enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
  days_mask INTEGER NOT NULL DEFAULT 127 CHECK(days_mask BETWEEN 1 AND 127),
  start_time TEXT,
  end_time TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE playlist_items (
  playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
  media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(playlist_id, media_item_id)
);
```

The actual migration must create `media_generations` before adding its foreign key from `media_items`, or create the tables in a transaction using a SQLite-compatible ordering. The SQL above is the target model, not a literal migration file.

### 6.2 Important settings

- `password_hash`
- `resolution`: `720p` or `1080p`
- `timezone`: IANA timezone such as `America/Chicago`
- `check_time`: local `HH:MM`
- `site_name`
- `default_duration_sec`, `default_slide_sec`, `default_volume`
- `content_quota_bytes`, `minimum_free_bytes`
- `intranet_allowlist`
- `remote_player_enabled`
- `playlist_version`
- `blank_test_enabled`
- `holiday_until`
- `boot_splash_seconds`, default `5`

---

## 7. Scheduling Semantics

All internal timestamps are stored as UTC ISO-8601 values. Schedule rules are interpreted in the configured IANA timezone.

Playlist selection is evaluated before item selection. All active non-default playlists participate at the same time; items shared by multiple active playlists are deduplicated. The default playlist participates only when no enabled non-default playlist schedule is active. An item then participates only when its own day/time rule is also active. The global display schedule, blanking test, and holiday mode can force the entire display off.

### 7.1 Per-item schedule

- `days_mask` uses bit 0 for Monday through bit 6 for Sunday.
- Both `start_time` and `end_time` must be set, or both must be null.
- Both null means all day on each selected weekday.
- Windows are half-open: `[start_time, end_time)`.
- `start_time < end_time` is a same-day window.
- `start_time > end_time` crosses midnight. The weekday refers to the day on which the window starts.
- Equal start and end times are rejected as ambiguous.
- DST transitions are handled by the timezone library and covered by tests.

At local time `T`:

```text
eligible = enabled
           AND T is within one of the item's selected-day windows
order by sort_order, id
```

### 7.2 Global display schedule

Each weekday has one explicit mode:

- `off`: screen remains off all day.
- `always_on`: screen remains on all day.
- `window`: use `on_time` and `off_time`, including an overnight window.

A manual override records `on` or `off` and an expiration. By default it expires at the next computed schedule boundary; the administrator can choose a shorter fixed duration.

### 7.3 Clock validity

- The worker waits for system-time synchronization during normal online boot.
- If an RTC is present, its time is checked and used by the OS normally.
- If the Pi cold-boots offline without a trustworthy RTC or plausible persisted time, the player displays cached content but marks schedule time as uncertain rather than claiming precise schedule enforcement.
- Large backward clock jumps are logged and trigger a schedule recalculation.

---

## 8. Durable Jobs, Refresh, and Atomic Publication

### 8.1 Job behavior

- Upload conversion, remote refresh, re-render, screenshot, archive, and video transcode operations are durable rows in `jobs`.
- Only one resource-heavy job runs at a time.
- A process restart returns abandoned `running` jobs to `queued` after cleanup of their staging directory.
- Duplicate manual refresh clicks coalesce into the existing queued/running job.
- Jobs expose progress, current stage, attempt, start time, and concise error text.
- Scheduled work uses `max_instances=1`, coalescing, and documented misfire grace behavior.

### 8.2 Atomic generation publication

1. Create `/var/lib/pi-agenda/staging/<job-id>/`.
2. Download, convert, archive, or optimize into staging.
3. Validate every required output: file count, nonzero size, decodability, dimensions, allowed type, and configured limits.
4. Compute a content hash and move staging to the immutable generation path.
5. In one database transaction, insert the generation, set it active, update item health, and increment `playlist_version`.
6. Keep the previous generation until at least the maximum active playlist cycle has elapsed, then remove it through a cleanup job.
7. On failure, delete only staging data and retain the active generation.

Only verified immutable generation URLs appear in the player playlist, so a refresh cannot invalidate an item already being displayed.

### 8.3 Refresh cadence

- Daily refresh at `settings.check_time` for `ppt_link` and `url` items.
- Failed refresh retries after 5 minutes with bounded exponential backoff after repeated failures.
- Manual Refresh Now and Refresh All enqueue work immediately without bypassing serialization or validation.
- Uploaded local content is refreshed only by replacement upload or resolution re-render.

---

## 9. Content Pipelines

### 9.1 PowerPoint upload

1. Validate and retain the original `.pptx`.
2. Run LibreOffice headlessly with a unique temporary user profile and a hard timeout.
3. Verify the produced PDF with Poppler.
4. Render pages using `pdftoppm` at the configured resolution target.
5. Decode every PNG, normalize names such as `slide-0001.png`, create a thumbnail, and publish the generation.

LibreOffice conversion is not a fidelity guarantee. The management UI presents the thumbnail review and recommends PDF upload when exact PowerPoint rendering matters.

### 9.2 PDF upload

- Validate page count, dimensions, and size limits.
- Render directly with Poppler, verify every output, create a thumbnail, and publish.
- PDF exported from PowerPoint is the preferred pixel-faithful deck path.

### 9.3 Microsoft 365 PowerPoint link

- Accept an actual PowerPoint for the web embed URL or a supported OneDrive/SharePoint share URL.
- Normalize only recognized Microsoft URL families; do not manufacture an embed URL from an unknown link pattern.
- Validate live embed and download capabilities separately during item creation.
- Supported modes:
  - `live`: Microsoft iframe; edits appear according to Microsoft's live embed behavior.
  - `converted`: download and process as a PowerPoint generation when the link permits it.
  - `screenshot`: capture a verified still fallback.
  - `auto`: prefer live while online, then converted generation, then screenshot.
- Because the Microsoft iframe is cross-origin, Pi-Agenda does not promise slide control, slide counting, or volume control inside it.
- For an opaque live embed, the teacher supplies item duration. For converted playback, Pi-Agenda controls each slide using `slide_sec`.

### 9.4 Images

- Apply EXIF orientation.
- Validate dimensions and decompression limits.
- Downscale to the configured maximum while preserving aspect ratio.
- Preserve supported GIF animation when within limits; otherwise convert to a safe static or animated WebP representation and disclose the result.
- Apply `contain`, `cover`, or `stretch` in the player.

### 9.5 Websites

- Attempt live display in a restrictive iframe.
- Record that many sites reject framing through browser security headers; preview verifies behavior before enabling an item.
- Daily refresh may create:
  - a bounded `wget` archive with rewritten links and validation, served only from the isolated cache origin; and
  - a Chromium screenshot.
- The player fallback order in `auto` mode is live, usable archive, screenshot, last verified screenshot.
- Login-required and highly dynamic sites are documented as live-or-screenshot sources rather than promised full offline archives.

### 9.6 Video

- Probe uploads with `ffprobe`.
- Copy already-compatible H.264/AAC MP4 where safe; otherwise transcode using FFmpeg.
- Produce a resolution-appropriate, even-dimension MP4 with browser-compatible pixel format and fast-start metadata.
- Apply per-item volume to the HTML video element.
- `duration_sec = 0` means play once to completion. A positive duration caps the item; shorter videos loop until the configured duration.
- Force HDMI audio through the tested OS audio configuration. Converted slide decks remain silent.

---

## 10. Player Behavior

### 10.1 Startup screen

The kiosk service launches Chromium at `http://127.0.0.1:8000/startup` after the backend readiness check succeeds.

The startup page:

- Shows the Pi-Agenda name.
- Shows `http://pi-agenda.local:8000/admin`.
- Shows every usable non-loopback IPv4 address as `http://<ip>:8000/admin`.
- Clearly labels port `8000` as the management interface.
- Optionally renders a QR code without requiring an external network service.
- Waits for an address for a short bounded period when networking is still initializing.
- Remains visible for at least `boot_splash_seconds`, default five seconds, after address discovery; if no address is available it shows the mDNS address and `Network address pending`.
- Redirects to `/player` automatically.

This page is opened only by the kiosk boot command. Restarting the web service does not unnecessarily replay it, while restarting the kiosk service does.

### 10.2 Rotation

- Fetch `/api/playlist-now` on load and every 15 seconds using an ETag or `playlist_version`.
- Reconcile a changed playlist at the next item boundary, except an immediate global power-off.
- Reuse one primary DOM node and preload at most one next item.
- Decks advance through immutable slide URLs at `slide_sec`.
- Images and websites use `duration_sec`.
- Videos follow the duration behavior in section 9.6.
- If no item is eligible, show a black standby page with clock, next scheduled display time, and discreet status.
- If the backend misses three polls, continue the last in-memory playlist and show a degraded status rather than going blank.
- A lightweight external kiosk heartbeat allows the worker or systemd watchdog to restart Chromium when JavaScript or the renderer becomes unresponsive.

### 10.3 Connectivity indicator

The player distinguishes:

- **Green:** current item is live or locally verified and all required services are healthy.
- **Amber:** current item is cached/stale, a remote refresh failed, time is uncertain, or WAN is unavailable.
- **Red:** current item has no playable generation or a local service is unhealthy.

Status is item-specific. Internet availability is not inferred from a single Microsoft probe. The worker performs configurable small connectivity probes and records source-specific refresh outcomes. The indicator shows the current item's last-good time where relevant.

---

## 11. Display Resolution and Power

### 11.1 Resolution

- `720p`: 1280×720 player target and lower slide/image render cap.
- `1080p`: 1920×1080 target and higher render cap.
- Changing resolution enqueues new generations for resolution-dependent local media. Existing verified generations remain active until replacements succeed.
- Display mode is set through the detected X11/KMS output when supported; Chromium window size alone is not treated as changing the HDMI mode.

### 11.2 Power saving

- Global schedule transitions first tell the player to fade to black and acknowledge readiness.
- A privileged helper then uses the capability proven on the installed OS: detected `xrandr` output, DPMS, or the tested Raspberry Pi display-power interface.
- Output names such as `HDMI-1` or `HDMI-A-1` are discovered at install/startup and never hard-coded.
- X11 operations receive the correct `DISPLAY` and `XAUTHORITY` for the kiosk session.
- On wake, the output is restored, Chromium is brought forward or restarted if needed, and the playlist is reloaded.
- Power state is checked at schedule boundaries and at least every 30 seconds for recovery.
- HDMI signal-off is documented as display standby, not zero wall power. CEC and smart-plug control remain optional future integrations, not hidden assumptions.

---

## 12. Administration Pages

1. **Dashboard** — now playing, up next, display state, network/source health, current IPs, worker state, CPU, RAM, temperature, throttling, disk, recent failures.
2. **Playlists** — named playlist cards, default indicator, schedule editor, item membership, thumbnails/announcement previews, type, rendering mode, duration, volume, schedule summary, enable toggle, per-playlist reorder, preview, refresh, duplicate, delete.
3. **Add Content** — upload, website, and Microsoft 365 workflows with validation, progress, capability test, and preview.
4. **Schedules** — global weekly grid, overnight-window UI, per-item schedule summary, timezone, clock/RTC warning, and manual override.
5. **Settings** — site name, resolution, daily check time, defaults, boot splash duration, quotas, intranet allowlist, remote-player option, blanking test, holiday mode, and one-click update.
6. **System** — addresses, service health, Refresh All, logs, backup, restore, password change, service restart, reboot, version, and platform diagnostics.

Destructive operations require a confirmation displaying the exact target. Delete removes the item from the playlist immediately but performs generation cleanup as a recoverable background action where practical.

---

## 13. API Outline

```text
GET  /api/ready                            minimal readiness probe
GET  /startup                              loopback/device-cookie boot address page
GET  /player                               loopback/device-cookie kiosk player
GET  /player/connect                       optional remote-device connection form
POST /player/connect                       validates token in request body
GET  /api/playlist-now                     loopback/device-cookie playlist
GET|POST /api/health/player                minimal player health/heartbeat

GET  /login
POST /login
POST /logout
GET  /admin
GET  /admin/playlist
GET  /admin/playlists
GET  /admin/playlists/<id>
GET  /admin/items/new
GET  /admin/schedules
GET  /admin/settings
GET  /admin/system

GET  /api/items                            authenticated
POST /api/items                            authenticated + CSRF
GET  /api/items/<id>                       authenticated
PATCH /api/items/<id>                      authenticated + CSRF
DELETE /api/items/<id>                     authenticated + CSRF
POST /api/items/<id>/replace               authenticated + CSRF
POST /api/items/<id>/refresh               authenticated + CSRF
POST /api/items/<id>/duplicate             authenticated + CSRF
POST /api/items/reorder                     authenticated + CSRF
GET  /admin/items/<id>/preview              authenticated

GET  /api/playlists                         authenticated
POST /api/playlists                         authenticated + CSRF
PATCH /api/playlists/<id>                   authenticated + CSRF
DELETE /api/playlists/<id>                  authenticated + CSRF
POST /api/playlists/<id>/default            authenticated + CSRF
POST /api/playlists/<id>/reorder            authenticated + CSRF

GET  /api/jobs                             authenticated
GET  /api/jobs/<id>                        authenticated
POST /api/jobs/<id>/cancel                 authenticated + CSRF
GET  /api/schedule                         authenticated
PUT  /api/schedule                         authenticated + CSRF
POST /api/display/override                 authenticated + CSRF
DELETE /api/display/override               authenticated + CSRF
POST /api/display/test-blank               authenticated + CSRF
POST|DELETE /api/display/holiday           authenticated + CSRF
GET  /api/settings                         authenticated
PUT  /api/settings                         authenticated + CSRF
GET  /api/system/status                    authenticated
GET  /api/system/logs                      authenticated
POST /api/system/services/restart          authenticated + CSRF
POST /api/system/reboot                    authenticated + CSRF
POST /api/system/password                  authenticated + CSRF
POST /api/system/refresh-all               authenticated + CSRF
GET  /api/system/backup                    authenticated
POST /api/system/restore                   authenticated + CSRF
POST /api/system/update                    authenticated + CSRF

GET  /media/<item>/<generation>/<file>     loopback, authenticated session, or device cookie
```

JSON responses use a consistent envelope:

```json
{"ok": true, "data": {}, "error": null}
```

Errors include a stable code and safe human-readable message. Internal command output and secrets are never returned to the browser.

---

## 14. `start.sh` — One-File Setup and Startup Entry Point

The repository root contains a simple executable `start.sh`. It is the only setup command the user needs:

```bash
chmod +x start.sh
sudo ./start.sh
```

No manual package installation, virtual-environment commands, service-file copying, desktop login configuration, or `systemctl enable` commands are required.

### 14.1 First-run responsibilities

`start.sh` performs the following in an idempotent and clearly logged sequence:

1. Verify Raspberry Pi hardware, supported Raspberry Pi OS release, architecture, free disk space, network state, and root privileges.
2. Detect the invoking user and avoid storing application data in that user's home directory.
3. Install required APT packages, including Git, Python venv support, X11/Openbox, Chromium, unclutter, Avahi/mDNS, zram support, LibreOffice Impress, Poppler, FFmpeg, `wget`, fonts, and required runtime libraries.
4. Create the locked-down `pi-agenda` system user and grant only the required video, render, and audio device access.
5. Install application code under `/opt/pi-agenda` and create `/var/lib/pi-agenda` with correct ownership.
6. Create or update the Python virtual environment and install pinned Python dependencies.
7. Initialize or migrate the SQLite database.
8. Prompt twice for the management password without echoing it and store the bcrypt hash through an application CLI command.
9. Generate the Flask secret and other installation-specific configuration when missing.
10. Detect the system timezone and ask for confirmation when interactive.
11. Detect the display connector and record diagnostics without assuming a fixed HDMI name.
12. Configure mDNS hostname advertisement for `pi-agenda.local` where the LAN supports it.
13. Generate/install the four systemd unit files with correct dependencies, users, working directories, environment files, restart policies, and boot targets.
14. Configure the kiosk under `multi-user.target` to run on the physical display without an interactive desktop sign-in, and bind Escape to a privileged fixed-command helper that returns to the OS.
15. Enable all services at boot and start or restart them in dependency order.
16. Wait for `/api/ready`, verify worker and kiosk status, and report actionable failures.
17. Print the management URLs using the hostname and all current usable IPv4 addresses, always including port `8000`.

Example completion output:

```text
Pi-Agenda is running.
Management: http://pi-agenda.local:8000/admin
Management: http://192.168.1.42:8000/admin
The kiosk will start automatically on every boot.
```

### 14.2 Re-running and maintenance

- A normal re-run updates dependencies, runs migrations, repairs ownership and service definitions, and preserves the password, secret, database, uploads, and generations.
- `sudo ./start.sh --reset-password` prompts for a replacement password.
- `sudo ./start.sh --repair` reapplies packages, permissions, display detection, and services without reinstalling content.
- `sudo ./start.sh --status` prints service state and management addresses without making changes.
- The authenticated Settings update button compares the installed commit with GitHub `main`; when different, a detached root helper clones that exact commit, runs its `start.sh --repair`, and reboots. When identical, it reports that no update is needed.
- `sudo ./start.sh --uninstall` is not implemented until a safe, explicit, backup-aware removal design exists.
- Failures stop with a concise explanation and a command for viewing the relevant journal. The script does not silently continue after a required dependency or service fails.

### 14.3 Boot sequence

```text
Pi power-on
  -> systemd starts network and Avahi
  -> pi-agenda-web and pi-agenda-cache become ready
  -> pi-agenda-worker starts and restores interrupted jobs
  -> pi-agenda-kiosk starts X11/Openbox/Chromium without login
  -> /startup shows hostname, IP address(es), and :8000 for >= 5 seconds
  -> /player begins the scheduled rotation
```

The address screen is rendered by the app so it always reflects the current DHCP address rather than an installation-time value.

---

## 15. Project Layout

```text
start.sh                              # only user-facing setup/start command
pyproject.toml                        # pinned Python package metadata
README.md
plan.md
src/pi_agenda/
  __init__.py
  app.py                              # Flask factory
  config.py
  db.py
  migrations.py
  auth.py
  csrf.py
  routes_admin.py
  routes_api.py
  routes_player.py
  models.py
  playlist.py
  schedules.py
  worker.py
  jobs.py
  generations.py
  health.py
  power.py
  system.py
  backup.py
  pipelines/
    common.py
    presentation.py
    image.py
    m365.py
    website.py
    video.py
  templates/
    startup.html
    player.html
    login.html
    base.html
    dashboard.html
    playlist.html
    item_form.html
    schedules.html
    settings.html
    system.html
  static/
    player.js
    admin.js
    style.css
    icons/
tests/
  unit/
  integration/
  fixtures/
```

Systemd definitions may be maintained as templates in application code for development, but `start.sh` installs them automatically; the user is never asked to copy or edit them.

---

## 16. Backup, Restore, Storage, and Logs

### 16.1 Backup and restore

- Backups use SQLite's online backup API or `VACUUM INTO`; the live database file is never copied unsafely.
- A backup bundle contains the database snapshot, original uploads, configuration manifest, schema version, and checksums.
- Backups include every active verified generation so a restore is immediately playable offline. Retired generations are omitted to control backup size.
- Secrets and password hashes are clearly identified in backup warnings.
- Restore validates checksums, compatibility, paths, and free space before changing active data. The existing installation is snapshotted first.

### 16.2 Storage controls

- Configurable content quota and minimum-free-space reserve.
- Upload and remote-fetch limits enforced before and during writes.
- Staging cleanup at boot and after failed jobs.
- Old inactive generations removed only after their retirement grace period.
- Logs use journald limits or log rotation.
- A disk-full condition preserves the database and active generations, rejects new work safely, and appears prominently in the UI.

### 16.3 Observability

- Structured application events with timestamps, item/job IDs, and safe error summaries.
- No passwords, cookies, CSRF tokens, signed URLs, or downloaded document content in logs.
- Status includes `vcgencmd get_throttled`, temperature, memory, disk, uptime, current IPs, display state, clock synchronization, and job queue depth.

---

## 17. Implementation Sequence

The sequence is organized to reduce integration risk, but completion means delivering every phase.

### Phase 0 — Target-device proof

- Validate current 32-bit Raspberry Pi OS Lite on both an actual Zero 2W and Pi 3 Model B.
- Prove X11/Openbox/Chromium boot without sign-in.
- Prove the five-second dynamic address screen.
- Test HDMI off/on and automatic connector discovery with the target television.
- Convert representative PowerPoint and PDF files while the player runs; measure RAM, swap, time, temperature, and throttling.
- Validate 720p and assess 1080p.
- Test exact OneDrive/SharePoint link types used by the teacher.

### Phase 1 — Foundation and secure installation

- Package layout, configuration, SQLite migrations, core models, authentication, CSRF, permissions, and `start.sh`.
- Four systemd services, readiness endpoints, boot ordering, mDNS, and recovery behavior.
- Startup address screen and initial admin shell.

### Phase 2 — Durable media core

- Job table/worker, immutable generations, atomic publication, quotas, cleanup, upload progress, authenticated preview, and playlist versioning.
- Complete item CRUD, reordering, schedule fields, and player rotation foundation.

### Phase 3 — All local content pipelines

- PowerPoint, PDF, image, GIF policy, and video probe/transcode/playback.
- Resolution-dependent generations, thumbnail review, fit modes, audio, and error handling.

### Phase 4 — Remote content and offline fallbacks

- Microsoft 365 capability detection, live/converted/screenshot modes.
- Website live iframe, separate-origin archive, screenshot, validation, and fallback selection.
- Daily refresh, retry policy, source-specific health, and manual refresh controls.

### Phase 5 — Scheduling and display control

- Per-item and global timezone-aware schedules, overnight behavior, RTC/time-validity status, manual overrides, power transitions, and wake recovery.

### Phase 6 — Complete administration experience

- Dashboard, playlist, add/edit content, schedules, settings, system page, progress UI, responsive styling, status explanations, and accessibility pass.

### Phase 7 — Operations and hardening

- Consistent backup/restore, log viewer, disk and resource safeguards, watchdogs, reboot/service controls, repair flow, failure injection, and documentation.

### Phase 8 — Release validation

- Full target-device acceptance suite, security review, classroom-network test, power-loss tests, long offline test, and multi-day soak.
- Pin the supported OS image and dependency set based on evidence from the completed tests.

---

## 18. Test and Acceptance Matrix

### Installation and boot

- [ ] Clean Pi OS Lite install requires only `sudo ./start.sh`.
- [ ] A hidden password prompt sets and confirms the password before admin exposure.
- [ ] Re-running `start.sh` preserves all data and credentials.
- [ ] `--reset-password`, `--repair`, and `--status` behave as documented.
- [ ] Cold boot requires no interactive sign-in.
- [ ] Escape exits to the desktop manager (or Lite console), while reboot starts the kiosk again.
- [ ] Boot screen shows mDNS name, current IP address, and port 8000 for at least five seconds.
- [ ] DHCP address changes appear correctly on the next boot.

### Security and privacy

- [ ] Remote unauthenticated requests cannot retrieve player playlists, full media, class photos, previews, logs, or backups.
- [ ] Every mutating route rejects missing or invalid CSRF tokens.
- [ ] Login rate limiting, session expiration, password change, and logout work.
- [ ] First-run admin ownership cannot be claimed through the browser.
- [ ] URL redirects cannot bypass address restrictions or download-size limits.
- [ ] Archived HTML cannot access management cookies/APIs or navigate the top-level kiosk.
- [ ] Reboot and power helpers accept no attacker-controlled commands or paths.

### Content

- [ ] Representative PPTX converts within limits and remains reviewable; PDF path preserves expected appearance.
- [ ] Images honor orientation, animation policy, and fit modes.
- [ ] Videos transcode/play at configured resolution with HDMI audio and volume 0/50/100.
- [ ] Microsoft live embed reflects a cloud edit; converted and screenshot fallbacks work when supported.
- [ ] A site that permits framing displays live.
- [ ] A site with frame restrictions degrades clearly to archive or screenshot.
- [ ] Login-heavy and JavaScript-heavy sites fail safely and retain the last good fallback.
- [ ] Announcement text, colors, size, alignment, preview, and rotation output match.
- [ ] Contain, cover, width, height, native, and stretch fit modes behave distinctly on representative aspect ratios.

### Atomicity and recovery

- [ ] Pull network during every remote pipeline stage; active generation remains playable.
- [ ] Kill the worker during conversion and publication; restart recovers or safely retries the job.
- [ ] Refresh a deck while it is playing; every referenced slide remains available.
- [ ] Corrupt, huge, deceptive, and unsupported uploads are rejected without exhausting RAM or disk.
- [ ] Disk-full behavior preserves the DB and active content.
- [ ] Sudden power loss during a DB write, backup, and cache promotion recovers cleanly.

### Scheduling and power

- [ ] Multiple simultaneous playlist schedules combine deterministically and deduplicate shared items.
- [ ] Item and playlist schedules are both required, including overnight boundaries.
- [ ] The default playlist runs only when no non-default playlist is active.
- [ ] Blanking test and holiday mode force off; their cancel controls restore scheduled behavior.

- [ ] Same-day, overnight, weekend, all-day, and disabled schedules behave at exact boundaries.
- [ ] DST forward/back transitions follow the documented rule.
- [ ] Cold boot with valid RTC and no WAN follows schedules.
- [ ] Cold boot without trustworthy time shows the time-uncertain state.
- [ ] Manual overrides expire at the correct boundary.
- [ ] HDMI turns off and reliably returns with the player refreshed.

### Performance and operations

- [ ] Four-hour mixed-content soak and multi-day idle soak show no Chromium or worker memory growth.
- [ ] Conversion does not make the active player unusable.
- [ ] 720p stays within the defined memory/temperature budget.
- [ ] 1080p is enabled as supported only if it passes the same tests.
- [ ] Backup is consistent during writes and restore reproduces all selected content.
- [ ] Logs rotate and contain no secrets.

---

## 19. Known Limits Presented to the User

- LibreOffice is not PowerPoint and can shift unsupported fonts, themes, transitions, or complex layout. PDF upload is the fidelity path.
- Uploaded local PowerPoint files do not synchronize with a teacher's computer automatically.
- Microsoft 365 iframes are cross-origin; Pi-Agenda cannot promise internal slide control or volume adjustment.
- Some Microsoft share links are viewable but not downloadable or embeddable.
- Many websites forbid iframe embedding, require login, or cannot be archived completely. Screenshot fallback is sometimes the only reliable offline representation.
- HTML website archives are best-effort, isolated, and never considered equivalent to the live site.
- Converted slide decks have no audio or animation.
- HDMI signal-off usually allows a display to sleep but does not remove wall power.
- Precise offline scheduling after total power loss requires a trustworthy hardware clock.
- Both supported boards are resource-constrained; 720p is the dependable target and conversions may take minutes, particularly on the Zero 2W.

---

## 20. Build Readiness Decisions

The following decisions are now part of the implementation direction:

- Build the complete product rather than a deck-only MVP.
- Support both Raspberry Pi Zero 2W and Raspberry Pi 3 Model B from the same installer and codebase.
- Include image, website, and video support in the main architecture and delivery plan.
- Use a separate durable worker and immutable content generations.
- Keep untrusted archived sites on a separate loopback origin.
- Protect player/media data from other LAN clients by default.
- Set the first password through `start.sh`, not a remotely claimable setup page.
- Use migrations from the first schema.
- Make schedule semantics timezone-aware and explicitly support overnight windows.
- Treat an external RTC as the solution for guaranteed scheduling across offline cold boots.
- Make `start.sh` the single setup, repair, password-reset, and status entry point.
- Start the kiosk through systemd without an interactive sign-in.
- Display the current management hostname, IP address, and port for at least five seconds on every kiosk boot before beginning playback.
- Complete Phase 0 on the target Pi before depending on display-power commands, Chromium flags, package names, Microsoft link transformations, or 1080p performance assumptions.
