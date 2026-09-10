# Pi-Agenda

Pi-Agenda is an offline-tolerant classroom digital-signage application for a Raspberry Pi connected to an HDMI television or monitor. It rotates PowerPoint presentations, PDFs, Microsoft 365 PowerPoint links, images, websites, videos, and text announcements using named playlist and item schedules.

The app includes a mobile-friendly management interface, automatic boot kiosk, local caching, background conversion, display power scheduling, and consistent backups.

## Supported Raspberry Pi models

- Raspberry Pi Zero 2W with 512 MB RAM
- Raspberry Pi 3 Model B, including board revisions identified by Raspberry Pi OS as a Pi 3 Model B

Raspberry Pi OS Lite 32-bit is recommended for both platforms. Trixie and Bookworm package layouts are recognized by the installer. The default display resolution is 720p to keep memory use predictable; 1080p is available but should be tested with the intended content.

An external RTC module is strongly recommended when schedules must remain accurate after a completely offline cold boot.

## Ports and addresses

| Service | Default address | Exposure |
|---|---|---|
| Management interface | `http://pi-agenda.local:8000/admin` | Local network, password protected |
| Management interface by IP | `http://<pi-ip>:8000/admin` | Local network, password protected |
| Kiosk player | `http://127.0.0.1:8000/player` | Pi loopback only by default |
| Archived website cache | `http://127.0.0.1:8002/` | Pi loopback only |

The default management port is **8000**. It can be changed for installation by setting `PI_AGENDA_PORT`, but the boot screen and documentation assume port 8000 unless configured otherwise.

## Installation

### 1. Prepare Raspberry Pi OS

Use Raspberry Pi Imager to install the current Raspberry Pi OS Lite 32-bit image. In Imager, configure Wi-Fi and enable SSH so the Pi can be reached for initial setup.

Connect the Pi to the HDMI display before booting it.

### 2. Clone the repository

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/bkgodwin/Pi-Agenda.git
cd Pi-Agenda
```

### 3. Run the one-file setup

```bash
chmod +x start.sh
sudo ./start.sh
```

`start.sh` performs the complete installation:

- Verifies Raspberry Pi Zero 2W or Raspberry Pi 3 Model B hardware.
- Installs X11, Openbox, Chromium, LibreOffice, Poppler, FFmpeg, fonts, Python, and supporting packages.
- Creates an isolated service account and application directories.
- Installs Python dependencies in a virtual environment.
- Initializes or migrates the SQLite database.
- Prompts twice for the management password without displaying it.
- Configures `pi-agenda.local` mDNS discovery.
- Installs and enables all systemd services.
- Starts the fullscreen kiosk without an interactive desktop sign-in.
- Prints the hostname and current IP address with management port 8000.

After installation, open one of the addresses printed by the script, for example:

```text
http://pi-agenda.local:8000/admin
http://192.168.1.42:8000/admin
```

On every Pi boot, the connected display shows the current management hostname, IP address, and port for at least five seconds before starting the playlist.

The installer makes `multi-user.target` the boot target, disables the detected graphical login manager at boot, and attaches the kiosk directly to `tty1`. Press **Esc** on a connected keyboard to stop the kiosk and return to Raspberry Pi OS. A Desktop installation starts its normal desktop login; Raspberry Pi OS Lite returns to its console. The kiosk starts again on the next reboot.

### If the Pi still stops at an account sign-in

Update the checkout and reapply the boot configuration:

```bash
cd Pi-Agenda
git pull --ff-only origin main
sudo ./start.sh --repair
sudo systemctl enable pi-agenda-kiosk.service
sudo systemctl set-default multi-user.target
sudo reboot
```

If it still does not enter the kiosk, inspect the exact startup error:

```bash
sudo systemctl status pi-agenda-kiosk.service --no-pager
sudo journalctl -u pi-agenda-kiosk.service --no-pager -n 100
```

Verify that the display is connected before boot and that no custom display-manager configuration is forcing a graphical login. Running `sudo ./start.sh --repair` is safe and preserves the database, password, uploads, playlists, and generated media.

## Installer maintenance commands

Run these from the cloned repository or `/opt/pi-agenda`:

```bash
sudo ./start.sh --status
sudo ./start.sh --repair
sudo ./start.sh --reset-password
```

- `--status` displays service information and current management addresses without changing the installation.
- `--repair` reapplies packages, ownership, service definitions, and application files while preserving content and credentials.
- `--reset-password` changes the management password without reinstalling the app.

A normal `sudo ./start.sh` re-run is idempotent: it updates the installed application, repairs services, runs database migrations, and preserves the existing password, secret key, uploads, and database.

## Adding content

Sign in to the management site, select **Add content**, and choose one of:

- PowerPoint `.pptx`
- PDF `.pdf`
- Microsoft 365 PowerPoint embed/share link
- JPG, PNG, WebP, or GIF image
- Website URL
- MP4, MOV, MKV, or WebM video
- Text announcement with background color, text color, size, and alignment

Uploaded files are processed by a single background worker so presentation or video conversion does not overwhelm the Pi. The playlist keeps using the previous verified generation until new output has been fully checked and published.

Microsoft 365 and website items support live and cached modes. **Auto** is the reliable default: Chromium allows up to 30 seconds for the live page to load and execute its rendering code, then Pi-Agenda verifies the resulting screenshot before publishing it. This avoids both premature loading/icon captures and blank frames from sites that forbid embedding. **Live** should be selected only for sites known to allow embedding; **Archive** displays the sanitized local HTML copy.

**Scrolling archive** is intended for long webpages. Pi-Agenda captures the rendered document, stores its images and styles locally, pauses at the top for 5% of the item duration, scrolls to the bottom during the middle 90%, and leaves the final 5% for reading the bottom. A 60-second or longer item duration is recommended. Auto, Screenshot, and converted Microsoft output use the media fit control; Live, Archive, and Scrolling archive use an independent webpage zoom control.

Image, slide, screenshot, and video items offer six fitting choices: Contain, Cover, Fill width, Fill height, Native size, and Stretch. Contain shows the whole source; Cover fills the screen and crops; width/height constrain a single dimension. Preview the item after changing fit.

Failed conversions and remote refreshes retry automatically with bounded backoff. The previous verified generation stays playable. Selecting Refresh manually wakes a delayed retry immediately.

## Playlists

- Create and name any number of playlists from **Playlists**.
- Each non-default playlist has its own days and optional time window.
- An item's days/time window and its playlist schedule must both be active for that item to rotate.
- Items may belong to multiple playlists and have a separate order in each.
- Scheduled playlists take priority whenever one or more are active.
- Exactly one playlist is the default. It cannot be scheduled or disabled and runs only when no scheduled playlist is active and the global display schedule is on.
- When another playlist is made default, the former default is disabled until you give it a schedule and enable it, preventing it from accidentally running all day.
- Deleting a playlist preserves items that would otherwise become orphaned by moving them to the default playlist.

## Schedules

- Each item can be assigned days and an optional time window.
- Overnight item windows are supported; the selected weekday is the day on which the window starts.
- The display has a separate weekly schedule with Off, Always on, or Time window for each day.
- Manual display overrides can temporarily force the screen on or off.
- Schedule boundaries use the timezone configured in Settings.

## Widgets

The **Widgets** tab configures overlays that remain readable without replacing the current content:

- A 12-hour digital clock can be placed in any corner or at the top/bottom center, with an adjustable text size and translucent glass background.
- A class progress bar tracks the active timed playlist from its start to end time. Its edge, height, and color are configurable; it is hidden for the default playlist and untimed playlists.
- A text ticker can run along the top or bottom with editable text and adjustable travel time.

When the ticker and progress bar share an edge, Pi-Agenda stacks them automatically. Clocks are offset past either edge band, so enabled widgets do not cover one another. Widgets disappear whenever the display is blanked or scheduled off.

## Display power

Pi-Agenda detects the active connector instead of assuming a fixed HDMI name. At an off boundary it blanks the player and requests HDMI/DPMS standby. This normally lets a television sleep, but it does not cut wall power.

The Dashboard and Settings pages include a display blanking control for testing the complete player-blackout and HDMI-off path. Pi-Agenda disables the active XRandR output (or Raspberry Pi firmware display power fallback) and records the display as off only after a follow-up query confirms that the video signal is disabled. The television should report **No signal** or enter standby—not merely show a black image. Select **Restore display** (or **End test and resume schedule** in Settings) to re-enable the output and return control to the normal schedule. Display commands are reapplied after service restarts so a stale saved state cannot leave the HDMI signal active. If verification fails, inspect `sudo journalctl -u pi-agenda-worker --no-pager -n 100`.

Holiday mode pauses every playlist and holds the display off for 1–365 days. It can be cancelled early from Settings.

## One-click updates

Select **Check for and install update** in Settings. Pi-Agenda compares the installed commit with the latest `main` branch. If already current, it reports that without rebooting. Otherwise it clones a clean copy, runs `start.sh --repair`, preserves application data and credentials, and reboots automatically.

The update control is installed by the current `start.sh`. An older installation must run `sudo ./start.sh --repair` once over SSH before the first one-click update. Update failures are available with:

```bash
sudo journalctl -u 'pi-agenda-update-*' --no-pager -n 150
```

## Backups

The System page creates a consistent SQLite snapshot and includes original uploads plus each item's active verified generation in the downloaded ZIP. The backup can therefore restore a playable offline display without first reconverting or downloading everything. Retired generations are omitted to control backup size.

## Security notes

- The password is set locally during installation; there is no browser-claimable first-run setup page.
- Administration changes require both an authenticated session and CSRF token.
- Full-resolution player media and playlists are loopback-only by default to protect classroom photos and schedules from other LAN clients.
- Optional remote-player access is disabled by default. When enabled in Settings, connect the additional display at `/player/connect` using the displayed device token; the token is submitted in the request body rather than placed in a logged URL.
- Downloaded web archives are stripped of scripts and active content and served on an isolated loopback port.
- HTTP is the default for simple LAN operation. On a shared or untrusted network, use an isolated VLAN or configure an HTTPS reverse proxy because plain HTTP traffic can be observed.

## Service management and logs

```bash
sudo systemctl status pi-agenda-web pi-agenda-worker pi-agenda-cache pi-agenda-kiosk
sudo journalctl -u 'pi-agenda-*' --no-pager -n 200
sudo systemctl restart pi-agenda-web pi-agenda-worker pi-agenda-cache pi-agenda-kiosk
```

Application data is stored under `/var/lib/pi-agenda`; installed code is under `/opt/pi-agenda`.

## Development

On Python 3.11 or later:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
export PI_AGENDA_DATA_DIR="$PWD/data"
export PI_AGENDA_TESTING=1
pi-agenda init-db
pi-agenda set-password
pi-agenda run-web
```

Run tests with:

```bash
pytest
```

The conversion integration tests require LibreOffice, Poppler, FFmpeg, and Chromium. Unit and web-route tests do not require Raspberry Pi hardware.

## Architecture and implementation plan

See [plan.md](plan.md) for the full architecture, security model, pipeline behavior, installation design, and acceptance matrix.
