from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class Config:
    bazarr_base_url: str
    bazarr_api_key: str
    base_languages: list[str]
    to_languages: list[str]
    min_score: int = 86
    num_workers: int = 1
    interval_between_scans: int = 300
    translation_timeout: int = 900
    action_cooldown_seconds: int = 3600
    series_scan: bool = True
    movies_scan: bool = True
    log_level: str = "INFO"
    log_directory: str = "logs/"
    source_profile_id: int | None = None
    target_profile_id: int | None = None

    @property
    def base_languages_set(self) -> frozenset[str]:
        return frozenset(self.base_languages)

    @property
    def to_languages_set(self) -> frozenset[str]:
        return frozenset(self.to_languages)

    @property
    def base_lang_priority(self) -> dict[str, int]:
        return {lang: idx for idx, lang in enumerate(self.base_languages)}

    def validate(self) -> None:
        if not self.bazarr_base_url:
            raise ValueError("BAZARR_BASE_URL is required")
        if not self.bazarr_api_key:
            raise ValueError("BAZARR_API_KEY is required")

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()

        def get_int(env: str, default: int) -> int:
            val = os.getenv(env)
            return int(val) if val is not None else default

        def get_bool(env: str, default: bool) -> bool:
            val = os.getenv(env)
            if val is None:
                return default
            return val.strip().lower() not in ("false", "0", "no")

        def get_optional_int(env: str) -> int | None:
            val = os.getenv(env)
            return int(val) if val else None

        def parse_langs(env: str) -> list[str]:
            val = os.getenv(env, "")
            return [lang.strip() for lang in val.split(",") if lang.strip()]

        return cls(
            bazarr_base_url=os.getenv("BAZARR_BASE_URL", ""),
            bazarr_api_key=os.getenv("BAZARR_API_KEY", ""),
            base_languages=parse_langs("BASE_LANGUAGES"),
            to_languages=parse_langs("TO_LANGUAGES"),
            min_score=get_int("MIN_SCORE", 86),
            num_workers=get_int("NUM_WORKERS", 1),
            interval_between_scans=get_int("INTERVAL_BETWEEN_SCANS", 300),
            translation_timeout=get_int("TRANSLATION_REQUEST_TIMEOUT", 900),
            action_cooldown_seconds=get_int("ACTION_COOLDOWN_SECONDS", 3600),
            series_scan=get_bool("SERIES_SCAN", True),
            movies_scan=get_bool("MOVIES_SCAN", True),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_directory=os.getenv("LOG_DIRECTORY", "logs/"),
            source_profile_id=get_optional_int("SOURCE_PROFILE_ID"),
            target_profile_id=get_optional_int("TARGET_PROFILE_ID"),
        )
