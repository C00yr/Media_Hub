import errno
import ipaddress
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from app.adapters.base import MetadataAdapter
from app.config.settings import get_settings


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_PROXY_BASE = "/api/tmdb/image"
PLACEHOLDER_POSTER = "https://placehold.co/342x513?text=No+Poster"
PLACEHOLDER_PROFILE = "https://placehold.co/185x278?text=No+Photo"
TMDB_DOH_URL = "https://doh.pub/resolve"
DEFAULT_TMDB_PROXY_URL = "http://mihomo:7890"
DISCOVER_SORT_OPTIONS = [
    {"value": "popularity.desc", "label": "综合排序"},
    {"value": "release_date.desc", "label": "首播时间"},
    {"value": "vote_average.desc", "label": "高分优先"},
    {"value": "vote_count.desc", "label": "讨论热度"},
]
DISCOVER_REGION_OPTIONS = [
    {"value": "", "label": "不限地区"},
    {"value": "CN", "label": "中国大陆"},
    {"value": "HK", "label": "中国香港"},
    {"value": "TW", "label": "中国台湾"},
    {"value": "US", "label": "美国"},
    {"value": "GB", "label": "英国"},
    {"value": "JP", "label": "日本"},
    {"value": "KR", "label": "韩国"},
    {"value": "FR", "label": "法国"},
    {"value": "DE", "label": "德国"},
    {"value": "IN", "label": "印度"},
    {"value": "TH", "label": "泰国"},
]
DISCOVER_LANGUAGE_OPTIONS = [
    {"value": "", "label": "不限语言"},
    {"value": "zh", "label": "中文"},
    {"value": "en", "label": "英语"},
    {"value": "ja", "label": "日语"},
    {"value": "ko", "label": "韩语"},
    {"value": "fr", "label": "法语"},
    {"value": "de", "label": "德语"},
    {"value": "es", "label": "西语"},
]
TMDB_DOH_HOSTS = {"api.themoviedb.org", "image.tmdb.org"}
TMDB_DOH_FALLBACK_IPV4S = {
    "api.themoviedb.org": ["65.9.175.91", "65.9.175.66", "65.9.175.72", "65.9.175.84"],
}
SUSPECT_TMDB_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "31.13.0.0/16",
        "69.171.0.0/16",
        "108.160.0.0/16",
        "157.240.0.0/16",
        "202.160.0.0/16",
    )
)
_IPV4_DNS_LOCK = threading.Lock()
_DOH_CACHE_LOCK = threading.Lock()
_DOH_CACHE: dict[str, tuple[float, list[str]]] = {}
_DOH_STALE_CACHE: dict[str, tuple[float, list[str]]] = {}
_DISCOVER_OPTIONS_LOCK = threading.Lock()
_DISCOVER_GENRE_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_DISCOVER_OPTIONS_TTL_SECONDS = 6 * 60 * 60


class TmdbConfigError(ValueError):
    pass


class TmdbDohError(RuntimeError):
    def __init__(self, message: str, error_type: str = "doh_error"):
        super().__init__(message)
        self.error_type = error_type


class TmdbImageError(RuntimeError):
    def __init__(self, message: str, network_detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.network_detail = network_detail or {}


class TmdbAdapter(MetadataAdapter):
    def __init__(self, config: dict[str, Any]):
        settings = get_settings()
        self.api_key = str(config.get("api_key") or "").strip()
        self.bearer_token = str(config.get("bearer_token") or config.get("token") or "").strip()
        self.language = str(config.get("language") or "zh-CN").strip()
        self.region = str(config.get("region") or "CN").strip()
        self.mode = str(config.get("mode") or settings.tmdb_mode or "direct").strip().lower()
        if self.mode not in {"direct", "proxy"}:
            self.mode = "direct"
        self.proxy_url = str(config.get("proxy_url") or settings.tmdb_proxy_url or DEFAULT_TMDB_PROXY_URL).strip()
        self.base_url = str(config.get("endpoint") or TMDB_API_BASE).strip().rstrip("/")
        self.timeout = int(config.get("timeout") or 12)
        if not self.api_key and not self.bearer_token:
            raise TmdbConfigError("TMDB API Key or Bearer Token is not configured")
        if self.mode == "proxy" and not self.proxy_url:
            raise TmdbConfigError("TMDB proxy URL is not configured")

    def search_media(self, query: str) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        payload = self._get("/search/multi", {"query": query, "include_adult": "false"})
        results = [item for item in payload.get("results", []) if item.get("media_type") in ("movie", "tv")]
        items = [self._normalize_item(item) for item in results[:8]]
        detailed: list[dict[str, Any]] = []
        detail_errors: list[str] = []
        for item in items:
            try:
                detailed.append(self.get_media_details(str(item["tmdb_id"]), str(item["media_type"])))
            except Exception as exc:
                detail_errors.append(str(exc))
        if items and not detailed:
            detail = detail_errors[-1] if detail_errors else "unknown detail request error"
            raise RuntimeError(f"TMDB 找到了候选作品，但详情查询全部失败（{len(items)} 项）：{detail}")
        return detailed

    def lookup_media(self, query: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Search by title when available, otherwise use TMDB Discover with natural-language filters."""
        filters = dict(filters or {})
        media_type = str(filters.get("media_type") or "all").lower()
        if query.strip():
            items = self.search_media(query)
        else:
            media_types = [media_type] if media_type in {"movie", "tv"} else ["movie", "tv"]
            items = []
            for current_type in media_types:
                discover_filters = {**filters, "media_type": current_type, "include_options": False, "pages": 1}
                genre = str(filters.get("genre") or "").strip()
                if genre:
                    genre_id = next((item["id"] for item in self._genres(current_type) if genre.lower() in str(item.get("name") or "").lower()), "")
                    discover_filters["genre"] = str(genre_id or "")
                items.extend(self.discover_media(discover_filters).get("items") or [])
        # Discover already applies the structured filters server-side. Its
        # compact response omits production countries, so re-filtering it here
        # would incorrectly discard region-specific queries.
        filtered = [item for item in items if self._matches_lookup_filters(item, filters)] if query.strip() else items
        sort_by = str(filters.get("sort_by") or "").lower()
        if sort_by == "vote_average.desc":
            filtered.sort(key=lambda item: (float(item.get("rating") or 0), int(item.get("vote_count") or 0)), reverse=True)
        elif sort_by == "release_date.desc":
            filtered.sort(key=lambda item: str(item.get("release_date") or ""), reverse=True)
        else:
            filtered.sort(key=lambda item: float(item.get("popularity") or 0), reverse=True)
        candidates = filtered[:10]
        if query.strip():
            return candidates

        # Discover responses omit credits and episode metadata. Hydrate the
        # compact candidates so assistant replies can use the same detail set
        # as a title lookup.
        detailed: list[dict[str, Any]] = []
        for item in candidates:
            try:
                detailed.append(self.get_media_details(str(item["tmdb_id"]), str(item["media_type"])))
            except Exception:
                detailed.append(item)
        return detailed

    @staticmethod
    def _matches_lookup_filters(item: dict[str, Any], filters: dict[str, Any]) -> bool:
        media_type = str(filters.get("media_type") or "all").lower()
        if media_type in {"movie", "tv"} and item.get("media_type") != media_type:
            return False
        if float(item.get("rating") or 0) < float(filters.get("min_rating") or 0):
            return False
        language = str(filters.get("language") or "").lower()
        if language and str(item.get("original_language") or "").lower() != language:
            return False
        year = str(filters.get("year") or "").strip()
        item_year = str(item.get("year") or "")
        if year.endswith("s") and year[:-1].isdigit() and not (int(year[:-1]) <= int(item_year or 0) <= int(year[:-1]) + 9):
            return False
        if year and not year.endswith("s") and item_year != year:
            return False
        genre = str(filters.get("genre") or "").strip().lower()
        if genre and not any(genre in str(value).lower() for value in item.get("genres") or []):
            return False
        region = str(filters.get("region") or "").upper()
        region_names = {"KR": ("korea", "韩国"), "CN": ("china", "中国"), "US": ("united states", "美国"), "JP": ("japan", "日本"), "GB": ("united kingdom", "英国")}
        if region and not any(token in " ".join(item.get("production_countries") or []).lower() for token in region_names.get(region, (region.lower(),))):
            return False
        return True

    def get_media_details(self, media_id: str, media_type: str) -> dict[str, Any]:
        payload = self._get(f"/{media_type}/{media_id}", {"append_to_response": "credits,external_ids,recommendations,similar"})
        return self._normalize_item({**payload, "media_type": media_type})

    def get_person_details(self, person_id: str) -> dict[str, Any]:
        payload = self._get(f"/person/{person_id}", {"append_to_response": "combined_credits,external_ids"})
        credits = payload.get("combined_credits") if isinstance(payload.get("combined_credits"), dict) else {}
        cast = credits.get("cast") if isinstance(credits.get("cast"), list) else []
        crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
        work_by_id: dict[str, dict[str, Any]] = {}
        for item in [*cast, *crew]:
            media_type = item.get("media_type")
            if media_type not in {"movie", "tv"}:
                continue
            normalized = self._normalize_item(item)
            work_by_id[str(normalized["id"])] = normalized
        known_for = sorted(work_by_id.values(), key=lambda value: float(value.get("popularity") or 0), reverse=True)
        external_ids = payload.get("external_ids") if isinstance(payload.get("external_ids"), dict) else {}
        profile_path = payload.get("profile_path")
        return {
            "id": payload.get("id"),
            "person_id": payload.get("id"),
            "name": payload.get("name") or "未命名",
            "profile": _tmdb_image_url("w185", profile_path, PLACEHOLDER_PROFILE),
            "biography": payload.get("biography") or "",
            "birthday": payload.get("birthday") or "",
            "deathday": payload.get("deathday") or "",
            "place_of_birth": payload.get("place_of_birth") or "",
            "known_for_department": payload.get("known_for_department") or "",
            "also_known_as": payload.get("also_known_as") if isinstance(payload.get("also_known_as"), list) else [],
            "gender": payload.get("gender"),
            "imdb_id": external_ids.get("imdb_id") or "",
            "known_for": known_for[:24],
        }

    def get_discover_lists(self) -> dict[str, Any]:
        movie_genres = self._genre_map("movie")
        tv_genres = self._genre_map("tv")
        genre_maps = {"movie": movie_genres, "tv": tv_genres}
        return {
            "source": "tmdb",
            "configured": True,
            "trending": self._list("/trending/all/day", {"include_adult": "false"}, media_type=None, genre_maps=genre_maps),
            "popular_movies": self._list("/movie/popular", {"region": self.region}, media_type="movie", genre_maps=genre_maps),
            "popular_tv": self._list("/tv/popular", {}, media_type="tv", genre_maps=genre_maps),
            "top_rated_movies": self._list("/movie/top_rated", {"region": self.region}, media_type="movie", genre_maps=genre_maps),
            "top_rated_tv": self._list("/tv/top_rated", {}, media_type="tv", genre_maps=genre_maps),
        }

    def discover_media(self, filters: dict[str, Any]) -> dict[str, Any]:
        media_type = str(filters.get("media_type") or "movie").strip().lower()
        if media_type not in {"movie", "tv"}:
            media_type = "movie"
        requested_sort = str(filters.get("sort_by") or "popularity.desc").strip()
        sort_by = requested_sort
        if requested_sort == "release_date.desc":
            sort_by = "primary_release_date.desc" if media_type == "movie" else "first_air_date.desc"
        elif requested_sort == "first_air_date.desc" and media_type == "movie":
            sort_by = "primary_release_date.desc"
        params: dict[str, Any] = {
            "include_adult": "false",
            "include_video": "false",
            "sort_by": sort_by,
            "vote_count.gte": 20,
        }
        genre = str(filters.get("genre") or "").strip()
        region = str(filters.get("region") or "").strip().upper()
        language = str(filters.get("language") or "").strip().lower()
        year = str(filters.get("year") or "").strip()
        min_rating = str(filters.get("min_rating") or "").strip()
        page = max(1, min(int(filters.get("page") or 1), 500))
        pages = max(1, min(int(filters.get("pages") or 1), 4))
        if genre:
            params["with_genres"] = genre
        if region:
            params["with_origin_country"] = region
            if media_type == "movie":
                params["region"] = region
        elif media_type == "movie":
            params["region"] = self.region
        if language:
            params["with_original_language"] = language
        if year.endswith("s") and year[:-1].isdigit():
            start_year = int(year[:-1])
            end_year = start_year + 9
            date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
            params[f"{date_field}.gte"] = f"{start_year}-01-01"
            params[f"{date_field}.lte"] = f"{end_year}-12-31"
        elif year:
            params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year
        if min_rating:
            params["vote_average.gte"] = min_rating
        genre_map = self._genre_map(media_type)
        normalized = []
        total_pages = 1
        total_results = 0
        current_page = page
        for offset in range(pages):
            request_page = page + offset
            if request_page > 500:
                break
            payload = self._get(f"/discover/{media_type}", {**params, "page": request_page})
            current_page = payload.get("page") or request_page
            total_pages = payload.get("total_pages") or total_pages
            total_results = payload.get("total_results") or total_results
            for item in payload.get("results", [])[:20]:
                normalized.append(self._normalize_discover_item({**item, "media_type": media_type}, media_type, genre_map))
            if request_page >= int(total_pages or 1):
                break
        result = {
            "source": "tmdb",
            "configured": True,
            "filters": {**filters, "media_type": media_type, "sort_by": requested_sort},
            "items": normalized,
            "page": current_page,
            "start_page": page,
            "pages": pages,
            "next_page": page + pages if page + pages <= int(total_pages or 1) else None,
            "total_pages": total_pages,
            "total_results": total_results or len(normalized),
        }
        if filters.get("include_options", True):
            result["options"] = self.get_discover_filter_options()
        return result

    def get_discover_filter_options(self) -> dict[str, Any]:
        return {
            "genres": {
                "movie": self._genres("movie"),
                "tv": self._genres("tv"),
            },
            "sorts": DISCOVER_SORT_OPTIONS,
            "regions": DISCOVER_REGION_OPTIONS,
            "languages": DISCOVER_LANGUAGE_OPTIONS,
        }

    def test_connection(self) -> dict[str, Any]:
        payload = self._get("/configuration", {})
        image_probe = self._test_image_connection()
        network = self.network_detail()
        network["image_host"] = "image.tmdb.org"
        network["image_probe"] = image_probe
        return {
            "ok": True,
            "images": isinstance(payload.get("images"), dict),
            "change_keys": len(payload.get("change_keys") or []) if isinstance(payload.get("change_keys"), list) else 0,
            "network": network,
        }

    def network_detail(self) -> dict[str, Any]:
        return {
            "network_mode": self.mode,
            "route_label": "TMDB：mihomo VPN 代理" if self.mode == "proxy" else "TMDB：直连 + DoH",
            "proxy_enabled": self.mode == "proxy",
            "proxy_url": _display_proxy_url(self.proxy_url) if self.mode == "proxy" else "",
            "non_tmdb_policy": "direct_only",
        }

    def _test_image_connection(self) -> dict[str, Any]:
        payload = self._get("/trending/all/day", {"include_adult": "false"})
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        image_path = ""
        for item in results:
            if not isinstance(item, dict):
                continue
            image_path = str(item.get("poster_path") or item.get("backdrop_path") or "").strip().lstrip("/")
            if image_path:
                break
        if not image_path:
            return {"checked": False, "ok": True, "reason": "no_image_path_from_probe"}
        request = Request(
            f"https://image.tmdb.org/t/p/w92/{image_path}",
            headers={"Accept": "image/jpeg,image/png,image/webp,*/*", "User-Agent": "PT-Media-Hub"},
        )
        try:
            with open_tmdb_network_request(request, self.mode, self.proxy_url, self.timeout) as response:
                content = response.read(32)
                if not _looks_like_image_bytes(content):
                    content_type = response.headers.get_content_type() if response.headers else ""
                    raise TmdbImageError(f"TMDB image host returned non-image content: {content_type or 'unknown content type'}")
        except Exception as exc:
            detail = self.network_detail()
            detail["image_host"] = "image.tmdb.org"
            detail["image_probe"] = {"checked": True, "ok": False, "error": str(exc)}
            raise TmdbImageError(f"TMDB image host is unreachable: {exc}", detail) from exc
        return {"checked": True, "ok": True, "size": "w92", "path": image_path}

    def _list(self, path: str, params: dict[str, Any], media_type: str | None, genre_maps: dict[str, dict[int, str]] | None = None) -> list[dict[str, Any]]:
        payload = self._get(path, params)
        items = payload.get("results", [])
        if media_type is None:
            items = [item for item in items if item.get("media_type") in ("movie", "tv")]
        normalized = []
        for item in items[:20]:
            item_media_type = item.get("media_type") or media_type or "movie"
            genre_map = (genre_maps or {}).get(str(item_media_type), {})
            normalized.append(self._normalize_discover_item({**item, "media_type": item_media_type}, item_media_type, genre_map))
        return normalized

    def _genres(self, media_type: str) -> list[dict[str, Any]]:
        cache_key = (self.language, media_type)
        now = time.monotonic()
        with _DISCOVER_OPTIONS_LOCK:
            expires_at, cached = _DISCOVER_GENRE_CACHE.get(cache_key, (0.0, []))
            if expires_at > now and cached:
                return [dict(item) for item in cached]
        payload = self._get(f"/genre/{media_type}/list", {})
        genres = payload.get("genres") if isinstance(payload.get("genres"), list) else []
        result = [{"id": str(genre.get("id")), "name": genre.get("name")} for genre in genres if genre.get("id") and genre.get("name")]
        with _DISCOVER_OPTIONS_LOCK:
            _DISCOVER_GENRE_CACHE[cache_key] = (now + _DISCOVER_OPTIONS_TTL_SECONDS, [dict(item) for item in result])
        return result

    def _genre_map(self, media_type: str) -> dict[int, str]:
        genres = self._genres(media_type)
        return {int(genre["id"]): str(genre["name"]) for genre in genres if str(genre.get("id", "")).isdigit()}

    def _normalize_discover_item(self, item: dict[str, Any], media_type: str, genre_map: dict[int, str]) -> dict[str, Any]:
        item_media_type = item.get("media_type") or media_type
        date = item.get("release_date") if item_media_type == "movie" else item.get("first_air_date")
        title = item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "Unknown"
        poster_path = item.get("poster_path")
        genre_ids = item.get("genre_ids") if isinstance(item.get("genre_ids"), list) else []
        return {
            "id": f"{item_media_type}-{item.get('id')}",
            "tmdb_id": item.get("id"),
            "media_type": item_media_type,
            "title": title,
            "original_title": item.get("original_title") or item.get("original_name") or title,
            "year": date[:4] if date else "",
            "release_date": date or "",
            "rating": round(float(item.get("vote_average") or 0), 1),
            "vote_count": int(item.get("vote_count") or 0),
            "popularity": round(float(item.get("popularity") or 0), 1),
            "poster": _tmdb_image_url("w342", poster_path, PLACEHOLDER_POSTER),
            "overview": item.get("overview") or "",
            "genres": [genre_map[genre_id] for genre_id in genre_ids if genre_id in genre_map][:3],
            "original_language": item.get("original_language") or "",
        }

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {"language": self.language, "page": 1, **{key: value for key, value in params.items() if value not in (None, "")}}
        if self.api_key and not self.bearer_token:
            query["api_key"] = self.api_key
        url = f"{self.base_url}{path}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(url, headers=headers)
        with open_tmdb_network_request(request, self.mode, self.proxy_url, self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        media_type = item.get("media_type") or ("tv" if item.get("name") else "movie")
        date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        title = item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name") or "未命名"
        poster_path = item.get("poster_path")
        backdrop_path = item.get("backdrop_path")
        genres = item.get("genres") if isinstance(item.get("genres"), list) else []
        genre_names = [genre.get("name") for genre in genres if genre.get("name")]
        if not genre_names:
            genre_ids = item.get("genre_ids") if isinstance(item.get("genre_ids"), list) else []
            genre_map = self._genre_map(media_type)
            genre_names = [genre_map[genre_id] for genre_id in genre_ids if genre_id in genre_map]
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
        countries = item.get("production_countries") if isinstance(item.get("production_countries"), list) else []
        seasons = item.get("seasons") if isinstance(item.get("seasons"), list) else []
        season_summaries = [self._normalize_tv_season(season) for season in seasons if isinstance(season, dict)]
        regular_seasons = [season for season in season_summaries if int(season.get("season_number") or 0) > 0]
        latest_season = max(regular_seasons, key=lambda season: int(season.get("season_number") or 0), default=None)
        recommendation_payload = item.get("recommendations") if isinstance(item.get("recommendations"), dict) else {}
        similar_payload = item.get("similar") if isinstance(item.get("similar"), dict) else {}
        recommendation_items = recommendation_payload.get("results") if isinstance(recommendation_payload.get("results"), list) else []
        if not recommendation_items:
            recommendation_items = similar_payload.get("results") if isinstance(similar_payload.get("results"), list) else []
        cast_members = [
            {
                "id": person.get("id"),
                "person_id": person.get("id"),
                "name": person.get("name"),
                "character": person.get("character") or "",
                "profile": _tmdb_image_url("w185", person.get("profile_path"), PLACEHOLDER_PROFILE),
                "order": person.get("order"),
            }
            for person in cast[:14]
            if person.get("id") and person.get("name")
        ]
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
            "poster": _tmdb_image_url("w342", poster_path, PLACEHOLDER_POSTER),
            "backdrop": _tmdb_image_url("w780", backdrop_path, ""),
            "overview": item.get("overview") or "",
            "genres": genre_names,
            "director": " / ".join(directors[:3]),
            "cast": [person.get("name") for person in cast[:8] if person.get("name")],
            "cast_members": cast_members,
            "runtime": runtime,
            "status": item.get("status") or "",
            "original_language": item.get("original_language") or "",
            "imdb_id": external_ids.get("imdb_id") or item.get("imdb_id") or "",
            "production_countries": [country.get("name") for country in countries if country.get("name")],
            "number_of_seasons": item.get("number_of_seasons") if media_type == "tv" else None,
            "number_of_episodes": item.get("number_of_episodes") if media_type == "tv" else None,
            "seasons": season_summaries if media_type == "tv" else [],
            "latest_season": latest_season if media_type == "tv" else None,
            "last_episode_to_air": self._normalize_tv_episode(item.get("last_episode_to_air")) if media_type == "tv" else None,
            "next_episode_to_air": self._normalize_tv_episode(item.get("next_episode_to_air")) if media_type == "tv" else None,
            "recommendations": [
                self._normalize_item({**related, "media_type": related.get("media_type") or media_type})
                for related in recommendation_items[:12]
                if (related.get("media_type") or media_type) in {"movie", "tv"}
            ],
        }

    @staticmethod
    def _normalize_tv_season(season: dict[str, Any]) -> dict[str, Any]:
        return {
            "season_number": season.get("season_number"),
            "episode_count": season.get("episode_count"),
            "air_date": season.get("air_date") or "",
            "name": season.get("name") or "",
            "overview": season.get("overview") or "",
            "poster": _tmdb_image_url("w342", season.get("poster_path"), ""),
        }

    @staticmethod
    def _normalize_tv_episode(episode: Any) -> dict[str, Any] | None:
        if not isinstance(episode, dict):
            return None
        return {
            "season_number": episode.get("season_number"),
            "episode_number": episode.get("episode_number"),
            "air_date": episode.get("air_date") or "",
            "name": episode.get("name") or "",
            "overview": episode.get("overview") or "",
        }


def _display_proxy_url(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"http://{value}")
    if not parsed.netloc:
        return value
    return parsed.netloc


def _tmdb_image_url(size: str, image_path: Any, fallback: str) -> str:
    clean_path = str(image_path or "").strip().lstrip("/")
    if not clean_path:
        return fallback
    return f"{TMDB_IMAGE_PROXY_BASE}/{size}/{clean_path}"


def _looks_like_image_bytes(content: bytes) -> bool:
    if len(content) < 12:
        return False
    if content.startswith(b"\xff\xd8\xff"):
        return True
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return True
    if content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return True
    return False


def open_tmdb_network_request(request: Request, mode: str, proxy_url: str, timeout: int):
    request.tmdb_proxy_url = proxy_url or DEFAULT_TMDB_PROXY_URL
    opener = _urlopen_with_proxy if mode == "proxy" else _urlopen_with_doh_ipv4
    return opener(request, timeout=timeout)


def _urlopen_no_proxy(request: Request, timeout: int):
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def _urlopen_with_proxy(request: Request, timeout: int):
    proxy_url = getattr(request, "tmdb_proxy_url", "") or DEFAULT_TMDB_PROXY_URL
    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return opener.open(request, timeout=timeout)


def _urlopen_with_ipv4_fallback(request: Request, timeout: int):
    try:
        return _urlopen_no_proxy(request, timeout=timeout)
    except URLError as exc:
        if not _is_network_unreachable(exc):
            raise
        with _force_ipv4_dns():
            return _urlopen_no_proxy(request, timeout=timeout)


def _urlopen_with_doh_ipv4(request: Request, timeout: int):
    host = (urlparse(request.full_url).hostname or "").lower()
    if host not in TMDB_DOH_HOSTS:
        return _urlopen_with_ipv4_fallback(request, timeout=timeout)
    ips = _resolve_ipv4_with_doh(host, timeout)
    last_error: Exception | None = None
    per_ip_timeout = max(4, min(timeout, 8))
    for index, ip in enumerate(ips):
        try:
            with _force_host_ipv4(host, [ip]):
                response = _urlopen_no_proxy(request, timeout=per_ip_timeout)
            if index:
                _prefer_doh_ip(host, ip)
            return response
        except HTTPError:
            if index:
                _prefer_doh_ip(host, ip)
            raise
        except (TimeoutError, OSError, URLError) as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise TmdbDohError("DNS over HTTPS returned no usable IPv4 records for TMDB", "doh_bad_answer")


def _resolve_ipv4_with_doh(host: str, timeout: int) -> list[str]:
    now = time.monotonic()
    with _DOH_CACHE_LOCK:
        expires_at, cached_ips = _DOH_CACHE.get(host, (0.0, []))
        if expires_at > now and cached_ips:
            return list(cached_ips)

    request = Request(
        f"{TMDB_DOH_URL}?{urlencode({'name': host, 'type': 'A'})}",
        headers={"Accept": "application/dns-json"},
    )
    try:
        with _force_ipv4_dns():
            with _urlopen_no_proxy(request, timeout=max(3, min(timeout, 20))) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        stale_ips = _stale_doh_ips(host, now)
        if stale_ips:
            return stale_ips
        raise TmdbDohError(f"DNS over HTTPS unavailable: {exc}", "doh_unavailable") from exc

    answers = payload.get("Answer") if isinstance(payload, dict) else None
    ips: list[str] = []
    ttls: list[int] = []
    if isinstance(answers, list):
        for item in answers:
            if not isinstance(item, dict) or int(item.get("type") or 0) != 1:
                continue
            value = str(item.get("data") or "").strip()
            if _is_usable_tmdb_ipv4(value):
                ips.append(value)
                try:
                    ttls.append(int(item.get("TTL") or 60))
                except (TypeError, ValueError):
                    pass

    if not ips:
        stale_ips = _stale_doh_ips(host, now)
        if stale_ips:
            return stale_ips
        raise TmdbDohError("DNS over HTTPS returned no usable IPv4 records for TMDB", "doh_bad_answer")

    cache_ttl = max(30, min(ttls or [60], default=60))
    with _DOH_CACHE_LOCK:
        _DOH_CACHE[host] = (now + cache_ttl, ips)
        _DOH_STALE_CACHE[host] = (now + 86400, ips)
    return ips


def _prefer_doh_ip(host: str, ip: str) -> None:
    with _DOH_CACHE_LOCK:
        expires_at, cached_ips = _DOH_CACHE.get(host, (0.0, []))
        stale_expires_at, stale_ips = _DOH_STALE_CACHE.get(host, (time.monotonic() + 86400, []))
        source_ips = cached_ips or stale_ips or TMDB_DOH_FALLBACK_IPV4S.get(host, [])
        if not source_ips:
            return
        ordered = [ip, *[item for item in source_ips if item != ip]]
        if cached_ips:
            _DOH_CACHE[host] = (expires_at, ordered)
        _DOH_STALE_CACHE[host] = (max(stale_expires_at, time.monotonic() + 86400), ordered)


def _stale_doh_ips(host: str, now: float) -> list[str]:
    with _DOH_CACHE_LOCK:
        expires_at, cached_ips = _DOH_STALE_CACHE.get(host, (0.0, []))
        if expires_at > now and cached_ips:
            return list(cached_ips)
    return [ip for ip in TMDB_DOH_FALLBACK_IPV4S.get(host, []) if _is_usable_tmdb_ipv4(ip)]


def _is_usable_tmdb_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version != 4 or not address.is_global:
        return False
    return not any(address in network for network in SUSPECT_TMDB_IPV4_NETWORKS)


def _is_network_unreachable(exc: URLError) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, OSError) and getattr(reason, "errno", None) == errno.ENETUNREACH:
        return True
    return "network is unreachable" in str(reason).lower()


@contextmanager
def _force_ipv4_dns():
    with _IPV4_DNS_LOCK:
        original_getaddrinfo = socket.getaddrinfo

        def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            results = original_getaddrinfo(host, port, family, type, proto, flags)
            ipv4_results = [item for item in results if item[0] == socket.AF_INET]
            return ipv4_results or results

        socket.getaddrinfo = ipv4_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo


@contextmanager
def _force_host_ipv4(hostname: str, ips: list[str]):
    with _IPV4_DNS_LOCK:
        original_getaddrinfo = socket.getaddrinfo
        normalized = hostname.lower()

        def doh_ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if str(host).lower() == normalized:
                socktype = type or socket.SOCK_STREAM
                protocol = proto or socket.IPPROTO_TCP
                return [
                    (socket.AF_INET, socktype, protocol, "", (ip, port))
                    for ip in ips
                ]
            results = original_getaddrinfo(host, port, family, type, proto, flags)
            ipv4_results = [item for item in results if item[0] == socket.AF_INET]
            return ipv4_results or results

        socket.getaddrinfo = doh_ipv4_getaddrinfo
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo
