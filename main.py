import os
import sys
import time
import httpx
import signal
import asyncio
import logging
import threading
from dotenv import load_dotenv
from typing import List, Optional
from unique_queue import UniqueQueue
from logging.handlers import TimedRotatingFileHandler
from class_types import Serie, Movie, SubtitleTranslate

def get_env_or_default(env, default):
    val = os.getenv(env)
    return val if val is not None else default

def parse_bool_env(env: str, default: bool) -> bool:
    val = os.getenv(env)
    if val is None:
        return default
    return val.strip().lower() not in ("false", "0", "no")

def get_attr_or_key(obj, name):
    if hasattr(obj, name):
        return getattr(obj, name)
    elif isinstance(obj, dict) and name in obj:
        return obj[name]
    else:
        raise AttributeError(f"Missing attribute or key '{name}'")

# Get configuration and setup things
load_dotenv()
base_languages_env = os.getenv("BASE_LANGUAGES")
base_languages = [lang.strip() for lang in base_languages_env.split(",")] if base_languages_env else []

to_languages_env = os.getenv("TO_LANGUAGES")
to_languages = [lang.strip() for lang in to_languages_env.split(",")] if to_languages_env else []

base_languages_set = set(base_languages)
to_languages_set = set(to_languages)
base_lang_priority = {lang: idx for idx, lang in enumerate(base_languages)}

translation_request_timeout = int(get_env_or_default("TRANSLATION_REQUEST_TIMEOUT", 15 * 60))
num_workers = int(get_env_or_default("NUM_WORKERS", 1))
interval_between_scans = int(get_env_or_default("INTERVAL_BETWEEN_SCANS", 5 * 60))
min_score = int(get_env_or_default("MIN_SCORE", 86))
log_level = get_env_or_default("LOG_LEVEL", "INFO")
log_directory = get_env_or_default("LOG_DIRECTORY", "logs/")
series_scan = parse_bool_env("SERIES_SCAN", True)
movies_scan = parse_bool_env("MOVIES_SCAN", True)

# Profile Migration Env Vars
source_profile_id = get_env_or_default("SOURCE_PROFILE_ID", None)
target_profile_id = get_env_or_default("TARGET_PROFILE_ID", None)
if source_profile_id: source_profile_id = int(source_profile_id)
if target_profile_id: target_profile_id = int(target_profile_id)

action_cooldown_cache = {}
action_cooldown_lock = threading.Lock()
ACTION_COOLDOWN_SECONDS = int(get_env_or_default("ACTION_COOLDOWN_SECONDS", 3600))
lingarr_semaphore = threading.Semaphore(1)
whisper_semaphore = threading.Semaphore(1)

key_fn = lambda x: f" {'s' if get_attr_or_key(x, 'is_serie') else 'm'} {get_attr_or_key(x, 'video_id')}_{get_attr_or_key(x, 'to_language')}"
search_key_fn = lambda x: f"search_{'s' if get_attr_or_key(x, 'is_serie') else 'm'}_{get_attr_or_key(x, 'video_id')}"
migration_key_fn = lambda x: f"mig_{x['type']}_{x['mig_id']}"

def check_and_set_cooldown(cache_key: str) -> bool:
    """Returns True and updates timestamp if cooldown has elapsed, False otherwise."""
    current_time = time.time()
    with action_cooldown_lock:
        if current_time - action_cooldown_cache.get(cache_key, 0) > ACTION_COOLDOWN_SECONDS:
            action_cooldown_cache[cache_key] = current_time
            return True
    return False

task_queue = UniqueQueue(key_fn=key_fn)
search_task_queue = UniqueQueue(key_fn=search_key_fn)
migration_queue = UniqueQueue(key_fn=migration_key_fn)
shutdown_event = asyncio.Event()
logger = logging.getLogger("bazarr_lingarr")

# Single persistent async client reused across all scans (avoids connection pool churn)
async_client: Optional[httpx.AsyncClient] = None


async def process_profile_migrations(base_url, api_key, media_type):
    """Independently checks for media missing 'no' and migrates their profile to 'nb'"""
    if not source_profile_id or not target_profile_id:
        return
        
    endpoint = f"{base_url}/api/{media_type}/wanted"
    headers = {"X-API-KEY": api_key}

    try:
        resp = await async_client.get(endpoint, headers=headers, params={"start": 0, "length": -1})
        resp.raise_for_status()
        wanted_data = resp.json().get("data", [])
    except Exception as e:
        logger.error(f"Migration check failed to get wanted {media_type}: {e}")
        return

    candidates = []
    for obj in wanted_data:
        missing = obj.get("missing_subtitles", [])
        if any(isinstance(sub, dict) and sub.get("code2") == "no" for sub in missing):
            vid_id = obj.get("radarrId") if media_type == "movies" else obj.get("sonarrEpisodeId")
            series_id = obj.get("sonarrSeriesId") if media_type == "episodes" else None
            if vid_id:
                candidates.append({"vid_id": vid_id, "series_id": series_id})

    if not candidates:
        return

    meta_endpoint = f"{base_url}/api/{media_type}"
    id_param = "radarrid[]" if media_type == "movies" else "episodeid[]"
    chunk_size = 50
    chunks = [candidates[i:i + chunk_size] for i in range(0, len(candidates), chunk_size)]

    async def fetch_chunk(chunk):
        params = {id_param: [c["vid_id"] for c in chunk]}
        meta_resp = await async_client.get(meta_endpoint, headers=headers, params=params)
        meta_resp.raise_for_status()
        return chunk, meta_resp.json().get("data", [])

    results = await asyncio.gather(*(fetch_chunk(c) for c in chunks), return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Migration metadata fetch failed: {result}")
            continue
        chunk, raw_items = result
        for raw_item in raw_items:
            prof_id = raw_item.get("language_profile_id") or raw_item.get("profile_id") or raw_item.get("profileId")
            if prof_id == source_profile_id:
                v_id = raw_item.get("radarrId") if media_type == "movies" else raw_item.get("sonarrEpisodeId")
                c = next((x for x in chunk if x["vid_id"] == v_id), None)
                if c:
                    mig_id = v_id if media_type == "movies" else c["series_id"]
                    if media_type == "episodes" and not mig_id:
                        mig_id = raw_item.get("sonarrSeriesId") or raw_item.get("seriesId")

                    if mig_id and not migration_queue.check({"type": media_type, "mig_id": mig_id}):
                        logger.info(f"Queued Profile Migration for {media_type} (Target ID: {mig_id})")
                        migration_queue.put({
                            "type": media_type,
                            "mig_id": mig_id,
                            "target_profile": target_profile_id
                        })

async def get_episodes_metadata(base_url: str, api_key: str, episode_ids: Optional[List[int]] = None) -> List[Serie] | None:
    endpoint = f"{base_url}/api/episodes"
    headers = {"X-API-KEY": api_key}
    try:
        if not episode_ids:
            response = await async_client.get(endpoint, headers=headers)
            response.raise_for_status()
            return [Serie.from_dict(obj) for obj in response.json()["data"]]

        chunk_size = 50
        chunks = [episode_ids[i:i + chunk_size] for i in range(0, len(episode_ids), chunk_size)]

        async def fetch(chunk):
            response = await async_client.get(endpoint, headers=headers, params={"episodeid[]": chunk})
            response.raise_for_status()
            return [Serie.from_dict(obj) for obj in response.json()["data"]]

        results = await asyncio.gather(*(fetch(c) for c in chunks))
        return [serie for batch in results for serie in batch]
    except Exception:
        logger.exception("Error while getting episode metadata:")
        return None

async def get_wanted_episodes(base_url: str, api_key: str) -> List[Serie] | None:
    endpoint = f"{base_url}/api/episodes/wanted"
    headers = {"X-API-KEY": api_key}
    try:
        response = await async_client.get(endpoint, headers=headers, params={"start": 0, "length": -1})
        response.raise_for_status()
        return [Serie.from_dict(obj) for obj in response.json()["data"]]
    except Exception:
        logger.exception("Error while getting wanted episodes:")
        return None

async def get_movies_metadata(base_url: str, api_key: str, movie_ids: Optional[List[int]] = None) -> List[Movie] | None:
    endpoint = f"{base_url}/api/movies"
    headers = {"X-API-KEY": api_key}
    try:
        if not movie_ids:
            response = await async_client.get(endpoint, headers=headers)
            response.raise_for_status()
            return [Movie.from_dict(obj) for obj in response.json()["data"]]

        chunk_size = 50
        chunks = [movie_ids[i:i + chunk_size] for i in range(0, len(movie_ids), chunk_size)]

        async def fetch(chunk):
            response = await async_client.get(endpoint, headers=headers, params={"radarrid[]": chunk})
            response.raise_for_status()
            return [Movie.from_dict(obj) for obj in response.json()["data"]]

        results = await asyncio.gather(*(fetch(c) for c in chunks))
        return [movie for batch in results for movie in batch]
    except Exception:
        logger.exception("Error while getting movies metadata:")
        return None

async def get_wanted_movies(base_url: str, api_key: str) -> List[Movie] | None:
    endpoint = f"{base_url}/api/movies/wanted"
    headers = {"X-API-KEY": api_key}
    try:
        response = await async_client.get(endpoint, headers=headers, params={"start": 0, "length": -1})
        response.raise_for_status()
        return [Movie.from_dict(obj) for obj in response.json()["data"]]
    except Exception:
        logger.exception("Error while getting wanted movies:")
        return None

def is_external_subtitle(sub, video_path) -> bool:
    if not sub.path:
        return False
    if video_path and sub.path == video_path:
        return False
    if sub.path.lower().endswith(('.srt', '.ass', '.vtt', '.sub')):
        return True
    return False

async def find_subtitles_to_process(base_url, api_key, videos: List[Serie] | List[Movie]):
    video_id_language_map = {}
    for video in videos:
        video_id = video.sonarr_episode_id if isinstance(video, Serie) else video.radarr_id
        missing_langs = []
        for missing_sub in video.missing_subtitles:
            if missing_sub.code2 in to_languages_set:
                missing_langs.append(missing_sub.code2)
        if missing_langs:
            video_id_language_map[video_id] = missing_langs

    if not video_id_language_map:
        return []

    metadata = None
    if isinstance(videos[0], Serie):
        metadata = await get_episodes_metadata(base_url, api_key, episode_ids=list(video_id_language_map.keys()))
    else:
        metadata = await get_movies_metadata(base_url, api_key, movie_ids=list(video_id_language_map.keys()))

    if not metadata:
        return []
    
    video_id_to_video_map = { (v.sonarr_episode_id if isinstance(v, Serie) else v.radarr_id): v for v in metadata }
    
    items_to_process = []

    for video_id, missing_langs in video_id_language_map.items():
        video = video_id_to_video_map.get(video_id)
        if not video:
            continue

        if video.subtitles is None:
            video.subtitles = []

        base_subs = [sub for sub in video.subtitles if sub.code2 in base_languages_set]
        external_base_subs = [sub for sub in base_subs if is_external_subtitle(sub, getattr(video, 'path', None))]

        if external_base_subs:
            external_base_subs.sort(key=lambda x: base_lang_priority[x.code2])

        series_id = getattr(video, 'sonarr_series_id', None) or getattr(video, 'series_id', None) if isinstance(video, Serie) else None
        
        if not search_task_queue.check({"is_serie": isinstance(video, Serie), "video_id": video_id}):
            items_to_process.append({
                "video_id": video_id, 
                "is_serie": isinstance(video, Serie),
                "series_id": series_id,
                "missing_languages": missing_langs,
                "external_base_sub": external_base_subs[0] if external_base_subs else None
            })

    return items_to_process


def migration_worker(worker_id, base_url, api_key):
    headers = {"X-API-KEY": api_key}
    with httpx.Client(timeout=15) as client:
        while True:
            item = None
            try:
                item = migration_queue.get()
                media_type = item["type"]
                mig_id = item["mig_id"]
                target_profile = item["target_profile"]
                
                if media_type == "movies":
                    endpoint = f"{base_url}/api/movies"
                    params = {"radarrid": mig_id, "profileid": target_profile}
                else:
                    endpoint = f"{base_url}/api/series"
                    params = {"seriesid": mig_id, "profileid": target_profile}
                    
                logger.info(f"[Migration Worker: {worker_id}] Changing profile for {media_type} ID {mig_id} to Profile {target_profile}")
                response = client.post(endpoint, headers=headers, params=params)
                response.raise_for_status()
                logger.info(f"[Migration Worker: {worker_id}] Profile changed successfully!")

            except Exception:
                logger.exception(f"[Migration Worker: {worker_id}] Error in profile migration:")
            finally:
                if item: migration_queue.done(item)

def translation_worker(worker_id, base_url, api_key):
    endpoint = f"{base_url}/api/subtitles"
    headers = {"X-API-KEY": api_key}
    with httpx.Client(timeout=translation_request_timeout) as client:
        while True:
            sub = None
            try:
                sub = task_queue.get()
                logger.info(f"[Translate Worker: {worker_id}] Translating: {sub.base_subtitle.path} to: {sub.to_language}")

                # Bazarr's /api/subtitles PATCH compares these with `== 'True'` (case-sensitive),
                # so they must be sent as the exact strings "True"/"False", not Python bools
                # (httpx serializes bool True as lowercase "true", which Bazarr reads as False).
                params = {
                    "action": "translate",
                    "language": sub.to_language,
                    "path": sub.base_subtitle.path,
                    "type": "episode" if sub.is_serie else "movie",
                    "id": sub.video_id,
                    "forced": "True" if sub.base_subtitle.forced else "False",
                    "hi": "True" if sub.base_subtitle.hi else "False",
                    "original_format": "True",
                }
                with lingarr_semaphore:
                    response = client.patch(endpoint, headers=headers, params=params)
                    response.raise_for_status()
                logger.info(f"[Translate Worker: {worker_id}] Translation finished")

            except Exception:
                logger.exception(f"[Translate Worker: {worker_id}] Error in translation:")
            finally:
                if sub: task_queue.done(sub)

def search_worker(worker_id, base_url, api_key):
    headers = {"X-API-KEY": api_key}
    with httpx.Client(timeout=translation_request_timeout) as client:
        while True:
            item = None
            try:
                item = search_task_queue.get()
                is_serie = item["is_serie"]
                video_id = item["video_id"]
                series_id = item.get("series_id")
                missing_languages = item.get("missing_languages", [])
                external_base_sub = item.get("external_base_sub")
                
                logger.info(f"[Search Worker: {worker_id}] Querying Providers for {'Episode' if is_serie else 'Movie'} ID: {video_id}")
                
                if is_serie:
                    get_endpoint = f"{base_url}/api/providers/episodes"
                    get_params = {"episodeid": video_id}
                else:
                    get_endpoint = f"{base_url}/api/providers/movies"
                    get_params = {"radarrid": video_id}

                get_response = client.get(get_endpoint, headers=headers, params=get_params)
                get_response.raise_for_status()
                data = get_response.json().get("data", [])
                
                def trigger_download(sub_candidate, use_gpu=False):
                    hi_flag = "true" if str(sub_candidate.get("hearing_impaired", "False")).lower() == "true" else "false"
                    forced_flag = "true" if str(sub_candidate.get("forced", "False")).lower() == "true" else "false"

                    post_params = {
                        "hi": hi_flag,
                        "forced": forced_flag,
                        "original_format": "true",
                        "provider": sub_candidate.get("provider"),
                        "subtitle": sub_candidate.get("subtitle")
                    }

                    if is_serie:
                        post_endpoint = f"{base_url}/api/providers/episodes"
                        post_params.update({"seriesid": series_id, "episodeid": video_id})
                    else:
                        post_endpoint = f"{base_url}/api/providers/movies"
                        post_params.update({"radarrid": video_id})

                    if use_gpu:
                        with whisper_semaphore:
                            post_response = client.post(post_endpoint, headers=headers, params=post_params)
                            post_response.raise_for_status()
                    else:
                        post_response = client.post(post_endpoint, headers=headers, params=post_params)
                        post_response.raise_for_status()
                    logger.info(f"[Search Worker: {worker_id}] Successfully triggered {sub_candidate['provider']} for ID: {video_id}")

                for target_lang in missing_languages:
                    # 1. Look for good Target Language candidates first (so we can avoid translation entirely)
                    target_candidates = [
                        c for c in data 
                        if c.get("language") == target_lang and (
                            c.get("provider") == "embeddedsubtitles" or c.get("score", 0) >= min_score
                        )
                    ]
                    
                    if target_candidates:
                        target_candidates.sort(key=lambda c: (0 if c.get("provider") == "embeddedsubtitles" else 1, -c.get("score", 0)))
                        best_target = target_candidates[0]
                        logger.info(f"[Search Worker: {worker_id}] Found valid {best_target['provider']} candidate for {target_lang} (Score: {best_target.get('score', 0)}). Triggering direct download...")
                        trigger_download(best_target)
                        continue
                        
                    # 2. If no target candidate, check if we already have an external base sub downloaded to translate
                    if external_base_sub:
                        sub_trans = SubtitleTranslate(external_base_sub, target_lang, video_id, is_serie)
                        if not task_queue.check(sub_trans):
                            if check_and_set_cooldown(f"trans_{video_id}_{target_lang}"):
                                task_queue.put(sub_trans)
                                logger.info(f"[Search Worker: {worker_id}] Queued Translate: {external_base_sub.path} -> {target_lang}")
                        continue

                    # 3. If no external base sub, check for valid Base Language candidates to extract/download/whisper
                    base_candidates = [
                        c for c in data
                        if c.get("language") in base_languages_set and (
                            c.get("provider") in ("embeddedsubtitles", "whisperai") or c.get("score", 0) >= min_score
                        )
                    ]

                    if base_candidates:
                        def base_sort_key(c):
                            prov = c.get("provider")
                            prov_priority = 0 if prov == "embeddedsubtitles" else 2 if prov == "whisperai" else 1
                            lang_priority = base_lang_priority.get(c.get("language"), 99)
                            score_priority = -c.get("score", 0)
                            return (prov_priority, lang_priority, score_priority)

                        base_candidates.sort(key=base_sort_key)
                        best_base = base_candidates[0]

                        if check_and_set_cooldown(f"base_dl_{video_id}_{best_base['provider']}"):
                            is_whisper = best_base.get("provider") == "whisperai"
                            logger.info(f"[Search Worker: {worker_id}] Found valid {best_base['provider']} base candidate for {best_base.get('language')} (Score: {best_base.get('score', 0)}). Triggering...")
                            trigger_download(best_base, use_gpu=is_whisper)
                    else:
                        logger.info(f"[Search Worker: {worker_id}] No valid base or target candidates found for ID: {video_id} (Target: {target_lang})")

            except Exception:
                logger.exception(f"[Search Worker: {worker_id}] Error in provider search/download:")
            finally:
                if item: search_task_queue.done(item)

async def scan_and_process(base_url, api_key, media_type="episodes"):
    logger.info(f"Scanning for {media_type}")
    
    # Run profile migrations separately
    await process_profile_migrations(base_url, api_key, media_type)
    
    if media_type == "episodes":
        items = await get_wanted_episodes(base_url, api_key)
    else:
        items = await get_wanted_movies(base_url, api_key)

    if not items:
        logger.info(f"Found no missing subtitles for {media_type}")
        return
    
    items_to_process = await find_subtitles_to_process(base_url, api_key, items)
    
    for item in items_to_process:
        if check_and_set_cooldown(f"search_{item['video_id']}"):
            search_task_queue.put(item)
            logger.info(f"Queued Provider Search for {'Episode' if item['is_serie'] else 'Movie'} ID {item['video_id']}")

async def main(base_url, api_key):
    global async_client

    for i in range(num_workers):
        threading.Thread(target=translation_worker, args=(i, base_url, api_key), daemon=True).start()
        threading.Thread(target=search_worker, args=(i, base_url, api_key), daemon=True).start()
        threading.Thread(target=migration_worker, args=(i, base_url, api_key), daemon=True).start()

    async with httpx.AsyncClient(timeout=60) as client:
        async_client = client
        while not shutdown_event.is_set():
            try:
                if series_scan: await scan_and_process(base_url, api_key, "episodes")
                if movies_scan: await scan_and_process(base_url, api_key, "movies")
            except Exception:
                logger.exception("Uncaught exception:")

            await asyncio.sleep(interval_between_scans)

def handle_shutdown():
    logger.info("Received exit signal")
    sys.exit(1)

if __name__ == "__main__":
    base_url = os.getenv("BAZARR_BASE_URL")
    api_key = os.getenv("BAZARR_API_KEY")

    if not base_url or not api_key:
        print("BAZARR_BASE_URL or BAZARR_API_KEY missing")
        sys.exit(1)

    os.makedirs(log_directory, exist_ok=True)
    file_handler = TimedRotatingFileHandler(os.path.join(log_directory, "bazarr_lingarr_autotranslate.log"), when="midnight", interval=1, backupCount=4)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(console_handler)

    logger.setLevel(logging.INFO if log_level.lower() == "info" else logging.DEBUG)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown)
    loop.run_until_complete(main(base_url, api_key))
