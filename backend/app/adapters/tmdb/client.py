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
from urllib.request import Request, urlopen

from app.adapters.base import MetadataAdapter
from app.config.settings import get_settings


TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TMDB_BACKDROP_BASE = "https://image.tmdb.org/t/p/w780"
TMDB_PROFILE_BASE = "https://image.tmdb.org/t/p/w185"
PLACEHOLDER_POSTER = "https://placehold.co/342x513?text=No+Poster"
PLACEHOLDER_PROFILE = "https://placehold.co/185x278?text=No+Photo"
TMDB_DOH_URL = "https://doh.pub/resolve"
TMDB_DOH_HOSTS = {"api.themoviedb.org"}
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


class TmdbConfigError(ValueError):
    pass


class TmdbGatewayError(RuntimeError):
    def __init__(self, message: str, error_type: str = "gateway_error", http_status: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status


class TmdbDohError(RuntimeError):
    def __init__(self, message: str, error_type: str = "doh_error"):
        super().__init__(message)
        self.error_type = error_type


class TmdbAdapter(MetadataAdapter):
    def __init__(self, config: dict[str, Any]):
        settings = get_settings()
        self.api_key = str(config.get("api_key") or "").strip()
        self.bearer_token = str(config.get("bearer_token") or config.get("token") or "").strip()
        self.language = str(config.get("language") or "zh-CN").strip()
        self.region = str(config.get("region") or "CN").strip()
        self.mode = str(config.get("mode") or settings.tmdb_mode or "direct").strip().lower()
        self.gateway_url = self._gateway_root(str(config.get("gateway_url") or settings.tmdb_gateway_url or "").strip())
        self.gateway_key = str(config.get("gateway_key") or settings.tmdb_gateway_key or "").strip()
        if self.mode not in {"direct", "gateway"}:
            self.mode = "direct"
        self.base_url = str(config.get("endpoint") or TMDB_API_BASE).strip().rstrip("/")
        if self.mode == "gateway":
            if not self.gateway_url:
                raise TmdbConfigError("TMDB Gateway URL is not configured")
            if not self.gateway_key:
                raise TmdbConfigError("TMDB Gateway Key is not configured")
            self.base_url = self._gateway_api_base(self.gateway_url)
        self.timeout = int(config.get("timeout") or 12)
        if self.mode == "direct" and not self.api_key and not self.bearer_token:
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
            "profile": f"{TMDB_PROFILE_BASE}{profile_path}" if profile_path else PLACEHOLDER_PROFILE,
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
        return {
            "source": "tmdb",
            "configured": True,
            "trending": self._list("/trending/all/day", {"include_adult": "false"}, media_type=None),
            "popular_movies": self._list("/movie/popular", {"region": self.region}, media_type="movie"),
            "popular_tv": self._list("/tv/popular", {}, media_type="tv"),
            "top_rated_movies": self._list("/movie/top_rated", {"region": self.region}, media_type="movie"),
            "top_rated_tv": self._list("/tv/top_rated", {}, media_type="tv"),
        }

    def test_connection(self) -> dict[str, Any]:
        payload = self._get("/configuration", {})
        return {
            "ok": True,
            "images": isinstance(payload.get("images"), dict),
            "change_keys": len(payload.get("change_keys") or []) if isinstance(payload.get("change_keys"), list) else 0,
        }

    def _list(self, path: str, params: dict[str, Any], media_type: str | None) -> list[dict[str, Any]]:
        payload = self._get(path, params)
        items = payload.get("results", [])
        if media_type is None:
            items = [item for item in items if item.get("media_type") in ("movie", "tv")]
        normalized = [self._normalize_item({**item, "media_type": item.get("media_type") or media_type}) for item in items[:20]]
        detailed: list[dict[str, Any]] = []
        for item in normalized:
            try:
                detailed.append(self.get_media_details(str(item["tmdb_id"]), str(item["media_type"])))
            except Exception:
                detailed.append(item)
        return detailed

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {"language": self.language, "page": 1, **params}
        if self.mode == "direct" and self.api_key:
            query["api_key"] = self.api_key
        url = f"{self.base_url}{path}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if self.mode == "gateway":
            headers["X-Gateway-Key"] = self.gateway_key
        elif self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(url, headers=headers)
        opener = _urlopen_with_doh_ipv4 if self.mode == "direct" else _urlopen_with_ipv4_fallback
        with opener(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_gateway_health(self) -> dict[str, Any]:
        if self.mode != "gateway":
            return {"ok": True, "mode": self.mode}
        request = Request(f"{self.gateway_url}/health", headers={"Accept": "application/json"})
        with _urlopen_with_ipv4_fallback(request, timeout=self.timeout) as response:
            status = getattr(response, "status", response.getcode())
            payload = json.loads(response.read().decode("utf-8"))
            if status >= 400 or not payload.get("ok"):
                raise TmdbGatewayError("Cloudflare Worker health check failed", "gateway_health_error", status)
            return payload

    @staticmethod
    def _gateway_root(value: str) -> str:
        base = value.rstrip("/")
        if base.endswith("/3"):
            return base[:-2].rstrip("/")
        return base

    @staticmethod
    def _gateway_api_base(value: str) -> str:
        base = value.rstrip("/")
        if base.endswith("/3"):
            return base
        return f"{base}/3"

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
        countries = item.get("production_countries") if isinstance(item.get("production_countries"), list) else []
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
                "profile": f"{TMDB_PROFILE_BASE}{person.get('profile_path')}" if person.get("profile_path") else PLACEHOLDER_PROFILE,
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
            "poster": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else PLACEHOLDER_POSTER,
            "backdrop": f"{TMDB_BACKDROP_BASE}{backdrop_path}" if backdrop_path else "",
            "overview": item.get("overview") or "",
            "genres": [genre.get("name") for genre in genres if genre.get("name")],
            "director": " / ".join(directors[:3]),
            "cast": [person.get("name") for person in cast[:8] if person.get("name")],
            "cast_members": cast_members,
            "runtime": runtime,
            "status": item.get("status") or "",
            "original_language": item.get("original_language") or "",
            "imdb_id": external_ids.get("imdb_id") or item.get("imdb_id") or "",
            "production_countries": [country.get("name") for country in countries if country.get("name")],
            "recommendations": [
                self._normalize_item({**related, "media_type": related.get("media_type") or media_type})
                for related in recommendation_items[:12]
                if (related.get("media_type") or media_type) in {"movie", "tv"}
            ],
        }


def _urlopen_with_ipv4_fallback(request: Request, timeout: int):
    try:
        return urlopen(request, timeout=timeout)
    except URLError as exc:
        if not _is_network_unreachable(exc):
            raise
        with _force_ipv4_dns():
            return urlopen(request, timeout=timeout)


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
                response = urlopen(request, timeout=per_ip_timeout)
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
            with urlopen(request, timeout=max(3, min(timeout, 20))) as response:
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
