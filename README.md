# Bazarr Auto-Translate & Acquire

A Python daemon that runs alongside your media stack and automatically fills subtitle gaps in Bazarr using a smart priority pipeline. It bridges Bazarr's built-in tools — Lingarr (translation), WhisperAI (audio transcription), and Embedded Subtitle extraction — into a single coordinated background queue that Bazarr itself has no native way to orchestrate.

> **Why not just use Lingarr or WhisperAI directly?**
> Running them outside Bazarr creates a desync: Bazarr never learns the subtitle exists, so it keeps marking the media as "missing" and will never upgrade it when a better subtitle appears on indexers. This project routes every action through Bazarr's API so that Bazarr registers each subtitle correctly and can upgrade it later.

---

## How It Works

### The Pipeline

Every scan cycle, the daemon checks Bazarr for media with missing subtitles and processes each item through a fixed priority chain. The moment a stage succeeds, the remaining stages are skipped.

```
Missing subtitle detected
         │
         ▼
1. Direct download ──── Is there an embedded subtitle or an online subtitle
                        scoring ≥ MIN_SCORE in the target language?
                        → Yes: download it immediately. Done.
                        → No: continue ↓
         │
         ▼
2. Local translation ── Is there an external base subtitle already on disk
                        (e.g. an English .srt)?
                        → Yes: queue it for Lingarr translation. Done.
                        → No: continue ↓
         │
         ▼
3. Base acquisition ─── Is there an embedded or high-scoring base subtitle
                        online?
                        → Yes: download the base subtitle.
                               On the next scan, step 2 will translate it.
                        → No: continue ↓
         │
         ▼
4. WhisperAI fallback ─ No quality subtitles exist anywhere.
                        → Trigger WhisperAI transcription of the audio track.
                           On the next scan, step 2 will translate it.
```

**Profile Migration (independent):** Optionally, at the start of each scan, the daemon checks if any media item uses a specific Bazarr Language Profile and automatically migrates it to a different one. This is useful when renaming language profiles (e.g. `NO` → `NB`).

---

### Scan Timing

| Event | Default interval | Env var |
|---|---|---|
| Full library scan | every 5 minutes | `INTERVAL_BETWEEN_SCANS` |
| Re-process same video | at most once per hour | `ACTION_COOLDOWN_SECONDS` |
| Translation timeout | 15 minutes | `TRANSLATION_REQUEST_TIMEOUT` |

The one-hour cooldown per video prevents the daemon from hammering Bazarr with the same requests on every scan. A video is re-queued only after the cooldown elapses — for example, after a base subtitle was downloaded and is now ready to translate.

---

### Internal Architecture

The project is organized into focused modules:

```
bazarr_autotranslate/
├── main.py          Entry point. Wires everything together and starts the loop.
├── config.py        Config dataclass. Reads all environment variables in one place.
├── models.py        Bazarr API response models (Serie, Movie, Subtitle...) and
│                    typed task objects (SearchTask, MigrationTask, SubtitleTranslate).
├── client.py        BazarrClient. All async HTTP calls to the Bazarr API.
├── scheduler.py     Orchestrator. Runs the scan loop, builds tasks, handles migrations.
├── workers.py       Three worker classes running in background threads:
│                      TranslationWorker  – calls Lingarr via Bazarr
│                      SearchWorker       – queries providers, triggers downloads
│                      MigrationWorker    – changes language profiles
├── cooldown.py      Thread-safe rate limiter used by the scheduler and search workers.
└── unique_queue.py  Thread-safe queue that silently drops duplicate entries.
```

The main loop (`Orchestrator`) runs in an `asyncio` event loop and makes all Bazarr API calls asynchronously. The three workers run in background daemon threads using a blocking `httpx.Client` — this is intentional because WhisperAI and Lingarr calls can take minutes, and blocking a thread is the right primitive for that workload.

A single persistent `httpx.AsyncClient` is shared across all scan operations so that the TCP connection pool is reused instead of torn down every 5 minutes.

---

### Bazarr API Calls

| Method | Endpoint | Used for |
|---|---|---|
| `GET` | `/api/episodes/wanted` | List episodes with missing subtitles |
| `GET` | `/api/movies/wanted` | List movies with missing subtitles |
| `GET` | `/api/episodes?episodeid[]=…` | Fetch full subtitle list for specific episodes |
| `GET` | `/api/movies?radarrid[]=…` | Fetch full subtitle list for specific movies |
| `GET` | `/api/providers/episodes?episodeid=…` | List available subtitle candidates from all providers |
| `GET` | `/api/providers/movies?radarrid=…` | Same, for movies |
| `POST` | `/api/providers/episodes` | Trigger download of a specific subtitle candidate |
| `POST` | `/api/providers/movies` | Same, for movies |
| `PATCH` | `/api/subtitles` | Trigger translation of an existing subtitle via Lingarr |
| `POST` | `/api/series` | Change language profile for a series |
| `POST` | `/api/movies` | Change language profile for a movie |

Metadata is fetched in chunks of 50 IDs per request (the Bazarr API limit) and those chunks are dispatched in parallel via `asyncio.gather`.

---

### GPU Concurrency

WhisperAI and Lingarr are protected by independent semaphores:

- **`whisper_semaphore`** — prevents two simultaneous WhisperAI transcription jobs.
- **`lingarr_semaphore`** — prevents two simultaneous Lingarr translation calls.

Both can run at the same time (they are independent services), but `NUM_WORKERS > 1` will not cause two of the same kind to overlap.

---

## Use Case: High-Score Subs + WhisperAI Fallback (English-only)

This is one of the most useful configurations. Bazarr always assigns WhisperAI a fixed score of ~66%. If you lower your cutoff to 66%, Bazarr will start accepting poor-quality online subtitles too. If you keep the cutoff high, WhisperAI never runs.

**The fix:** Set `BASE_LANGUAGES=en` and `TO_LANGUAGES=en`. The daemon enforces `MIN_SCORE` for online providers but always bypasses the score requirement for embedded tracks and WhisperAI. You get high-quality online subtitles when available, and a clean WhisperAI transcription when they aren't.

---

## Configuration Reference

All settings are read from environment variables (or a `.env` file in the project root).

### Required

| Variable | Description |
|---|---|
| `BAZARR_BASE_URL` | Full URL to your Bazarr instance, e.g. `http://192.168.1.10:6767` |
| `BAZARR_API_KEY` | Bazarr API key — Settings → General → Security |
| `BASE_LANGUAGES` | Comma-separated ISO 639-1 source language codes, priority-ordered, e.g. `en,fr` |
| `TO_LANGUAGES` | Comma-separated ISO 639-1 target language codes, e.g. `pt,es` |

### Optional

| Variable | Default | Description |
|---|---|---|
| `MIN_SCORE` | `86` | Minimum Bazarr provider score to accept an online subtitle. Embedded and WhisperAI always bypass this. |
| `NUM_WORKERS` | `1` | Parallel worker threads for search and translation. Keep at `1` if your GPU cannot handle concurrent jobs. |
| `INTERVAL_BETWEEN_SCANS` | `300` | Seconds between full library scans. |
| `TRANSLATION_REQUEST_TIMEOUT` | `900` | Seconds to wait for a single Lingarr translation before treating it as failed. |
| `ACTION_COOLDOWN_SECONDS` | `3600` | Minimum seconds between re-processing the same video. Prevents hammering on every scan. |
| `SERIES_SCAN` | `true` | Set to `false` to skip TV show scanning. |
| `MOVIES_SCAN` | `true` | Set to `false` to skip movie scanning. |
| `SOURCE_PROFILE_ID` | — | Bazarr Language Profile ID to migrate *from*. Leave unset to disable migration. |
| `TARGET_PROFILE_ID` | — | Bazarr Language Profile ID to migrate *to*. |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose output, `INFO` for normal operation. |
| `LOG_DIRECTORY` | `logs/` | Directory for rotating daily log files (4 days retained). |

> **Finding Profile IDs:** In Bazarr, go to Settings → Languages → Language Profiles. The ID is visible in the URL when editing a profile, e.g. `…/settings/languages#2`.

---

## Installation

### Prerequisites in Bazarr

Before starting the daemon, make sure the following are configured in Bazarr:

1. **Providers** — At least one subtitle provider enabled (Settings → Providers).
2. **Lingarr** — If you want translation: Lingarr must be installed and configured as a Bazarr provider.
3. **WhisperAI** — If you want audio transcription: the WhisperAI provider must be enabled in Bazarr.
4. **Language Profiles** — Each series and movie must have a Language Profile assigned that includes the target language as *missing* (i.e., not yet filled).
5. **Upgrade subtitles** — Recommended: enable "Upgrade previously downloaded subtitles" in Bazarr so it can replace translated subtitles when a native one appears.

---

### Docker Compose (standalone)

Create a `docker-compose.yml` file:

```yaml
services:
  bazarr-autotranslate:
    image: ghcr.io/salaroli/bazarr_autotranslate:latest
    container_name: bazarr_autotranslate
    restart: unless-stopped
    environment:
      - BAZARR_BASE_URL=http://192.168.1.10:6767
      - BAZARR_API_KEY=your_api_key_here
      - BASE_LANGUAGES=en
      - TO_LANGUAGES=pt
      - MIN_SCORE=86
      - NUM_WORKERS=1
      - INTERVAL_BETWEEN_SCANS=300
      - ACTION_COOLDOWN_SECONDS=3600
      - SERIES_SCAN=true
      - MOVIES_SCAN=true
      - LOG_LEVEL=INFO
    volumes:
      - ./logs:/usr/src/app/logs
```

```bash
docker compose up -d
```

---

### Adding to an existing stack

If Bazarr is already defined in a `docker-compose.yml`, add the service block in the same file so they share the same Docker network and can reach each other by service name:

```yaml
services:
  bazarr:
    image: lscr.io/linuxserver/bazarr:latest
    # ... your existing bazarr config

  bazarr-autotranslate:
    image: ghcr.io/salaroli/bazarr_autotranslate:latest
    container_name: bazarr_autotranslate
    restart: unless-stopped
    environment:
      # Use the service name as hostname when in the same compose network
      - BAZARR_BASE_URL=http://bazarr:6767
      - BAZARR_API_KEY=your_api_key_here
      - BASE_LANGUAGES=en
      - TO_LANGUAGES=pt
      - MIN_SCORE=86
      - LOG_LEVEL=INFO
    volumes:
      - ./logs:/usr/src/app/logs
    depends_on:
      - bazarr
```

---

### Portainer (Stack deployment)

1. In Portainer, go to **Stacks → Add stack**.
2. Give it a name, e.g. `bazarr-autotranslate`.
3. Select **Web editor** and paste the compose below.
4. Scroll down to **Environment variables** and fill in your values there (recommended) — or embed them directly in the editor.
5. Click **Deploy the stack**.

```yaml
services:
  bazarr-autotranslate:
    image: ghcr.io/salaroli/bazarr_autotranslate:latest
    container_name: bazarr_autotranslate
    restart: unless-stopped
    environment:
      - BAZARR_BASE_URL=${BAZARR_BASE_URL}
      - BAZARR_API_KEY=${BAZARR_API_KEY}
      - BASE_LANGUAGES=${BASE_LANGUAGES:-en}
      - TO_LANGUAGES=${TO_LANGUAGES}
      - MIN_SCORE=${MIN_SCORE:-86}
      - NUM_WORKERS=${NUM_WORKERS:-1}
      - INTERVAL_BETWEEN_SCANS=${INTERVAL_BETWEEN_SCANS:-300}
      - ACTION_COOLDOWN_SECONDS=${ACTION_COOLDOWN_SECONDS:-3600}
      - SERIES_SCAN=${SERIES_SCAN:-true}
      - MOVIES_SCAN=${MOVIES_SCAN:-true}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    volumes:
      - /your/host/path/logs:/usr/src/app/logs
    networks:
      - your_existing_network

networks:
  your_existing_network:
    external: true
```

> **Network:** If Bazarr runs in a different stack, set `your_existing_network` to the name of the Docker network that stack uses. In Portainer you can inspect it under **Networks**. Alternatively, use Bazarr's host IP and port instead of the service name.

> **Environment variables in Portainer:** Use the "Environment variables" section at the bottom of the stack editor to define `BAZARR_BASE_URL`, `BAZARR_API_KEY`, and `TO_LANGUAGES` without putting them in the YAML file. This keeps sensitive values out of your stack definition.

---

## Monitoring & Debugging

Logs are written to both stdout (visible in `docker logs bazarr_autotranslate` or Portainer's container log view) and to a rotating daily file under `LOG_DIRECTORY` (4 days retained).

```bash
# Follow live logs
docker logs -f bazarr_autotranslate

# Inspect log files
tail -f ./logs/bazarr_lingarr_autotranslate.log
```

### Log levels

| Level | Set via | What you see |
|---|---|---|
| `INFO` | `LOG_LEVEL=INFO` (default) | Startup banner, connectivity test, actions taken, per-scan summary |
| `DEBUG` | `LOG_LEVEL=DEBUG` | Everything above + reasons why items were skipped, candidate counts |

### Startup output

On every start the daemon prints a configuration summary and immediately tests connectivity to Bazarr:

```
=======================================================
Bazarr Auto-Translate starting
  Bazarr URL    : http://192.168.1.10:6767
  Base langs    : en
  Target langs  : pt
  Min score     : 86
  Workers       : 1
  Scan interval : 300s
  Cooldown      : 3600s
  Series scan   : True
  Movies scan   : True
=======================================================
Connected to Bazarr at http://192.168.1.10:6767
Started 1 worker(s) of each type. Entering scan loop.
[Translate Worker: 0] Started
[Search Worker: 0] Started
[Migration Worker: 0] Started
```

If the URL or API key are wrong you will see one of these errors and the daemon will exit:

```
# Wrong API key
Bazarr returned HTTP 401 — invalid API key or wrong URL? Response: ...

# Bazarr unreachable
Cannot reach Bazarr at http://192.168.1.10:6767: ...
```

### Typical scan output (INFO)

```
2025-01-15 03:00:00 - INFO - Scanning for episodes
2025-01-15 03:00:01 - INFO - Queued Provider Search for Episode ID 1042
2025-01-15 03:00:01 - INFO - Queued Provider Search for Episode ID 1087
2025-01-15 03:00:01 - INFO - Scan done [episodes]: 47 missing, 2 queued, 45 in cooldown
2025-01-15 03:00:01 - INFO - [Search Worker: 0] Querying Providers for Episode ID: 1042
2025-01-15 03:00:02 - INFO - [Search Worker: 0] Found embeddedsubtitles for pt (Score: 0). Direct download...
2025-01-15 03:00:02 - INFO - [Search Worker: 0] Triggered embeddedsubtitles for ID: 1042
2025-01-15 03:00:03 - INFO - [Search Worker: 0] Queued Translate: /media/show/s01e01.en.srt -> pt
2025-01-15 03:00:03 - INFO - [Translate Worker: 0] Translating: /media/show/s01e01.en.srt to: pt
2025-01-15 03:07:45 - INFO - [Translate Worker: 0] Translation finished
```

### Debugging why items are not being processed (DEBUG)

If a video is not being queued and you don't know why, set `LOG_LEVEL=DEBUG`. The two most common causes are logged explicitly:

```
# Item still within the 1-hour cooldown
DEBUG - [episodes] Skipping ID 1042: cooldown not elapsed yet

# Item is already sitting in the search queue from a previous scan
DEBUG - [episodes] Skipping ID 1042: already in search queue

# Translation skipped — already queued
DEBUG - [Search Worker: 0] ID 1042 → pt: translation already in queue

# Translation skipped — cooldown
DEBUG - [Search Worker: 0] ID 1042 → pt: translation cooldown not elapsed

# How many subtitle candidates Bazarr returned for a given video
DEBUG - [Search Worker: 0] 12 candidate(s) returned for ID 1042
```

Debug also shows the raw item counts at the start of each scan:

```
DEBUG - [episodes] 47 item(s) with missing subtitles from Bazarr
```

---

## Important Warnings

**Volume of requests:** The daemon scans your entire library every `INTERVAL_BETWEEN_SCANS` seconds. On a large library the first run will queue a large number of jobs. The one-hour cooldown per video prevents the same video from being retried immediately, but there is no daily global cap.

**Paid APIs:** If Lingarr is connected to a paid translation service (e.g. DeepL), monitor your usage closely during the first few days. Translate a small library first to estimate cost.

**`NUM_WORKERS`:** With `NUM_WORKERS=1` (default), there is exactly one search thread and one translation thread. Increasing it adds more of each — but if your GPU cannot run multiple Lingarr or WhisperAI jobs simultaneously, keep it at `1`. The daemon uses independent semaphores to prevent two concurrent WhisperAI jobs and two concurrent Lingarr jobs regardless of `NUM_WORKERS`, but they can run one of each at the same time.

**Bazarr must be reachable:** The daemon connects to Bazarr at startup and on every scan. If Bazarr restarts, the daemon will recover automatically on the next scan attempt.

---

## Building from source

```bash
git clone https://github.com/salaroli/bazarr_autotranslate.git
cd bazarr_autotranslate
pip install -r requirements.txt
cp .env.example .env   # edit with your values
python main.py
```

The Docker image is built automatically via GitHub Actions on manual dispatch and published to `ghcr.io/salaroli/bazarr_autotranslate`. It supports `linux/amd64` and `linux/arm64`.
