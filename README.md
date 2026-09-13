# YT Quid - YouTube Artist Analytics, AI Music Radar, Creator Studio & Publishing Platform

A full-featured music analytics, channel monitoring, historical growth intelligence, AI discovery radar, automated video rendering factory, and YouTube automated publishing dispatcher built with **Django**, **Tailwind CSS**, **Chart.js**, **Waitress**, and **Electron**.

Available as both a **Web Server** and a **Standalone Native Windows Desktop Application** (`.exe` Installer & Portable).

---

## 📥 Download Desktop Application (Windows)

Get the latest version of YT Quid for Windows directly from the GitHub releases:

| Package | Download Link | Description |
| :--- | :--- | :--- |
| **Windows Installer** | 📦 [**Download `YT Quid Setup.exe`**](https://github.com/malualbiar/YoutubeAnalytics/releases/latest) | Recommended. Full installer with Desktop & Start Menu shortcuts, auto-updater, and uninstaller. |
| **Portable Version** | ⚡ [**Download `YT Quid Portable.exe`**](https://github.com/malualbiar/YoutubeAnalytics/releases/latest) | Zero-installation standalone executable. Run directly from any folder or USB drive. |
| **All Releases & Notes** | 📋 [**View GitHub Releases**](https://github.com/malualbiar/YoutubeAnalytics/releases) | Release notes, changelog history, and SHA-256 checksums. |

---

## 🌟 Key Features

### 1. 📊 Interactive Analytics & Catalog Monitoring
- **Real-Time Channel & Video Tracking**: Connect official YouTube channels via channel URL, `@handle`, or raw Channel ID (`UC...`).
- **Growth Calculations & Snapshots**: Accurately tracks **Total Cumulative Views** vs. **Views Gained** (today, this week, this month, 90D, 1Y, all-time).
- **Side-by-Side Comparisons**: Multi-artist and multi-video benchmarking matrices with multi-line growth charts.
- **Milestones & Velocity**: Milestone unlock notifications (10K, 50K, 100K, 1M, 5M, 10M views) with daily velocity indicators.
- **Daily Upload Tracker**: 30-day activity heatmap, streak counter, and customizable posting goals.
- **Executive Reports**: One-click CSV and printable PDF export.

### 2. 📡 AI Music Discovery Radar (`/radar/`)
- **Granular Time Filtering**: Real-time discovery of breakout AI music (Suno, Udio, Lofi, Synthwave, Hip-Hop, Pop, Rock) published in the past 30m, 1h, 6h, 12h, 24h, 48h, 7d, or 30d.
- **Velocity Arbitrage**: Sort by views/hour or filter by underdogs (<10K views) to catch rising trends before they saturate.
- **Lossless Extraction**: 1-click lossless WAV / MP3 audio downloader powered by `yt-dlp`.
- **Direct Studio Bridge**: 1-click export from Radar directly into the Creator Studio.

### 3. 🎬 Creator Studio & Automated Video Factory (`/studio/`)
- **Synced Lyric Video Generator (`/studio/lyrics/`)**:
  - **Whisper AI & LRCLIB Integration**: Auto-transcribe vocals or query synced lyric databases in seconds.
  - **Interactive Tap-to-Sync (Karaoke Mode)**: Play audio and tap `Spacebar` on lyric hits for rhythm precision.
  - **Import / Export `.LRC` & `.SRT`**: Full two-way compatibility with standard synchronized lyrics files.
  - **Multi-Style Typography & Karaoke FX**: Real-time karaoke glow wipes, 3-line teleprompters, cinematic minimal, and cyber neon styles.
  - **Multi-Format Export**: 1080p 16:9 Landscape (YouTube) and 9:16 Vertical (Shorts/Reels/TikTok).
- **Short Video Generator & Multi-Clip Chopper (`/studio/shorts/`)**:
  - **1 Long Video → Multiple Shorts**: Upload any long video or audio track and chop it into vertical 9:16 clips (15s, 30s, 60s, or custom intervals).
  - **Timeline Scrubber**: Interactive visual timeline to adjust boundaries, reorder clips, and preview before rendering.
  - **9:16 Vertical Re-Framing**: Blurred ambient backdrop, center crop, or letterbox modes.
  - **Viral Hook Badges & Watermarks**: Banner pills, part counters, and channel branding.
- **Non-Stop Continuous Mix Maker (`/studio/mix/`)**:
  - DJ-style seamless multi-track blending with 5 crossfade curves (Equal-Power Quarter-Sine, Linear, Triangular, Exponential, Quick Club Cut).
  - Automatic YouTube chapters and descriptions generator.
- **1-Hour Extended Study Loops & Visualizers**:
  - 60-minute seamless watch-time loops and 1080p official visualizers.

### 4. 🚀 YouTube Automated Publishing & Drafts Dispatcher (`/publishing/`)
- **Multi-Engine Publishing Architecture**:
  - ⚡ **YouTube Data API v3**: Background resumable chunked upload engine (10MB chunks) via official Google OAuth 2.0.
  - 🤖 **Zero-Quota Browser Bot Automation**: Playwright-powered background bot for automated YouTube Studio upload sessions with 0 API quota consumption.
  - 📋 **1-Click Studio Assistant**: Pre-populates video titles, descriptions, and chapters to clipboard, reveals files in Explorer, and opens YouTube Studio.
- **Full Video Format Coverage**:
  - Direct 1-click dispatch from all Creator Studio modules (Lyric Videos, Shorts/Chops, Non-Stop Mixes, 1-Hour Loops, or Custom MP4 files).
  - **Batch Shorts Dispatch**: Drip-schedule multiple generated Shorts over consecutive days.
- **Publishing Modes**:
  - **Private Drafts**: Safely upload videos to YouTube Studio for final inspection before going live.
  - **Unlisted Previews**: Share preview links with your team/artists.
  - **Scheduled Drops**: Auto-publish at a specific future date and time.
  - **Immediate Public Posts**: Instant public release.
- **Queue & History Tracker (`/publishing/queue/`)**:
  - Real-time progress monitoring (0–100%) with live auto-polling.
  - Filter by status, search by video ID, cancel active uploads, or retry failed jobs.
  - One-click **"Edit in YouTube Studio"** button directly to the video's management page.

---

## 💻 Standalone Desktop Application (v1.3.0)

YT Quid runs as a native desktop application with an embedded Python/Django WSGI server powered by **Waitress** and an **Electron** frontend.

👉 **[Download the Latest Windows Release (v1.3.0)](https://github.com/malualbiar/YoutubeAnalytics/releases/latest)**

### Desktop Architecture
- **Zero Configuration**: Automatically applies database migrations and seeds initial demo data on first launch.
- **Data Persistence**: Stores the SQLite database (`db.sqlite3`), `.env` overrides, and generated media/video outputs in `%APPDATA%\YTQuid\` (or `~/.config/ytquid/` on Linux/macOS).
- **Bundled Binaries**: Ships with embedded Python runtime dependencies and bundled FFmpeg.

---

## 🛠️ Building the Desktop App Locally

### 1. Prerequisites
- Python 3.10+ installed
- Node.js 18+ and npm installed

### 2. One-Click Build (Windows)
Run the automated build batch script from the project root:
```cmd
build_desktop.bat
```

### 3. Manual Step-by-Step Build
```bash
# 1. Install dependencies
pip install -r requirements.txt
npm install

# 2. Package the backend into dist-backend/server/
npm run build:backend

# 3. Test the packaged backend executable
dist-backend\server\server.exe --test

# 4. Build the Electron Desktop Installer and Portable App
npm run dist
```

### Build Artifacts
Outputs are generated in `dist-electron/`:
- **`YT Quid Setup 1.3.0.exe`**: Full NSIS Windows installer (desktop shortcut, start menu shortcut, uninstaller).
- **`YT Quid 1.3.0.exe`**: Standalone zero-install portable executable.

---

## 🚀 CI/CD Automated Release Workflows

The repository includes a GitHub Actions workflow located at [`.github/workflows/release-desktop.yml`](.github/workflows/release-desktop.yml) for building and publishing desktop releases.

### How to Trigger a Release
1. **Via Git Tag**:
   ```bash
   git tag v1.3.0
   git push origin v1.3.0
   ```
2. **Via GitHub Actions Web UI**:
   - Go to the **Actions** tab on GitHub.
   - Select **Release Desktop App**.
   - Click **Run workflow** and specify the version tag (e.g. `v1.3.0`).

### Automated Workflow Pipeline
1. Sets up clean Windows runners with Python 3.11 and Node.js 20.
2. Compiles the embedded Django backend via PyInstaller.
3. Builds the NSIS Installer and Portable Executables via `electron-builder`.
4. Calculates SHA-256 checksums (`checksums-sha256.txt`).
5. Publishes a new **GitHub Release** with all executable assets attached.

---

## ☁️ Deploying to Render

YT Quid ships with a [`render.yaml`](render.yaml) Blueprint and a [`build.sh`](build.sh) script for one-click deployment on [Render](https://render.com).

### Option A — One-Click Blueprint Deploy (Recommended)

1. Push the repository to GitHub (or GitLab).
2. In the Render Dashboard click **New → Blueprint** and select this repository.
3. Render reads `render.yaml` and creates:
   - A **Web Service** (`yt-quid`) running Gunicorn.
   - A **PostgreSQL database** (`yt-quid-db`, free tier).
4. In the **Environment** tab of the web service, fill in the secrets that have `sync: false`:
   | Variable | Value |
   | :--- | :--- |
   | `YOUTUBE_API_KEY` | Your YouTube Data API v3 key |
   | `GOOGLE_OAUTH_CLIENT_ID` | OAuth 2.0 client ID |
   | `GOOGLE_OAUTH_CLIENT_SECRET` | OAuth 2.0 client secret |
   | `GOOGLE_OAUTH_REDIRECT_URI` | `https://<your-service>.onrender.com/publishing/oauth/callback/` |
5. Click **Save Changes** — Render triggers a fresh deploy automatically.

### Option B — Manual Service Setup

1. Create a **New Web Service** from your repository.
2. Set runtime to **Python**, build command to `./build.sh`, and start command to:
   ```
   gunicorn core.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120
   ```
3. Add a **PostgreSQL** database and copy its **Internal Connection String** as `DATABASE_URL`.
4. Set the environment variables listed in [`.env.example`](.env.example) — `DEBUG=false`, a strong `SECRET_KEY`, and your API credentials.

### What the build script does

The [`build.sh`](build.sh) script runs automatically on every deploy:
1. Installs Python dependencies (`pip install -r requirements.txt`).
2. Collects static files (`collectstatic`) — served by WhiteNoise directly from Gunicorn, no separate CDN needed.
3. Applies database migrations (`migrate`).
4. Seeds demo data on first deploy (`seed_demo_data`).

### Notes
- **Media uploads** are stored on a 5 GB persistent disk mounted at `/opt/render/project/src/media` (configured in `render.yaml`). Upgrade the disk size in the Render dashboard as your library grows.
- **Google OAuth**: add `https://<your-service>.onrender.com/publishing/oauth/callback/` as an authorised redirect URI in your Google Cloud OAuth client.
- The free-tier PostgreSQL database on Render is deleted after 90 days. Upgrade to a paid tier for a persistent production database.

---

## 🌐 Running in Web Server Mode

You can also run YT Quid as a standard web application:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure .env
cp .env.example .env

# 3. Migrate and seed demo data
python manage.py migrate
python manage.py seed_demo_data

# 4. Start local server
python manage.py runserver
```
Visit **`http://127.0.0.1:8000/`** in your browser.

---

## 🔑 YouTube & Google Cloud API Setup

1. **YouTube Data API v3 Key** (for syncing analytics, video stats, and Radar):
   - Obtain an API key from Google Cloud Console.
   - Add to `.env`: `YOUTUBE_API_KEY=your_key_here`.

2. **Google OAuth 2.0 Web Client** (for direct YouTube Publishing):
   - Create OAuth 2.0 Credentials (Web Application) in Google Cloud Console.
   - Add authorized redirect URI: `http://127.0.0.1:8000/publishing/oauth/callback/`.
   - Add to `.env`:
     ```env
     GOOGLE_OAUTH_CLIENT_ID=your_client_id.apps.googleusercontent.com
     GOOGLE_OAUTH_CLIENT_SECRET=your_client_secret
     GOOGLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/publishing/oauth/callback/
     ```

---

## 👥 Demo Accounts

The database comes pre-seeded with 4 active artists (*Luna Vance*, *Kairo Beats*, *Nova Sound*, *Aria Vega*), 22+ songs, 30 days of historical snapshots, and 3 demo user roles:

| Role | Email | Password | Permissions |
| :--- | :--- | :--- | :--- |
| **Super Admin** | `admin@analytics.com` | `admin123` | Full Control, Channel Sync, Creator Studio, Publishing |
| **Manager** | `manager@analytics.com` | `manager123` | Artist Management, Reports, Dashboard |
| **Viewer** | `viewer@analytics.com` | `viewer123` | Read-only access to Analytics & Charts |

---

## 🧪 Running Automated Tests

Run the test suite across all modules:
```bash
python manage.py test
```

---

## 📁 Project Structure

```
YoutubeAnalytics/
├── .github/workflows/
│   └── release-desktop.yml         # CI/CD automated desktop release workflow
├── build_desktop.bat               # 1-click Windows desktop compilation script
├── main.js                         # Electron main process & server launcher
├── preload.js                      # Electron secure context bridge
├── server.py                       # Embedded Waitress WSGI server launcher
├── server.spec                     # PyInstaller configuration & asset bundler
├── package.json                    # Electron build configuration & scripts
├── requirements.txt                # Python backend dependencies
├── bin/
│   └── ffmpeg.exe                  # Bundled FFmpeg multimedia engine
├── core/                           # Django project settings, WSGI & URLs
├── apps/
│   ├── authentication/             # Custom User, Auth & RBAC
│   ├── artists/                    # Artist & YouTubeChannel models & views
│   ├── videos/                     # Video & Snapshot models, catalog & detail
│   ├── analytics/                  # Core metrics engine & comparisons
│   ├── milestones/                 # Milestones & velocity alerts
│   ├── reports/                    # CSV & printable report generator
│   ├── youtube/                    # YouTube Data API v3 services & quota tracker
│   ├── studio/                     # Creator Studio: Lyrics, Shorts, Mix, 1-Hour Loop
│   ├── radar/                      # AI Music Discovery Radar
│   └── publishing/                 # YouTube Publishing & Automated Drafts Dispatcher
├── templates/                      # Tailwind CSS Django templates
│   ├── base.html                   # Global sidebar, navigation, search modal
│   ├── auth/                       # Authentication templates
│   ├── dashboard/                  # Analytics dashboard
│   ├── studio/                     # Creator Studio generator templates
│   ├── publishing/                 # YouTube Publishing Hub & Queue templates
│   └── ...
├── static/                         # Static assets (icons, JS, styles)
└── dist-electron/                  # Compiled desktop installer & portable .exe
```
