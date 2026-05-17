from __future__ import annotations
import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from models import Serie, Movie

if TYPE_CHECKING:
    from config import Config

logger = logging.getLogger("bazarr_lingarr")
_CHUNK_SIZE = 50


class BazarrClient:
    def __init__(self, config: Config) -> None:
        self._base_url = config.bazarr_base_url.rstrip("/")
        self._headers = {"X-API-KEY": config.bazarr_api_key}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> BazarrClient:
        self._client = httpx.AsyncClient(timeout=60)
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> bool:
        """Verifies connectivity and API key validity against Bazarr."""
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/episodes/wanted",
                headers=self._headers,
                params={"start": 0, "length": 1},
            )
            resp.raise_for_status()
            logger.info(f"Connected to Bazarr at {self._base_url}")
            return True
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Bazarr returned HTTP {e.response.status_code} — "
                f"invalid API key or wrong URL? Response: {e.response.text[:300]}"
            )
            return False
        except Exception as e:
            logger.error(f"Cannot reach Bazarr at {self._base_url}: {e}")
            return False

    async def get_wanted(self, media_type: str) -> list[Serie | Movie]:
        endpoint = f"{self._base_url}/api/{media_type}/wanted"
        parse = Serie.from_dict if media_type == "episodes" else Movie.from_dict
        try:
            resp = await self._client.get(endpoint, headers=self._headers,
                                          params={"start": 0, "length": -1})
            resp.raise_for_status()
            return [parse(obj) for obj in resp.json().get("data", [])]
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} fetching wanted {media_type}: {e.response.text[:300]}")
            return []
        except Exception:
            logger.exception(f"Error fetching wanted {media_type}:")
            return []

    async def get_metadata(self, media_type: str, ids: list[int]) -> list[Serie | Movie]:
        endpoint = f"{self._base_url}/api/{media_type}"
        id_param = "episodeid[]" if media_type == "episodes" else "radarrid[]"
        parse = Serie.from_dict if media_type == "episodes" else Movie.from_dict
        chunks = [ids[i:i + _CHUNK_SIZE] for i in range(0, len(ids), _CHUNK_SIZE)]

        async def fetch(chunk: list[int]) -> list:
            resp = await self._client.get(endpoint, headers=self._headers,
                                          params={id_param: chunk})
            resp.raise_for_status()
            return [parse(obj) for obj in resp.json().get("data", [])]

        try:
            results = await asyncio.gather(*(fetch(c) for c in chunks))
            return [item for batch in results for item in batch]
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} fetching {media_type} metadata: {e.response.text[:300]}")
            return []
        except Exception:
            logger.exception(f"Error fetching {media_type} metadata:")
            return []

    async def get_raw_metadata(self, media_type: str, ids: list[int]) -> list[dict]:
        """Returns raw JSON dicts, used for migration profile checks."""
        endpoint = f"{self._base_url}/api/{media_type}"
        id_param = "radarrid[]" if media_type == "movies" else "episodeid[]"
        chunks = [ids[i:i + _CHUNK_SIZE] for i in range(0, len(ids), _CHUNK_SIZE)]

        async def fetch(chunk: list[int]) -> list[dict]:
            resp = await self._client.get(endpoint, headers=self._headers,
                                          params={id_param: chunk})
            resp.raise_for_status()
            return resp.json().get("data", [])

        results = await asyncio.gather(*(fetch(c) for c in chunks), return_exceptions=True)
        raw: list[dict] = []
        for result in results:
            if isinstance(result, httpx.HTTPStatusError):
                logger.error(f"HTTP {result.response.status_code} fetching migration metadata: {result.response.text[:300]}")
            elif isinstance(result, Exception):
                logger.error(f"Migration metadata fetch failed: {result}")
            else:
                raw.extend(result)
        return raw
