from __future__ import annotations
import logging
import threading
from typing import Callable, TYPE_CHECKING

import httpx

from cooldown import CooldownCache
from models import SubtitleTranslate, SearchTask, MigrationTask
from unique_queue import UniqueQueue

if TYPE_CHECKING:
    from config import Config

logger = logging.getLogger("bazarr_lingarr")


class TranslationWorker:
    def __init__(self, worker_id: int, config: Config,
                 queue: UniqueQueue, semaphore: threading.Semaphore) -> None:
        self._id = worker_id
        self._endpoint = f"{config.bazarr_base_url}/api/subtitles"
        self._headers = {"X-API-KEY": config.bazarr_api_key}
        self._timeout = config.translation_timeout
        self._queue = queue
        self._semaphore = semaphore

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def _run(self) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            while True:
                sub: SubtitleTranslate | None = None
                try:
                    sub = self._queue.get()
                    logger.info(f"[Translate Worker: {self._id}] Translating: {sub.base_subtitle.path} to: {sub.to_language}")
                    params = {
                        "action": "translate",
                        "language": sub.to_language,
                        "path": sub.base_subtitle.path,
                        "type": "episode" if sub.is_serie else "movie",
                        "id": sub.video_id,
                        # Bazarr PATCH /api/subtitles compares with == 'True' (case-sensitive).
                        # httpx serializes Python bool True as "true", which Bazarr reads as False.
                        "forced": "True" if sub.base_subtitle.forced else "False",
                        "hi": "True" if sub.base_subtitle.hi else "False",
                        "original_format": "True",
                    }
                    with self._semaphore:
                        client.patch(self._endpoint, headers=self._headers, params=params).raise_for_status()
                    logger.info(f"[Translate Worker: {self._id}] Translation finished")
                except Exception:
                    logger.exception(f"[Translate Worker: {self._id}] Error in translation:")
                finally:
                    if sub:
                        self._queue.done(sub)


class SearchWorker:
    def __init__(self, worker_id: int, config: Config,
                 search_queue: UniqueQueue, translation_queue: UniqueQueue,
                 whisper_semaphore: threading.Semaphore,
                 cooldown: CooldownCache) -> None:
        self._id = worker_id
        self._base_url = config.bazarr_base_url
        self._headers = {"X-API-KEY": config.bazarr_api_key}
        self._timeout = config.translation_timeout
        self._min_score = config.min_score
        self._base_languages_set = config.base_languages_set
        self._base_lang_priority = config.base_lang_priority
        self._search_queue = search_queue
        self._translation_queue = translation_queue
        self._whisper_semaphore = whisper_semaphore
        self._cooldown = cooldown

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def _run(self) -> None:
        with httpx.Client(timeout=self._timeout) as client:
            while True:
                task: SearchTask | None = None
                try:
                    task = self._search_queue.get()
                    self._process(client, task)
                except Exception:
                    logger.exception(f"[Search Worker: {self._id}] Error in provider search/download:")
                finally:
                    if task:
                        self._search_queue.done(task)

    def _process(self, client: httpx.Client, task: SearchTask) -> None:
        if task.is_serie:
            endpoint = f"{self._base_url}/api/providers/episodes"
            params = {"episodeid": task.video_id}
        else:
            endpoint = f"{self._base_url}/api/providers/movies"
            params = {"radarrid": task.video_id}

        logger.info(f"[Search Worker: {self._id}] Querying Providers for {'Episode' if task.is_serie else 'Movie'} ID: {task.video_id}")
        resp = client.get(endpoint, headers=self._headers, params=params)
        resp.raise_for_status()
        candidates = resp.json().get("data", [])

        for target_lang in task.missing_languages:
            self._handle_language(client, task, candidates, target_lang)

    def _handle_language(self, client: httpx.Client, task: SearchTask,
                         candidates: list, target_lang: str) -> None:
        # Priority 1: direct target language candidate (embedded or high score)
        target = [
            c for c in candidates
            if c.get("language") == target_lang and (
                c.get("provider") == "embeddedsubtitles" or c.get("score", 0) >= self._min_score
            )
        ]
        if target:
            target.sort(key=lambda c: (0 if c.get("provider") == "embeddedsubtitles" else 1, -c.get("score", 0)))
            best = target[0]
            logger.info(f"[Search Worker: {self._id}] Found {best['provider']} for {target_lang} (Score: {best.get('score', 0)}). Direct download...")
            self._trigger(client, task, best)
            return

        # Priority 2: external base subtitle exists → queue for Lingarr translation
        if task.external_base_sub:
            sub_trans = SubtitleTranslate(task.external_base_sub, target_lang, task.video_id, task.is_serie)
            if not self._translation_queue.check(sub_trans):
                if self._cooldown.check_and_set(f"trans_{task.video_id}_{target_lang}"):
                    self._translation_queue.put(sub_trans)
                    logger.info(f"[Search Worker: {self._id}] Queued Translate: {task.external_base_sub.path} -> {target_lang}")
            return

        # Priority 3: download base language candidate (to translate next cycle)
        base = [
            c for c in candidates
            if c.get("language") in self._base_languages_set and (
                c.get("provider") in ("embeddedsubtitles", "whisperai") or c.get("score", 0) >= self._min_score
            )
        ]
        if base:
            base.sort(key=lambda c: (
                0 if c.get("provider") == "embeddedsubtitles" else 2 if c.get("provider") == "whisperai" else 1,
                self._base_lang_priority.get(c.get("language"), 99),
                -c.get("score", 0),
            ))
            best_base = base[0]
            if self._cooldown.check_and_set(f"base_dl_{task.video_id}_{best_base['provider']}"):
                logger.info(f"[Search Worker: {self._id}] Found {best_base['provider']} base for {best_base.get('language')} (Score: {best_base.get('score', 0)}). Triggering...")
                self._trigger(client, task, best_base, use_whisper_sem=best_base.get("provider") == "whisperai")
        else:
            logger.info(f"[Search Worker: {self._id}] No candidates found for ID: {task.video_id} (Target: {target_lang})")

    def _trigger(self, client: httpx.Client, task: SearchTask,
                 candidate: dict, use_whisper_sem: bool = False) -> None:
        hi_flag = "true" if str(candidate.get("hearing_impaired", "False")).lower() == "true" else "false"
        forced_flag = "true" if str(candidate.get("forced", "False")).lower() == "true" else "false"
        base_params = {
            "hi": hi_flag,
            "forced": forced_flag,
            "original_format": "true",
            "provider": candidate.get("provider"),
            "subtitle": candidate.get("subtitle"),
        }

        if task.is_serie:
            endpoint = f"{self._base_url}/api/providers/episodes"
            params = {**base_params, "seriesid": task.series_id, "episodeid": task.video_id}
        else:
            endpoint = f"{self._base_url}/api/providers/movies"
            params = {**base_params, "radarrid": task.video_id}

        if use_whisper_sem:
            with self._whisper_semaphore:
                client.post(endpoint, headers=self._headers, params=params).raise_for_status()
        else:
            client.post(endpoint, headers=self._headers, params=params).raise_for_status()

        logger.info(f"[Search Worker: {self._id}] Triggered {candidate['provider']} for ID: {task.video_id}")


class MigrationWorker:
    def __init__(self, worker_id: int, config: Config, queue: UniqueQueue) -> None:
        self._id = worker_id
        self._base_url = config.bazarr_base_url
        self._headers = {"X-API-KEY": config.bazarr_api_key}
        self._queue = queue

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def _run(self) -> None:
        with httpx.Client(timeout=15) as client:
            while True:
                task: MigrationTask | None = None
                try:
                    task = self._queue.get()
                    if task.media_type == "movies":
                        endpoint = f"{self._base_url}/api/movies"
                        params = {"radarrid": task.mig_id, "profileid": task.target_profile}
                    else:
                        endpoint = f"{self._base_url}/api/series"
                        params = {"seriesid": task.mig_id, "profileid": task.target_profile}

                    logger.info(f"[Migration Worker: {self._id}] Changing profile for {task.media_type} ID {task.mig_id} to Profile {task.target_profile}")
                    client.post(endpoint, headers=self._headers, params=params).raise_for_status()
                    logger.info(f"[Migration Worker: {self._id}] Profile changed successfully!")
                except Exception:
                    logger.exception(f"[Migration Worker: {self._id}] Error in profile migration:")
                finally:
                    if task:
                        self._queue.done(task)
