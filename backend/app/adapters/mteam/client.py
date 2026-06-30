import json
import re
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.adapters.base import TrackerAdapter


MTEAM_API_BASE = "https://api.m-team.cc"


class MTeamConfigError(ValueError):
    pass


class MTeamApiError(RuntimeError):
    def __init__(self, message: str, code: Any = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class MTeamAdapter(TrackerAdapter):
    def __init__(self, config: dict[str, Any]):
        self.headers = dict(config.get("headers") or {})
        self.api_key = self._pick_api_key(config)
        self.base_url = self._api_base(str(config.get("api_base_url") or config.get("base_url") or MTEAM_API_BASE))
        self.timeout = int(config.get("timeout") or 10)
        if not self.api_key:
            raise MTeamConfigError("M-Team API Key 未配置")

    def get_user_stats(self) -> dict[str, Any]:
        payload = self._first_success(
            [
                ("POST", "/api/member/profile", {}),
                ("GET", "/api/member/profile", None),
                ("POST", "/api/member/getUserInfo", {}),
                ("GET", "/api/member/getUserInfo", None),
            ]
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        flat = _flatten(data)
        member_count = data.get("memberCount") if isinstance(data.get("memberCount"), dict) else {}
        member_status = data.get("memberStatus") if isinstance(data.get("memberStatus"), dict) else {}
        user_id = _string_from_any(data.get("id") or _find_value(flat, "id"))
        seeding_stats = self._get_seeding_stats(user_id)
        upload_total = _bytes_from_any(member_count.get("uploaded") or _find_value(flat, "uploaded", "uploadTotal", "totalUpload", "uploadedBytes", "uploadBytes"))
        download_total = _bytes_from_any(member_count.get("downloaded") or _find_value(flat, "downloaded", "downloadTotal", "totalDownload", "downloadedBytes", "downloadBytes"))
        bonus = _float_from_any(member_count.get("bonus") or _find_value(flat, "bonus", "bonusValue", "magic", "magicPoint", "point", "points", "credit"))
        ratio = _float_from_any(member_count.get("shareRate") or _find_value(flat, "shareRate", "ratio", "share_rate", "uploadedDownloadedRatio"))
        seed_count = seeding_stats["seed_count"] or int(_float_from_any(_find_value(flat, "seedCount", "seedingCount", "activeUploads", "seeders")) or 0)
        seed_size = seeding_stats["seed_size"] or _bytes_from_any(_find_value(flat, "seedSize", "seedingSize", "seedVolume", "seedingVolume"))
        joined_at = _string_from_any(member_status.get("createdDate") or _find_value(flat, "joinDate", "joinedAt", "createdAt", "createdDate", "registerDate"))
        user_level = _mteam_role_label(data.get("role")) or _string_from_any(_find_value(flat, "level", "memberLevel", "class", "className", "role")) or "User"
        return {
            "user_level": user_level,
            "username": _string_from_any(data.get("username")),
            "user_id": user_id,
            "upload_total": upload_total,
            "upload_delta_label": None,
            "download_total": download_total,
            "download_delta_label": None,
            "bonus": bonus,
            "bonus_delta_label": None,
            "ratio": ratio,
            "ratio_delta_label": None,
            "seed_count": seed_count,
            "seed_count_delta_label": None,
            "seed_size": seed_size,
            "seed_size_delta_label": None,
            "joined_at": _date_label(joined_at),
            "active_uploads": seed_count,
            "active_downloads": seeding_stats["leech_count"],
            "seedtime_seconds": int(_float_from_any(data.get("seedtime")) or 0),
            "leechtime_seconds": int(_float_from_any(data.get("leechtime")) or 0),
            "allow_download": bool(data.get("allowDownload", True)),
            "vip": bool(member_status.get("vip")),
            "warned": bool(member_status.get("warned")),
            "bonus_per_hour_label": "M-Team 原始数据",
            "source": "M-Team 原始数据（Real API）",
            "updated_at": datetime.utcnow().isoformat(),
            "traffic_history": _traffic_history(data),
            "seeding_info": seeding_stats["items"],
            "raw_summary": _compact_raw_summary(data),
        }

    def test_connection(self) -> dict[str, Any]:
        payload = self._request("POST", "/api/member/profile", {})
        data = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(data, dict) or not data.get("id"):
            raise MTeamApiError("M-Team API 未返回有效用户资料")
        member_count = data.get("memberCount") if isinstance(data.get("memberCount"), dict) else {}
        return {
            "success": True,
            "username": _string_from_any(data.get("username")),
            "user_id": _string_from_any(data.get("id")),
            "user_level": _mteam_role_label(data.get("role")) or "User",
            "upload_total": _bytes_from_any(member_count.get("uploaded")),
            "download_total": _bytes_from_any(member_count.get("downloaded")),
            "checked_at": datetime.utcnow().isoformat(),
        }

    def search_torrents(self, query: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        request_payload = {
            "mode": "normal",
            "keyword": query.strip(),
            "pageNumber": 1,
            "pageSize": int((filters or {}).get("page_size") or 20),
        }
        payload = self._request("POST", "/api/torrent/search", request_payload)
        data = payload.get("data") if isinstance(payload, dict) else payload
        items = _extract_items(data)
        return [self._normalize_torrent(item) for item in items]

    def get_download_payload(self, torrent_id: str) -> dict[str, Any]:
        return {"torrent_id": torrent_id, "download_url": f"{self.base_url}/api/torrent/download/{torrent_id}"}

    def _get_seeding_stats(self, user_id: str) -> dict[str, Any]:
        stats = {"seed_count": 0, "seed_size": 0.0, "leech_count": 0, "items": []}
        if not user_id:
            return stats
        peer_status = self._optional_request("POST", "/api/tracker/myPeerStatus", {"uid": user_id})
        peer_data = peer_status.get("data") if isinstance(peer_status, dict) else {}
        if isinstance(peer_data, dict):
            stats["seed_count"] = int(_float_from_any(peer_data.get("seeder")) or 0)
            stats["leech_count"] = int(_float_from_any(peer_data.get("leecher")) or 0)

        page_number = 1
        page_size = 200
        while page_number <= 10:
            payload = self._optional_request(
                "POST",
                "/api/member/getUserTorrentList",
                {"pageNumber": page_number, "pageSize": page_size, "type": "SEEDING", "userid": user_id},
            )
            page = payload.get("data") if isinstance(payload, dict) else {}
            if not isinstance(page, dict):
                break
            torrents = page.get("data") or []
            if not isinstance(torrents, list):
                break
            for item in torrents:
                if not isinstance(item, dict):
                    continue
                torrent = item.get("torrent")
                if not isinstance(torrent, dict):
                    continue
                size = _bytes_from_any(torrent.get("size"))
                stats["seed_size"] += size
                stats["items"].append(
                    {
                        "id": _string_from_any(torrent.get("id")),
                        "name": _string_from_any(torrent.get("name")),
                        "size": size,
                    }
                )
            total = int(_float_from_any(page.get("total")) or 0)
            if total and not stats["seed_count"]:
                stats["seed_count"] = total
            total_pages = int(_float_from_any(page.get("totalPages")) or 0)
            if not torrents or (total_pages and page_number >= total_pages) or (total and len(stats["items"]) >= total):
                break
            page_number += 1
        if stats["items"] and (not stats["seed_count"] or len(stats["items"]) > stats["seed_count"]):
            stats["seed_count"] = len(stats["items"])
        return stats

    def _optional_request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return self._request(method, path, body)
        except MTeamApiError:
            return {}

    def _normalize_torrent(self, item: dict[str, Any]) -> dict[str, Any]:
        title = _string_from_any(_pick(item, "name", "title", "smallDescr", "subtitle")) or "M-Team 资源"
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        size_value = _pick(item, "size", "fileSize", "torrentSize")
        return {
            "id": str(_pick(item, "id", "torrentId", "tid") or title),
            "title": title,
            "resolution": _string_from_any(_pick(item, "standard", "resolution")) or _guess_resolution(title),
            "codec": _string_from_any(_pick(item, "videoCodec", "codec")) or _guess_codec(title),
            "hdr": _string_from_any(_pick(item, "hdr", "processing")) or "",
            "size": _size_label(size_value),
            "group": _string_from_any(_pick(item, "team", "group", "author")) or "",
            "seeders": int(_float_from_any(_pick(item, "seeders", "seedCount", default=status.get("seeders"))) or 0),
            "downloads": int(_float_from_any(_pick(item, "leechers", "downloaders", "downloads", default=status.get("leechers"))) or 0),
            "published_at": _string_from_any(_pick(item, "createdDate", "createdAt", "publishDate", "releaseDate")) or "",
        }

    def _first_success(self, attempts: list[tuple[str, str, dict[str, Any] | None]]) -> dict[str, Any]:
        last_error: Exception | None = None
        for method, path, body in attempts:
            try:
                return self._request(method, path, body)
            except MTeamApiError as exc:
                last_error = exc
                if str(exc.code) in {"401", "403"} or "key" in str(exc).lower():
                    raise
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise MTeamApiError("M-Team API 未返回可用数据")

    def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(f"{self.base_url}{path}", data=data, headers=self._request_headers(), method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise MTeamApiError(text or exc.reason, http_status=exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise MTeamApiError(f"M-Team 网络请求失败：{exc}") from exc
        if isinstance(payload, dict) and "code" in payload:
            code = payload.get("code")
            success_codes = {0, "0", "SUCCESS", "success", 200, "200"}
            if code not in success_codes:
                raise MTeamApiError(str(payload.get("message") or "M-Team API 返回失败"), code=code)
        return payload if isinstance(payload, dict) else {"data": payload}

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": str(self.headers.get("User-Agent") or self.headers.get("user-agent") or "PT-Media-Hub"),
            "x-api-key": self.api_key,
        }
        for key in ("Cookie", "Authorization", "Referer", "Origin"):
            if self.headers.get(key):
                headers[key] = str(self.headers[key])
        return headers

    def _pick_api_key(self, config: dict[str, Any]) -> str:
        candidates = [
            config.get("api_key"),
            config.get("apikey"),
            self.headers.get("X-API-Key"),
            self.headers.get("x-api-key"),
            self.headers.get("Api-Key"),
        ]
        authorization = str(self.headers.get("Authorization") or self.headers.get("authorization") or "").strip()
        if authorization and not authorization.lower().startswith(("bearer ", "basic ")):
            candidates.append(authorization)
        if authorization.lower().startswith("bearer "):
            candidates.append(authorization[7:].strip())
        candidates.append(config.get("passkey"))
        return next((str(item).strip() for item in candidates if str(item or "").strip()), "")

    def _api_base(self, value: str) -> str:
        url = value.strip().rstrip("/") or MTEAM_API_BASE
        host = urlparse(url).netloc.lower()
        if "kp.m-team" in host or "m-team.cc" == host:
            return MTEAM_API_BASE
        return url


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result[path] = item
            result.update(_flatten(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value[:20]):
            result.update(_flatten(item, f"{prefix}.{index}" if prefix else str(index)))
    return result


def _find_value(flat: dict[str, Any], *names: str) -> Any:
    lowered = {key.lower().replace("_", "").replace("-", ""): value for key, value in flat.items()}
    for name in names:
        target = name.lower().replace("_", "").replace("-", "")
        for key, value in lowered.items():
            if key.endswith(target):
                return value
    return None


def _pick(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return default


def _float_from_any(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else 0.0


def _bytes_from_any(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    number = _float_from_any(text)
    units = {"kb": 1024, "kib": 1024, "mb": 1024**2, "mib": 1024**2, "gb": 1024**3, "gib": 1024**3, "tb": 1024**4, "tib": 1024**4, "pb": 1024**5, "pib": 1024**5}
    unit_match = re.search(r"([kmgtp]i?b)", text, re.IGNORECASE)
    return number * units.get(unit_match.group(1).lower(), 1) if unit_match else number


def _string_from_any(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _date_label(value: str) -> str:
    if not value:
        return "-"
    return value[:10] if len(value) >= 10 else value


def _mteam_role_label(value: Any) -> str:
    roles = {
        "1": "User",
        "2": "Power User",
        "3": "Elite User",
        "4": "Crazy User",
        "5": "Insane User",
        "6": "Veteran User",
        "7": "Extreme User",
        "8": "Ultimate User",
        "9": "Nexus Master",
        "10": "VIP",
        "11": "Retiree",
        "12": "Uploader",
        "13": "Moderator",
        "14": "Administrator",
        "15": "Sysop",
        "16": "Staff",
        "17": "Offer memberStaff",
        "18": "Bet memberStaff",
    }
    return roles.get(str(value or "").strip(), "")


def _size_label(value: Any) -> str:
    size = _bytes_from_any(value)
    if size <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    current = size
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.2f} {unit}" if unit in {"GB", "TB", "PB"} else f"{current:.0f} {unit}"
        current /= 1024
    return f"{current:.2f} PB"


def _guess_resolution(title: str) -> str:
    match = re.search(r"(2160p|1080p|720p|4k)", title, re.IGNORECASE)
    return match.group(1) if match else "-"


def _guess_codec(title: str) -> str:
    match = re.search(r"(H\.?265|H\.?264|HEVC|AVC|AV1|x265|x264)", title, re.IGNORECASE)
    return match.group(1) if match else "-"


def _extract_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "records", "list", "results", "torrents"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _extract_items(value)
                if nested:
                    return nested
    return []


def _traffic_history(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = _extract_items(data.get("trafficHistory") or data.get("history") or data.get("traffic") or [])
    history = []
    for item in items[-30:]:
        flat = _flatten(item)
        history.append(
            {
                "date": _string_from_any(_find_value(flat, "date", "day", "capturedAt"))[:10],
                "upload_total": _bytes_from_any(_find_value(flat, "upload", "uploaded", "uploadTotal")),
                "download_total": _bytes_from_any(_find_value(flat, "download", "downloaded", "downloadTotal")),
            }
        )
    return history


def _compact_raw_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if isinstance(value, (str, int, float, bool)) and key.lower() not in {"passkey", "apikey", "api_key", "token"}}
