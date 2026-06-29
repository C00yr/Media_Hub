from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from app.adapters.base import MetadataAdapter


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
PLACEHOLDER_POSTER = "https://placehold.co/342x513?text=No+Poster"


class TmdbConfigError(ValueError):
    pass


class TmdbAdapter(MetadataAdapter):
    def __init__(self, config: dict[str, Any]):
        self.api_key = str(config.get("api_key") or "").strip()
        self.bearer_token = str(config.get("bearer_token") or config.get("token") or "").strip()
        self.language = str(config.get("language") or "zh-CN").strip()
        self.region = str(config.get("region") or "CN").strip()
        self.base_url = str(config.get("endpoint") or TMDB_API_BASE).strip().rstrip("/")
        self.timeout = int(config.get("timeout") or 12)
        if not self.api_key and not self.bearer_token:
            raise TmdbConfigError("TMDB API Key 或 Bearer Token 未配置")

    def search_media(self, query: str) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        payload = self._get("/search/multi", {"query": query, "include_adult": "false"})
        return [self._normalize_item(item) for item in payload.get("results", []) if item.get("media_type") in ("movie", "tv")]

    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]:
        payload = self._get(f"/{media_type}/{media_id}", {})
        return self._normalize_item({**payload, "media_type": media_type})

    def get_discover_lists(self) -> dict[str, Any]:
        return {
            "source": "tmdb",
            "configured": True,
            "trending": self._list("/trending/all/day", {"include_adult": "false"}, media_type=None),
            "popular_movies": self._list("/movie/popular", {"region": self.region}, media_type="movie"),
            "popular_tv": self._list("/tv/popular", {}, media_type="tv"),
            "top_rated_movies": self._list("/movie/top_rated", {"region": self.region}, media_type="movie"),
            "top_rated_tv": self._list("/tv/top_rated", {}, media_type="tv"),
        }

    def _list(self, path: str, params: dict[str, Any], media_type: str | None) -> list[dict[str, Any]]:
        payload = self._get(path, params)
        items = payload.get("results", [])
        if media_type is None:
            items = [item for item in items if item.get("media_type") in ("movie", "tv")]
        return [self._normalize_item({**item, "media_type": item.get("media_type") or media_type}) for item in items[:20]]

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {"language": self.language, "page": 1, **params}
        if self.api_key:
            query["api_key"] = self.api_key
        url = f"{self.base_url}{path}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(url, headers=headers)
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        media_type = item.get("media_type") or ("tv" if item.get("name") else "movie")
        date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        title = item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "未命名"
        poster_path = item.get("poster_path")
        return {
            "id": f"{media_type}-{item.get('id')}",
            "tmdb_id": item.get("id"),
            "media_type": media_type,
            "title": title,
            "original_title": item.get("original_title") or item.get("original_name") or title,
            "year": date[:4] if date else "未知",
            "rating": round(float(item.get("vote_average") or 0), 1),
            "poster": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else PLACEHOLDER_POSTER,
            "overview": item.get("overview") or "",
        }
