import json
from datetime import datetime
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from app.adapters.base import QbittorrentAdapter
from app.utils.ids import trace_id


class QbittorrentConfigError(ValueError):
    pass


class QbittorrentApiError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class QbittorrentWebAdapter(QbittorrentAdapter):
    def __init__(self, config: dict[str, Any]):
        self.name = str(config.get("name") or "").strip()
        self.base_url = str(config.get("base_url") or "").strip().rstrip("/")
        self.username = str(config.get("username") or "").strip()
        self.password = str(config.get("password") or "")
        self.timeout = int(config.get("timeout") or 10)
        self.default_save_path = str(config.get("default_save_path") or "").strip()
        self.default_category = str(config.get("category") or "").strip()
        tags = config.get("tags") or []
        self.default_tags = ",".join(tags) if isinstance(tags, list) else str(tags)
        if not self.base_url or not self.username or not self.password:
            raise QbittorrentConfigError("qB WebUI 地址、用户名和密码必须填写")
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self._logged_in = False

    def get_server_state(self, downloader_id: str) -> dict[str, Any]:
        data = self._json_request("GET", "/api/v2/sync/maindata", query={"rid": 0})
        state = data.get("server_state") if isinstance(data, dict) else {}
        if not isinstance(state, dict):
            state = {}
        torrents = data.get("torrents") if isinstance(data, dict) else {}
        torrent_values = list(torrents.values()) if isinstance(torrents, dict) else []
        active_downloads = sum(1 for item in torrent_values if _float(item.get("dlspeed")) > 0 or str(item.get("state") or "").lower() in DOWNLOADING_STATES)
        active_uploads = sum(1 for item in torrent_values if _float(item.get("upspeed")) > 0 or str(item.get("state") or "").lower() in UPLOADING_STATES)
        paused = sum(1 for item in torrent_values if "paused" in str(item.get("state") or "").lower())
        errors = sum(1 for item in torrent_values if "error" in str(item.get("state") or "").lower())
        return {
            "id": downloader_id,
            "name": self.name or f"qB {downloader_id.replace('qb', '')}",
            "online": True,
            "download_speed": _float(state.get("dl_info_speed")),
            "upload_speed": _float(state.get("up_info_speed")),
            "downloaded_total": _float(state.get("alltime_dl")),
            "uploaded_total": _float(state.get("alltime_ul")),
            "active_downloads": active_downloads,
            "active_uploads": active_uploads,
            "paused": paused,
            "errors": errors,
            "free_space": _float(state.get("free_space_on_disk")),
            "total_space": _float(state.get("total_space") or state.get("total_disk_space")),
            "connection_status": state.get("connection_status"),
            "source": "qB Web API 原始数据（Real）",
            "updated_at": datetime.utcnow().isoformat(),
        }

    def get_torrents(self, downloader_id: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = dict(filters or {})
        items = self._json_request("GET", "/api/v2/torrents/info", query=query)
        if not isinstance(items, list):
            return []
        return [self._normalize_torrent(item) for item in items if isinstance(item, dict)]

    def get_torrent_detail(self, downloader_id: str, torrent_hash: str) -> dict[str, Any]:
        properties = self._json_request("GET", "/api/v2/torrents/properties", query={"hash": torrent_hash})
        files = self._json_request("GET", "/api/v2/torrents/files", query={"hash": torrent_hash})
        trackers = self._json_request("GET", "/api/v2/torrents/trackers", query={"hash": torrent_hash})
        if not isinstance(properties, dict):
            properties = {}
        if not isinstance(files, list):
            files = []
        if not isinstance(trackers, list):
            trackers = []
        return {
            "downloader_id": downloader_id,
            "hash": torrent_hash,
            "properties": self._normalize_properties(properties),
            "files": [self._normalize_file(index, item) for index, item in enumerate(files) if isinstance(item, dict)],
            "trackers": [self._normalize_tracker(item) for item in trackers if isinstance(item, dict)],
            "source": "qB Web API 原始数据（Real）",
            "updated_at": datetime.utcnow().isoformat(),
        }

    def set_file_priority(self, downloader_id: str, torrent_hash: str, file_id: int, priority: int) -> dict[str, Any]:
        if priority not in {0, 1, 6, 7}:
            raise QbittorrentApiError("文件优先级只支持：不下载、普通、高、最高")
        self._text_request(
            "POST",
            "/api/v2/torrents/filePrio",
            form={"hash": torrent_hash, "id": str(file_id), "priority": str(priority)},
        )
        return {"accepted": True, "trace_id": trace_id("QBFILE"), "downloader_id": downloader_id, "hash": torrent_hash, "file_id": file_id, "priority": priority}

    def add_torrent(self, downloader_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        torrent_url = _first_text(payload, "download_url", "torrent_url", "magnet", "url", "urls")
        if not torrent_url:
            raise QbittorrentApiError("当前资源没有可提交给 qB 的下载链接")
        form = {"urls": torrent_url}
        save_path = _first_text(payload, "save_path") or self.default_save_path
        category = _first_text(payload, "category") or self.default_category
        tags = _first_text(payload, "tags") or self.default_tags
        if save_path:
            form["savepath"] = save_path
        if category:
            form["category"] = category
        if tags:
            form["tags"] = tags
        self._text_request("POST", "/api/v2/torrents/add", form=form)
        return {"accepted": True, "trace_id": trace_id("DL"), "downloader_id": downloader_id, "task_hash": None}

    def mutate_torrent(self, downloader_id: str, torrent_hash: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "pause":
            self._torrent_command(["/api/v2/torrents/pause", "/api/v2/torrents/stop"], {"hashes": torrent_hash})
        elif action == "resume":
            self._torrent_command(["/api/v2/torrents/resume", "/api/v2/torrents/start"], {"hashes": torrent_hash})
        elif action == "category":
            self._text_request("POST", "/api/v2/torrents/setCategory", form={"hashes": torrent_hash, "category": str(payload.get("category") or "")})
        elif action == "tags":
            tags = payload.get("tags") or self.default_tags or "pt-media-hub"
            if isinstance(tags, list):
                tags = ",".join(str(item) for item in tags if item)
            self._text_request("POST", "/api/v2/torrents/addTags", form={"hashes": torrent_hash, "tags": str(tags)})
        elif action == "limits":
            if "download_limit" in payload:
                self._text_request("POST", "/api/v2/torrents/setDownloadLimit", form={"hashes": torrent_hash, "limit": str(payload["download_limit"])})
            if "upload_limit" in payload:
                self._text_request("POST", "/api/v2/torrents/setUploadLimit", form={"hashes": torrent_hash, "limit": str(payload["upload_limit"])})
        elif action in {"delete", "delete_files"}:
            self._text_request("POST", "/api/v2/torrents/delete", form={"hashes": torrent_hash, "deleteFiles": "true" if action == "delete_files" else "false"})
        else:
            raise QbittorrentApiError(f"不支持的 qB 操作：{action}")
        return {"accepted": True, "trace_id": trace_id("QBACT"), "downloader_id": downloader_id, "hash": torrent_hash, "action": action}

    def test_connection(self) -> dict[str, Any]:
        version = self._text_request("GET", "/api/v2/app/version").strip()
        state = self.get_server_state("qb")
        return {
            "success": True,
            "version": version,
            "download_speed": state["download_speed"],
            "upload_speed": state["upload_speed"],
            "checked_at": datetime.utcnow().isoformat(),
        }

    def _normalize_torrent(self, item: dict[str, Any]) -> dict[str, Any]:
        tags = item.get("tags") or ""
        tag_list = [part.strip() for part in str(tags).split(",") if part.strip()]
        added_at = _timestamp_label(item.get("added_on"))
        completed_at = _timestamp_label(item.get("completion_on"))
        return {
            "hash": str(item.get("hash") or ""),
            "name": str(item.get("name") or ""),
            "size": _float(item.get("size")),
            "total_size": _float(item.get("total_size") or item.get("size")),
            "amount_left": _float(item.get("amount_left")),
            "completed": _float(item.get("completed")),
            "progress": max(0.0, min(1.0, _float(item.get("progress")))),
            "download_speed": _float(item.get("dlspeed")),
            "upload_speed": _float(item.get("upspeed")),
            "uploaded": _float(item.get("uploaded")),
            "downloaded": _float(item.get("downloaded")),
            "uploaded_session": _float(item.get("uploaded_session")),
            "downloaded_session": _float(item.get("downloaded_session")),
            "ratio": _float(item.get("ratio")),
            "eta": _float(item.get("eta")),
            "availability": _float(item.get("availability")),
            "priority": int(_float(item.get("priority"))),
            "num_seeds": int(_float(item.get("num_seeds"))),
            "num_complete": int(_float(item.get("num_complete"))),
            "num_leechs": int(_float(item.get("num_leechs"))),
            "num_incomplete": int(_float(item.get("num_incomplete"))),
            "download_limit": _float(item.get("dl_limit")),
            "upload_limit": _float(item.get("up_limit")),
            "category": item.get("category") or "",
            "tags": tag_list,
            "save_path": item.get("save_path") or "",
            "content_path": item.get("content_path") or "",
            "tracker": item.get("tracker") or "",
            "added_at": added_at,
            "completed_at": completed_at if completed_at != "1970-01-01T00:00:00" else None,
            "last_activity_at": _timestamp_label(item.get("last_activity")),
            "seen_complete_at": _timestamp_label(item.get("seen_complete")),
            "time_active": _float(item.get("time_active")),
            "seeding_time": _float(item.get("seeding_time")),
            "state": item.get("state") or "",
            "source": "qB Web API 原始数据（Real）",
        }

    def _normalize_properties(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "save_path": item.get("save_path") or "",
            "creation_date": _timestamp_label(item.get("creation_date")),
            "addition_date": _timestamp_label(item.get("addition_date")),
            "completion_date": _timestamp_label(item.get("completion_date")),
            "last_seen": _timestamp_label(item.get("last_seen")),
            "created_by": item.get("created_by") or "",
            "comment": item.get("comment") or "",
            "total_size": _float(item.get("total_size")),
            "piece_size": _float(item.get("piece_size")),
            "pieces_have": int(_float(item.get("pieces_have"))),
            "pieces_num": int(_float(item.get("pieces_num"))),
            "total_wasted": _float(item.get("total_wasted")),
            "total_uploaded": _float(item.get("total_uploaded")),
            "total_uploaded_session": _float(item.get("total_uploaded_session")),
            "total_downloaded": _float(item.get("total_downloaded")),
            "total_downloaded_session": _float(item.get("total_downloaded_session")),
            "download_limit": _float(item.get("dl_limit")),
            "upload_limit": _float(item.get("up_limit")),
            "download_speed": _float(item.get("dl_speed")),
            "upload_speed": _float(item.get("up_speed")),
            "download_speed_avg": _float(item.get("dl_speed_avg")),
            "upload_speed_avg": _float(item.get("up_speed_avg")),
            "eta": _float(item.get("eta")),
            "time_elapsed": _float(item.get("time_elapsed")),
            "seeding_time": _float(item.get("seeding_time")),
            "connections": int(_float(item.get("nb_connections"))),
            "connections_limit": int(_float(item.get("nb_connections_limit"))),
            "share_ratio": _float(item.get("share_ratio")),
            "seeds": int(_float(item.get("seeds"))),
            "seeds_total": int(_float(item.get("seeds_total"))),
            "peers": int(_float(item.get("peers"))),
            "peers_total": int(_float(item.get("peers_total"))),
            "reannounce": _float(item.get("reannounce")),
        }

    def _normalize_file(self, index: int, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": index,
            "name": item.get("name") or "",
            "size": _float(item.get("size")),
            "progress": max(0.0, min(1.0, _float(item.get("progress")))),
            "priority": int(_float(item.get("priority"))),
            "availability": _float(item.get("availability")),
            "is_seed": bool(item.get("is_seed")),
            "piece_range": item.get("piece_range") or [],
        }

    def _normalize_tracker(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": item.get("url") or "",
            "status": int(_float(item.get("status"))),
            "tier": int(_float(item.get("tier"))),
            "num_peers": int(_float(item.get("num_peers"))),
            "num_seeds": int(_float(item.get("num_seeds"))),
            "num_leeches": int(_float(item.get("num_leeches"))),
            "num_downloaded": int(_float(item.get("num_downloaded"))),
            "message": item.get("msg") or "",
        }

    def _torrent_command(self, paths: list[str], form: dict[str, Any]) -> None:
        last_error: QbittorrentApiError | None = None
        for path in paths:
            try:
                self._text_request("POST", path, form=form)
                return
            except QbittorrentApiError as exc:
                last_error = exc
                if exc.http_status != 404:
                    raise
        if last_error:
            raise last_error

    def _json_request(self, method: str, path: str, query: dict[str, Any] | None = None, form: dict[str, Any] | None = None) -> Any:
        text = self._text_request(method, path, query=query, form=form)
        return json.loads(text or "{}")

    def _text_request(self, method: str, path: str, query: dict[str, Any] | None = None, form: dict[str, Any] | None = None) -> str:
        self._ensure_login()
        return self._raw_request(method, path, query=query, form=form)

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        body = self._raw_request("POST", "/api/v2/auth/login", form={"username": self.username, "password": self.password}, skip_login=True)
        if body.strip().lower() not in {"ok.", "ok"}:
            raise QbittorrentApiError("qB 登录失败，请检查用户名和密码", 401)
        self._logged_in = True

    def _raw_request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        skip_login: bool = False,
    ) -> str:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode({key: value for key, value in query.items() if value is not None})}"
        data = urlencode(form).encode("utf-8") if form is not None else None
        headers = {"User-Agent": "PT-Media-Hub"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            raise QbittorrentApiError(f"qB Web API 返回 HTTP {exc.code}: {detail}", exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise QbittorrentApiError(f"无法连接 qB WebUI：{exc}") from exc


DOWNLOADING_STATES = {"downloading", "stalleddl", "metadl", "forceddl", "queueddl", "allocating", "checkingdl"}
UPLOADING_STATES = {"uploading", "stalledup", "forcedup", "queuedup", "checkingup"}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _timestamp_label(value: Any) -> str | None:
    seconds = _float(value)
    if seconds <= 0:
        return None
    return datetime.utcfromtimestamp(seconds).isoformat()


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            value = ",".join(str(item) for item in value if item)
        if value not in (None, ""):
            return str(value).strip()
    return ""
