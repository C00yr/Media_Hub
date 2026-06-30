from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from app.adapters.base import MetadataAdapter


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"
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
        results = [item for item in payload.get("results", []) if item.get("media_type") in ("movie", "tv")]
        items = [self._normalize_item(item) for item in results[:8]]
        detailed = []
        for item in items:
            try:
                detailed.append(self.get_media_details(str(item["tmdb_id"]), str(item["media_type"])))
            except Exception:
                detailed.append(item)
        return detailed

    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]:
        payload = self._get(f"/{media_type}/{media_id}", {"append_to_response": "credits,external_ids"})
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
        backdrop_path = item.get("backdrop_path")
        genres = item.get("genres") if isinstance(item.get("genres"), list) else []
        credits = item.get("credits") if isinstance(item.get("credits"), dict) else {}
        cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
        crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
        directors = [
            person.get("name")
            for person in crew
            if person.get("job") in {"Director", "Series Director"} and person.get("name")
        ]
        creators = item.get("created_by") if isinstance(item.get("created_by"), list) else []
        if not directors:
            directors = [person.get("name") for person in creators if person.get("name")]
        runtime = item.get("runtime")
        if not runtime:
            runtimes = item.get("episode_run_time") if isinstance(item.get("episode_run_time"), list) else []
            runtime = runtimes[0] if runtimes else None
        external_ids = item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
        return {
            "id": f"{media_type}-{item.get('id')}",
            "tmdb_id": item.get("id"),
            "media_type": media_type,
            "title": title,
            "original_title": item.get("original_title") or item.get("original_name") or title,
            "year": date[:4] if date else "未知",
            "release_date": date or "",
            "rating": round(float(item.get("vote_average") or 0), 1),
            "vote_count": int(item.get("vote_count") or 0),
            "popularity": round(float(item.get("popularity") or 0), 1),
            "poster": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else PLACEHOLDER_POSTER,
            "backdrop": f"{TMDB_BACKDROP_BASE}{backdrop_path}" if backdrop_path else "",
            "overview": item.get("overview") or "",
            "genres": [genre.get("name") for genre in genres if genre.get("name")],
            "director": " / ".join(directors[:3]),
            "cast": [person.get("name") for person in cast[:8] if person.get("name")],
            "runtime": runtime,
            "status": item.get("status") or "",
            "original_language": item.get("original_language") or "",
            "imdb_id": external_ids.get("imdb_id") or item.get("imdb_id") or "",
        }
