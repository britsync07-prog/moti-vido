# Moti-Vido
<div align="center">

![License](https://img.shields.io/github/license/britsync07-prog/moti-vido?style=flat-square&label=license&color=06b6d4) ![Language](https://img.shields.io/github/languages/top/britsync07-prog/moti-vido?style=flat-square&color=0ea5e9) ![Stars](https://img.shields.io/github/stars/britsync07-prog/moti-vido?style=flat-square&color=f59e0b) ![Last commit](https://img.shields.io/github/last-commit/britsync07-prog/moti-vido?style=flat-square&color=22c55e) ![Repo size](https://img.shields.io/github/repo-size/britsync07-prog/moti-vido?style=flat-square&color=94a3b8)

</div>

> A containerized motivational-video factory: transcribe narration, render word-synced captions frame-by-frame in a headless browser, mix background music with FFmpeg, and stream the results.

Moti-Vido is a Python video-generation service built around FastAPI. It accepts an audio narration plus background images per session, produces word-timestamped transcripts with faster-whisper, rasterizes every video frame from an HTML template through Playwright/Chromium (vertical 1080x1920 or horizontal 1920x1080), assembles frames and audio with FFmpeg into numbered segments, concatenates them, optionally loops a music track underneath at low volume, and archives final videos per user. A Streamlit dashboard provides administration and scheduling, PostgreSQL persists users/posts/jobs, an n8n webhook drives calendar-based publishing, and a dedicated streaming server serves range requests for smooth playback.

## Overview
The system splits into five cooperating components:

1. **Video engine (`server.py`)** - FastAPI app exposing upload, segment-creation, merge, cleanup, and webhook endpoints. It owns temporary `storage/` processing space and persistent `users_data/` archives.
2. **Render pipeline** - faster-whisper ("small", int8, CPU) extracts word timings; Playwright loads `template.html`, injects words via `loadWords()`, switches orientation with `setMode()`, and screenshots each frame at 30 FPS; FFmpeg encodes JPEG frames + audio to H.264/AAC MP4.
3. **Scheduler** - a 60-second asyncio loop queries due `posts` rows and POSTs their payloads to the configured n8n webhook, marking them published on success.
4. **Dashboard (`dashboard.py`)** - Streamlit UI with salted-hash authentication, admin user management, post calendar, per-user video quota (5 videos), and storage browsing.
5. **Stream server (`stream_server.py`)** - separate FastAPI process on port 8002 implementing HTTP Range (206) streaming over whitelisted directories only.

## Features
- Session-scoped uploads for audio, images, and music with UUID filenames.
- Word-level caption synchronization driven by whisper word timestamps.
- Vertical (1080x1920) and horizontal (1920x1080) render modes selected per request.
- Segmented generation: `/create-segment` writes sequentially numbered `N.mp4` files so long narrations can be produced incrementally.
- One-shot `/merge-all`: FFmpeg concat of segments, optional music mixing (`volume=0.12`, looping, random or chosen track, "none" to skip), faststart flag for web playback.
- Background-task cleanup that wipes temp session files except the final artifact; orphan sessions fall back to an `unknown_user` folder rather than failing.
- Calendar webhook API (`POST`/`GET /webhook/calendar`) plus scheduled auto-publishing through n8n.
- HTTP Range streaming endpoint with directory-whitelist path traversal protection.
- Streamlit admin dashboard with default-admin bootstrap, suspension flags, and job tracking.
- Docker Compose stack: engine, dashboard, PostgreSQL 15, shared volumes, external network.
- Manual migration helpers (`migrate_status.py`, `migrate_temp_jobs.py`) for schema evolution.

## Tech Stack
| Layer | Technology |
| :--- | :--- |
| API framework | FastAPI + Uvicorn (ports 8000 and 8002) |
| Transcription | faster-whisper (small model, int8, CPU) |
| Frame rendering | Playwright async Chromium + HTML template |
| Video encoding | FFmpeg (libx264, AAC, concat demuxer, amix filter) |
| Dashboard | Streamlit |
| Database | PostgreSQL 15 via SQLAlchemy ORM |
| Automation | n8n webhooks, asyncio background scheduler |
| Packaging | Docker (Playwright base image v1.49.0-jammy), docker-compose |

## Architecture
```
Client / Streamlit dashboard
        | uploads (audio, images, music)
        v
FastAPI server.py  --storage/<session>/-->  faster-whisper transcript
        |                                        | word timings
        v                                        v
Playwright Chromium  --> frame_0001..N.jpg --> FFmpeg -> N.mp4 segments
        |
        v
/merge-all: concat + music loop/amix --> final_video.mp4
        |
        +--> /upload-final --> users_data/<user_id>/<session_id>/
        +--> stream_server.py :8002 (HTTP Range)
PostgreSQL (users, posts, video_jobs) <--- scheduler ---> n8n webhook
```

## Project Structure
```
moti-vido/
|-- server.py               # FastAPI engine: uploads, render, merge, webhook, scheduler
|-- stream_server.py        # Range-request streaming server (port 8002)
|-- dashboard.py            # Streamlit admin dashboard (auth, posts, quotas)
|-- template.html           # Caption/background rendering template used by Chromium
|-- Dockerfile              # Playwright jammy image + ffmpeg + pip deps
|-- docker-compose.yml      # video-engine + dashboard + postgres services
|-- migrate_status.py       # Adds 'status' column to posts when missing
|-- migrate_temp_jobs.py    # Temp-job table migration helper
|-- storage/                # Temporary per-session processing (volume)
|-- users_data/             # Final archived videos per user (volume)
|-- music/                  # Background track library (volume)
```

## Getting Started
### Prerequisites
- Docker with Compose (recommended), or locally: Python 3.10+, FFmpeg on PATH, Playwright browsers
- A reachable PostgreSQL instance
- An n8n webhook URL if calendar publishing is desired

### Installation
```bash
git clone <repo-url>
cd moti-vido
docker compose up -d --build     # starts engine :8000, dashboard :8501, postgres :5432
```
Local development instead:
```bash
pip install fastapi uvicorn python-multipart faster-whisper requests playwright sqlalchemy psycopg2-binary streamlit watchdog
playwright install chromium
uvicorn server:app --host 0.0.0.0 --port 8000
uvicorn stream_server:app --port 8002   # separate terminal
streamlit run dashboard.py --server.port 8501
```

### Configuration / Environment Variables
```
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<db>
N8N_WEBHOOK_URL=https://<your-n8n-host>/webhook/<id>
ENGINE_PORT=8000
DASHBOARD_PORT=8501
WORKERS_COUNT=2
STORAGE_ROOT=/app/storage
USERS_ROOT=/app/users_data
MUSIC_DIR=/app/music
```

### Running
Typical flow:
```bash
# 1) upload assets for a session
curl -F session_id=s1 -F file=@voice.mp3  http://localhost:8000/upload
curl -F session_id=s1 -F file=@img1.jpg   http://localhost:8000/upload
# 2) render a segment (vertical default; pass video_mode=horizontal for landscape)
curl -X POST http://localhost:8000/create-segment -H "Content-Type: application/json" \
     -d '{"session_id":"s1","audio_filename":"<uuid>.mp3","image_filenames":["<uuid>.jpg"],"video_mode":"vertical"}'
# 3) concatenate and mix music
curl -X POST http://localhost:8000/merge-all -H "Content-Type: application/json" \
     -d '{"session_id":"s1","music_filename":"random"}' -o final_video.mp4
# 4) archive it
curl -F session_id=s1 http://localhost:8000/upload-final
```
Schedule posts via `POST /webhook/calendar`; stream results at `http://localhost:8002/stream?path=<file>`.

## Challenges Faced & Solutions
**Challenge**: Captions must land exactly on each spoken word across minutes of narration.
**Solution**: The pipeline uses faster-whisper's `word_timestamps=True`, converts every word into `{word, start, end}` data injected into the template, and lets `updateFrame(t)` pick the active word per rendered frame.

**Challenge**: Screenshotting thousands of frames is slow and memory-heavy in one browser page.
**Solution**: Frames are chunked across `WORKERS_COUNT` parallel pages (`render_chunk`) gathered with `asyncio.gather`, encoded as quality-80 JPEG, deleted immediately after FFmpeg merge, and the whole temp session is cleaned by a background task that preserves only `final_video.mp4`.

**Challenge**: Scheduled posts could silently disappear if the n8n webhook failed.
**Solution**: The 60-second scheduler wraps each dispatch in error handling, logs webhook failures with status codes, keeps the post pending until a 200 response confirms publication, and `migrate_status.py` backfills the required `status` column on existing databases.

**Challenge**: Final videos were sometimes uploaded without a resolvable owner session.
**Solution**: `/upload-final` looks up the `video_jobs` mapping created at scheduling time; unknown sessions are routed to an `unknown_user` debug folder instead of being dropped, keeping artifacts recoverable.

**Challenge**: Serving MP4s naively prevented seeking and broke mobile players.
**Solution**: A dedicated `stream_server.py` implements byte-range parsing with `206 Partial Content`, 1 MB chunks, correct `Content-Range`/`Accept-Ranges` headers, and rejects any path outside the `storage`/`users_data` whitelists (HTTP 416/404 handling included).

## Known Limitations & Roadmap
- Render speed is bounded by full-frame screenshots at 30 FPS; GPU encoding or direct canvas capture would be major wins.
- The compose file ships example database credentials and an external network named `app-network` must exist before `up`.
- Only one worker is enabled by default in code (`WORKERS_COUNT = 1`); compose raises it to 2.
- Roadmap candidates: TTS narration generation, subtitle styling options per template, multi-worker queues, and automated tests for the merge state machine.

## Security Notes
- The dashboard bootstraps a default admin account with well-known credentials and a hardcoded static salt using unsalted SHA-256; change these immediately and move to a proper KDF before exposing the dashboard publicly.
- The streaming whitelist uses prefix matching on absolute paths; tighten to canonical-path comparison to eliminate edge-case traversal concerns.
- Webhook endpoints are unauthenticated; put them behind network controls or add shared-secret validation.
- Released under the MIT License (see [LICENSE](./LICENSE)).

## License
MIT License � Copyright (c) 2026 Musfiqur Rahman Saimon. See [LICENSE](./LICENSE).


---
Keywords: video factory, whisper captions, playwright, ffmpeg, automation, motivational videos

