from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import logging


# --- JSON parsing helpers ---

def from_str(x: Any) -> Optional[str]:
    return None if x is None else str(x)

def from_bool(x: Any) -> bool:
    assert isinstance(x, bool)
    return x

def from_int(x: Any) -> int:
    if x is None:
        return 0
    try:
        return int(x)
    except Exception:
        return 0

def from_none(x: Any) -> None:
    assert x is None
    return x

def from_list(f, x: Any) -> list:
    if not isinstance(x, list):
        return []
    result = []
    for item in x:
        try:
            result.append(f(item))
        except Exception:
            logging.getLogger("bazarr_lingarr").exception(
                f"Skipped parsing a list item due to strict typing. Item data: {item}"
            )
    return result

def from_union(fs, x: Any) -> Any:
    for f in fs:
        try:
            return f(x)
        except Exception:
            pass
    logging.getLogger("bazarr_lingarr").warning(
        f"Type assertion failed for data. Returning None. Data: {x}"
    )
    return None


# --- Bazarr API models ---

class MissingSubtitle:
    def __init__(self, name: Optional[str], code2: Optional[str], code3: Optional[str],
                 forced: bool, hi: bool) -> None:
        self.name = name
        self.code2 = code2
        self.code3 = code3
        self.forced = forced
        self.hi = hi

    @staticmethod
    def from_dict(obj: Any) -> MissingSubtitle:
        assert isinstance(obj, dict)
        return MissingSubtitle(
            name=from_str(obj.get("name")),
            code2=from_str(obj.get("code2")),
            code3=from_str(obj.get("code3")),
            forced=from_bool(obj.get("forced")),
            hi=from_bool(obj.get("hi")),
        )


class Subtitle:
    def __init__(self, name: Optional[str], code2: Optional[str], code3: Optional[str],
                 path: Optional[str], forced: bool, hi: bool,
                 file_size: Optional[int]) -> None:
        self.name = name
        self.code2 = code2
        self.code3 = code3
        self.path = path
        self.forced = forced
        self.hi = hi
        self.file_size = file_size

    @staticmethod
    def from_dict(obj: Any) -> Subtitle:
        assert isinstance(obj, dict)
        return Subtitle(
            name=from_str(obj.get("name")),
            code2=from_str(obj.get("code2")),
            code3=from_str(obj.get("code3")),
            path=from_str(obj.get("path")),
            forced=from_bool(obj.get("forced")),
            hi=from_bool(obj.get("hi")),
            file_size=from_union([from_int, from_none], obj.get("file_size")),
        )


class Serie:
    def __init__(self, missing_subtitles: list[MissingSubtitle], monitored: Optional[bool],
                 sonarr_episode_id: int, sonarr_series_id: int,
                 subtitles: Optional[list[Subtitle]], title: Optional[str],
                 series_title: Optional[str], episode_number: Optional[str],
                 episode_title: Optional[str]) -> None:
        self.missing_subtitles = missing_subtitles
        self.monitored = monitored
        self.sonarr_episode_id = sonarr_episode_id
        self.sonarr_series_id = sonarr_series_id
        self.subtitles = subtitles
        self.title = title
        self.series_title = series_title
        self.episode_number = episode_number
        self.episode_title = episode_title

    @staticmethod
    def from_dict(obj: Any) -> Serie:
        assert isinstance(obj, dict)
        return Serie(
            missing_subtitles=from_list(MissingSubtitle.from_dict, obj.get("missing_subtitles")),
            monitored=from_union([from_bool, from_none], obj.get("monitored")),
            sonarr_episode_id=from_int(obj.get("sonarrEpisodeId")),
            sonarr_series_id=from_int(obj.get("sonarrSeriesId")),
            subtitles=from_union([lambda x: from_list(Subtitle.from_dict, x), from_none], obj.get("subtitles")),
            title=from_union([from_str, from_none], obj.get("title")),
            series_title=from_union([from_str, from_none], obj.get("seriesTitle")),
            episode_number=from_union([from_str, from_none], obj.get("episode_number")),
            episode_title=from_union([from_str, from_none], obj.get("episodeTitle")),
        )


class Movie:
    def __init__(self, title: str, missing_subtitles: list[MissingSubtitle], radarr_id: int,
                 monitored: Optional[bool], path: Optional[str],
                 subtitles: Optional[list[Subtitle]]) -> None:
        self.title = title
        self.missing_subtitles = missing_subtitles
        self.radarr_id = radarr_id
        self.monitored = monitored
        self.path = path
        self.subtitles = subtitles

    @staticmethod
    def from_dict(obj: Any) -> Movie:
        assert isinstance(obj, dict)
        return Movie(
            title=from_str(obj.get("title")),
            missing_subtitles=from_list(MissingSubtitle.from_dict, obj.get("missing_subtitles")),
            radarr_id=from_int(obj.get("radarrId")),
            monitored=from_union([from_bool, from_none], obj.get("monitored")),
            path=from_union([from_str, from_none], obj.get("path")),
            subtitles=from_union([lambda x: from_list(Subtitle.from_dict, x), from_none], obj.get("subtitles")),
        )


# --- Queue task types ---

@dataclass
class SubtitleTranslate:
    base_subtitle: Subtitle
    to_language: str
    video_id: int
    is_serie: bool

    @property
    def queue_key(self) -> str:
        return f"{'s' if self.is_serie else 'm'}_{self.video_id}_{self.to_language}"


@dataclass
class SearchTask:
    video_id: int
    is_serie: bool
    series_id: Optional[int]
    missing_languages: list[str]
    external_base_sub: Optional[Subtitle]

    @property
    def queue_key(self) -> str:
        return f"search_{'s' if self.is_serie else 'm'}_{self.video_id}"


@dataclass
class MigrationTask:
    media_type: str
    mig_id: int
    target_profile: int

    @property
    def queue_key(self) -> str:
        return f"mig_{self.media_type}_{self.mig_id}"
