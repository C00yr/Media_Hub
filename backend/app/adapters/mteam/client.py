import json
import re
import base64
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from app.adapters.base import TrackerAdapter


MTEAM_API_BASE = "https://api.m-team.cc"

MTEAM_CATEGORY_LABELS = {
    "401": "电影/SD",
    "402": "影剧/综艺/HD",
    "403": "纪录/教育",
    "404": "动漫",
    "405": "音乐",
    "406": "体育",
    "407": "软件",
    "408": "游戏",
    "409": "电子书",
    "410": "有声读物",
    "411": "MV",
    "412": "综艺",
    "419": "电影/HD",
    "420": "剧集/HD",
    "421": "动画/HD",
    "422": "纪录/HD",
}
MTEAM_STANDARD_LABELS = {
    "1": "1080p",
    "2": "720p",
    "3": "2160p",
    "4": "1080i",
    "5": "480p",
}
MTEAM_VIDEO_CODEC_LABELS = {
    "1": "H.264",
    "2": "H.265",
    "3": "VC-1",
    "4": "MPEG-2",
    "5": "AV1",
}
MTEAM_AUDIO_CODEC_LABELS = {
    "1": "DTS",
    "2": "DTS-HD MA",
    "3": "TrueHD",
    "4": "FLAC",
    "5": "LPCM",
    "6": "AAC",
    "7": "DDP",
    "8": "AC3",
    "9": "MP3",
}
MTEAM_SOURCE_LABELS = {
    "1": "Blu-ray",
    "2": "DVD",
    "3": "HDTV",
    "4": "WEB",
    "5": "UHD Blu-ray",
}
MTEAM_MEDIUM_LABELS = {
    "1": "Blu-ray",
    "2": "Remux",
    "3": "Encode",
    "4": "WEB-DL",
    "5": "WEBRip",
    "6": "HDTV",
}
MTEAM_PROCESSING_LABELS = {
    "1": "原盘",
    "2": "Remux",
    "3": "Encode",
}


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
        self.opener = build_opener(ProxyHandler({}))
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
        profile_seed_count = int(_float_from_any(_find_value(flat, "seedCount", "seedingCount", "activeUploads", "seeders")) or 0)
        profile_seed_size = _bytes_from_any(_find_value(flat, "seedSize", "seedingSize", "seedVolume", "seedingVolume"))
        seeding_stats = self._get_seeding_stats(user_id, seed_count_hint=profile_seed_count, seed_size_hint=profile_seed_size)
        upload_total = _bytes_from_any(member_count.get("uploaded") or _find_value(flat, "uploaded", "uploadTotal", "totalUpload", "uploadedBytes", "uploadBytes"))
        download_total = _bytes_from_any(member_count.get("downloaded") or _find_value(flat, "downloaded", "downloadTotal", "totalDownload", "downloadedBytes", "downloadBytes"))
        bonus = _float_from_any(member_count.get("bonus") or _find_value(flat, "bonus", "bonusValue", "magic", "magicPoint", "point", "points", "credit"))
        ratio = _float_from_any(member_count.get("shareRate") or _find_value(flat, "shareRate", "ratio", "share_rate", "uploadedDownloadedRatio"))
        seed_count = seeding_stats["seed_count"] or profile_seed_count
        seed_size = seeding_stats["seed_size"] or profile_seed_size
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
            "active_uploads": seeding_stats["active_uploads"],
            "active_downloads": seeding_stats["active_downloads"],
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
        return {"torrent_id": torrent_id, "download_url": f"{self.base_url}/api/torrent/genDlToken", "download_method": "POST"}

    def download_torrent_file(self, torrent_id: str) -> dict[str, Any]:
        torrent_id = str(torrent_id or "").strip()
        if not torrent_id:
            raise MTeamApiError("缺少 M-Team 种子 ID")
        attempts: list[tuple[str, str, dict[str, Any] | None]] = []
        errors: list[str] = []
        try:
            token_url = self._generate_download_token_url(torrent_id)
        except MTeamApiError as exc:
            token_url = ""
            errors.append(f"genDlToken: {exc}")
        if token_url:
            attempts.append(("GET", token_url, None))
        attempts.extend(
            [
                ("GET", f"{self.base_url}/api/torrent/download/{torrent_id}", None),
                ("GET", f"{self.base_url}/api/torrent/download?{urlencode({'id': torrent_id})}", None),
                ("POST", f"{self.base_url}/api/torrent/download", {"id": torrent_id}),
            ]
        )
        for method, url, body in attempts:
            try:
                content, content_type, filename = self._download_bytes(method, url, body)
                content, content_type, filename = self._resolve_download_response(content, content_type, filename)
                if content and not _looks_like_json(content, content_type):
                    return {
                        "torrent_id": torrent_id,
                        "filename": filename or f"mteam-{torrent_id}.torrent",
                        "content_type": content_type or "application/x-bittorrent",
                        "content": content,
                    }
                summary = _json_error_summary(content)
                if summary:
                    errors.append(f"{method} {urlparse(url).path}: {summary}")
            except MTeamApiError as exc:
                errors.append(f"{method} {urlparse(url).path}: {exc}")
        detail = f"：{'; '.join(errors[-4:])}" if errors else ""
        raise MTeamApiError(f"M-Team 未返回有效的 .torrent 文件{detail}")

    def _generate_download_token_url(self, torrent_id: str) -> str:
        payload = self._request_form("POST", "/api/torrent/genDlToken", {"id": torrent_id})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, str):
            value = data.strip()
            if value.startswith("http"):
                return value
            if value:
                return f"{self.base_url}/api/torrent/download?credential={value}"
        if isinstance(data, dict):
            value = _download_url_from_payload({"data": data})
            if value:
                return f"{self.base_url}{value}" if value.startswith("/") else value
            credential = data.get("credential") or data.get("token")
            if credential:
                return f"{self.base_url}/api/torrent/download?credential={credential}"
        return ""

    def _resolve_download_response(self, content: bytes, content_type: str, filename: str) -> tuple[bytes, str, str]:
        if not _looks_like_json(content, content_type):
            return content, content_type, filename
        payload = json.loads(content.decode("utf-8", "replace"))
        nested_url = _download_url_from_payload(payload)
        if nested_url:
            if nested_url.startswith("/"):
                nested_url = f"{self.base_url}{nested_url}"
            return self._download_bytes("GET", nested_url, None)
        encoded = _download_base64_from_payload(payload)
        if encoded:
            try:
                return base64.b64decode(encoded), "application/x-bittorrent", filename
            except ValueError:
                return content, content_type, filename
        return content, content_type, filename

    def _get_seeding_stats(self, user_id: str, seed_count_hint: int = 0, seed_size_hint: float = 0.0) -> dict[str, Any]:
        stats = {"seed_count": seed_count_hint, "seed_size": seed_size_hint, "active_uploads": 0, "active_downloads": 0, "items": []}
        if not user_id:
            return stats
        peer_status = self._optional_request("POST", "/api/tracker/myPeerStatus", {"uid": user_id})
        peer_data = peer_status.get("data") if isinstance(peer_status, dict) else {}
        if isinstance(peer_data, dict):
            stats["active_uploads"] = int(_float_from_any(peer_data.get("seeder")) or 0)
            stats["active_downloads"] = int(_float_from_any(peer_data.get("leecher")) or 0)
        if seed_size_hint > 0:
            return stats

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
        flat = _flatten(item)
        title = _string_from_any(_pick(item, "name", "title", "smallDescr", "subtitle")) or "M-Team 资源"
        subtitle = _string_from_any(_pick(item, "smallDescr", "subtitle", "description") or _find_value(flat, "smallDescr", "subtitle", "description"))
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        size_value = _pick(item, "size", "fileSize", "torrentSize")
        torrent_id = str(_pick(item, "id", "torrentId", "tid") or _find_value(flat, "id", "torrentId", "tid") or title)
        category = _category_label(_pick(item, "category", "categoryName", "browseType") or _find_value(flat, "categoryName", "browseType", "category"))
        labels = _torrent_labels(item, title, subtitle, flat)
        promotion = _promotion_info(item, flat)
        resolution = _enum_label(_pick(item, "standard", "resolution"), MTEAM_STANDARD_LABELS) or _guess_resolution(title)
        codec = _enum_label(_pick(item, "videoCodec", "codec"), MTEAM_VIDEO_CODEC_LABELS) or _guess_codec(title)
        audio_codec = _enum_label(_pick(item, "audioCodec", "audio"), MTEAM_AUDIO_CODEC_LABELS) or _guess_audio(title)
        return {
            "id": torrent_id,
            "title": title,
            "subtitle": subtitle,
            "resolution": resolution,
            "codec": codec,
            "hdr": _hdr_label(_pick(item, "hdr", "processing") or _find_value(flat, "hdr", "processing"), title),
            "audio_codec": audio_codec,
            "size": _size_label(size_value),
            "size_bytes": _bytes_from_any(size_value),
            "group": _display_text(_pick(item, "team", "group", "author")) or _release_group(title),
            "category": category,
            "labels": labels,
            "discount": promotion["raw_type"],
            "promotion_type": promotion["type"],
            "promotion_label": promotion["label"],
            "promotion_until": promotion["until"],
            "promotion_remaining": promotion["remaining"],
            "imdb_rating": _rating_label(_pick(item, "imdbRating", "imdb_rate") or _find_value(flat, "imdbRating", "imdbRate")),
            "douban_rating": _rating_label(_pick(item, "doubanRating", "douban_rate") or _find_value(flat, "doubanRating", "doubanRate")),
            "seeders": int(_float_from_any(_pick(item, "seeders", "seedCount", default=status.get("seeders"))) or 0),
            "downloads": int(_float_from_any(_pick(item, "leechers", "downloaders", "downloads", default=status.get("leechers"))) or 0),
            "completed": int(_float_from_any(_pick(item, "completed", "snatched", "finish", "finishCount") or _find_value(flat, "completed", "snatched", "finishCount")) or 0),
            "comments": int(_float_from_any(_pick(item, "comments", "commentCount") or _find_value(flat, "commentCount", "comments")) or 0),
            "published_at": _string_from_any(_pick(item, "createdDate", "createdAt", "publishDate", "releaseDate")) or "",
            "detail_url": f"https://kp.m-team.cc/detail/{torrent_id}" if torrent_id else "",
            "download_url": f"{self.base_url}/api/torrent/download/{torrent_id}" if torrent_id else "",
            "raw_summary": _compact_raw_summary(item),
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
            with self.opener.open(request, timeout=self.timeout) as response:
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

    def _request_form(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = urlencode(body).encode("utf-8")
        headers = {
            **self._request_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        request = Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
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

    def _download_bytes(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[bytes, str, str]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(url, data=data, headers={**self._request_headers(), "Accept": "application/x-bittorrent,*/*"}, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                disposition = response.headers.get("Content-Disposition", "")
                return response.read(), content_type, _filename_from_disposition(disposition)
        except HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise MTeamApiError(text or exc.reason, http_status=exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise MTeamApiError(f"M-Team 种子下载失败：{exc}") from exc

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


def _display_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "title", "label", "value", "id"):
            if value.get(key) not in (None, ""):
                return str(value[key])
        return ""
    return _string_from_any(value)


def _enum_label(value: Any, mapping: dict[str, str]) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    return mapping.get(key, "" if key.isdigit() else key)


def _category_label(value: Any) -> str:
    return _enum_label(value, MTEAM_CATEGORY_LABELS) or _display_text(value)


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


def _guess_audio(title: str) -> str:
    match = re.search(r"(TrueHD|DTS[-\s]?HD|DTS|Atmos|AAC|FLAC|LPCM|DDP?\.?5\.1|DDP?\.?7\.1)", title, re.IGNORECASE)
    return match.group(1) if match else ""


def _hdr_label(value: Any, title: str) -> str:
    raw = _string_from_any(value).strip()
    if raw and not raw.isdigit():
        return raw
    match = re.search(r"(HDR10\+?|DoVi|Dolby Vision|HLG|SDR)", title, re.IGNORECASE)
    return match.group(1) if match else ""


def _release_group(title: str) -> str:
    match = re.search(r"-([A-Za-z0-9][A-Za-z0-9._-]{1,24})$", title.strip())
    return match.group(1) if match else ""


def _rating_label(value: Any) -> str:
    rating = _float_from_any(value)
    return f"{rating:.1f}" if rating > 0 else ""


def _torrent_labels(item: dict[str, Any], title: str, subtitle: str, flat: dict[str, Any]) -> list[str]:
    text = f"{title} {subtitle}".lower()
    labels: list[str] = []
    category = _category_label(_pick(item, "category", "categoryName", "browseType") or _find_value(flat, "categoryName", "browseType"))
    if category:
        labels.append(category)
    for source, mapping in (
        (_pick(item, "source"), MTEAM_SOURCE_LABELS),
        (_pick(item, "medium"), MTEAM_MEDIUM_LABELS),
        (_pick(item, "processing"), MTEAM_PROCESSING_LABELS),
        (_pick(item, "standard", "resolution"), MTEAM_STANDARD_LABELS),
        (_pick(item, "videoCodec", "codec"), MTEAM_VIDEO_CODEC_LABELS),
        (_pick(item, "audioCodec", "audio"), MTEAM_AUDIO_CODEC_LABELS),
    ):
        label = _enum_label(source, mapping)
        if label:
            labels.append(label)
    labels_new = item.get("labelsNew")
    if isinstance(labels_new, list):
        for label in labels_new:
            text_label = _display_text(label)
            if text_label:
                labels.append(text_label)
    checks = [
        ("4K", r"\b(2160p|4k|uhd)\b"),
        ("1080p", r"\b1080p\b"),
        ("720p", r"\b720p\b"),
        ("HDR10", r"\bhdr10\b|\bhdr\b"),
        ("DoVi", r"\bdovi\b|dolby vision"),
        ("中字", r"中字|ch[si]|cns|cht"),
        ("原盘", r"remux|blu-?ray|原盘"),
        ("WEB-DL", r"web-?dl"),
        ("Netflix", r"\bnf\b|netflix"),
        ("AMZN", r"\bamzn\b|amazon"),
    ]
    for label, pattern in checks:
        if re.search(pattern, text, re.IGNORECASE):
            labels.append(label)
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    for tag in tags:
        name = _display_text(tag)
        if name:
            labels.append(name)
    result = []
    for label in labels:
        if label and label not in result:
            result.append(label)
    return result[:8]


def _promotion_info(item: dict[str, Any], flat: dict[str, Any]) -> dict[str, str]:
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    promotion_rule = status.get("promotionRule") if isinstance(status.get("promotionRule"), dict) else {}
    mall_single_free = status.get("mallSingleFree") if isinstance(status.get("mallSingleFree"), dict) else {}
    rule_type = _string_from_any(promotion_rule.get("discount") or mall_single_free.get("discount")).strip()
    status_type = _string_from_any(status.get("discount")).strip()
    item_type = _string_from_any(
        _pick(item, "discount", "statusDiscount", "discountType", "promotion", "promotionType")
        or _find_value(flat, "discount", "statusDiscount", "discountType", "promotion", "promotionType")
    ).strip()
    raw_type = rule_type or status_type or item_type
    explicit_free_until = (
        promotion_rule.get("endTime")
        or mall_single_free.get("endTime")
        or _pick(item, "freeUntil", "freeEndTime", "freeDeadline", "freeDeadlineDate")
        or _find_value(flat, "freeUntil", "freeEndTime", "freeDeadline", "freeDeadlineDate")
    )
    generic_until = (
        explicit_free_until
        or status.get("discountEndTime")
        or _pick(item, "discountEndTime", "promotionEndTime", "promotionUntil", "discountUntil")
        or _find_value(flat, "discountEndTime", "promotionEndTime", "promotionUntil", "discountUntil")
    )
    until = _iso_datetime_label(generic_until)
    remaining = _time_left_label(generic_until)
    normalized = raw_type.upper().replace("-", "_").replace(" ", "_")

    if rule_type.upper() == "FREE" or explicit_free_until or normalized in {"FREE", "PERCENT_100", "FREE_100", "FREE100", "ZERO", "PERCENT_0"}:
        label = "FREE"
        promo_type = "free"
    elif normalized.startswith("PERCENT_"):
        percent = normalized.removeprefix("PERCENT_")
        label = f"{percent}%"
        promo_type = "percent"
    elif normalized in {"HALF", "HALF_DOWN", "HALFDOWNLOAD"}:
        label = "50%"
        promo_type = "percent"
    elif normalized in {"DOUBLE", "TWO_X", "X2", "DOUBLE_UPLOAD"}:
        label = "2X"
        promo_type = "multiplier"
    elif normalized and normalized not in {"NORMAL", "NONE", "NO"}:
        label = raw_type
        promo_type = "custom"
    else:
        label = ""
        promo_type = ""

    if label == "FREE" and remaining:
        label = f"FREE {remaining}"
    elif label and remaining and promo_type != "custom":
        label = f"{label} {remaining}"
    return {"raw_type": raw_type, "type": promo_type, "label": label, "until": until, "remaining": remaining}


def _iso_datetime_label(value: Any) -> str:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else ""


def _time_left_label(value: Any) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return ""
    seconds = int((parsed - datetime.now(parsed.tzinfo or timezone.utc)).total_seconds())
    if seconds <= 0:
        return ""
    days, rest = divmod(seconds, 86400)
    hours = rest // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = (rest % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,13}", text):
            seconds = float(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except ValueError:
                return None
    if seconds > 10_000_000_000:
        seconds /= 1000
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


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


def _looks_like_json(content: bytes, content_type: str) -> bool:
    prefix = content[:32].lstrip()
    return "json" in content_type.lower() or prefix.startswith(b"{") or prefix.startswith(b"[")


def _download_url_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        data = payload.get("data")
        for value in (
            payload.get("download_url"),
            payload.get("downloadUrl"),
            payload.get("url"),
            data.get("download_url") if isinstance(data, dict) else None,
            data.get("downloadUrl") if isinstance(data, dict) else None,
            data.get("url") if isinstance(data, dict) else None,
            data if isinstance(data, str) else None,
        ):
            if value:
                return str(value)
    return ""


def _download_base64_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        data = payload.get("data")
        for value in (
            payload.get("torrent"),
            payload.get("torrentBase64"),
            payload.get("content"),
            data.get("torrent") if isinstance(data, dict) else None,
            data.get("torrentBase64") if isinstance(data, dict) else None,
            data.get("content") if isinstance(data, dict) else None,
        ):
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _json_error_summary(content: bytes) -> str:
    try:
        payload = json.loads(content.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("msg") or payload.get("error")
        code = payload.get("code")
        if message:
            return f"{message}（code: {code}）" if code not in (None, "") else str(message)
        return str({key: payload.get(key) for key in ("code", "data") if key in payload})[:220]
    return str(payload)[:220]


def _filename_from_disposition(value: str) -> str:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value or "", re.IGNORECASE)
    return match.group(1).strip() if match else ""


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
