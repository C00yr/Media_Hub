import os
import hashlib
import json
import logging
import mimetypes
import re
import shutil
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from socket import timeout as SocketTimeout
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.ai import AIConfigError, AIServiceError, DeepSeekChatAdapter
from app.adapters.mock import MockMetadataAdapter
from app.adapters.mteam import MTeamAdapter, MTeamApiError, MTeamConfigError
from app.adapters.qbittorrent import QbittorrentApiError, QbittorrentConfigError, QbittorrentWebAdapter
from app.adapters.tmdb import TmdbAdapter
from app.adapters.tmdb.client import DEFAULT_TMDB_PROXY_URL, TmdbConfigError, TmdbDohError, TmdbImageError, open_tmdb_network_request
from app.adapters.wechat_claw import WechatClawAdapter, WechatClawApiError, WechatClawConfigError
from app.api.deps import get_current_user, has_qb2_grant, require_admin
from app.auth.security import create_access_token, decode_token, hash_password, verify_password
from app.config.settings import get_settings
from app.db.session import SessionLocal, get_db
from app.diagnostics.tracing import TraceRecorder
from app.models.entities import (
    ConfigAuditLog,
    DebugTrace,
    DiagnosticExport,
    DownloadAction,
    IntegrationConfig,
    MTeamSnapshot,
    Notification,
    QbSnapshot,
    Setting,
    User,
    UserSession,
)
from app.services.integrations import (
    PROVIDERS,
    get_config,
    get_decrypted_config,
    record_test_result,
    serialize_config,
    set_enabled,
    upsert_config,
)
from app.utils.ids import trace_id
from app.utils.redaction import redact_payload


router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
_PRELOAD_TASK_LOCK = Lock()
_PRELOAD_TASKS_RUNNING: set[str] = set()
_WECHAT_CLAW_POLL_LOCKS: dict[int, Lock] = {}
_WECHAT_CLAW_POLL_LOCK_GUARD = Lock()
_WECHAT_CLAW_ACTIVE_USER_ID: ContextVar[int | None] = ContextVar("wechat_claw_active_user_id", default=None)
PRELOAD_PREFIX = "preload."
TMDB_IMAGE_SIZES = {"w92", "w154", "w185", "w342", "w500", "w780", "w1280", "original"}
TMDB_IMAGE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)
TMDB_DIRECT_IMAGE_RE = re.compile(
    r"https://image\.tmdb\.org/t/p/(?P<size>w\d+|original)/(?P<path>[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp))",
    re.IGNORECASE,
)
TMDB_IMAGE_MAX_BYTES = 20 * 1024 * 1024


class SetupAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminVerifyRequest(BaseModel):
    username: str
    password: str


class IntegrationPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class QbActionPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm_token: str | None = None


class AssistantChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AssistantExecuteRequest(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class NotificationPreferencesPayload(BaseModel):
    preferences: dict[str, bool] = Field(default_factory=dict)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class WechatClawMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    user_id: str = ""
    conversation_id: str = ""
    context_token: str = ""


DEFAULT_NOTIFICATION_PREFERENCES: dict[str, bool] = {
    "download_started": True,
    "download_completed": True,
    "resource_search": False,
    "status_query": False,
    "wechat_claw_push": True,
}

NOTIFICATION_PREFERENCES_KEY = "notification.preferences"
WECHAT_CLAW_INTERACTIONS_KEY = "wechat_claw.interactions"
WECHAT_CLAW_ILINK_STATE_KEY = "wechat_claw.ilink_state"
WECHAT_CLAW_LAST_POLL_KEY = "wechat_claw.last_poll"
WECHAT_CLAW_INTERACTION_LIMIT = 20
WECHAT_CLAW_PENDING_LIMIT = 100


def wechat_claw_setting_key(key: str, user_id: int | None = None) -> str:
    effective_user_id = user_id if user_id is not None else _WECHAT_CLAW_ACTIVE_USER_ID.get()
    return key if effective_user_id is None else f"{key}.user.{effective_user_id}"


def wechat_claw_poll_lock(user_id: int | None) -> Lock:
    key = int(user_id or 0)
    with _WECHAT_CLAW_POLL_LOCK_GUARD:
        return _WECHAT_CLAW_POLL_LOCKS.setdefault(key, Lock())

WECHAT_CLAW_MOBILE_SECTIONS: list[dict[str, Any]] = [
    {"key": "discover", "label": "发现", "description": "TMDB 趋势、热门、筛选和 M-Team 搜索入口。"},
    {"key": "dashboard", "label": "仪表盘", "description": "M-Team、qB 下载器、NAS 空间总览。"},
    {"key": "downloads", "label": "下载", "description": "qB1/qB2/qB3 下载任务状态与操作入口。"},
    {"key": "notifications", "label": "通知", "description": "通知中心、AI 助手和通知偏好。"},
    {"key": "settings", "label": "设置", "description": "M-Team、qB、TMDB、AI、WeChat claw 等运行配置。"},
    {"key": "diagnostics", "label": "诊断", "description": "管理员诊断健康检查与脱敏导出。"},
]

WECHAT_CLAW_CAPABILITIES: list[dict[str, Any]] = [
    {"action": "resource_search", "label": "资源查询", "examples": ["帮我搜一下沙丘 2160p", "查 M-Team 上有没有周处除三害"]},
    {"action": "download_started", "label": "下载开始通知", "examples": ["记录一下沙丘已经开始下载"]},
    {"action": "download_completed", "label": "下载完成通知", "examples": ["沙丘下载完成了，提醒我"]},
    {
        "action": "status_query",
        "label": "状态查询",
        "targets": ["dashboard", "mteam", "qb", "downloads", "notifications", "stats", "diagnostics", "discover"],
        "examples": ["qB1 现在速度多少", "查一下仪表盘状态", "诊断模块是否正常"],
    },
]


def bytes_label(value: float, digits: int = 1) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.{digits}f} {unit}"
        current /= 1024
    return f"{current:.{digits}f} PB"


DEFAULT_STORAGE_MOUNT_PATHS = ("/mnt/storage1", "/mnt/storage2", "/mnt/storage3")


def signed_bytes_delta(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{bytes_label(abs(value), 2)}"


def signed_number_delta(value: float, digits: int = 1) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.{digits}f}"


def signed_int_delta(value: int) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value)}"


def apply_mteam_five_hour_deltas(db: Session, mteam: dict[str, Any]) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(hours=5)
    previous = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.source != "mock", MTeamSnapshot.captured_at <= cutoff)
        .order_by(MTeamSnapshot.captured_at.desc())
        .first()
    )
    mteam["delta_window_label"] = "近5h"
    if previous is None:
        mteam["delta_preview"] = True
        return mteam

    current_level = str(mteam.get("user_level") or "")
    previous_level = str(previous.user_level or "")
    mteam["user_level_delta_label"] = "无变化" if not previous_level or previous_level == current_level else f"{previous_level} → {current_level}"
    mteam["upload_delta_label"] = signed_bytes_delta(float(mteam.get("upload_total") or 0) - float(previous.upload_total or 0))
    mteam["download_delta_label"] = signed_bytes_delta(float(mteam.get("download_total") or 0) - float(previous.download_total or 0))
    mteam["bonus_delta_label"] = signed_number_delta(float(mteam.get("bonus") or 0) - float(previous.bonus or 0), 1)
    mteam["ratio_delta_label"] = signed_number_delta(float(mteam.get("ratio") or 0) - float(previous.ratio or 0), 3)
    mteam["seed_size_delta_label"] = signed_bytes_delta(float(mteam.get("seed_size") or 0) - float(getattr(previous, "seed_size", 0) or 0))
    upload_delta = int(mteam.get("active_uploads") or 0) - int(previous.active_uploads or 0)
    download_delta = int(mteam.get("active_downloads") or 0) - int(previous.active_downloads or 0)
    mteam["active_delta_label"] = f"上传 {signed_int_delta(upload_delta)} / 下载 {signed_int_delta(download_delta)}"
    return mteam


def preload_cache_key(name: str) -> str:
    return f"{PRELOAD_PREFIX}{name}"


def get_preload_cache(db: Session, name: str) -> dict[str, Any] | None:
    row = db.query(Setting).filter(Setting.key == preload_cache_key(name)).one_or_none()
    if not row or not isinstance(row.value, dict) or "payload" not in row.value:
        return None
    return row.value


def set_preload_cache(db: Session, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == preload_cache_key(name)).one_or_none()
    value = {"payload": payload, "cached_at": datetime.utcnow().isoformat(), "name": name}
    if row is None:
        row = Setting(key=preload_cache_key(name), value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    db.commit()
    return value


def _rewrite_tmdb_image_url(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        size = match.group("size")
        image_path = match.group("path").lstrip("/")
        if size not in TMDB_IMAGE_SIZES:
            return match.group(0)
        return f"/api/tmdb/image/{size}/{image_path}"

    return TMDB_DIRECT_IMAGE_RE.sub(replace, value)


def rewrite_tmdb_image_urls(payload: Any) -> Any:
    if isinstance(payload, str):
        return _rewrite_tmdb_image_url(payload)
    if isinstance(payload, list):
        return [rewrite_tmdb_image_urls(item) for item in payload]
    if isinstance(payload, dict):
        return {key: rewrite_tmdb_image_urls(value) for key, value in payload.items()}
    return payload


def with_preload_meta(payload: dict[str, Any], cache: dict[str, Any] | None, preloaded: bool) -> dict[str, Any]:
    result = dict(rewrite_tmdb_image_urls(payload))
    result["_preload"] = {
        "preloaded": preloaded,
        "cached_at": cache.get("cached_at") if cache else None,
        "refreshing": False,
    }
    return result


def add_preload_task_once(background_tasks: BackgroundTasks, key: str, task, *args: Any) -> None:
    with _PRELOAD_TASK_LOCK:
        if key in _PRELOAD_TASKS_RUNNING:
            return
        _PRELOAD_TASKS_RUNNING.add(key)
    background_tasks.add_task(run_preload_task_once, key, task, *args)


def run_preload_task_once(key: str, task, *args: Any) -> None:
    try:
        task(*args)
    finally:
        with _PRELOAD_TASK_LOCK:
            _PRELOAD_TASKS_RUNNING.discard(key)


def _safe_tmdb_image_path(image_path: str) -> str:
    clean_path = image_path.strip().lstrip("/")
    if not clean_path or "\\" in clean_path or ".." in clean_path or clean_path.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid TMDB image path")
    if len(clean_path) > 240 or not TMDB_IMAGE_PATH_RE.fullmatch(clean_path):
        raise HTTPException(status_code=400, detail="Invalid TMDB image path")
    return clean_path


def _tmdb_image_cache_file(size: str, image_path: str) -> Path:
    digest = hashlib.sha1(f"{size}/{image_path}".encode("utf-8")).hexdigest()
    suffix = Path(image_path).suffix.lower() or ".jpg"
    return Path(settings.tmdb_image_cache_dir) / f"{digest}{suffix}"


def _tmdb_image_headers(cache_file: Path) -> dict[str, str]:
    stat = cache_file.stat()
    return {
        "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
        "ETag": f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"',
    }


def _valid_tmdb_image_bytes(content: bytes) -> bool:
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


def _tmdb_image_media_type_from_bytes(content: bytes, fallback: str = "image/jpeg") -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return fallback


def _valid_tmdb_image_file(cache_file: Path) -> bool:
    try:
        with cache_file.open("rb") as handle:
            return _valid_tmdb_image_bytes(handle.read(32))
    except OSError:
        return False


def _tmdb_cached_image_media_type(cache_file: Path, fallback: str) -> str:
    try:
        with cache_file.open("rb") as handle:
            return _tmdb_image_media_type_from_bytes(handle.read(32), fallback)
    except OSError:
        return fallback


def _tmdb_image_error_label(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, TmdbDohError):
        return exc.error_type
    if isinstance(exc, URLError):
        reason = str(getattr(exc, "reason", exc))
        return f"url_error:{reason}"[:180]
    return f"{exc.__class__.__name__}:{exc}"[:180]


def _tmdb_image_placeholder_response(reason: str = "fetch_failed") -> Response:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="342" height="513" viewBox="0 0 342 513">'
        '<rect width="342" height="513" fill="#e5e9f0"/>'
        '<path d="M96 208h150v97H96z" fill="#cbd5e1"/>'
        '<circle cx="130" cy="238" r="17" fill="#94a3b8"/>'
        '<path d="M107 288l44-45 31 31 21-22 34 36z" fill="#94a3b8"/>'
        '<text x="171" y="350" text-anchor="middle" font-family="Arial,sans-serif" '
        'font-size="24" font-weight="700" fill="#64748b">No Image</text>'
        "</svg>"
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-TMDB-Image-Status": "placeholder",
            "X-TMDB-Image-Error": reason[:180],
        },
    )


def _prune_tmdb_image_cache(cache_dir: Path) -> None:
    max_bytes = max(1, int(settings.tmdb_image_cache_max_mb)) * 1024 * 1024
    files = [path for path in cache_dir.iterdir() if path.is_file() and not path.name.endswith(".tmp")]
    total = sum(path.stat().st_size for path in files)
    if total <= max_bytes:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        try:
            total -= path.stat().st_size
            path.unlink()
        except OSError:
            continue
        if total <= max_bytes:
            break


def _tmdb_image_network_config(db: Session) -> tuple[str, str, int]:
    row = get_config(db, "tmdb")
    config = get_decrypted_config(db, "tmdb") if row and row.encrypted_payload else {}
    mode = str((config or {}).get("mode") or settings.tmdb_mode or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        mode = "direct"
    proxy_url = str((config or {}).get("proxy_url") or settings.tmdb_proxy_url or DEFAULT_TMDB_PROXY_URL).strip()
    timeout = int((config or {}).get("timeout") or 12)
    return mode, proxy_url, timeout


def refresh_dashboard_preload_task() -> None:
    db = SessionLocal()
    try:
        refresh_dashboard_preload(db)
    except Exception:
        pass
    finally:
        db.close()


def refresh_discover_preload_task() -> None:
    db = SessionLocal()
    try:
        refresh_discover_preload(db)
    except Exception:
        pass
    finally:
        db.close()


def refresh_discover_filter_preload_task(filters: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        refresh_discover_filter_preload(db, filters)
    except Exception:
        pass
    finally:
        db.close()


def refresh_download_preload_task(downloader_id: str) -> None:
    db = SessionLocal()
    try:
        refresh_download_preload(db, downloader_id)
    except Exception:
        pass
    finally:
        db.close()


def tmdb_network_detail_from_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    mode = str(config.get("mode") or settings.tmdb_mode or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        mode = "direct"
    proxy_url = str(config.get("proxy_url") or settings.tmdb_proxy_url or DEFAULT_TMDB_PROXY_URL).strip()
    proxy_host = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}").netloc if proxy_url else ""
    return {
        "network_mode": mode,
        "route_label": "TMDB：mihomo VPN 代理" if mode == "proxy" else "TMDB：直连 + DoH",
        "proxy_enabled": mode == "proxy",
        "proxy_url": proxy_host if mode == "proxy" else "",
        "non_tmdb_policy": "direct_only",
    }


def tmdb_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "tmdb",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success,
        "trace_id": trace,
        "detail": detail or tmdb_network_detail_from_config(),
    }


def classify_tmdb_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, TmdbConfigError):
        return tmdb_test_result(
            False,
            trace,
            "还没有填写 TMDB 密钥。",
            "系统需要 TMDB API Key 或 Bearer Token 才能访问真实片单数据。",
            "请到 TMDB 账号后台复制 API Key 或 Bearer Token，填入其中一个字段后再点击“保存并测试”。",
            "missing_credentials",
        )
    if isinstance(exc, TmdbDohError):
        if exc.error_type == "doh_bad_answer":
            return tmdb_test_result(
                False,
                trace,
                "TMDB DoH 解析结果不可用。",
                "系统已经访问到 DNS over HTTPS 服务，但没有拿到可用于 TMDB 的 IPv4 地址，可能是 DNS 返回被污染或格式异常。",
                "请稍后重试；如果一直失败，可以切换 DoH 源或改用路由器级代理/VPN。",
                "doh_bad_answer",
            )
        return tmdb_test_result(
            False,
            trace,
            "DNS over HTTPS 不可用。",
            "NAS 容器无法访问 doh.pub，因此还没有进入 TMDB Token 校验阶段。",
            "请确认 NAS 容器可以访问 https://doh.pub；如果不可用，可以切换 DoH 源或改用路由器级代理/VPN。",
            "doh_unavailable",
        )
    if isinstance(exc, TmdbImageError):
        return tmdb_test_result(
            False,
            trace,
            "TMDB 图片域无法访问。",
            "应用可以测试 TMDB API，但后端容器无法下载 image.tmdb.org 上的海报图片，所以发现页只能显示占位图。",
            "请在设置页把 TMDB 切换为 mihomo VPN 代理并重新测试；如果已经是代理模式，请确认 mihomo 容器运行正常，且规则包含 image.tmdb.org 走代理。",
            "tmdb_image_network_error",
            detail=getattr(exc, "network_detail", None) or tmdb_network_detail_from_config(),
        )
    if isinstance(exc, HTTPError):
        status_code = int(exc.code)
        if status_code in (401, 403):
            return tmdb_test_result(
                False,
                trace,
                "TMDB 密钥验证失败。",
                "TMDB 拒绝了这次请求，通常是密钥复制不完整、填错字段，或这个密钥还没有权限。",
                "请检查 API Key 和 Bearer Token 是否填反、是否多复制了空格，并确认 TMDB 后台的密钥已经启用。",
                "invalid_credentials",
                status_code,
            )
        if status_code == 429:
            return tmdb_test_result(False, trace, "TMDB 请求太频繁。", "TMDB 暂时限制了请求频率，密钥本身不一定有问题。", "请等待几分钟后再点击“保存并测试”。", "rate_limited", status_code)
        if status_code >= 500:
            return tmdb_test_result(False, trace, "TMDB 服务暂时不可用。", "TMDB 服务器返回了异常状态，通常需要稍后重试。", "请稍后再测试。如果一直失败，再检查网络或代理设置。", "tmdb_service_error", status_code)
        return tmdb_test_result(False, trace, "TMDB 返回了无法识别的错误。", f"TMDB 返回 HTTP {status_code}，应用无法确认具体原因。", "请检查密钥、语言/地区设置和网络环境，然后重试。", "http_error", status_code)
    if isinstance(exc, (TimeoutError, SocketTimeout)):
        return tmdb_test_result(False, trace, "连接 TMDB 超时。", "应用等待 TMDB 响应太久，可能是当前网络访问 TMDB 较慢或被阻断。", "请检查网络、代理、Docker/NAS 出网能力，或把超时时间调大后再测试。", "timeout")
    if isinstance(exc, URLError):
        reason = str(getattr(exc, "reason", exc))
        return tmdb_test_result(False, trace, "无法连接到 TMDB。", "应用没有连上 TMDB，常见原因是 DNS、代理、NAS/Docker 出网或防火墙问题。", f"请先确认这台机器能访问 api.themoviedb.org。系统底层提示：{reason}", "network_error")
    return tmdb_test_result(False, trace, "TMDB 测试失败。", "应用遇到了未能自动识别的问题。", "请检查填写内容后重试；如果仍然失败，可以把这条提示截图发给开发者排查。", "unknown_error")


def classify_tmdb_gateway_test_error(exc: Exception, trace: str, phase: str = "api") -> dict[str, Any]:
    result = classify_tmdb_test_error(exc, trace)
    result["message"] = "Cloudflare Worker 方案已停用。"
    result["explanation"] = "当前版本只保留 TMDB 直连 + DoH 和 mihomo VPN 代理两种连接方式。"
    result["next_step"] = "请在设置页选择“直连 + DoH”或“mihomo VPN 代理”，保存并重新测试。"
    result["error_type"] = "gateway_removed"
    return result


def mock_test_result(provider: str, trace: str, row: IntegrationConfig | None) -> dict[str, Any]:
    return {
        "success": True,
        "provider": provider,
        "mode": "mock",
        "http_status": 200,
        "duration_ms": 120,
        "message": "Mock 只读连接测试成功。",
        "trace_id": trace,
        "config_version": row.config_version if row else 0,
    }


def qb_config_test_result(provider: str, trace: str, config: dict[str, Any], row: IntegrationConfig | None) -> dict[str, Any]:
    missing = [
        label
        for key, label in (("base_url", "qB WebUI 地址"), ("username", "用户名"), ("password", "密码"))
        if not str(config.get(key) or "").strip()
    ]
    success = not missing
    return {
        "success": success,
        "provider": provider,
        "mode": "config",
        "http_status": None,
        "duration_ms": 0,
        "message": "qB 配置字段完整。" if success else "qB 配置还缺少必填项。",
        "explanation": "已经保存 qB WebUI 连接所需字段；下一步接入真实 qB Web API 后会用这些字段登录并读取实时任务。" if success else f"缺少：{', '.join(missing)}。",
        "next_step": "请继续补齐真实 qB Web API 适配器，或先保存草稿。" if success else "请填好 WebUI 地址、用户名和密码后再测试。",
        "error_type": None if success else "missing_required_fields",
        "can_enable": False,
        "trace_id": trace,
        "config_version": row.config_version if row else 0,
    }


def qb_test_result(success: bool, provider: str, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": provider,
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
        "detail": detail or {},
    }


def classify_qb_test_error(provider: str, exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, QbittorrentConfigError):
        return qb_test_result(
            False,
            provider,
            trace,
            "qB 配置还缺少必填项。",
            "真实 qB Web API 需要 WebUI 地址、用户名和密码。",
            "请填好 WebUI 地址、用户名和密码后再测试。",
            "missing_required_fields",
            can_enable=False,
        )
    if isinstance(exc, QbittorrentApiError):
        http_status = exc.http_status
        if http_status in (401, 403):
            return qb_test_result(
                False,
                provider,
                trace,
                "qB 登录失败。",
                "qB WebUI 拒绝了当前账号密码，或 WebUI 禁止了该来源登录。",
                "请检查用户名、密码、WebUI 地址，以及 qBittorrent WebUI 的 CSRF/Host Header/反向代理设置。",
                "invalid_credentials",
                http_status,
                False,
            )
        return qb_test_result(
            False,
            provider,
            trace,
            "qB 连接测试失败。",
            str(exc),
            "请确认这台机器能访问 qB WebUI 地址，然后重新测试。",
            "qb_api_error",
            http_status,
            False,
        )
    return qb_test_result(
        False,
        provider,
        trace,
        "qB 连接测试失败。",
        str(exc),
        "请检查 qB WebUI 地址、账号密码和网络连通性后再测试。",
        "unknown_error",
        can_enable=False,
    )


def mteam_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "mteam",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
    }


def classify_mteam_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, MTeamConfigError):
        return mteam_test_result(
            False,
            trace,
            "还没有填写 M-Team API Key。",
            "真实 M-Team API 需要 API Key。Cookie 和 Passkey 不能替代 API Key。",
            "请在 M-Team 个人资料里复制 API Key，填入设置页的“API Key”字段后再保存并测试。",
            "missing_api_key",
        )
    if isinstance(exc, MTeamApiError):
        message = str(exc)
        lower = message.lower()
        if "key" in lower or "401" in lower or "403" in lower or str(exc.code) in {"401", "403"}:
            return mteam_test_result(
                False,
                trace,
                "M-Team API Key 验证失败。",
                "M-Team 已响应请求，但拒绝了当前 API Key。",
                "请确认填入的是 M-Team API Key，不是下载 Passkey；如果刚生成过 Key，请刷新站点后重新复制。",
                "invalid_api_key",
                exc.http_status,
            )
        return mteam_test_result(
            False,
            trace,
            "M-Team API 返回失败。",
            f"M-Team 返回：{message}",
            "请检查 API Key、站点地址和网络环境，然后重试。",
            "mteam_api_error",
            exc.http_status,
        )
    return mteam_test_result(
        False,
        trace,
        "M-Team 测试失败。",
        "应用遇到了未能自动识别的问题。",
        "请检查填写内容后重试；如果仍然失败，可以把这条提示截图发给开发者排查。",
        "unknown_error",
    )


def mteam_connection_payload(row: IntegrationConfig | None) -> dict[str, Any]:
    result = row.last_test_result if row else None
    configured = bool(row and row.redacted_summary)
    enabled = bool(row and row.enabled)
    last_test_success = bool(result and result.get("success"))
    if enabled and last_test_success:
        message = "M-Team 已启用，仪表盘正在读取真实站点数据。"
    elif configured:
        message = "M-Team 配置已保存，请在设置页完成测试并启用。"
    else:
        message = "M-Team 尚未配置。"
    return {
        "configured": configured,
        "enabled": enabled,
        "last_test_success": last_test_success,
        "config_version": row.config_version if row else 0,
        "last_tested_at": row.last_tested_at.isoformat() if row and row.last_tested_at else None,
        "last_test_result": result,
        "data_source": "M-Team 原始数据（Real API）",
        "message": message,
    }


def ai_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "ai",
        "mode": "real",
        "http_status": http_status,
        "duration_ms": 0,
        "message": message,
        "explanation": explanation,
        "next_step": next_step,
        "error_type": error_type,
        "can_enable": success if can_enable is None else can_enable,
        "trace_id": trace,
        "detail": detail or {},
    }


def classify_ai_test_error(exc: Exception, trace: str) -> dict[str, Any]:
    if isinstance(exc, AIConfigError):
        return ai_test_result(
            False,
            trace,
            "DeepSeek 配置不完整。",
            "需要在设置页填写 API Key、base_url 和模型名称。base_url 默认是 https://api.deepseek.com。",
            "请填写 DeepSeek API Key，模型建议使用 deepseek-v4-flash 或 deepseek-v4-pro。",
            "missing_credentials",
            can_enable=False,
        )
    if isinstance(exc, AIServiceError):
        return ai_test_result(
            False,
            trace,
            "DeepSeek 调用失败。",
            str(exc),
            "请检查 API Key、模型名称、余额、网络连通性和 base_url 后重试。",
            "deepseek_api_error",
            exc.http_status,
            False,
        )
    return ai_test_result(
        False,
        trace,
        "AI 模块测试失败。",
        str(exc),
        "请检查 DeepSeek 配置后重试。",
        "unknown_error",
        can_enable=False,
    )


def get_ai_adapter_or_error(db: Session) -> DeepSeekChatAdapter:
    row = get_config(db, "ai")
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail="请先在设置中配置 DeepSeek API Key 和模型。")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="AI 模块尚未启用，请先在设置中测试并启用。")
    try:
        return DeepSeekChatAdapter(get_decrypted_config(db, "ai") or {})
    except AIConfigError as exc:
        raise HTTPException(status_code=409, detail="DeepSeek API Key、base_url 或模型名称未配置。") from exc


def format_assistant_result(intent: dict[str, Any], result: dict[str, Any]) -> str:
    action = intent.get("action")
    if action == "resource_search":
        items = result.get("items") or []
        if not items:
            return "没有找到匹配的 M-Team 资源。"
        lines = ["找到这些资源："]
        for index, item in enumerate(items[:5], 1):
            lines.append(f"{index}. {item.get('title') or item.get('name') or '-'} / {item.get('resolution') or '-'} / {item.get('size') or '-'} / 做种 {item.get('seeders', 0)} / ID {item.get('id') or '-'}")
        return "\n".join(lines)
    if action == "download_started":
        return f"已记录下载开始通知：{result.get('notification', {}).get('title', '下载已开始')}"
    if action == "download_completed":
        return f"已记录下载完成通知：{result.get('notification', {}).get('title', '下载已完成')}"
    if action == "status_query":
        if result.get("target") == "mteam":
            data = result.get("mteam") or result
            return f"M-Team 状态：等级 {data.get('user_level', '-')}，分享率 {data.get('ratio', 0)}，做种 {data.get('seed_count', 0)}。"
        if result.get("target") in {"qb", "downloads"}:
            qbs = result.get("qbs") or []
            if qbs:
                return "qB 状态：" + "；".join(f"{item.get('name') or item.get('id')} 下载 {bytes_label(item.get('download_speed') or 0)}/s，上传 {bytes_label(item.get('upload_speed') or 0)}/s，任务 {item.get('active_downloads', 0)}" for item in qbs)
        overview = result.get("overview") or {}
        return f"总览：下载 {bytes_label(overview.get('total_download_speed') or 0)}/s，上传 {bytes_label(overview.get('total_upload_speed') or 0)}/s，活动下载 {overview.get('download_tasks', 0)}，NAS {overview.get('nas_space_label') or '-'}。"
    return "请求已处理。"


def execute_assistant_intent(db: Session, intent: dict[str, Any], message: str, user: User) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "resource_search":
        query = str(intent.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="资源查询缺少关键词。")
        try:
            items = get_mteam_adapter_or_error(db).search_torrents(query)[: int(intent.get("limit") or 5)]
        except MTeamApiError as exc:
            raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc
        return {"action": action, "query": query, "items": items, "count": len(items)}

    if action in {"download_started", "download_completed"}:
        title = "下载已开始" if action == "download_started" else "下载已完成"
        detail = str(intent.get("message") or message or "").strip() or title
        row = Notification(title=title, message=detail, level="info" if action == "download_started" else "success", source="ai")
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "action": action,
            "notification": {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "level": row.level,
                "source": row.source,
                "created_at": row.created_at.isoformat(),
            },
        }

    if action == "status_query":
        target = str(intent.get("target") or "dashboard")
        downloader_id = str(intent.get("downloader_id") or "all")
        if target == "mteam":
            return {"action": action, "target": target, "mteam": mteam_stats(db, user)}
        if target in {"qb", "downloads"}:
            if downloader_id in {"qb1", "qb2", "qb3"}:
                state = get_qb_state_for_dashboard(db, downloader_id)
                return {"action": action, "target": target, "qbs": [state]}
            return {"action": action, "target": target, **build_dashboard_qbs_payload(db)}
        if target == "notifications":
            return {"action": action, "target": target, **notifications(user, db)}
        return {"action": action, "target": "dashboard", **build_dashboard_payload(db)}

    raise HTTPException(status_code=400, detail="不支持的 AI 指令。")


def get_wechat_claw_adapter_or_error(db: Session) -> WechatClawAdapter:
    row = get_config(db, "wechat_claw")
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail="请先在设置中配置 WeChat claw。")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="WeChat claw 尚未启用，请先在设置中测试并启用。")
    try:
        config = get_decrypted_config(db, "wechat_claw") or {}
        state = get_wechat_claw_ilink_state(db)
        return WechatClawAdapter(wechat_claw_config_with_state(config, state))
    except WechatClawConfigError as exc:
        raise HTTPException(status_code=409, detail=f"WeChat claw 配置不完整：{exc}") from exc


def get_notification_preferences(db: Session) -> dict[str, bool]:
    row = db.query(Setting).filter(Setting.key == NOTIFICATION_PREFERENCES_KEY).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    merged = dict(DEFAULT_NOTIFICATION_PREFERENCES)
    for key in DEFAULT_NOTIFICATION_PREFERENCES:
        if key in value:
            merged[key] = bool(value[key])
    return merged


def save_notification_preferences(db: Session, preferences: dict[str, bool]) -> dict[str, bool]:
    current = get_notification_preferences(db)
    normalized = {
        key: bool(preferences.get(key, current[key]))
        for key in DEFAULT_NOTIFICATION_PREFERENCES
    }
    row = db.query(Setting).filter(Setting.key == NOTIFICATION_PREFERENCES_KEY).one_or_none()
    if row is None:
        row = Setting(key=NOTIFICATION_PREFERENCES_KEY, value=normalized)
        db.add(row)
    else:
        row.value = normalized
        row.updated_at = datetime.utcnow()
    db.commit()
    return normalized


def get_wechat_claw_recent_interactions(db: Session, user_id: int | None = None) -> list[dict[str, Any]]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY, user_id)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    items = value.get("items") if isinstance(value, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][:WECHAT_CLAW_INTERACTION_LIMIT]


def get_wechat_claw_ilink_state(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def list_wechat_claw_binding_user_ids(db: Session) -> list[int | None]:
    prefix = f"{WECHAT_CLAW_ILINK_STATE_KEY}.user."
    rows = db.query(Setting).filter(Setting.key.like(f"{prefix}%")).all()
    ids: list[int] = []
    for row in rows:
        try:
            user_id = int(str(row.key).removeprefix(prefix))
        except ValueError:
            continue
        if isinstance(row.value, dict) and row.value.get("bot_token"):
            ids.append(user_id)
    if ids:
        active_ids = {item.id for item in db.query(User.id).filter(User.id.in_(ids), User.is_active.is_(True)).all()}
        return sorted(active_ids)
    return [None]


def save_wechat_claw_ilink_state(db: Session, state: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    key = wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = datetime.utcnow()
    db.commit()
    return state


def update_wechat_claw_ilink_state(db: Session, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db, user_id)
    state.update({key: value for key, value in kwargs.items() if value is not None})
    return save_wechat_claw_ilink_state(db, state, user_id)


def get_wechat_claw_last_poll(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def save_wechat_claw_last_poll(db: Session, payload: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    safe_payload = {
        "success": bool(payload.get("success")),
        "stage": str(payload.get("stage") or "poll"),
        "raw_count": int(payload.get("raw_count") or 0),
        "parsed_count": int(payload.get("parsed_count") or 0),
        "handled_count": int(payload.get("handled_count") or 0),
        "reply_sent_count": int(payload.get("reply_sent_count") or 0),
        "pending_count": int(payload.get("pending_count") or 0),
        "message": trim_wechat_claw_text(payload.get("message"), 360),
        "updated_at": datetime.utcnow().isoformat(),
    }
    key = wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=safe_payload)
        db.add(row)
    else:
        row.value = safe_payload
        row.updated_at = datetime.utcnow()
    db.commit()
    return safe_payload


def refresh_wechat_claw_login_state(db: Session) -> dict[str, Any]:
    config = get_decrypted_config(db, "wechat_claw") or {}
    state = get_wechat_claw_ilink_state(db)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    if not qrcode.get("qrcode") or state.get("bot_token"):
        return state
    try:
        adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
        result = adapter.get_qrcode_status(str(qrcode.get("qrcode")))
        updated_qrcode = dict(qrcode)
        updated_qrcode["status"] = result.get("status") or updated_qrcode.get("status") or "waiting"
        updated_qrcode["updated_at"] = int(datetime.utcnow().timestamp())
        if result.get("qrcode_url"):
            updated_qrcode["qrcode_url"] = result.get("qrcode_url")
        update_payload: dict[str, Any] = {"qrcode": updated_qrcode}
        if result.get("token"):
            update_payload.update(
                {
                    "bot_token": result.get("token"),
                    "account_id": result.get("account_id"),
                    "sync_buf": "",
                    "base_url": (result.get("base_url") or adapter.base_url).rstrip("/"),
                }
            )
        return update_wechat_claw_ilink_state(db, **update_payload)
    except Exception as exc:
        logger.warning("刷新 WeChat claw iLink 扫码状态失败：%s", exc)
        return state


def clear_wechat_claw_ilink_state(db: Session) -> dict[str, Any]:
    return save_wechat_claw_ilink_state(db, {"qrcode": {}, "known_targets": {}})


def clear_wechat_claw_session(db: Session, qrcode_status: str | None = None) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    if qrcode_status and qrcode:
        qrcode = {**qrcode, "status": qrcode_status, "updated_at": int(datetime.utcnow().timestamp())}
    return update_wechat_claw_ilink_state(
        db,
        bot_token="",
        account_id="",
        sync_buf="",
        known_targets={},
        pending_messages=[],
        qrcode=qrcode,
    )


def is_wechat_claw_session_timeout(message: Any) -> bool:
    text = str(message or "").strip().lower()
    return "session timeout" in text or "session expired" in text or "errcode=-14" in text or "ret=-14" in text


def wechat_claw_pending_message_id(item: dict[str, Any]) -> str:
    message_id = str(item.get("message_id") or item.get("conversation_id") or "").strip()
    if message_id:
        return f"message:{message_id}"
    identity = json.dumps(
        {
            "user_id": str(item.get("user_id") or "").strip(),
            "context_token": str(item.get("context_token") or "").strip(),
            "text": str(item.get("text") or item.get("message") or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"hash:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def get_wechat_claw_pending_messages(state: dict[str, Any]) -> list[dict[str, Any]]:
    values = state.get("pending_messages") if isinstance(state.get("pending_messages"), list) else []
    pending: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        pending_id = str(item.get("id") or "").strip()
        if not pending_id:
            continue
        pending.append(dict(item, id=pending_id))
    return pending[:WECHAT_CLAW_PENDING_LIMIT]


def save_wechat_claw_pending_messages(db: Session, pending: list[dict[str, Any]], sync_buf: str | None = None) -> list[dict[str, Any]]:
    values = pending[:WECHAT_CLAW_PENDING_LIMIT]
    payload: dict[str, Any] = {"pending_messages": values}
    if sync_buf is not None:
        payload["sync_buf"] = sync_buf
    update_wechat_claw_ilink_state(db, **payload)
    return values


def queue_wechat_claw_messages(db: Session, messages: list[dict[str, Any]], sync_buf: str | None) -> int:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    known_ids = {str(item.get("id")) for item in pending}
    added = 0
    for item in messages:
        message = str(item.get("text") or "").strip()
        user_id = str(item.get("user_id") or "").strip()
        context_token = str(item.get("context_token") or "").strip()
        if not message or not user_id or not context_token:
            continue
        pending_id = wechat_claw_pending_message_id(item)
        if pending_id in known_ids:
            continue
        pending.append(
            {
                "id": pending_id,
                "message": message,
                "user_id": user_id,
                "conversation_id": str(item.get("message_id") or "").strip(),
                "context_token": context_token,
                "received_at": datetime.utcnow().isoformat(),
                "attempts": 0,
            }
        )
        known_ids.add(pending_id)
        added += 1
    save_wechat_claw_pending_messages(db, pending, str(sync_buf or ""))
    return added


def update_wechat_claw_pending_message(db: Session, pending_id: str, **changes: Any) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    for index, item in enumerate(pending):
        if item.get("id") == pending_id:
            pending[index] = {**item, **changes}
            break
    save_wechat_claw_pending_messages(db, pending)


def remove_wechat_claw_pending_message(db: Session, pending_id: str) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = [item for item in get_wechat_claw_pending_messages(state) if item.get("id") != pending_id]
    save_wechat_claw_pending_messages(db, pending)


def wechat_claw_retry_after_seconds(pending: list[dict[str, Any]]) -> float | None:
    now = datetime.utcnow().timestamp()
    retry_times = [float(item.get("next_retry_at") or 0) for item in pending if item.get("next_retry_at")]
    if not retry_times:
        return None
    return max(0.0, min(retry_times) - now)


def retry_wechat_claw_pending_message(db: Session, pending: dict[str, Any], error: str) -> float:
    attempts = int(pending.get("attempts") or 0) + 1
    delay_seconds = min(60, 2 ** min(attempts, 6))
    update_wechat_claw_pending_message(
        db,
        str(pending["id"]),
        attempts=attempts,
        last_error=trim_wechat_claw_text(error, 240),
        next_retry_at=int(datetime.utcnow().timestamp()) + delay_seconds,
    )
    return float(delay_seconds)


def process_wechat_claw_pending_messages(db: Session, adapter: WechatClawAdapter) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    now = datetime.utcnow().timestamp()
    handled: list[dict[str, Any]] = []
    reply_sent_count = 0

    for item in pending:
        next_retry_at = float(item.get("next_retry_at") or 0)
        if next_retry_at > now:
            continue
        pending_id = str(item["id"])
        try:
            reply = str(item.get("reply") or "").strip()
            if not reply:
                response = handle_wechat_claw_text(
                    db,
                    WechatClawMessageRequest(
                        message=str(item.get("message") or ""),
                        user_id=str(item.get("user_id") or ""),
                        conversation_id=str(item.get("conversation_id") or ""),
                        context_token=str(item.get("context_token") or ""),
                    ),
                )
                reply = str(response.get("reply") or "").strip()
                if not reply:
                    raise RuntimeError("empty AI reply")
                # Persist the generated reply before delivery. A transient send failure
                # must retry the same answer rather than execute the user intent twice.
                update_wechat_claw_pending_message(db, pending_id, reply=reply, handled_at=datetime.utcnow().isoformat())
            delivery = adapter.send_text(str(item.get("user_id") or ""), reply, str(item.get("context_token") or ""))
            if not delivery.get("sent"):
                reason = str(delivery.get("message") or delivery.get("reason") or "send rejected")
                retry_wechat_claw_pending_message(db, item, reason)
                handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": reason})
                continue
        except HTTPException as exc:
            retry_wechat_claw_pending_message(db, item, str(exc.detail))
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": str(exc.detail)})
            continue
        except Exception as exc:
            logger.exception("WeChat claw message handling failed: user_id=%s", item.get("user_id"))
            error = str(exc)[:240]
            retry_wechat_claw_pending_message(db, item, error)
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": error})
            continue

        remove_wechat_claw_pending_message(db, pending_id)
        reply_sent_count += 1
        handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": True})

    current_pending = get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db))
    return {
        "handled": handled,
        "reply_sent_count": reply_sent_count,
        "pending_count": len(current_pending),
        "retry_after_seconds": wechat_claw_retry_after_seconds(current_pending),
    }


def wechat_claw_config_with_state(config: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    merged = dict(config)
    merged.update(
        {
            "bot_token": state.get("bot_token"),
            "account_id": state.get("account_id"),
            "sync_buf": state.get("sync_buf"),
            "known_targets": state.get("known_targets") or {},
            "base_url": state.get("base_url") or config.get("base_url"),
        }
    )
    return merged


def trim_wechat_claw_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1]}…"


def wechat_claw_qr_payload(config: dict[str, Any]) -> dict[str, Any]:
    try:
        timeout = int(config.get("poll_timeout") or config.get("timeout") or 25)
    except (TypeError, ValueError):
        timeout = 25
    qrcode = config.get("qrcode") if isinstance(config.get("qrcode"), dict) else {}
    return {
        "type": "pt-media-hub.wechat-claw",
        "version": 1,
        "name": "PT Media Hub",
        "mode": str(config.get("mode") or "ilink"),
        "base_url": str(config.get("base_url") or "https://ilinkai.weixin.qq.com").rstrip("/"),
        "qrcode": qrcode.get("qrcode"),
        "qrcode_url": qrcode.get("qrcode_url"),
        "qrcode_status": qrcode.get("status"),
        "default_downloader_id": str(config.get("default_downloader_id") or "all"),
        "default_target": str(config.get("default_target") or ""),
        "timeout": timeout,
    }


def build_wechat_claw_setup(db: Session) -> dict[str, Any]:
    row = get_config(db, "wechat_claw")
    config = get_decrypted_config(db, "wechat_claw") or {}
    state = get_wechat_claw_ilink_state(db)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    qr_payload = wechat_claw_qr_payload({**config, **state, "qrcode": qrcode})
    return {
        "configured": bool(row and row.encrypted_payload),
        "enabled": bool(row and row.enabled),
        "mode": str(config.get("mode") or "ilink"),
        "connected": bool(state.get("bot_token")),
        "account_id": state.get("account_id"),
        "base_url": state.get("base_url") or config.get("base_url") or "https://ilinkai.weixin.qq.com",
        "config_version": row.config_version if row else 0,
        "last_test_result": row.last_test_result if row else None,
        "qr_payload": qr_payload,
        "qr_text": qrcode.get("qrcode_url") or json.dumps(qr_payload, ensure_ascii=False, separators=(",", ":")),
        "qrcode": qrcode,
        "known_targets": state.get("known_targets") or {},
        "mobile_sections": WECHAT_CLAW_MOBILE_SECTIONS,
        "capabilities": WECHAT_CLAW_CAPABILITIES,
        "recent_interactions": get_wechat_claw_recent_interactions(db),
        "last_poll": get_wechat_claw_last_poll(db),
    }


def record_wechat_claw_interaction(
    db: Session,
    request: WechatClawMessageRequest,
    intent: dict[str, Any],
    result: dict[str, Any],
    reply: str,
) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    target = str(intent.get("target") or intent.get("downloader_id") or result.get("target") or "")
    item = {
        "user_id": trim_wechat_claw_text(request.user_id or "unknown", 120),
        "conversation_id": trim_wechat_claw_text(request.conversation_id, 120),
        "message": trim_wechat_claw_text(request.message, 240),
        "reply": trim_wechat_claw_text(reply, 360),
        "action": action,
        "target": target,
        "created_at": datetime.utcnow().isoformat(),
    }
    current = get_wechat_claw_recent_interactions(db)
    row = db.query(Setting).filter(Setting.key == WECHAT_CLAW_INTERACTIONS_KEY).one_or_none()
    value = {"items": [item, *current][:WECHAT_CLAW_INTERACTION_LIMIT]}
    if row is None:
        row = Setting(key=WECHAT_CLAW_INTERACTIONS_KEY, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = datetime.utcnow()
    db.commit()
    state = get_wechat_claw_ilink_state(db)
    known_targets = state.get("known_targets") if isinstance(state.get("known_targets"), dict) else {}
    if item["user_id"] and item["user_id"] != "unknown":
        known_targets[item["user_id"]] = {
            "username": item["user_id"],
            "last_active": int(datetime.utcnow().timestamp()),
            "context_token": request.context_token,
        }
        update_wechat_claw_ilink_state(db, known_targets=known_targets)
    return item


def push_wechat_claw_notification(db: Session, notification: Notification, action: str) -> dict[str, Any]:
    # App-originated download events have no active chat scope. Fan them out to
    # every independently bound member instead of the legacy administrator bot.
    if _WECHAT_CLAW_ACTIVE_USER_ID.get() is None:
        user_ids = [item for item in list_wechat_claw_binding_user_ids(db) if item is not None]
        if user_ids:
            deliveries: dict[str, dict[str, Any]] = {}
            for user_id in user_ids:
                scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user_id)
                try:
                    deliveries[str(user_id)] = push_wechat_claw_notification(db, notification, action)
                finally:
                    _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
            return {"sent": any(item.get("sent") for item in deliveries.values()), "deliveries": deliveries}
    preferences = get_notification_preferences(db)
    if not preferences.get("wechat_claw_push", True):
        return {"sent": False, "reason": "disabled_by_preferences"}
    row = get_config(db, "wechat_claw")
    if not row or not row.enabled or not row.encrypted_payload:
        return {"sent": False, "reason": "wechat_claw_not_enabled"}
    try:
        config = get_decrypted_config(db, "wechat_claw") or {}
        state = get_wechat_claw_ilink_state(db)
        adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
        return adapter.send_message(
            {
                "type": action,
                "title": notification.title,
                "message": notification.message,
                "level": notification.level,
                "source": notification.source,
                "notification_id": notification.id,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            }
        )
    except (WechatClawConfigError, WechatClawApiError) as exc:
        return {"sent": False, "reason": str(exc), "http_status": getattr(exc, "http_status", None)}


def format_assistant_result_v2(intent: dict[str, Any], result: dict[str, Any]) -> str:
    action = intent.get("action")
    if action == "resource_search":
        items = result.get("items") or []
        if not items:
            return "【资源查询】\n没有找到匹配的 M-Team 资源。"
        lines = ["【资源查询】", f"找到 {len(items)} 个资源。", "", "结果"]
        for index, item in enumerate(items[:5], 1):
            lines.append(f"- {index}. {item.get('title') or item.get('name') or '-'} / {item.get('resolution') or '-'} / {item.get('size') or '-'} / 做种 {item.get('seeders', 0)} / ID {item.get('id') or '-'}")
        return "\n".join(lines)
    if action in {"download_started", "download_completed"}:
        label = "下载开始" if action == "download_started" else "下载完成"
        notification = result.get("notification") or {}
        if result.get("notification_skipped"):
            return f"【{label}】\n已按通知偏好跳过通知中心记录。"
        lines = [f"【{label}】", f"已记录通知：{notification.get('title', label)}", f"通知 ID：{notification.get('id', '-')}"]
        push = result.get("wechat_claw_push") or {}
        if push:
            lines.append(f"WeChat claw：{'已推送' if push.get('sent') else push.get('reason', '未推送')}")
        return "\n".join(lines)
    if action == "status_query":
        if result.get("target") == "stats":
            series = result.get("series") or []
            return f"【统计状态】\n可用区间：{', '.join(result.get('ranges') or [])}\n最近样本：{len(series)} 条"
        if result.get("target") == "diagnostics":
            modules = result.get("modules") or []
            failed = [item for item in modules if item.get("status") == "failed"]
            return f"【诊断状态】\n模块数：{len(modules)}\n异常：{len(failed)}"
        if result.get("target") == "discover":
            return f"【发现页状态】\n已配置：{'是' if result.get('configured') else '否'}\n已启用：{'是' if result.get('enabled') else '否'}\n{result.get('message') or ''}".strip()
        if result.get("target") == "mteam":
            data = result.get("mteam") or result
            return f"【M-Team 状态】\n等级：{data.get('user_level', '-')}\n分享率：{data.get('ratio', 0)}\n做种：{data.get('seed_count', 0)}"
        if result.get("target") in {"qb", "downloads"}:
            qbs = result.get("qbs") or []
            if qbs:
                lines = ["【qB 状态】"]
                lines.extend(f"- {item.get('name') or item.get('id')}：下载 {bytes_label(item.get('download_speed') or 0)}/s，上传 {bytes_label(item.get('upload_speed') or 0)}/s，任务 {item.get('active_downloads', 0)}" for item in qbs)
                return "\n".join(lines)
        overview = result.get("overview") or {}
        return f"【总览状态】\n下载：{bytes_label(overview.get('total_download_speed') or 0)}/s\n上传：{bytes_label(overview.get('total_upload_speed') or 0)}/s\n活动下载：{overview.get('download_tasks', 0)}\nNAS：{overview.get('nas_space_label') or '-'}"
    return "【处理结果】\n请求已处理。"


def attach_optional_assistant_notification(db: Session, intent: dict[str, Any], result: dict[str, Any], title: str, level: str = "info") -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if not get_notification_preferences(db).get(action, False):
        return result
    detail = format_assistant_result_v2(intent, result)
    row = Notification(title=title[:200], message=detail, level=level, source="ai")
    db.add(row)
    db.commit()
    db.refresh(row)
    updated = dict(result)
    updated["notification"] = {
        "id": row.id,
        "title": row.title,
        "message": row.message,
        "level": row.level,
        "source": row.source,
        "created_at": row.created_at.isoformat(),
    }
    updated["wechat_claw_push"] = push_wechat_claw_notification(db, row, action)
    return updated


def execute_assistant_intent_v2(db: Session, intent: dict[str, Any], message: str, user: User) -> dict[str, Any]:
    action = str(intent.get("action") or "")
    if action == "status_query":
        target = str(intent.get("target") or "dashboard")
        if target == "stats":
            result = {"action": action, "target": target, **stats(user, db)}
            return attach_optional_assistant_notification(db, intent, result, "状态查询：统计")
        if target == "diagnostics":
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="诊断状态需要管理员权限。")
            result = {"action": action, "target": target, **diagnostics_health(user, db)}
            return attach_optional_assistant_notification(db, intent, result, "状态查询：诊断")
        if target == "discover":
            row = get_config(db, "tmdb")
            result = {
                "action": action,
                "target": target,
                "configured": bool(row and row.redacted_summary),
                "enabled": bool(row and row.enabled),
                "last_test_result": row.last_test_result if row else None,
                "message": "发现页使用 TMDB 配置获取趋势、热门和筛选内容。",
            }
            return attach_optional_assistant_notification(db, intent, result, "状态查询：发现页")
        result = execute_assistant_intent(db, intent, message, user)
        return attach_optional_assistant_notification(db, intent, result, "状态查询")

    if action == "resource_search":
        result = execute_assistant_intent(db, intent, message, user)
        query = str(intent.get("query") or "").strip()
        title = f"资源查询：{query}" if query else "资源查询"
        return attach_optional_assistant_notification(db, intent, result, title)

    if action in {"download_started", "download_completed"}:
        title = "下载已开始" if action == "download_started" else "下载已完成"
        detail = str(intent.get("message") or message or "").strip() or title
        if not get_notification_preferences(db).get(action, True):
            return {
                "action": action,
                "notification_skipped": True,
                "reason": "disabled_by_preferences",
                "title": title,
                "message": detail,
            }
        row = Notification(title=title, message=detail, level="info" if action == "download_started" else "success", source="ai")
        db.add(row)
        db.commit()
        db.refresh(row)
        push_result = push_wechat_claw_notification(db, row, action)
        return {
            "action": action,
            "notification": {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "level": row.level,
                "source": row.source,
                "created_at": row.created_at.isoformat(),
            },
            "wechat_claw_push": push_result,
        }

    raise HTTPException(status_code=400, detail="不支持的 AI 指令。")


def empty_mteam_stats(message: str = "请先在设置中启用 M-Team。") -> dict[str, Any]:
    return {
        "user_level": "-",
        "upload_total": 0,
        "upload_delta_label": None,
        "download_total": 0,
        "download_delta_label": None,
        "bonus": 0,
        "bonus_delta_label": None,
        "ratio": 0,
        "ratio_delta_label": None,
        "seed_count": 0,
        "seed_count_delta_label": None,
        "seed_size": 0,
        "seed_size_delta_label": None,
        "joined_at": "-",
        "active_uploads": 0,
        "active_downloads": 0,
        "bonus_per_hour_label": "",
        "source": "M-Team 未启用",
        "updated_at": None,
        "traffic_history": [],
        "message": message,
    }


def get_mteam_adapter_or_error(db: Session) -> MTeamAdapter:
    row = get_config(db, "mteam")
    if not row or not row.enabled:
        raise HTTPException(status_code=409, detail="请先在设置中保存、测试并启用 M-Team。")
    try:
        return MTeamAdapter(get_decrypted_config(db, "mteam") or {})
    except MTeamConfigError as exc:
        raise HTTPException(status_code=409, detail="M-Team API Key 未配置。") from exc


def qb_placeholder_state(downloader_id: str, row: IntegrationConfig | None, message: str | None = None) -> dict[str, Any]:
    index = downloader_id.replace("qb", "")
    configured = bool(row and row.encrypted_payload)
    enabled = bool(row and row.enabled)
    name = (row.redacted_summary or {}).get("name") if row else None
    return qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, message)


def qb_placeholder_state_from_meta(downloader_id: str, configured: bool, enabled: bool, name: str | None = None, message: str | None = None) -> dict[str, Any]:
    index = downloader_id.replace("qb", "")
    return {
        "id": downloader_id,
        "name": name or f"qB{index}",
        "online": False,
        "configured": configured,
        "enabled": enabled,
        "download_speed": 0,
        "upload_speed": 0,
        "active_downloads": 0,
        "active_uploads": 0,
        "paused": 0,
        "errors": 0,
        "free_space": 0,
        "total_space": 0,
        "source": "qB Web API 未启用",
        "updated_at": datetime.utcnow().isoformat(),
        "message": message or ("配置已保存，请在设置中测试并启用。" if configured else "请先在设置中配置 qB WebUI。"),
    }


def downloader_label(downloader_id: str) -> str:
    if downloader_id.lower().startswith("qb"):
        return f"qB{downloader_id[2:]}"
    return downloader_id


def get_qb_adapter_or_error(db: Session, downloader_id: str) -> QbittorrentWebAdapter:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    row = get_config(db, downloader_id)
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail=f"{downloader_label(downloader_id)} 尚未配置")
    if not row.enabled:
        raise HTTPException(status_code=409, detail=f"{downloader_label(downloader_id)} 尚未启用，请先在设置中测试并启用")
    try:
        return QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {})
    except QbittorrentConfigError as exc:
        raise HTTPException(status_code=409, detail="qB WebUI 地址、用户名或密码未配置") from exc


def get_qb_state_for_dashboard(db: Session, downloader_id: str) -> dict[str, Any]:
    row = get_config(db, downloader_id)
    if not row or not row.encrypted_payload or not row.enabled:
        return qb_placeholder_state(downloader_id, row)
    try:
        return QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {}).get_server_state(downloader_id)
    except Exception as exc:
        return qb_placeholder_state(downloader_id, row, f"qB 真实数据读取失败：{exc}")


def _storage_disk_key(path: Path, raw_path: str) -> Any:
    try:
        return path.stat().st_dev
    except OSError:
        return os.path.normcase(path.anchor or raw_path)


def nas_storage_from_configs(db: Session) -> dict[str, Any]:
    del db
    disks_seen: set[Any] = set()
    primary_usage: Any | None = None
    readable_paths: list[str] = []
    errors: list[dict[str, str]] = []
    primary_path = DEFAULT_STORAGE_MOUNT_PATHS[0]

    for raw_path in DEFAULT_STORAGE_MOUNT_PATHS:
        path = Path(raw_path)
        try:
            if not path.exists():
                errors.append({"path": raw_path, "message": "Path is not mounted or the container cannot access it."})
                continue
            readable_paths.append(raw_path)
            disk_key = _storage_disk_key(path, raw_path)
            disks_seen.add(disk_key)
            if raw_path == primary_path:
                primary_usage = shutil.disk_usage(path)
        except Exception as exc:
            errors.append({"path": raw_path, "message": str(exc)})

    pool_count = len(disks_seen)
    folder_count = len(readable_paths)
    summary_label = f"已识别 {pool_count} 个存储池，{folder_count} 个文件夹可访问" if folder_count else "未检测到存储挂载"
    common = {
        "nas_storage_pool_count": pool_count,
        "nas_storage_folder_count": folder_count,
        "nas_storage_detected_paths": readable_paths,
        "nas_storage_summary_label": summary_label,
        "nas_storage_paths": readable_paths,
        "nas_storage_errors": errors,
    }

    total_space = float(primary_usage.total) if primary_usage else 0.0
    free_space = float(primary_usage.free) if primary_usage else 0.0

    if total_space <= 0:
        return {
            "nas_free_space": 0,
            "nas_total_space": 0,
            "nas_used_space": 0,
            "nas_usage_percent": None,
            "nas_free_space_label": "-",
            "nas_free_space_source": "",
            "nas_space_label": "-",
            "nas_space_helper": f"请在部署 YAML 中把 qB1 的 NAS 路径挂载到 {primary_path}",
            "nas_storage_configured": folder_count > 0,
            "nas_storage_readable": False,
            **common,
        }

    used_space = max(0.0, total_space - free_space)
    usage_percent = round((used_space / total_space) * 100, 1)
    return {
        "nas_free_space": free_space,
        "nas_total_space": total_space,
        "nas_used_space": used_space,
        "nas_usage_percent": usage_percent,
        "nas_free_space_label": bytes_label(free_space, 2),
        "nas_free_space_source": "Docker 固定存储挂载",
        "nas_space_label": f"{bytes_label(used_space, 2)} / {bytes_label(total_space, 2)}",
        "nas_space_helper": f"已使用 {usage_percent}%",
        "nas_storage_configured": True,
        "nas_storage_readable": True,
        **common,
    }


TRAFFIC_DIMENSIONS = ("hour", "day", "week", "month", "year")


def build_mteam_traffic_series(db: Session) -> dict[str, list[dict[str, Any]]]:
    points = mteam_snapshot_delta_points(db)
    return {dimension: aggregate_traffic_points(points, dimension) for dimension in TRAFFIC_DIMENSIONS}


def mteam_snapshot_delta_points(db: Session, limit: int = 5000) -> list[dict[str, Any]]:
    rows = list(
        reversed(
            db.query(MTeamSnapshot)
            .filter(MTeamSnapshot.source != "mock")
            .order_by(MTeamSnapshot.captured_at.desc())
            .limit(limit)
            .all()
        )
    )
    points = []
    previous: MTeamSnapshot | None = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        points.append(
            {
                "captured_at": row.captured_at,
                "upload_total": max(0.0, float(row.upload_total or 0) - float(previous.upload_total or 0)),
                "download_total": max(0.0, float(row.download_total or 0) - float(previous.download_total or 0)),
                "source": "app_calculated",
            }
        )
        previous = row
    return points


def aggregate_traffic_points(points: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    buckets: dict[datetime, dict[str, float]] = {}
    for point in points:
        captured_at = point.get("captured_at")
        if not isinstance(captured_at, datetime):
            continue
        period = traffic_period_start(captured_at, dimension)
        if period not in buckets:
            buckets[period] = {"upload_total": 0.0, "download_total": 0.0}
        buckets[period]["upload_total"] += float(point.get("upload_total") or 0)
        buckets[period]["download_total"] += float(point.get("download_total") or 0)
    return [
        {
            "date": period.isoformat(),
            "label": traffic_period_label(period, dimension),
            "dimension": dimension,
            "upload_total": totals["upload_total"],
            "download_total": totals["download_total"],
        }
        for period, totals in sorted(buckets.items())
    ]


def traffic_period_start(value: datetime, dimension: str) -> datetime:
    if dimension == "hour":
        return datetime(value.year, value.month, value.day, value.hour)
    day = datetime(value.year, value.month, value.day)
    if dimension == "week":
        return day - timedelta(days=day.weekday())
    if dimension == "month":
        return datetime(value.year, value.month, 1)
    if dimension == "year":
        return datetime(value.year, 1, 1)
    return day


def traffic_period_label(value: datetime, dimension: str) -> str:
    display_value = value + timedelta(hours=8)
    if dimension == "hour":
        return display_value.strftime("%m/%d %H:00")
    if dimension == "year":
        return display_value.strftime("%Y")
    if dimension == "month":
        return display_value.strftime("%Y/%m")
    if dimension == "week":
        end = display_value + timedelta(days=6)
        return f"{display_value.strftime('%m/%d')}~{end.strftime('%m/%d')}"
    return display_value.strftime("%m/%d")


def persist_dashboard_snapshots(db: Session, mteam: dict[str, Any], qbs: list[dict[str, Any]], mteam_ok: bool) -> None:
    has_rows = False
    if mteam_ok:
        db.add(
            MTeamSnapshot(
                user_level=str(mteam.get("user_level") or ""),
                upload_total=float(mteam.get("upload_total") or 0),
                download_total=float(mteam.get("download_total") or 0),
                bonus=float(mteam.get("bonus") or 0),
                ratio=float(mteam.get("ratio") or 0),
                seed_size=float(mteam.get("seed_size") or 0),
                active_uploads=int(mteam.get("active_uploads") or 0),
                active_downloads=int(mteam.get("active_downloads") or 0),
                source="real",
            )
        )
        has_rows = True
    for qb in qbs:
        if not qb.get("online"):
            continue
        db.add(
            QbSnapshot(
                downloader_id=str(qb.get("id") or ""),
                download_speed=float(qb.get("download_speed") or 0),
                upload_speed=float(qb.get("upload_speed") or 0),
                downloaded_total=float(qb.get("downloaded_total") or 0),
                uploaded_total=float(qb.get("uploaded_total") or 0),
                active_downloads=int(qb.get("active_downloads") or 0),
                active_uploads=int(qb.get("active_uploads") or 0),
                source="real",
            )
        )
        has_rows = True
    if has_rows:
        db.commit()


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"initialized": db.query(User).count() > 0}


@router.post("/setup/admin")
def setup_admin(request: SetupAdminRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.query(User).count() > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup already completed")
    user = User(username=request.username, password_hash=hash_password(request.password), role="admin")
    db.add(user)
    db.add(Notification(title="欢迎", message="PT Media Hub 初始化完成。", level="success"))
    db.commit()
    db.refresh(user)
    token = create_access_token(db, user)
    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username, "role": user.role}}


@router.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    trace = TraceRecorder(db, "login", "LOGIN")
    user = db.query(User).filter(User.username == request.username).one_or_none()
    if not user or not verify_password(request.password, user.password_hash):
        trace.add("login failed", {"username": request.username})
        trace.finish(status="failed", error_summary="用户名或密码错误")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(db, user)
    trace.add("login success", {"username": user.username, "role": user.role})
    trace.finish()
    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username, "role": user.role}}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "role": user.role}


@router.get("/admin/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    users = db.query(User).order_by(User.created_at.asc(), User.id.asc()).all()
    return {"items": [{"id": item.id, "username": item.username, "role": item.role, "is_active": item.is_active} for item in users]}


@router.post("/admin/users")
def create_user(request: CreateUserRequest, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    username = request.username.strip()
    if db.query(User).filter(User.username == username).one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(username=username, password_hash=hash_password(request.password), role="user")
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": user.is_active}


@router.post("/auth/admin-verify")
def admin_verify(request: AdminVerifyRequest, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin = db.query(User).filter(User.username == request.username, User.role == "admin").one_or_none()
    if not admin or not verify_password(request.password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员验证失败")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1])
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one()
    expires = datetime.utcnow() + timedelta(minutes=settings.qb2_grant_minutes)
    session.qb2_grant_expires_at = expires
    db.commit()
    trace = TraceRecorder(db, "admin_verify", "ADMIN")
    trace.add("qB2 grant issued", {"actor": user.username, "admin": admin.username, "expires_at": expires.isoformat()})
    trace.finish()
    return {"qb2_granted": True, "expires_at": expires.isoformat()}


@router.post("/auth/qb2-grant/revoke")
def revoke_qb2_grant(authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1])
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one_or_none()
    if session:
        session.qb2_grant_expires_at = None
        db.commit()
    trace = TraceRecorder(db, "qb2_grant_revoke", "ADMIN")
    trace.add("qB2 grant revoked", {"actor": user.username})
    trace.finish()
    return {"qb2_granted": False}


@router.get("/admin/integrations")
def list_integrations(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = {row.provider: row for row in db.query(IntegrationConfig).all()}
    return {
        "providers": [
            serialize_config(existing[provider], include_plain_payload=True)
            if provider in existing
            else {"provider": provider, "config_version": 0, "enabled": False, "redacted_summary": {}, "saved_payload": {}, "last_tested_at": None, "last_test_result": None, "updated_at": None}
            for provider in PROVIDERS
        ]
    }


@router.get("/admin/storage/status")
def admin_storage_status(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return nas_storage_from_configs(db)


@router.put("/admin/integrations/{provider}")
def save_integration(provider: str, request: IntegrationPayload, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return serialize_config(upsert_config(db, provider, request.payload, actor_user_id=user.id))


@router.post("/admin/integrations/{provider}/test")
def test_integration(provider: str, request: IntegrationPayload | None = None, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    row = upsert_config(db, provider, request.payload, actor_user_id=user.id, action="save_and_test") if request and request.payload else get_config(db, provider)
    test_trace_id = trace_id("CFGTEST")
    if provider == "tmdb":
        config = get_decrypted_config(db, provider) or {}
        network_detail = tmdb_network_detail_from_config(config)
        try:
            adapter = TmdbAdapter(config)
            detail = adapter.test_connection().get("network") or adapter.network_detail()
            result = tmdb_test_result(True, test_trace_id, "TMDB 连接成功。", "应用已经成功访问 TMDB，并验证了当前 Bearer Token 可以使用。", "你现在可以点击“启用”，然后回到“发现”页查看真实 TMDB 数据。", None, 200, detail)
        except Exception as exc:
            result = classify_tmdb_test_error(exc, test_trace_id)
            result["detail"] = network_detail
            if network_detail.get("network_mode") == "proxy" and result.get("error_type") in {"network_error", "timeout", "unknown_error"}:
                result["message"] = "无法通过 mihomo 代理连接 TMDB。"
                result["explanation"] = "TMDB 已按设置交给 mihomo 代理，但代理容器、节点或上游网络没有完成请求。APP 的 qB、M-Team 和 NAS 功能仍会直连，不受这个错误影响。"
                result["next_step"] = "请确认 nas-mihomo/config.yaml 已部署，mihomo 容器正在监听 7890，并在 mihomo 面板里切换到可用节点后重试。"
        result["config_version"] = row.config_version if row else 0
    elif provider == "mteam":
        try:
            config = get_decrypted_config(db, provider) or {}
            adapter = MTeamAdapter(config)
            stats = adapter.get_user_stats()
            result = mteam_test_result(
                True,
                test_trace_id,
                "M-Team 连接成功。",
                f"应用已经成功读取 M-Team 个人资料，当前等级为 {stats.get('user_level') or 'User'}。",
                "你现在可以点击“启用”，然后回到仪表盘查看真实 M-Team 站点数据。",
                None,
                200,
            )
        except Exception as exc:
            result = classify_mteam_test_error(exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider in {"qb1", "qb2", "qb3"}:
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = QbittorrentWebAdapter(config).test_connection()
            result = qb_test_result(
                True,
                provider,
                test_trace_id,
                "qB 连接成功。",
                f"应用已经成功登录 qB WebUI，并读取到版本 {detail.get('version') or 'unknown'}。",
                "你现在可以启用这个 qB 下载器，仪表盘和下载页会读取真实实时数据。",
                detail=detail,
            )
        except Exception as exc:
            result = classify_qb_test_error(provider, exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider == "ai":
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = DeepSeekChatAdapter(config).test_connection()
            result = ai_test_result(
                True,
                test_trace_id,
                "DeepSeek 连接成功。",
                f"已成功调用 {detail.get('model') or 'DeepSeek'}，可以把自然语言转换为结构化 JSON。",
                "现在可以启用 AI 模块，然后在通知页测试自然语言查询。",
                None,
                200,
                detail=detail,
            )
        except Exception as exc:
            result = classify_ai_test_error(exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    elif provider == "wechat_claw":
        config = get_decrypted_config(db, provider) or {}
        try:
            detail = WechatClawAdapter(config).test_connection()
            result = {
                "success": True,
                "provider": "wechat_claw",
                "mode": "real",
                "http_status": 200,
                "duration_ms": 0,
                "message": "WeChat claw iLink 配置可用。",
                "explanation": "当前模式通过 iLink 长轮询接收微信消息，不需要 NAS 拥有公网 IP。",
                "next_step": "启用后点击刷新二维码，用微信扫码绑定；绑定后可轮询测试消息速度。",
                "error_type": None,
                "can_enable": True,
                "trace_id": test_trace_id,
                "detail": detail,
                "config_version": row.config_version if row else 0,
            }
        except WechatClawConfigError as exc:
            result = {
                "success": False,
                "provider": "wechat_claw",
                "mode": "real",
                "http_status": None,
                "duration_ms": 0,
                "message": "WeChat claw 配置不完整。",
                "explanation": str(exc),
                "next_step": "请检查 iLink 地址，默认通常使用 https://ilinkai.weixin.qq.com。",
                "error_type": "missing_required_fields",
                "can_enable": False,
                "trace_id": test_trace_id,
                "config_version": row.config_version if row else 0,
            }
    else:
        result = mock_test_result(provider, test_trace_id, row)
    return serialize_config(record_test_result(db, provider, result, actor_user_id=user.id))


@router.post("/admin/integrations/tmdb/cloudflare-secrets")
def set_tmdb_cloudflare_secrets(request: IntegrationPayload, _: User = Depends(require_admin)) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="Cloudflare Worker 方案已停用。请改用 TMDB 直连 + DoH 或 mihomo VPN 代理。")


@router.post("/admin/integrations/{provider}/enable")
def enable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider == "tmdb":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "tmdb" or result.get("mode") != "real" or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 TMDB 连接成功后再启用。")
    if provider == "mteam":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "mteam" or result.get("mode") != "real" or result.get("success") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 M-Team 配置可用后再启用。")
    if provider in {"qb1", "qb2", "qb3"}:
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != provider or result.get("mode") != "real" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 qB WebUI 连接成功后再启用。")
    if provider == "ai":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "ai" or result.get("mode") != "real" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先保存并测试 DeepSeek，确认 AI 模块可用后再启用。")
    if provider == "wechat_claw":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "wechat_claw" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先保存并测试 WeChat claw，确认手机端入口配置可用后再启用。")
    return serialize_config(set_enabled(db, provider, True, actor_user_id=user.id))


@router.post("/admin/integrations/{provider}/disable")
def disable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return serialize_config(set_enabled(db, provider, False, actor_user_id=user.id))


@router.get("/admin/integrations/{provider}/audit")
def integration_audit(provider: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(ConfigAuditLog).filter(ConfigAuditLog.provider == provider).order_by(ConfigAuditLog.created_at.desc()).limit(30).all()
    return {"items": [{"action": row.action, "config_version": row.config_version, "test_success": row.test_success, "trace_id": row.trace_id, "created_at": row.created_at.isoformat()} for row in rows]}


def build_dashboard_payload(db: Session) -> dict[str, Any]:
    mteam_row = get_config(db, "mteam")
    connection = mteam_connection_payload(mteam_row)
    mteam = empty_mteam_stats()
    mteam_ok = False
    if mteam_row and mteam_row.enabled:
        try:
            mteam = MTeamAdapter(get_decrypted_config(db, "mteam") or {}).get_user_stats()
            mteam_ok = True
        except Exception as exc:
            connection["last_test_success"] = False
            connection["message"] = f"M-Team 真实数据读取失败：{exc}"
            mteam = empty_mteam_stats("M-Team 真实数据读取失败，请回到设置页重新测试。")
    qbs = [get_qb_state_for_dashboard(db, downloader_id) for downloader_id in ["qb1", "qb2", "qb3"]]
    nas_space = nas_storage_from_configs(db)
    persist_dashboard_snapshots(db, mteam, qbs, mteam_ok)
    if mteam_ok:
        apply_mteam_five_hour_deltas(db, mteam)
    mteam["traffic_series"] = build_mteam_traffic_series(db)
    mteam["traffic_history"] = mteam["traffic_series"]["day"]
    mteam["traffic_calculation"] = {
        "source": "APP 本地累计快照",
        "formula": "周期流量 = 周期内相邻累计快照差值之和；负增量按 0 处理，避免站点计数回退污染统计。",
        "snapshot_interval_minutes": settings.snapshot_interval_minutes,
    }
    return {
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "nas_free_space": nas_space["nas_free_space"],
            "nas_total_space": nas_space["nas_total_space"],
            "nas_used_space": nas_space["nas_used_space"],
            "nas_usage_percent": nas_space["nas_usage_percent"],
            "nas_free_space_label": nas_space["nas_free_space_label"],
            "nas_free_space_source": nas_space["nas_free_space_source"],
            "nas_space_label": nas_space["nas_space_label"],
            "nas_space_helper": nas_space["nas_space_helper"],
            "nas_storage_configured": nas_space["nas_storage_configured"],
            "nas_storage_readable": nas_space["nas_storage_readable"],
            "nas_storage_paths": nas_space["nas_storage_paths"],
            "nas_storage_pool_count": nas_space["nas_storage_pool_count"],
            "nas_storage_folder_count": nas_space["nas_storage_folder_count"],
            "nas_storage_detected_paths": nas_space["nas_storage_detected_paths"],
            "nas_storage_summary_label": nas_space["nas_storage_summary_label"],
            "nas_storage_errors": nas_space["nas_storage_errors"],
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "mteam_connection": connection,
        "mteam": mteam,
        "qbs": qbs,
        "updated_at": datetime.utcnow().isoformat(),
    }


def refresh_dashboard_preload(db: Session) -> dict[str, Any]:
    payload = build_dashboard_payload(db)
    set_preload_cache(db, "dashboard", payload)
    return payload


def build_dashboard_qbs_payload(db: Session) -> dict[str, Any]:
    configs: list[tuple[str, dict[str, Any], str | None]] = []
    states: list[dict[str, Any] | None] = []
    for downloader_id in ["qb1", "qb2", "qb3"]:
        row = get_config(db, downloader_id)
        configured = bool(row and row.encrypted_payload)
        enabled = bool(row and row.enabled)
        name = (row.redacted_summary or {}).get("name") if row else None
        if not configured or not enabled:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name))
            continue
        try:
            configs.append((downloader_id, get_decrypted_config(db, downloader_id) or {}, name))
            states.append(None)
        except Exception as exc:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, f"qB 配置读取失败：{exc}"))
    db.close()

    config_index = 0
    for index, state in enumerate(states):
        if state is not None:
            continue
        downloader_id, config, name = configs[config_index]
        config_index += 1
        try:
            states[index] = QbittorrentWebAdapter(config).get_server_state(downloader_id)
        except Exception as exc:
            states[index] = qb_placeholder_state_from_meta(downloader_id, True, True, name, f"qB 真实数据读取失败：{exc}")

    qbs = [item for item in states if item is not None]
    return {
        "qbs": qbs,
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "updated_at": datetime.utcnow().isoformat(),
    }


def build_download_payload(db: Session, downloader_id: str) -> dict[str, Any]:
    adapter = get_qb_adapter_or_error(db, downloader_id)
    summary = adapter.get_server_state(downloader_id)
    items = adapter.get_torrents(downloader_id)
    summary.update(adapter.summarize_torrents(items))
    return {
        "downloader_id": downloader_id,
        "summary": summary,
        "items": items,
        "source": "qB Web API 原始数据（Real）",
        "updated_at": datetime.utcnow().isoformat(),
    }


def refresh_download_preload(db: Session, downloader_id: str) -> dict[str, Any]:
    payload = build_download_payload(db, downloader_id)
    set_preload_cache(db, f"downloads.{downloader_id}", payload)
    return payload


@router.get("/dashboard")
def dashboard(
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if cached and not refresh:
        cache = get_preload_cache(db, "dashboard")
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_dashboard_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "dashboard"), False)


@router.get("/dashboard/qbs")
def dashboard_qbs(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return build_dashboard_qbs_payload(db)


@router.get("/mteam/stats")
def mteam_stats(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return get_mteam_adapter_or_error(db).get_user_stats()
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 数据获取失败：{exc}") from exc


@router.post("/mteam/test")
def mteam_quick_test(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        result = get_mteam_adapter_or_error(db).test_connection()
        return {
            "success": True,
            "message": "M-Team 连接正常",
            "detail": result,
        }
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 连通性测试失败：{exc}") from exc


@router.get("/downloads/{downloader_id}/overview")
def download_overview(
    downloader_id: str,
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    cache_name = f"downloads.{downloader_id}"
    if cached and not refresh:
        cache = get_preload_cache(db, cache_name)
        if cache:
            add_preload_task_once(background_tasks, f"downloads.{downloader_id}", refresh_download_preload_task, downloader_id)
            return with_preload_meta(cache["payload"], cache, True)
    try:
        payload = refresh_download_preload(db, downloader_id)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 下载页数据获取失败：{exc}") from exc
    return with_preload_meta(payload, get_preload_cache(db, cache_name), False)


@router.post("/qb/{downloader_id}/test")
def qb_test_connection(downloader_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    row = get_config(db, downloader_id)
    test_trace_id = trace_id("QBTEST")
    try:
        detail = QbittorrentWebAdapter(get_decrypted_config(db, downloader_id) or {}).test_connection()
        result = qb_test_result(
            True,
            downloader_id,
            test_trace_id,
            "qB 连接成功。",
            f"应用已经成功登录 qB WebUI，并读取到版本 {detail.get('version') or 'unknown'}。",
            "下载器连接正常，可以继续读取仪表盘和任务数据。",
            detail=detail,
        )
    except Exception as exc:
        result = classify_qb_test_error(downloader_id, exc, test_trace_id)
    result["config_version"] = row.config_version if row else 0
    record_test_result(db, downloader_id, result, actor_user_id=user.id)
    return {
        "success": result["success"],
        "provider": downloader_id,
        "message": result["message"],
        "explanation": result.get("explanation"),
        "next_step": result.get("next_step"),
        "trace_id": test_trace_id,
        "updated_at": datetime.utcnow().isoformat(),
    }


@router.get("/qb/{downloader_id}/summary")
def qb_summary(downloader_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return get_qb_adapter_or_error(db, downloader_id).get_server_state(downloader_id)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 数据获取失败：{exc}") from exc


@router.get("/qb/{downloader_id}/torrents")
def qb_torrents(downloader_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return {"items": get_qb_adapter_or_error(db, downloader_id).get_torrents(downloader_id)}
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 任务列表获取失败：{exc}") from exc


@router.get("/qb/{downloader_id}/torrents/{torrent_hash}/detail")
def qb_torrent_detail(downloader_id: str, torrent_hash: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        return get_qb_adapter_or_error(db, downloader_id).get_torrent_detail(downloader_id, torrent_hash)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 任务详情获取失败：{exc}") from exc


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/files/{file_id}/priority")
def qb_file_priority(downloader_id: str, torrent_hash: str, file_id: int, request: QbActionPayload, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        priority = int(request.payload.get("priority"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="文件优先级参数无效") from exc
    try:
        return get_qb_adapter_or_error(db, downloader_id).set_file_priority(downloader_id, torrent_hash, file_id, priority)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 文件优先级设置失败：{exc}") from exc


@router.post("/qb/{downloader_id}/torrents")
def qb_add_torrent(downloader_id: str, request: QbActionPayload, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        result = get_qb_adapter_or_error(db, downloader_id).add_torrent(downloader_id, request.payload)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 添加任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add", actor_user_id=user.id, status="accepted"))
    db.commit()
    return result


@router.post("/mteam/torrents/{torrent_id}/download-to/{downloader_id}")
def mteam_download_to_qb(
    torrent_id: str,
    downloader_id: str,
    request: QbActionPayload,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="未知下载器")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    stage = "mteam_config"
    try:
        mteam_adapter = get_mteam_adapter_or_error(db)
        stage = "mteam_download_torrent"
        torrent_file = mteam_adapter.download_torrent_file(torrent_id)
        stage = "qb_config"
        qb_adapter = get_qb_adapter_or_error(db, downloader_id)
        stage = "qb_add_torrent_file"
        result = qb_adapter.add_torrent_file(
            downloader_id,
            torrent_file["filename"],
            torrent_file["content"],
            request.payload,
        )
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail={"stage": stage, "provider": "mteam", "torrent_id": torrent_id, "message": str(exc)}) from exc
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail={"stage": stage, "provider": downloader_id, "torrent_id": torrent_id, "message": str(exc)}) from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add_mteam", target_hash=torrent_id, actor_user_id=user.id, status="accepted"))
    db.commit()
    return {**result, "torrent_id": torrent_id, "filename": torrent_file["filename"]}


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/delete-confirm")
def qb_delete_confirm(downloader_id: str, torrent_hash: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"confirm_token": trace_id("DEL"), "message": "用户确认风险后，请在 DELETE 请求中提交这个确认令牌。", "target": {"downloader_id": downloader_id, "hash": torrent_hash, "actor": user.username}}


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/{action}")
def qb_mutate(downloader_id: str, torrent_hash: str, action: str, request: QbActionPayload, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if action not in {"pause", "resume", "limits", "category", "tags"}:
        raise HTTPException(status_code=404, detail="不支持的操作")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    try:
        result = get_qb_adapter_or_error(db, downloader_id).mutate_torrent(downloader_id, torrent_hash, action, request.payload)
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 操作失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


@router.delete("/qb/{downloader_id}/torrents/{torrent_hash}")
def qb_delete(downloader_id: str, torrent_hash: str, confirm_token: str = Query(min_length=8), delete_files: bool = Query(default=False), authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not confirm_token.startswith("DEL-"):
        raise HTTPException(status_code=400, detail="需要服务端删除确认令牌")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")
    action = "delete_files" if delete_files else "delete"
    try:
        result = get_qb_adapter_or_error(db, downloader_id).mutate_torrent(downloader_id, torrent_hash, action, {"delete_files": delete_files})
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 删除任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


def build_discover_payload(db: Session) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if not config_row or not config_row.enabled:
        return {"source": "tmdb", "configured": False, "message": "请先在设置中保存并启用 TMDB API Key 或 Bearer Token。", "trending": [], "popular_movies": [], "popular_tv": [], "top_rated_movies": [], "top_rated_tv": []}
    try:
        return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_discover_lists()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 数据获取失败：{exc}") from exc


def default_discover_filter() -> dict[str, Any]:
    return {
        "media_type": "movie",
        "sort_by": "popularity.desc",
        "genre": "",
        "region": "",
        "language": "",
        "year": "",
        "min_rating": None,
        "page": 1,
        "pages": 1,
    }


def discover_filter_cache_name(filters: dict[str, Any]) -> str:
    normalized = {
        key: value
        for key, value in filters.items()
        if key in {"media_type", "sort_by", "genre", "region", "language", "year", "min_rating", "page", "pages"} and value not in (None, "")
    }
    digest = hashlib.sha1(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:18]
    return f"discover.filter.{digest}"


def build_discover_filter_payload(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).discover_media(filters)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 条件筛选失败：{exc}") from exc
    return MockMetadataAdapter().discover_media(filters)


def refresh_discover_filter_preload(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    payload = build_discover_filter_payload(db, filters)
    set_preload_cache(db, discover_filter_cache_name(filters), payload)
    return payload


def refresh_discover_preload(db: Session) -> dict[str, Any]:
    payload = build_discover_payload(db)
    set_preload_cache(db, "discover", payload)
    try:
        refresh_discover_filter_preload(db, default_discover_filter())
    except Exception:
        pass
    return payload


@router.get("/discover/lists")
def discover_lists(
    background_tasks: BackgroundTasks,
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if cached and not refresh:
        cache = get_preload_cache(db, "discover")
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_discover_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "discover"), False)


@router.get("/discover/filter")
def discover_filter(
    background_tasks: BackgroundTasks,
    media_type: str = Query(default="movie"),
    sort_by: str = Query(default="popularity.desc"),
    genre: str = Query(default=""),
    region: str = Query(default=""),
    language: str = Query(default=""),
    year: str = Query(default=""),
    min_rating: float | None = Query(default=None, ge=0, le=10),
    page: int = Query(default=1, ge=1, le=500),
    pages: int = Query(default=1, ge=1, le=4),
    include_options: bool = Query(default=True),
    cached: bool = Query(default=False),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    filters = {
        "media_type": media_type,
        "sort_by": sort_by,
        "genre": genre,
        "region": region,
        "language": language,
        "year": year,
        "min_rating": min_rating,
        "page": page,
        "pages": pages,
        "include_options": include_options,
    }
    cache_name = discover_filter_cache_name(filters)
    if cached and not refresh:
        cache = get_preload_cache(db, cache_name)
        if cache:
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_discover_filter_preload(db, filters)
    return with_preload_meta(payload, get_preload_cache(db, cache_name), False)


@router.get("/tmdb/image/{size}/{image_path:path}")
def tmdb_image_proxy(size: str, image_path: str, db: Session = Depends(get_db)) -> Response:
    if size not in TMDB_IMAGE_SIZES:
        raise HTTPException(status_code=400, detail="Unsupported TMDB image size")
    clean_path = _safe_tmdb_image_path(image_path)
    cache_dir = Path(settings.tmdb_image_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _tmdb_image_cache_file(size, clean_path)
    media_type = mimetypes.guess_type(clean_path)[0] or "image/jpeg"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        if _valid_tmdb_image_file(cache_file):
            cached_media_type = _tmdb_cached_image_media_type(cache_file, media_type)
            return FileResponse(cache_file, media_type=cached_media_type, headers=_tmdb_image_headers(cache_file))
        try:
            cache_file.unlink()
            logger.warning("Removed invalid TMDB image cache file: size=%s path=%s file=%s", size, clean_path, cache_file)
        except OSError:
            logger.warning("TMDB image cache file is invalid but could not be removed: size=%s path=%s file=%s", size, clean_path, cache_file)

    url = f"https://image.tmdb.org/t/p/{size}/{clean_path}"
    request = Request(url, headers={"Accept": "image/jpeg,image/png,image/webp,*/*", "User-Agent": "PT-Media-Hub"})
    mode, proxy_url, timeout = _tmdb_image_network_config(db)
    try:
        with open_tmdb_network_request(request, mode, proxy_url, timeout) as response:
            content = response.read(TMDB_IMAGE_MAX_BYTES + 1)
            if len(content) > TMDB_IMAGE_MAX_BYTES:
                logger.warning("TMDB image fetch rejected because it is too large: size=%s path=%s mode=%s", size, clean_path, mode)
                return _tmdb_image_placeholder_response("image_too_large")
            if not _valid_tmdb_image_bytes(content):
                logger.warning("TMDB image fetch returned non-image content: size=%s path=%s mode=%s content_type=%s", size, clean_path, mode, media_type)
                return _tmdb_image_placeholder_response("invalid_image_content")
            media_type = _tmdb_image_media_type_from_bytes(content, response.headers.get_content_type() or media_type)
    except (HTTPError, HTTPException, Exception) as exc:
        reason = _tmdb_image_error_label(exc)
        logger.warning(
            "TMDB image fetch failed: size=%s path=%s mode=%s proxy=%s error=%s",
            size,
            clean_path,
            mode,
            urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}").netloc if proxy_url else "",
            reason,
        )
        return _tmdb_image_placeholder_response(reason)

    tmp_file = cache_file.with_suffix(f"{cache_file.suffix}.tmp")
    tmp_file.write_bytes(content)
    tmp_file.replace(cache_file)
    _prune_tmdb_image_cache(cache_dir)
    return FileResponse(cache_file, media_type=media_type, headers=_tmdb_image_headers(cache_file))


@router.get("/search/media")
def search_media(q: str = Query(default=""), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return {"items": TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).search_media(q)}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 搜索失败：{exc}") from exc
    return {"items": MockMetadataAdapter().search_media(q)}


@router.get("/tmdb/media/{media_type}/{media_id}")
def tmdb_media_detail(media_type: str, media_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=404, detail="不支持的 TMDB 媒体类型")
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_media_details(media_id, media_type)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 详情获取失败：{exc}") from exc
    return MockMetadataAdapter().get_media_details(media_id, media_type)


@router.get("/tmdb/person/{person_id}")
def tmdb_person_detail(person_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if config_row and config_row.enabled:
        try:
            return TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_person_details(person_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"TMDB 演员详情获取失败：{exc}") from exc
    return MockMetadataAdapter().get_person_details(person_id)


@router.get("/search/mteam")
def search_mteam(q: str = Query(default=""), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if not q.strip():
        return {"items": []}
    try:
        return {"items": get_mteam_adapter_or_error(db).search_torrents(q)}
    except MTeamApiError as exc:
        raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc


@router.get("/stats")
def stats(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    snapshots = db.query(QbSnapshot).order_by(QbSnapshot.captured_at.desc()).limit(18).all()
    series = [{"downloader_id": row.downloader_id, "download_speed": row.download_speed, "upload_speed": row.upload_speed, "captured_at": row.captured_at.isoformat(), "source": row.source, "completeness": row.completeness} for row in reversed(snapshots)]
    return {
        "ranges": ["24h", "7d", "30d", "custom"],
        "series": series,
        "explainability": {"source": "下载器原始数据与应用计算数据（Mock）", "formula": "周期增量 = 当前有效快照 - 起始有效快照", "completeness": "完整" if series else "部分缺失"},
    }


@router.post("/assistant/execute")
def assistant_execute(request: AssistantExecuteRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    intent = request.intent
    result = execute_assistant_intent_v2(db, intent, request.message, user)
    return {
        "intent": intent,
        "result": result,
        "reply": format_assistant_result_v2(intent, result),
        "source": "structured_json",
        "handled_at": datetime.utcnow().isoformat(),
    }


@router.post("/assistant/chat")
def assistant_chat(request: AssistantChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    adapter = get_ai_adapter_or_error(db)
    try:
        intent = adapter.parse_intent(request.message)
    except AIServiceError as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek 解析失败：{exc}") from exc
    result = execute_assistant_intent_v2(db, intent, request.message, user)
    try:
        reply = adapter.summarize_result(request.message, intent, result)
    except AIServiceError:
        reply = format_assistant_result_v2(intent, result)
    return {
        "intent": intent,
        "result": result,
        "reply": reply,
        "source": "deepseek",
        "handled_at": datetime.utcnow().isoformat(),
    }


@router.get("/notification-preferences")
def notification_preferences(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": get_notification_preferences(db), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.put("/notification-preferences")
def update_notification_preferences(request: NotificationPreferencesPayload, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": save_notification_preferences(db, request.preferences), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.get("/admin/wechat-claw/setup")
def admin_wechat_claw_setup(
    refresh_remote: bool = Query(default=True),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if refresh_remote:
        refresh_wechat_claw_login_state(db)
    return build_wechat_claw_setup(db)


@router.post("/admin/wechat-claw/refresh")
def admin_wechat_claw_refresh(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    config = get_decrypted_config(db, "wechat_claw") or {}
    adapter = WechatClawAdapter(config)
    result = adapter.get_qrcode()
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message") or "获取 iLink 二维码失败")
    qrcode = {
        "qrcode": result.get("qrcode"),
        "qrcode_url": result.get("qrcode_url"),
        "status": result.get("status") or "waiting",
        "updated_at": int(datetime.utcnow().timestamp()),
    }
    # A fresh QR code starts a new iLink session. Retaining the old token here
    # would prevent the background worker from polling the new QR status.
    update_wechat_claw_ilink_state(
        db,
        qrcode=qrcode,
        base_url=adapter.base_url,
        bot_token="",
        account_id="",
        sync_buf="",
        known_targets={},
    )
    return build_wechat_claw_setup(db)


@router.post("/admin/wechat-claw/logout")
def admin_wechat_claw_logout(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    clear_wechat_claw_ilink_state(db)
    return build_wechat_claw_setup(db)


def require_wechat_claw_token(db: Session, header_token: str | None, query_token: str | None) -> None:
    config = get_decrypted_config(db, "wechat_claw") or {}
    expected = str(config.get("inbound_token") or "").strip()
    provided = str(header_token or query_token or "").strip()
    if not expected or not provided or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid WeChat claw token")


def assistant_actor_for_wechat_claw(db: Session) -> User:
    binding_user_id = _WECHAT_CLAW_ACTIVE_USER_ID.get()
    if binding_user_id is not None:
        bound_user = db.get(User, binding_user_id)
        if bound_user and bound_user.is_active:
            return bound_user
    user = db.query(User).filter(User.role == "admin", User.is_active.is_(True)).order_by(User.id.asc()).first()
    if user is None:
        user = db.query(User).filter(User.is_active.is_(True)).order_by(User.id.asc()).first()
    if user is None:
        raise HTTPException(status_code=409, detail="No active user is available for WeChat claw actions")
    return user


def handle_wechat_claw_text(db: Session, request: WechatClawMessageRequest) -> dict[str, Any]:
    user = assistant_actor_for_wechat_claw(db)
    ai_adapter = get_ai_adapter_or_error(db)
    try:
        parsed_intent = ai_adapter.parse_intent(request.message)
    except AIServiceError as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek 解析失败：{exc}") from exc
    result = execute_assistant_intent_v2(db, parsed_intent, request.message, user)
    try:
        reply = ai_adapter.summarize_result(request.message, parsed_intent, result)
    except AIServiceError:
        reply = format_assistant_result_v2(parsed_intent, result)
    interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply)
    return {
        "reply": reply,
        "intent": parsed_intent,
        "result": result,
        "interaction": interaction,
        "source": "wechat_claw",
        "conversation_id": request.conversation_id,
        "handled_at": datetime.utcnow().isoformat(),
    }


def poll_wechat_claw_messages(db: Session, user_id: int | None = None) -> dict[str, Any]:
    # iLink advances one shared get_updates_buf cursor. Never allow the admin
    # diagnostic button to race the background long-poll in this process.
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user_id)
    lock = wechat_claw_poll_lock(user_id)
    if not lock.acquire(blocking=False):
        pending_count = len(get_wechat_claw_pending_messages(get_wechat_claw_ilink_state(db)))
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
        return {
            "success": False,
            "stage": "busy",
            "raw_count": 0,
            "parsed_count": 0,
            "handled_count": 0,
            "reply_sent_count": 0,
            "pending_count": pending_count,
            "message": "WeChat claw 正在由后台接收消息，请勿并发轮询。",
        }
    try:
        return _poll_wechat_claw_messages_locked(db)
    finally:
        lock.release()
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def _poll_wechat_claw_messages_locked(db: Session) -> dict[str, Any]:
    refresh_wechat_claw_login_state(db)
    row = get_config(db, "wechat_claw")
    if not row or not row.enabled or not row.encrypted_payload:
        return save_wechat_claw_last_poll(db, {"success": False, "stage": "disabled", "message": "WeChat claw 未启用"})
    config = get_decrypted_config(db, "wechat_claw") or {}
    state = get_wechat_claw_ilink_state(db)
    adapter = WechatClawAdapter(wechat_claw_config_with_state(config, state))
    if not adapter.bot_token:
        return save_wechat_claw_last_poll(db, {"success": False, "stage": "login", "message": "iLink 登录 token 尚未保存，请刷新设置状态后重试"})

    # A reply that failed after iLink had delivered its update is durable here.
    # Retry it before starting another long-poll so failures recover promptly.
    previous = process_wechat_claw_pending_messages(db, adapter)
    if previous["pending_count"]:
        status = save_wechat_claw_last_poll(
            db,
            {
                "success": False,
                "stage": "retry_wait",
                "handled_count": len(previous["handled"]),
                "reply_sent_count": previous["reply_sent_count"],
                "pending_count": previous["pending_count"],
                "message": "消息已保留，正在等待下一次自动重试。",
            },
        )
        return {**status, "handled": previous["handled"], "retry_after_seconds": previous["retry_after_seconds"] or 1.0}
    try:
        poll_result = adapter.poll_updates(timeout_seconds=int(config.get("poll_timeout") or 25))
    except WechatClawApiError as exc:
        return save_wechat_claw_last_poll(
            db,
            {"success": False, "stage": "getupdates", "message": str(exc), "raw_count": 0, "parsed_count": 0},
        )
    if not poll_result.get("success") and is_wechat_claw_session_timeout(poll_result.get("message")):
        state = get_wechat_claw_ilink_state(db)
        current_qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
        # Keep a newly generated waiting QR code so the worker can finish the new login.
        clear_wechat_claw_session(db, None if current_qrcode.get("status") == "waiting" else "expired")
        return save_wechat_claw_last_poll(
            db,
            {
                "success": False,
                "stage": "session_expired",
                "raw_count": poll_result.get("raw_count", 0),
                "parsed_count": poll_result.get("parsed_count", 0),
                "message": "iLink 登录会话已失效，已清除旧 token 并等待新的二维码登录。",
            },
        )
    queue_wechat_claw_messages(db, poll_result.get("messages") or [], poll_result.get("sync_buf"))
    processed = process_wechat_claw_pending_messages(db, adapter)
    status = save_wechat_claw_last_poll(
        db,
        {
            "success": bool(poll_result.get("success")),
            "stage": "handled" if processed["handled"] else "getupdates",
            "raw_count": poll_result.get("raw_count", 0),
            "parsed_count": poll_result.get("parsed_count", 0),
            "handled_count": len(processed["handled"]),
            "reply_sent_count": processed["reply_sent_count"],
            "pending_count": processed["pending_count"],
            "message": poll_result.get("message"),
        },
    )
    return {**status, "handled": processed["handled"], "retry_after_seconds": processed["retry_after_seconds"]}


@router.post("/wechat-claw/message")
def wechat_claw_message(
    request: WechatClawMessageRequest,
    x_wechat_claw_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_adapter_or_error(db)
    require_wechat_claw_token(db, x_wechat_claw_token, token)
    return handle_wechat_claw_text(db, request)


@router.post("/admin/wechat-claw/poll")
def admin_wechat_claw_poll(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return poll_wechat_claw_messages(db)


@router.get("/me/wechat-claw/setup")
def my_wechat_claw_setup(
    refresh_remote: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_setup(refresh_remote, user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/refresh")
def my_wechat_claw_refresh(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_refresh(user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/logout")
def my_wechat_claw_logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(user.id)
    try:
        return admin_wechat_claw_logout(user, db)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/me/wechat-claw/poll")
def my_wechat_claw_poll(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return poll_wechat_claw_messages(db, user.id)


@router.get("/wechat-claw/capabilities")
def wechat_claw_capabilities(
    x_wechat_claw_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    adapter = get_wechat_claw_adapter_or_error(db)
    require_wechat_claw_token(db, x_wechat_claw_token, token)
    preferences = get_notification_preferences(db)
    return {
        "name": "PT Media Hub WeChat claw",
        "public_base_url": adapter.public_base_url,
        "mobile_app_url": adapter.public_base_url,
        "message_endpoint": f"{adapter.public_base_url}/api/wechat-claw/message",
        "capabilities_endpoint": f"{adapter.public_base_url}/api/wechat-claw/capabilities",
        "mobile_sections": WECHAT_CLAW_MOBILE_SECTIONS,
        "capabilities": WECHAT_CLAW_CAPABILITIES,
        "notification_preferences": preferences,
    }


@router.get("/notifications")
def notifications(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(Notification).order_by(Notification.created_at.desc()).limit(50).all()
    if not rows:
        rows = [Notification(title="示例通知", message="下载监控已在线。", level="info", source="mock"), Notification(title="示例阈值", message="NAS 剩余空间状态健康。", level="success", source="mock")]
    return {"items": [{"id": row.id, "title": row.title, "message": row.message, "level": row.level, "read": row.read, "source": row.source, "created_at": row.created_at.isoformat() if row.created_at else None} for row in rows]}


@router.get("/diagnostics/health")
def diagnostics_health(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    integrations = {row.provider: row for row in db.query(IntegrationConfig).all()}
    modules = []
    for provider in PROVIDERS + ["nas_disk", "stats_engine"]:
        row = integrations.get(provider)
        result = row.last_test_result if row else None
        if provider in ["nas_disk", "stats_engine"]:
            status = "success"
        elif result and result.get("success") is True:
            status = "success"
        elif result and result.get("success") is False:
            status = "failed"
        else:
            status = "not_tested"
        modules.append({"module": provider, "status": status, "enabled": bool(row.enabled) if row else provider in ["nas_disk", "stats_engine"], "last_success_at": row.last_tested_at.isoformat() if row and row.last_tested_at else None, "duration_ms": result.get("duration_ms") if result else None, "last_error": result.get("message") if isinstance(result, dict) else None})
    return {"modules": modules}


@router.get("/diagnostics/traces")
def diagnostics_traces(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(DebugTrace).order_by(DebugTrace.created_at.desc()).limit(50).all()
    return {"items": [{"trace_id": row.trace_id, "event_type": row.event_type, "status": row.status, "timeline": row.timeline, "duration_ms": row.duration_ms, "config_version": row.config_version, "error_summary": row.error_summary, "created_at": row.created_at.isoformat()} for row in rows]}


@router.post("/diagnostics/export")
def diagnostics_export(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    export_trace = trace_id("DIAG")
    traces = db.query(DebugTrace).order_by(DebugTrace.created_at.desc()).limit(50).all()
    errors = db.query(DebugTrace).filter(DebugTrace.status == "failed").order_by(DebugTrace.created_at.desc()).limit(20).all()
    payload = redact_payload(
        {
            "version": settings.app_version,
            "environment": settings.environment,
            "timezone": "UTC",
            "cpu_arch": "runtime-detected-by-container",
            "recent_traces": [row.timeline for row in traces],
            "recent_errors": [{"trace_id": row.trace_id, "error": row.error_summary} for row in errors],
            "health": diagnostics_health(user, db),
            "generated_at": datetime.utcnow().isoformat(),
        }
    )
    db.add(DiagnosticExport(trace_id=export_trace, payload=payload, created_by=user.id))
    db.commit()
    return {"trace_id": export_trace, "payload": payload}
