from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING

from client import BazarrClient
from cooldown import CooldownCache
from models import Serie, Movie, SearchTask, MigrationTask
from unique_queue import UniqueQueue

if TYPE_CHECKING:
    from config import Config

logger = logging.getLogger("bazarr_lingarr")
_SUBTITLE_EXTENSIONS = (".srt", ".ass", ".vtt", ".sub")


class Orchestrator:
    def __init__(self, config: Config, client: BazarrClient,
                 search_queue: UniqueQueue, migration_queue: UniqueQueue,
                 cooldown: CooldownCache) -> None:
        self._config = config
        self._client = client
        self._search_queue = search_queue
        self._migration_queue = migration_queue
        self._cooldown = cooldown

    async def run(self) -> None:
        while True:
            try:
                if self._config.series_scan:
                    await self._scan("episodes")
                if self._config.movies_scan:
                    await self._scan("movies")
            except Exception:
                logger.exception("Uncaught exception in scan loop:")
            await asyncio.sleep(self._config.interval_between_scans)

    async def _scan(self, media_type: str) -> None:
        logger.info(f"Scanning for {media_type}")
        await self._process_migrations(media_type)

        wanted = await self._client.get_wanted(media_type)
        if not wanted:
            logger.info(f"No missing subtitles found for {media_type}")
            return

        logger.debug(f"[{media_type}] {len(wanted)} item(s) with missing subtitles from Bazarr")
        tasks = await self._build_search_tasks(media_type, wanted)

        queued = skipped_cooldown = 0
        for task in tasks:
            if self._cooldown.check_and_set(f"search_{task.video_id}"):
                self._search_queue.put(task)
                queued += 1
                logger.info(f"Queued Provider Search for {'Episode' if task.is_serie else 'Movie'} ID {task.video_id}")
            else:
                skipped_cooldown += 1
                logger.debug(f"[{media_type}] Skipping ID {task.video_id}: cooldown not elapsed yet")

        parts = [f"{len(wanted)} missing"]
        if queued:
            parts.append(f"{queued} queued")
        if skipped_cooldown:
            parts.append(f"{skipped_cooldown} in cooldown")
        logger.info(f"Scan done [{media_type}]: {', '.join(parts)}")

    async def _build_search_tasks(self, media_type: str,
                                  wanted: list[Serie | Movie]) -> list[SearchTask]:
        is_serie = media_type == "episodes"
        to_langs = self._config.to_languages_set
        base_langs = self._config.base_languages_set
        base_priority = self._config.base_lang_priority

        id_to_missing: dict[int, list[str]] = {}
        for video in wanted:
            vid_id = video.sonarr_episode_id if is_serie else video.radarr_id
            missing = [s.code2 for s in video.missing_subtitles if s.code2 in to_langs]
            if missing:
                id_to_missing[vid_id] = missing

        if not id_to_missing:
            return []

        metadata = await self._client.get_metadata(media_type, list(id_to_missing))
        if not metadata:
            return []

        id_to_video = {
            (v.sonarr_episode_id if is_serie else v.radarr_id): v for v in metadata
        }

        tasks: list[SearchTask] = []
        for vid_id, missing_langs in id_to_missing.items():
            video = id_to_video.get(vid_id)
            if not video:
                continue

            subtitles = video.subtitles or []
            video_path = getattr(video, "path", None)
            base_subs = [s for s in subtitles if s.code2 in base_langs]
            external = sorted(
                [s for s in base_subs if self._is_external(s, video_path)],
                key=lambda s: base_priority[s.code2],
            )
            series_id = (
                getattr(video, "sonarr_series_id", None) if is_serie else None
            )

            task = SearchTask(
                video_id=vid_id,
                is_serie=is_serie,
                series_id=series_id,
                missing_languages=missing_langs,
                external_base_sub=external[0] if external else None,
            )
            if not self._search_queue.check(task):
                tasks.append(task)
            else:
                logger.debug(f"[{media_type}] Skipping ID {vid_id}: already in search queue")

        return tasks

    async def _process_migrations(self, media_type: str) -> None:
        cfg = self._config
        if not cfg.source_profile_id or not cfg.target_profile_id:
            return

        wanted = await self._client.get_wanted(media_type)
        if not wanted:
            return

        is_serie = media_type == "episodes"
        candidates: list[dict] = []
        for video in wanted:
            vid_id = video.sonarr_episode_id if is_serie else video.radarr_id
            if any(s.code2 == "no" for s in video.missing_subtitles):
                series_id = getattr(video, "sonarr_series_id", None) if is_serie else None
                candidates.append({"vid_id": vid_id, "series_id": series_id})

        if not candidates:
            return

        raw_items = await self._client.get_raw_metadata(media_type, [c["vid_id"] for c in candidates])
        candidate_map = {c["vid_id"]: c for c in candidates}

        for raw in raw_items:
            prof_id = raw.get("language_profile_id") or raw.get("profile_id") or raw.get("profileId")
            if prof_id != cfg.source_profile_id:
                continue

            v_id = raw.get("radarrId") if media_type == "movies" else raw.get("sonarrEpisodeId")
            c = candidate_map.get(v_id)
            if not c:
                continue

            mig_id = v_id if media_type == "movies" else (
                c["series_id"] or raw.get("sonarrSeriesId") or raw.get("seriesId")
            )
            if not mig_id:
                continue

            task = MigrationTask(media_type=media_type, mig_id=mig_id,
                                 target_profile=cfg.target_profile_id)
            if not self._migration_queue.check(task):
                logger.info(f"Queued Profile Migration for {media_type} (Target ID: {mig_id})")
                self._migration_queue.put(task)

    @staticmethod
    def _is_external(sub, video_path: str | None) -> bool:
        if not sub.path:
            return False
        if video_path and sub.path == video_path:
            return False
        return sub.path.lower().endswith(_SUBTITLE_EXTENSIONS)
