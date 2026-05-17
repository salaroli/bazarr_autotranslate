from typing import Any, List, Optional
import logging


def from_str(x):
    if x is None:
        return None
    return str(x)

def from_bool(x: Any) -> bool:
    assert isinstance(x, bool)
    return x

def from_list(f, x):
    if not isinstance(x, list):
        return []
    res = []
    for y in x:
        try:
            res.append(f(y))
        except Exception:
            # Logs the full trace AND the raw subtitle dict that was rejected
            logging.getLogger("bazarr_lingarr").exception(f"Skipped parsing a list item due to strict typing. Item data: {y}")
    return res

def from_union(fs, x):
    for f in fs:
        try:
            return f(x)
        except Exception:
            pass
    logging.getLogger("bazarr_lingarr").warning(f"Type assertion failed for data. Returning None. Data: {x}")
    return None

def from_int(x) -> int:
    if x is None:
        return 0
    try:
        return int(x)
    except Exception:
        return 0

def from_none(x: Any) -> Any:
    assert x is None
    return x


class MissingSubtitle:
    name: str
    code2: str
    code3: str
    forced: bool
    hi: bool

    def __init__(self, name: str, code2: str, code3: str, forced: bool, hi: bool) -> None:
        self.name = name
        self.code2 = code2
        self.code3 = code3
        self.forced = forced
        self.hi = hi

    @staticmethod
    def from_dict(obj: Any) -> 'MissingSubtitle':
        assert isinstance(obj, dict)
        name = from_str(obj.get("name"))
        code2 = from_str(obj.get("code2"))
        code3 = from_str(obj.get("code3"))
        forced = from_bool(obj.get("forced"))
        hi = from_bool(obj.get("hi"))
        return MissingSubtitle(name, code2, code3, forced, hi)


class Subtitle:
    name: str
    code2: str
    code3: str
    path: str
    forced: bool
    hi: bool
    file_size: Optional[int]

    def __init__(self, name: str, code2: str, code3: str, path: str, forced: bool, hi: bool, file_size: Optional[int]) -> None:
        self.name = name
        self.code2 = code2
        self.code3 = code3
        self.path = path
        self.forced = forced
        self.hi = hi
        self.file_size = file_size

    @staticmethod
    def from_dict(obj: Any) -> 'Subtitle':
        assert isinstance(obj, dict)
        name = from_str(obj.get("name"))
        code2 = from_str(obj.get("code2"))
        code3 = from_str(obj.get("code3"))
        path = from_str(obj.get("path"))
        forced = from_bool(obj.get("forced"))
        hi = from_bool(obj.get("hi"))
        file_size = from_union([from_int, from_none], obj.get("file_size"))
        return Subtitle(name, code2, code3, path, forced, hi, file_size)


class Serie:
    missing_subtitles: List[MissingSubtitle]
    monitored: Optional[bool]
    path: Optional[str]
    sonarr_episode_id: int
    sonarr_series_id: int
    subtitles: Optional[List[Subtitle]]
    title: Optional[str]
    series_title: Optional[str]
    episode_number: Optional[str]
    episode_title: Optional[str]

    def __init__(self, missing_subtitles: List[MissingSubtitle], monitored: Optional[bool], sonarr_episode_id: int, sonarr_series_id: int, subtitles: Optional[List[Subtitle]], title: Optional[str], series_title: Optional[str], episode_number: Optional[str], episode_title: Optional[str]) -> None:
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
    def from_dict(obj: Any) -> 'Serie':
        assert isinstance(obj, dict)
        missing_subtitles = from_list(MissingSubtitle.from_dict, obj.get("missing_subtitles"))
        monitored = from_union([from_bool, from_none], obj.get("monitored"))
        sonarr_episode_id = from_int(obj.get("sonarrEpisodeId"))
        sonarr_series_id = from_int(obj.get("sonarrSeriesId"))
        subtitles = from_union([lambda x: from_list(Subtitle.from_dict, x), from_none], obj.get("subtitles"))
        title = from_union([from_str, from_none], obj.get("title"))
        series_title = from_union([from_str, from_none], obj.get("seriesTitle"))
        episode_number = from_union([from_str, from_none], obj.get("episode_number"))
        episode_title = from_union([from_str, from_none], obj.get("episodeTitle"))
        return Serie(missing_subtitles, monitored, sonarr_episode_id, sonarr_series_id, subtitles, title, series_title, episode_number, episode_title)


class Movie:
    title: str
    missing_subtitles: List[MissingSubtitle]
    radarr_id: int
    monitored: Optional[bool]
    path: Optional[str]
    subtitles: Optional[List[Subtitle]]

    def __init__(self, title: str, missing_subtitles: List[MissingSubtitle], radarr_id: int, monitored: Optional[bool], path: Optional[str], subtitles: Optional[List[Subtitle]]) -> None:
        self.title = title
        self.missing_subtitles = missing_subtitles
        self.radarr_id = radarr_id
        self.monitored = monitored
        self.path = path
        self.subtitles = subtitles

    @staticmethod
    def from_dict(obj: Any) -> 'Movie':
        assert isinstance(obj, dict)
        title = from_str(obj.get("title"))
        missing_subtitles = from_list(MissingSubtitle.from_dict, obj.get("missing_subtitles"))
        radarr_id = from_int(obj.get("radarrId"))
        monitored = from_union([from_bool, from_none], obj.get("monitored"))
        path = from_union([from_str, from_none], obj.get("path"))
        subtitles = from_union([lambda x: from_list(Subtitle.from_dict, x), from_none], obj.get("subtitles"))
        return Movie(title, missing_subtitles, radarr_id, monitored, path, subtitles)


class SubtitleTranslate:
    base_subtitle: Subtitle
    to_language: str
    video_id: int
    is_serie: bool

    def __init__(self, base_subtitle: Subtitle, to_language: str, video_id: int, is_serie: bool) -> None:
        self.base_subtitle = base_subtitle
        self.to_language = to_language
        self.video_id = video_id
        self.is_serie = is_serie
