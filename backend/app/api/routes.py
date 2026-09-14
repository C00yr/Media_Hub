import os
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from socket import timeout as SocketTimeout
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import Request

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request as FastAPIRequest, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.ai import AIConfigError, AIServiceError, DeepSeekChatAdapter, redact_ai_user_text
from app.adapters.mteam import MTeamAdapter, MTeamApiError, MTeamConfigError
from app.adapters.qbittorrent import QbittorrentApiError, QbittorrentConfigError, QbittorrentWebAdapter
from app.adapters.tmdb import TmdbAdapter
from app.adapters.tmdb.client import (
    TmdbConfigError,
    TmdbDohError,
    TmdbImageError,
    TmdbProxyError,
    open_tmdb_network_request,
    resolve_tmdb_proxy_settings,
    tmdb_network_detail,
)
from app.adapters.wechat_claw import WechatClawAdapter, WechatClawApiError, WechatClawConfigError
from app.api.deps import get_current_user, has_qb2_grant, require_admin
from app.auth.security import (
    DEFAULT_CREDENTIALS_PENDING_KEY,
    LEGACY_DEFAULT_ADMIN_PASSWORD,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.config.settings import get_settings
from app.db.session import SessionLocal, get_db
from app.diagnostics.tracing import TraceRecorder
from app.models.entities import (
    AgentDownloadOperation,
    ConfigAuditLog,
    DebugTrace,
    DownloadAction,
    IntegrationConfig,
    MTeamSnapshot,
    MTeamTrafficRollup,
    MediaFavorite,
    Notification,
    QbDeleteConfirmation,
    QbSnapshot,
    Setting,
    User,
    UserSession,
    WechatClawBinding,
)
from app.services.knowledge import search_knowledge
from app.services.integrations import (
    PROVIDERS,
    decode_saved_payload,
    get_config,
    get_decrypted_config,
    public_test_result,
    record_test_result,
    serialize_config,
    set_enabled,
    upsert_config,
)
from app.utils.ids import trace_id
from app.utils.redaction import redact_payload
from app.utils.time import format_system_datetime, parse_datetime, system_datetime, system_time_context, system_timezone_name, utc_datetime, utc_iso, utc_now, utc_now_naive


router = APIRouter()
settings = get_settings()
logger = logging.getLogger(__name__)
_PRELOAD_TASK_LOCK = Lock()
_PRELOAD_TASKS_RUNNING: set[str] = set()
_WECHAT_CLAW_POLL_LOCKS: dict[int, Lock] = {}
_WECHAT_CLAW_POLL_LOCK_GUARD = Lock()
_WECHAT_CLAW_BINDING_CREATE_LOCK = Lock()
_QB_TASK_METADATA_LOCK = Lock()
_AGENT_DOWNLOAD_LOCK_GUARD = Lock()
_AGENT_DOWNLOAD_LOCKS: dict[str, Lock] = {}
_WECHAT_CLAW_ACTIVE_USER_ID: ContextVar[int | None] = ContextVar("wechat_claw_active_user_id", default=None)
PRELOAD_PREFIX = "preload."
TMDB_IMAGE_SIZES = {"w92", "w154", "w185", "w342", "w500", "w780", "w1280", "original"}
TMDB_IMAGE_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)
TMDB_DIRECT_IMAGE_RE = re.compile(
    r"https://image\.tmdb\.org/t/p/(?P<size>w\d+|original)/(?P<path>[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp))",
    re.IGNORECASE,
)
TMDB_IMAGE_MAX_BYTES = 20 * 1024 * 1024
TORRENT_FILE_MAX_BYTES = 20 * 1024 * 1024
QB_DELETE_CONFIRM_TTL_SECONDS = 2 * 60
NAS_STORAGE_SETUP_STATUS_KEY = "setup.nas_storage.status"


class SetupAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminVerifyRequest(BaseModel):
    username: str
    password: str


class AccountUpdateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class IntegrationPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class QbActionPayload(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm_token: str | None = None


class DefaultDownloaderPayload(BaseModel):
    downloader_id: str = Field(min_length=3, max_length=3)


class AssistantChatRequest(BaseModel):
    conversation_id: str = Field(default="", max_length=160)
    message: str = Field(min_length=1, max_length=2000)


class FavoritePayload(BaseModel):
    media: dict[str, Any] = Field(default_factory=dict)


class AssistantExecuteRequest(BaseModel):
    intent: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class NotificationPreferencesPayload(BaseModel):
    preferences: dict[str, bool] = Field(default_factory=dict)


class WechatClawMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    user_id: str = ""
    conversation_id: str = ""
    context_token: str = ""
    admin_password: str = Field(default="", max_length=200)


class WechatClawBindingPayload(BaseModel):
    display_name: str = Field(default="", max_length=120)
    role_name: str = Field(default="微信助手", min_length=1, max_length=120)
    enabled: bool = True
    notification_preferences: dict[str, Any] = Field(default_factory=dict)


DEFAULT_NOTIFICATION_PREFERENCES: dict[str, bool] = {
    "download_started": True,
    "download_completed": True,
    "resource_search": False,
    "status_query": False,
    "wechat_claw_push": True,
}
DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES: dict[str, bool] = {
    "qb1_download_started": False,
    "qb1_download_completed": False,
    "qb2_download_started": False,
    "qb2_download_completed": False,
    "qb3_download_started": False,
    "qb3_download_completed": False,
    "mteam_exception": True,
    "qb_exception": True,
    "ai_exception": True,
    "tmdb_exception": True,
    "wechat_claw_exception": True,
}
WECHAT_CLAW_AVATAR_KEYS = ("mint", "violet", "coral", "sky", "amber", "rose", "indigo", "lime")

NOTIFICATION_PREFERENCES_KEY = "notification.preferences"
DEFAULT_DOWNLOADER_SETTING_KEY = "downloads.default_downloader"
DOWNLOADER_IDS = ("qb1", "qb2", "qb3")
WECHAT_CLAW_INTERACTIONS_KEY = "wechat_claw.interactions"
WECHAT_CLAW_ILINK_STATE_KEY = "wechat_claw.ilink_state"
WECHAT_CLAW_LAST_POLL_KEY = "wechat_claw.last_poll"
WECHAT_CLAW_MEMBER_LIMIT = 5
WECHAT_CLAW_INTERACTION_LIMIT = 5
WECHAT_CLAW_PENDING_LIMIT = 100
MODULE_HEALTH_PREFIX = "module.health."
LIVE_DIAGNOSTIC_MODULES = tuple(PROVIDERS) + ("nas_disk",)
QB_TASK_STATE_PREFIX = "qb.task_state."
QB_TASK_METADATA_PREFIX = "qb.task_metadata."
WECHAT_CLAW_MOBILE_SELECTION_LIMIT = 10
WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS = 30 * 60
ASSISTANT_AGENT_SESSIONS_KEY = "assistant.agent_sessions"
ASSISTANT_AGENT_SESSION_TTL_SECONDS = 24 * 60 * 60
ASSISTANT_AGENT_HISTORY_LIMIT = 12
ASSISTANT_AGENT_MAX_TOOL_STEPS = 6
AGENT_DOWNLOAD_CONFIRM_TTL_SECONDS = 30 * 60
AGENT_DOWNLOAD_VERIFY_TIMEOUT_SECONDS = 4.0
AGENT_DOWNLOAD_VERIFY_INTERVAL_SECONDS = 0.35
MTEAM_INITIAL_DISPLAY_LIMIT = 5
MTEAM_SHOW_ALL_RESULTS_RE = re.compile(r"(?:查看|显示|展示|给我).*(?:全部|完整)(?:资源|结果|列表|信息)?|(?:全部|完整).*(?:资源|结果|列表|信息)", re.IGNORECASE)
MTEAM_SITE_DATA_RE = re.compile(r"(?:馒头|m[-\s]?team).*(?:站点|账号|数据|信息|状态)|(?:站点|账号|数据|信息|状态).*(?:馒头|m[-\s]?team)", re.IGNORECASE)
WECHAT_CLAW_DOWNLOAD_SELECTION_RE = re.compile(r"^\s*(?:下载|选择|选)\s*(?:第)?\s*(\d+)\s*$", re.IGNORECASE)
WECHAT_CLAW_ADMIN_VERIFY_RE = re.compile(r"^\s*(?:验证管理员密码|管理员验证|验证密码)\s*[:： ]\s*(.+?)\s*$", re.IGNORECASE)
WECHAT_CLAW_DOWNLOAD_CONFIRM_RE = re.compile(r"(?:确认|就它|就这个|就下载|可以下载|开始下载|下吧|下载吧)", re.IGNORECASE)
WECHAT_CLAW_EXPLICIT_DOWNLOAD_CONFIRM_RE = re.compile(
    r"^\s*(?:\u786e\u8ba4(?:\u4e0b\u8f7d)?|\u5c31\u5b83|\u5c31\u8fd9\u4e2a|\u5c31\u4e0b\u8f7d|\u5f00\u59cb\u4e0b\u8f7d|\u4e0b\u8f7d\u5427|\u4e0b\u5427|\u53ef\u4ee5\u4e0b\u8f7d)\s*[.!\u3002\uff01]*$"
    r"|(?:\u786e\u8ba4|\u53ef\u4ee5|\u5f00\u59cb|\u7acb\u5373).{0,8}(?:\u4e0b\u8f7d|\u63a8\u9001|\u6dfb\u52a0)"
)
WECHAT_CLAW_SELECTION_WORDS = {"第一个": 1, "第一部": 1, "第1个": 1, "推荐的": -1, "推荐那个": -1, "推荐的那个": -1}
WECHAT_CLAW_PRIVACY_GRANT_SECONDS = 15 * 60
SUPER_PASSWORD_SETTING_KEY = "auth.super_password"


AGENT_TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "name": "resolve_media_from_clues",
        "description": "Identify a movie or TV work from people, director, year, plot or other fuzzy clues. Every returned identity is verified against TMDB; model memory is only a search hypothesis.",
        "arguments": {"query": "the user's complete clue sentence", "people": "explicit person names", "director": "explicit director clue", "year": "explicit year", "media_type": "movie|tv|all", "title_hypotheses": "possible titles used only for TMDB lookup", "wants_details": "boolean", "wants_resources": "boolean"},
    },
    {
        "name": "tmdb_lookup",
        "description": "Find movies or TV shows by title, or discover them by type, region, language, genre, year, rating and sort order. This is the former TMDB intent template.",
        "arguments": {"query": "title or keywords; may be empty for discovery", "media_type": "movie|tv|all", "region": "two-letter region code", "language": "language code", "genre": "genre", "year": "year or decade", "min_rating": "0-10", "sort_by": "vote_average.desc|popularity.desc|release_date.desc", "limit": "1-10"},
    },
    {
        "name": "tmdb_media_details",
        "description": "Fetch the complete overview, credits, seasons, episodes, similar and recommended titles for a TMDB result. Prefer result_index for a recent result.",
        "arguments": {"result_index": "one-based index in recent TMDB results", "media_id": "explicit TMDB ID", "media_type": "movie|tv"},
    },
    {
        "name": "tmdb_person_details",
        "description": "Fetch biography and known works for an explicit TMDB person_id.",
        "arguments": {"person_id": "TMDB person ID"},
    },
    {
        "name": "mteam_search",
        "description": "Search M-Team (馒头, 馒头站点, or PT站 in this app) with strict canonical resource constraints.",
        "arguments": {"query": "movie or release keywords", "resolutions": "array such as 2160p or 1080p", "min_size_gb": "minimum size", "max_size_gb": "maximum size", "download_factor_max": "0 to 1; FREE=0, 30%=0.3, 50%=0.5", "min_seeders": "minimum seeders", "official_only": "boolean", "has_chinese_subtitles": "boolean or null", "promotion_expires_after_minutes": "minimum remaining promotion minutes", "limit": "1-10"},
    },
    {
        "name": "mteam_result_details",
        "description": "Call M-Team again for current details of a numbered recent result, including promotion deadline, remaining time, seeders and release data.",
        "arguments": {"result_index": "one-based index in recent M-Team results"},
    },
    {
        "name": "dashboard_query",
        "description": "Read app dashboard, M-Team account, NAS, downloader summaries, statistics or diagnostics. This is the former dashboard intent template.",
        "arguments": {"sections": "array of overview|mteam|nas|qb1|qb2|qb3|downloads|stats|diagnostics"},
    },
    {
        "name": "qb_list_torrents",
        "description": "List tasks from a qB downloader, optionally filtered by task name. qB2 requires an active privacy grant.",
        "arguments": {"downloader_id": "qb1|qb2|qb3", "query": "optional task-name keyword", "limit": "1-10"},
    },
    {
        "name": "qb_torrent_details",
        "description": "Read progress, files and connection properties for a numbered recent qB task without exposing paths, hashes or tracker URLs.",
        "arguments": {"downloader_id": "qb1|qb2|qb3", "result_index": "one-based index in recent results for that downloader"},
    },
    {
        "name": "prepare_mteam_download",
        "description": "Select a numbered recent M-Team result and ask for confirmation. This never starts a download.",
        "arguments": {"result_index": "one-based index in recent M-Team results", "downloader_id": "optional qb1|qb2|qb3 override; otherwise use the configured default"},
    },
    {
        "name": "confirm_mteam_download",
        "description": "Send the prepared M-Team release to the default qB downloader. Use only when the current user message explicitly confirms.",
        "arguments": {},
    },
    {
        "name": "find_media_with_mteam",
        "description": "Find TMDB works and verify matching M-Team resources in a bounded workflow. Use for requests combining movie qualities such as genre, rating, or release dates with torrent constraints.",
        "arguments": {"query": "optional title keywords", "media_type": "movie|tv|all", "genre": "genre name", "release_date_from": "YYYY-MM-DD", "release_date_to": "YYYY-MM-DD", "min_rating": "0-10; high-rated defaults to 7.5", "min_vote_count": "default 200 for high-rated", "target_count": "1-10; default 3", "max_size_gb": "maximum resource size", "download_factor_max": "0 to 1", "min_seeders": "minimum seeders", "official_only": "boolean", "resolutions": "array"},
    },
    {
        "name": "knowledge_search",
        "description": "Search versioned local Media Hub and M-Team explanatory knowledge. Never use it for current site, torrent, or downloader facts.",
        "arguments": {"query": "rule, terminology, manual, privacy, or troubleshooting question", "limit": "1-5"},
    },
]
AGENT_TOOL_NAMES = frozenset(item["name"] for item in AGENT_TOOL_CATALOG)


def wechat_claw_setting_key(key: str, user_id: int | None = None) -> str:
    effective_user_id = user_id if user_id is not None else _WECHAT_CLAW_ACTIVE_USER_ID.get()
    return key if effective_user_id is None else f"{key}.binding.{effective_user_id}"


def wechat_claw_poll_lock(user_id: int | None) -> Lock:
    key = int(user_id or 0)
    with _WECHAT_CLAW_POLL_LOCK_GUARD:
        return _WECHAT_CLAW_POLL_LOCKS.setdefault(key, Lock())


def normalized_wechat_claw_binding_preferences(preferences: dict[str, Any] | None = None) -> dict[str, Any]:
    source = preferences if isinstance(preferences, dict) else {}
    normalized: dict[str, Any] = {
        key: bool(source.get(key, default))
        for key, default in DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES.items()
    }
    try:
        hours = int(source.get("exception_after_hours", 5))
    except (TypeError, ValueError):
        hours = 5
    normalized["exception_after_hours"] = hours if hours in {3, 5, 24} else 5
    return normalized


def next_wechat_claw_member_name(db: Session) -> str:
    names = [item[0] for item in db.query(WechatClawBinding.display_name).all()]
    numbers = [int(match.group(1)) for name in names if (match := re.fullmatch(r"成员(\d+)", str(name).strip()))]
    return f"成员{(max(numbers) if numbers else 0) + 1}"


def next_wechat_claw_avatar_key(db: Session) -> str:
    used = {str(item[0] or "") for item in db.query(WechatClawBinding.avatar_key).all()}
    candidates = [key for key in WECHAT_CLAW_AVATAR_KEYS if key not in used] or list(WECHAT_CLAW_AVATAR_KEYS)
    return secrets.choice(candidates)


def serialize_wechat_claw_binding(binding: WechatClawBinding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "display_name": binding.display_name,
        "role_name": binding.role_name,
        "avatar_key": binding.avatar_key or "mint",
        "enabled": binding.enabled,
        "notification_preferences": normalized_wechat_claw_binding_preferences(binding.notification_preferences),
        "created_at": utc_iso(binding.created_at) if binding.created_at else None,
        "updated_at": utc_iso(binding.updated_at) if binding.updated_at else None,
    }


def wechat_claw_binding_status(db: Session, binding: WechatClawBinding) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db, binding.id)
    last_poll = get_wechat_claw_last_poll(db, binding.id)
    provider = get_config(db, "wechat_claw")
    configured = bool(provider and provider.encrypted_payload)
    module_enabled = bool(provider and provider.enabled)
    member_enabled = bool(binding.enabled)
    connected = bool(state.get("bot_token"))
    return {
        **serialize_wechat_claw_binding(binding),
        "configured": configured,
        "module_enabled": module_enabled,
        "connected": connected,
        "operational": module_enabled and member_enabled and connected,
        "account_id": state.get("account_id") or "",
        "qrcode_status": (state.get("qrcode") or {}).get("status") if isinstance(state.get("qrcode"), dict) else "",
        "last_poll": last_poll,
        "recent_interactions": get_wechat_claw_recent_interactions(db, binding.id),
    }


def module_health_setting_key(module: str) -> str:
    return f"{MODULE_HEALTH_PREFIX}{module}"


def qb_task_state_setting_key(downloader_id: str) -> str:
    return f"{QB_TASK_STATE_PREFIX}{downloader_id}"


def qb_task_metadata_setting_key(downloader_id: str) -> str:
    return f"{QB_TASK_METADATA_PREFIX}{downloader_id}"


def _bencoded_bytes(data: bytes, index: int) -> tuple[bytes, int]:
    colon = data.find(b":", index)
    if colon < 0 or not data[index:colon].isdigit():
        raise ValueError("invalid bencoded bytes")
    length = int(data[index:colon])
    start = colon + 1
    end = start + length
    if end > len(data):
        raise ValueError("truncated bencoded bytes")
    return data[start:end], end


def _bencoded_value_end(data: bytes, index: int, depth: int = 0) -> int:
    if index >= len(data) or depth > 100:
        raise ValueError("invalid bencoded value")
    token = data[index:index + 1]
    if token == b"i":
        end = data.find(b"e", index + 1)
        if end < 0:
            raise ValueError("unterminated bencoded integer")
        return end + 1
    if token in {b"l", b"d"}:
        cursor = index + 1
        while cursor < len(data) and data[cursor:cursor + 1] != b"e":
            cursor = _bencoded_value_end(data, cursor, depth + 1)
            if token == b"d":
                cursor = _bencoded_value_end(data, cursor, depth + 1)
        if cursor >= len(data):
            raise ValueError("unterminated bencoded collection")
        return cursor + 1
    if token.isdigit():
        return _bencoded_bytes(data, index)[1]
    raise ValueError("unsupported bencoded token")


def torrent_info_hashes(content: bytes) -> list[str]:
    try:
        if not content.startswith(b"d"):
            return []
        cursor = 1
        while cursor < len(content) and content[cursor:cursor + 1] != b"e":
            key, cursor = _bencoded_bytes(content, cursor)
            value_start = cursor
            cursor = _bencoded_value_end(content, cursor)
            if key == b"info":
                encoded_info = content[value_start:cursor]
                return [hashlib.sha1(encoded_info).hexdigest(), hashlib.sha256(encoded_info).hexdigest()]
    except (ValueError, OverflowError):
        return []
    return []


def normalized_mteam_task_metadata(item: dict[str, Any] | None) -> dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    return {
        "source": "mteam",
        "torrent_id": str(source.get("id") or source.get("torrent_id") or "").strip()[:80],
        "subtitle": str(source.get("subtitle") or "").strip()[:500],
        "promotion_label": str(source.get("promotion_label") or "").strip()[:80],
        "resolution": str(source.get("resolution") or "").strip()[:80],
        "codec": str(source.get("codec") or "").strip()[:80],
        "hdr": str(source.get("hdr") or "").strip()[:80],
        "audio_codec": str(source.get("audio_codec") or "").strip()[:80],
        "imdb_rating": str(source.get("imdb_rating") or "").strip()[:40],
        "douban_rating": str(source.get("douban_rating") or "").strip()[:40],
        "created_at": utc_iso(),
    }


def save_qb_task_metadata(
    db: Session,
    downloader_id: str,
    torrent_content: bytes,
    item: dict[str, Any] | None,
) -> list[str]:
    info_hashes = torrent_info_hashes(torrent_content)
    if not info_hashes:
        logger.warning("Unable to calculate torrent info hash for M-Team notification metadata")
        return []
    with _QB_TASK_METADATA_LOCK:
        key = qb_task_metadata_setting_key(downloader_id)
        row = db.query(Setting).filter(Setting.key == key).one_or_none()
        value = dict(row.value) if row and isinstance(row.value, dict) else {}
        tasks = value.get("tasks") if isinstance(value.get("tasks"), dict) else {}
        metadata = normalized_mteam_task_metadata(item)
        for info_hash in info_hashes:
            tasks[info_hash.lower()] = metadata
        tasks = dict(list(tasks.items())[-200:])
        payload = {"tasks": tasks, "updated_at": utc_iso()}
        if row is None:
            db.add(Setting(key=key, value=payload))
        else:
            row.value = payload
            row.updated_at = utc_now_naive()
        db.commit()
    return info_hashes


def get_qb_task_metadata(db: Session, downloader_id: str, task_hash: str) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == qb_task_metadata_setting_key(downloader_id)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    tasks = value.get("tasks") if isinstance(value.get("tasks"), dict) else {}
    metadata = tasks.get(str(task_hash or "").strip().lower())
    return dict(metadata) if isinstance(metadata, dict) else {}


def qb_task_description(metadata: dict[str, Any] | None) -> str:
    source = metadata if isinstance(metadata, dict) and metadata.get("source") == "mteam" else {}
    details: list[str] = []
    subtitle = str(source.get("subtitle") or "").strip()
    if subtitle:
        details.append(subtitle)
    promotion = str(source.get("promotion_label") or "").strip()
    if promotion:
        details.append(f"优惠：{promotion}")
    quality = " · ".join(
        str(source.get(key) or "").strip()
        for key in ("resolution", "codec", "hdr", "audio_codec")
        if str(source.get(key) or "").strip()
    )
    if quality:
        details.append(f"清晰度：{quality}")
    ratings = " / ".join(
        label
        for label in (
            f"IMDb {str(source.get('imdb_rating') or '').strip()}" if source.get("imdb_rating") else "",
            f"豆瓣 {str(source.get('douban_rating') or '').strip()}" if source.get("douban_rating") else "",
        )
        if label
    )
    if ratings:
        details.append(f"评分：{ratings}")
    return "；".join(details) if details else "暂无信息"


def module_display_name(module: str) -> str:
    return {
        "mteam": "M-Team",
        "qb1": "qB1 下载器",
        "qb2": "qB2 下载器",
        "qb3": "qB3 下载器",
        "ai": "影视中枢 Agent",
        "tmdb": "TMDB",
        "wechat_claw": "WeChat claw",
    }.get(module, module)


def module_exception_notification(module: str, failed_hours: int, error: str) -> Notification:
    detail = str(error or "未返回可用数据").strip()[:180]
    return Notification(
        title="模块异常提醒",
        message="\n".join(
            [
                f"模块：{module_display_name(module)}",
                f"持续异常：{failed_hours} 小时",
                f"检查结果：{detail}",
            ]
        ),
        level="error",
        source="module_health",
    )


def _qb_task_completed(task: dict[str, Any]) -> bool:
    state = str(task.get("state") or "").lower()
    try:
        progress = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        progress = 0
    return progress >= 0.999 or bool(task.get("completed_at")) or state in {"uploading", "stalledup", "forcedup", "queuedup"}


def _qb_task_error(task: dict[str, Any]) -> bool:
    state = str(task.get("state") or "").lower()
    return "error" in state or state in {"missingfiles", "unknown"}


def _qb_task_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(task.get("name") or "未命名任务")[:200],
        "size": float(task.get("total_size") or task.get("size") or 0),
        "state": str(task.get("state") or "未知状态")[:80],
        "completed": _qb_task_completed(task),
        "error": _qb_task_error(task),
    }


def _qb_task_notification(
    downloader_id: str,
    event: str,
    task: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> Notification:
    state = _qb_task_state(task)
    downloader = module_display_name(downloader_id)
    if event == "started":
        title, level = f"{downloader}：下载已开始", "info"
        lines = [f"任务：{state['name']}", f"大小：{bytes_label(state['size'])}", f"描述：{qb_task_description(metadata)}"]
    elif event == "completed":
        title, level = f"{downloader}：下载已完成", "success"
        lines = [f"任务：{state['name']}", f"大小：{bytes_label(state['size'])}", f"描述：{qb_task_description(metadata)}"]
    else:
        title, level = "下载任务异常", "error"
        lines = [f"下载器：{downloader}", f"任务：{state['name']}", f"状态：{state['state']}", "检查结果：任务进入异常状态"]
    return Notification(title=title, message="\n".join(lines), level=level, source="qb_task_monitor")


def qb_task_monitoring_response(action: str) -> dict[str, Any]:
    return {
        "action": action,
        "monitoring": True,
        "message": "下载开始、完成和任务异常由 qB 后台轮询识别后主动通知。",
    }


def record_qb_task_transitions(db: Session, downloader_id: str, tasks: list[dict[str, Any]]) -> list[str]:
    """Persist qB task state and send only genuine state transitions after the first baseline."""
    key = qb_task_state_setting_key(downloader_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    previous = dict(row.value) if row and isinstance(row.value, dict) else {}
    previous_tasks = previous.get("tasks") if isinstance(previous.get("tasks"), dict) else {}
    current_tasks: dict[str, dict[str, Any]] = {}
    transitions: list[tuple[str, dict[str, Any]]] = []

    for task in tasks:
        task_hash = str(task.get("hash") or "").strip()
        if not task_hash:
            continue
        current = _qb_task_state(task)
        current_tasks[task_hash] = current
        before = previous_tasks.get(task_hash)
        if not isinstance(before, dict):
            if previous.get("initialized") and not current["completed"] and not current["error"]:
                transitions.append(("started", task))
            continue
        if not bool(before.get("completed")) and current["completed"]:
            transitions.append(("completed", task))
        elif not bool(before.get("error")) and current["error"]:
            transitions.append(("error", task))

    state = {"initialized": True, "updated_at": utc_iso(), "tasks": current_tasks}
    if row is None:
        db.add(Setting(key=key, value=state))
    else:
        row.value = state
        row.updated_at = utc_now_naive()
    db.commit()

    emitted: list[str] = []
    for event, task in transitions:
        task_hash = str(task.get("hash") or "").strip()
        metadata = get_qb_task_metadata(db, downloader_id, task_hash)
        notification = _qb_task_notification(downloader_id, event, task, metadata)
        db.add(notification)
        db.commit()
        db.refresh(notification)
        preference_key = "qb_exception" if event == "error" else f"{downloader_id}_download_{event}"
        try:
            push_wechat_claw_notification(db, notification, preference_key)
        except Exception:
            logger.exception("qB task notification delivery failed: downloader=%s event=%s", downloader_id, event)
        emitted.append(event)
    return emitted


def default_credentials_change_required(db: Session) -> bool:
    setting = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
    return bool(setting and isinstance(setting.value, dict) and setting.value.get("required"))


def serialize_auth_user(db: Session, user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "must_change_credentials": default_credentials_change_required(db),
    }


def is_super_password(db: Session, password: str) -> bool:
    setting = db.query(Setting).filter(Setting.key == SUPER_PASSWORD_SETTING_KEY).one_or_none()
    password_hash = str(setting.value.get("password_hash") or "") if setting and isinstance(setting.value, dict) else ""
    if not password_hash:
        return False
    try:
        return verify_password(password, password_hash)
    except ValueError:
        return False


def get_module_health(db: Session, module: str) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == module_health_setting_key(module)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def record_module_collection_result(db: Session, module: str, success: bool, error: str = "") -> dict[str, Any]:
    """Track one result per UTC hour so short retry bursts do not count as hours of outage."""
    key = module_health_setting_key(module)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    state = dict(row.value) if row and isinstance(row.value, dict) else {}
    now = utc_now_naive()
    hour = now.strftime("%Y-%m-%dT%H")
    if success:
        state.update({"consecutive_failed_hours": 0, "last_failure_hour": "", "last_success_at": utc_iso(now), "last_error": "", "last_checked_at": utc_iso(now)})
    else:
        if state.get("last_failure_hour") != hour:
            state["consecutive_failed_hours"] = int(state.get("consecutive_failed_hours") or 0) + 1
            state["last_failure_hour"] = hour
        state.update({"last_error": str(error)[:300], "last_checked_at": utc_iso(now)})
    if row is None:
        row = Setting(key=key, value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = now
    db.commit()

    failed_hours = int(state.get("consecutive_failed_hours") or 0)
    if success or not failed_hours:
        return state
    preference_key = "qb_exception" if module.startswith("qb") else f"{module}_exception"
    targets = [
        binding for binding in ensure_wechat_claw_bindings(db)
        if binding.enabled
        and normalized_wechat_claw_binding_preferences(binding.notification_preferences).get(preference_key)
        and failed_hours == normalized_wechat_claw_binding_preferences(binding.notification_preferences).get("exception_after_hours")
    ]
    if not targets:
        return state
    notification = module_exception_notification(module, failed_hours, error)
    db.add(notification)
    db.commit()
    db.refresh(notification)
    for binding in targets:
        scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding.id)
        try:
            push_wechat_claw_notification(db, notification, preference_key)
        except Exception:
            logger.exception("module exception notification delivery failed: module=%s binding=%s", module, binding.id)
        finally:
            _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
    return state


def get_wechat_claw_binding_or_error(db: Session, binding_id: int) -> WechatClawBinding:
    binding = db.get(WechatClawBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="WeChat claw 绑定不存在")
    return binding


def ensure_wechat_claw_bindings(db: Session) -> list[WechatClawBinding]:
    bindings = db.query(WechatClawBinding).order_by(WechatClawBinding.id.asc()).all()
    if bindings:
        return bindings
    config = get_decrypted_config(db, "wechat_claw") or {}
    legacy_state = get_wechat_claw_ilink_state(db)
    legacy_prefix = f"{WECHAT_CLAW_ILINK_STATE_KEY}.user."
    legacy_state_rows = db.query(Setting).filter(Setting.key.like(f"{legacy_prefix}%")).all()
    if not config and not legacy_state and not legacy_state_rows:
        return []

    sources: list[int | None] = []
    if legacy_state:
        sources.append(None)
    for row in legacy_state_rows:
        try:
            sources.append(int(str(row.key).removeprefix(legacy_prefix)))
        except ValueError:
            continue
    if not sources:
        sources.append(None)

    migrated: list[tuple[WechatClawBinding, int | None]] = []
    for source_id in dict.fromkeys(sources):
        source_user = db.get(User, source_id) if source_id is not None else None
        display_name = source_user.username if source_user else str(config.get("name") or "微信成员").strip() or "微信成员"
        binding = WechatClawBinding(
            display_name=display_name,
            role_name="微信助手",
            notification_preferences=dict(DEFAULT_WECHAT_CLAW_BINDING_PREFERENCES),
        )
        db.add(binding)
        migrated.append((binding, source_id))
    db.commit()
    for binding, source_id in migrated:
        db.refresh(binding)
        for key in (WECHAT_CLAW_ILINK_STATE_KEY, WECHAT_CLAW_INTERACTIONS_KEY, WECHAT_CLAW_LAST_POLL_KEY):
            source_key = key if source_id is None else f"{key}.user.{source_id}"
            legacy = db.query(Setting).filter(Setting.key == source_key).one_or_none()
            if legacy and isinstance(legacy.value, dict):
                db.add(Setting(key=wechat_claw_setting_key(key, binding.id), value=legacy.value))
    db.commit()
    return [binding for binding, _ in migrated]

WECHAT_CLAW_MOBILE_SECTIONS: list[dict[str, Any]] = [
    {"key": "discover", "label": "发现", "description": "TMDB 趋势、热门、筛选和 M-Team 搜索入口。"},
    {"key": "dashboard", "label": "仪表盘", "description": "M-Team、qB 下载器、NAS 空间总览。"},
    {"key": "downloads", "label": "下载", "description": "qB1/qB2/qB3 下载任务状态与操作入口。"},
    {"key": "notifications", "label": "通知", "description": "成员通知偏好与影视中枢 Agent 对话。"},
    {"key": "settings", "label": "设置", "description": "M-Team、qB、TMDB、影视中枢 Agent、WeChat claw 等运行配置。"},
    {"key": "diagnostics", "label": "诊断", "description": "管理员诊断健康检查与脱敏导出。"},
]

WECHAT_CLAW_CAPABILITIES: list[dict[str, Any]] = [
    {"action": "tmdb_lookup", "label": "TMDB 影视查询", "examples": ["找个高分韩剧看看", "沙丘 2 评分和简介"]},
    {"action": "mteam_search", "label": "M-Team 资源搜索", "examples": ["搜索沙丘 2160p", "推荐一个沙丘资源"]},
    {"action": "dashboard_query", "label": "仪表盘查询", "examples": ["qB1 现在速度多少", "查一下仪表盘状态"]},
    {"action": "download_selected", "label": "手机端添加下载", "examples": ["第一个", "下载推荐的那个", "确认开始下载"]},
    {"action": "general_chat", "label": "影视中枢 Agent 对话", "examples": ["周末适合看什么电影"]},
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
    cutoff = utc_now_naive() - timedelta(hours=5)
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


def set_preload_cache(
    db: Session,
    name: str,
    payload: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == preload_cache_key(name)).one_or_none()
    value = {"payload": payload, "cached_at": utc_iso(), "name": name}
    if context is not None:
        value["context"] = context
    if row is None:
        row = Setting(key=preload_cache_key(name), value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = utc_now_naive()
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


FAVORITE_MEDIA_FIELDS = {
    "id",
    "tmdb_id",
    "media_type",
    "title",
    "original_title",
    "year",
    "release_date",
    "runtime",
    "rating",
    "vote_count",
    "popularity",
    "poster",
    "backdrop",
    "overview",
    "genres",
    "original_language",
    "production_countries",
    "director",
    "cast",
}


def normalize_favorite_media(media: dict[str, Any]) -> tuple[str, int, dict[str, Any]]:
    media_type = str(media.get("media_type") or "").strip().lower()
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=422, detail="收藏条目缺少有效的影片类型。")
    raw_tmdb_id = media.get("tmdb_id")
    if not raw_tmdb_id:
        raw_id = str(media.get("id") or "")
        raw_tmdb_id = raw_id.rsplit("-", 1)[-1] if "-" in raw_id else raw_id
    try:
        tmdb_id = int(raw_tmdb_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="收藏条目缺少有效的 TMDB ID。") from exc
    if tmdb_id <= 0:
        raise HTTPException(status_code=422, detail="收藏条目缺少有效的 TMDB ID。")
    payload = {key: value for key, value in media.items() if key in FAVORITE_MEDIA_FIELDS}
    payload.update(
        {
            "id": f"{media_type}-{tmdb_id}",
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": str(media.get("title") or "未命名").strip()[:240],
        }
    )
    return media_type, tmdb_id, payload


def serialize_media_favorite(favorite: MediaFavorite) -> dict[str, Any]:
    payload = dict(favorite.media_payload or {})
    payload.update(
        {
            "favorite_id": favorite.id,
            "tmdb_id": favorite.tmdb_id,
            "media_type": favorite.media_type,
            "favorited_at": utc_iso(favorite.created_at),
        }
    )
    return rewrite_tmdb_image_urls(payload)


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


def _tmdb_image_network_config(db: Session) -> tuple[bool, str, frozenset[str], int]:
    row = get_config(db, "tmdb")
    if row and not row.enabled:
        config = {"proxy_enabled": False}
    else:
        config = get_decrypted_config(db, "tmdb") if row and row.encrypted_payload else {}
    proxy_enabled, proxy_url, proxy_domains = resolve_tmdb_proxy_settings(config or {})
    timeout = int((config or {}).get("timeout") or 12)
    return proxy_enabled, proxy_url, proxy_domains, timeout


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
    proxy_enabled, proxy_url, proxy_domains = resolve_tmdb_proxy_settings(config or {})
    return tmdb_network_detail(proxy_enabled, proxy_url, proxy_domains)


def tmdb_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "tmdb",
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
    if isinstance(exc, TmdbProxyError):
        messages = {
            "proxy_dns_error": (
                "找不到 NAS Mihomo 容器。",
                "Media Hub 无法解析代理主机名，通常是两个容器尚未加入同一个 Docker 网络，或 Mihomo 缺少固定网络别名。",
                "检查 media-hub-egress 网络，并确认 Mihomo 的网络别名是 tmdb-egress-proxy。",
            ),
            "proxy_tcp_error": (
                "无法连接 NAS Mihomo 代理端口。",
                "代理主机可以找到，但指定端口没有接受连接。",
                "检查 Mihomo 的 mixed-port、监听地址和 Media Hub 中填写的端口。",
            ),
            "proxy_tunnel_error": (
                "NAS Mihomo 无法连接 TMDB。",
                "代理端口可访问，但 Mihomo 没有成功建立到 TMDB 的代理隧道。",
                "在 Mihomo 面板检查当前节点、代理组和节点健康状态。",
            ),
        }
        message, explanation, next_step = messages.get(
            exc.error_type,
            ("NAS Mihomo 代理测试失败。", "代理链路未能完成连接。", "检查 Mihomo 状态后重新测试。"),
        )
        return tmdb_test_result(
            False,
            trace,
            message,
            explanation,
            next_step,
            exc.error_type,
            detail=exc.network_detail or None,
        )
    if isinstance(exc, TmdbConfigError):
        return tmdb_test_result(
            False,
            trace,
            "还没有填写 TMDB 密钥。",
            "请填写 TMDB Bearer Token 后再测试连接。",
            "从 TMDB 账号设置复制 Bearer Token 并保存。",
            "missing_credentials",
        )
    if isinstance(exc, TmdbDohError):
        if exc.error_type == "doh_bad_answer":
            return tmdb_test_result(
                False,
                trace,
                "暂时无法连接 TMDB。",
                "当前网络未能完成连接。",
                "请稍后重试；如持续失败，可在高级设置中启用代理。",
                "doh_bad_answer",
            )
        return tmdb_test_result(
            False,
            trace,
            "暂时无法连接 TMDB。",
            "当前网络未能完成连接。",
            "请稍后重试；如持续失败，可在高级设置中启用代理。",
            "doh_unavailable",
        )
    if isinstance(exc, TmdbImageError):
        return tmdb_test_result(
            False,
            trace,
            "TMDB 已连接，但海报暂时无法加载。",
            "影视信息可正常使用，部分海报可能显示为空。",
            "请稍后重试；如持续发生，可在高级设置中启用代理。",
            "tmdb_image_network_error",
            detail=getattr(exc, "network_detail", None) or tmdb_network_detail_from_config(),
        )
    if isinstance(exc, HTTPError):
        status_code = int(exc.code)
        if status_code == 407:
            return tmdb_test_result(
                False,
                trace,
                "NAS Mihomo 代理认证失败。",
                "Media Hub 已连接到代理端口，但用户名或密码没有通过验证。",
                "检查代理用户名和密码后重新测试；未启用认证时请将两项留空。",
                "proxy_auth_error",
                status_code,
            )
        if status_code in (401, 403):
            return tmdb_test_result(
                False,
                trace,
                "TMDB 密钥验证失败。",
                "请检查 Bearer Token 是否有效或重新复制后再试。",
                "更新 Bearer Token 后重新测试。",
                "invalid_credentials",
                status_code,
            )
        if status_code == 429:
            return tmdb_test_result(False, trace, "TMDB 请求太频繁。", "TMDB 暂时限制了请求频率，密钥本身不一定有问题。", "请等待几分钟后再点击“保存并测试”。", "rate_limited", status_code)
        if status_code >= 500:
            return tmdb_test_result(False, trace, "TMDB 服务暂时不可用。", "TMDB 服务器返回了异常状态，通常需要稍后重试。", "请稍后再测试。如果一直失败，再检查网络或代理设置。", "tmdb_service_error", status_code)
        return tmdb_test_result(False, trace, "TMDB 暂时无法完成请求。", "服务返回了异常响应。", "请稍后重试。", "http_error", status_code)
    if isinstance(exc, (TimeoutError, SocketTimeout)):
        return tmdb_test_result(False, trace, "连接 TMDB 超时。", "当前网络响应较慢。", "请稍后重试；如持续发生，可在高级设置中调整连接方式。", "timeout")
    if isinstance(exc, URLError):
        return tmdb_test_result(False, trace, "无法连接 TMDB。", "当前网络未能完成连接。", "请稍后重试；如持续失败，可在高级设置中调整连接方式。", "network_error")
    return tmdb_test_result(False, trace, "TMDB 测试失败。", "暂时无法完成测试。", "请稍后重试。", "unknown_error")


def classify_tmdb_gateway_test_error(exc: Exception, trace: str, phase: str = "api") -> dict[str, Any]:
    result = classify_tmdb_test_error(exc, trace)
    result["message"] = "当前连接方式已不再支持。"
    result["explanation"] = "请使用 TMDB 的默认连接方式，或在高级设置中配置代理。"
    result["next_step"] = "保存后重新测试。"
    result["error_type"] = "gateway_removed"
    return result


def qb_test_result(success: bool, provider: str, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": provider,
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
            "请填写 WebUI 地址、用户名和密码。",
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
                "用户名或密码可能不正确。",
                "请检查 WebUI 地址、用户名和密码后重试。",
                "invalid_credentials",
                http_status,
                False,
            )
        return qb_test_result(
            False,
            provider,
            trace,
            "qB 连接测试失败。",
            "暂时无法连接下载器。",
            "请检查 WebUI 地址后重新测试。",
            "qb_api_error",
            http_status,
            False,
        )
    return qb_test_result(
        False,
        provider,
        trace,
        "qB 连接测试失败。",
        "暂时无法完成连接测试。",
        "请检查 WebUI 地址、用户名和密码后重试。",
        "unknown_error",
        can_enable=False,
    )


def mteam_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "mteam",
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
        "请检查填写内容后重试；如果问题持续，可前往诊断页查看详细信息。",
        "unknown_error",
    )


def mteam_connection_payload(row: IntegrationConfig | None) -> dict[str, Any]:
    result = public_test_result(row.last_test_result) if row else None
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
        "last_tested_at": utc_iso(row.last_tested_at) if row and row.last_tested_at else None,
        "last_test_result": result,
        "data_source": "M-Team 站点数据",
        "message": message,
    }


def ai_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None, can_enable: bool | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "provider": "ai",
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
            "影视中枢 Agent 尚未配置完成。",
            "请填写 API Key 后再测试。",
            "保存 API Key 并重新测试。",
            "missing_credentials",
            can_enable=False,
        )
    if isinstance(exc, AIServiceError):
        return ai_test_result(
            False,
            trace,
            "影视中枢 Agent 暂时无法连接。",
            "请检查 API Key 是否有效，以及服务是否可用。",
            "请稍后重试。",
            "deepseek_api_error",
            exc.http_status,
            False,
        )
    return ai_test_result(
        False,
        trace,
        "影视中枢 Agent 测试失败。",
        "暂时无法完成测试。",
        "请稍后重试。",
        "unknown_error",
        can_enable=False,
    )


def get_ai_adapter_or_error(db: Session) -> DeepSeekChatAdapter:
    row = get_config(db, "ai")
    if not row or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail="请先在设置中配置影视中枢 Agent。")
    if not row.enabled:
        raise HTTPException(status_code=409, detail="影视中枢 Agent 尚未启用，请先在设置中测试并启用。")
    try:
        return DeepSeekChatAdapter(get_decrypted_config(db, "ai") or {})
    except AIConfigError as exc:
        raise HTTPException(status_code=409, detail="影视中枢 Agent 配置不完整。") from exc


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
        return qb_task_monitoring_response(action)

    if action == "status_query":
        target = str(intent.get("target") or "dashboard")
        downloader_id = str(intent.get("downloader_id") or "all")
        if target == "mteam":
            return {"action": action, "target": target, "mteam": mteam_stats(db, user)}
        if target in {"qb", "downloads"}:
            if downloader_id in {"qb1", "qb2", "qb3"}:
                state = get_qb_state_for_dashboard(db, downloader_id)
                return {"action": action, "target": target, "qbs": [state]}
            dashboard = build_dashboard_payload(db)
            return {
                "action": action,
                "target": target,
                "qbs": dashboard.get("qbs") or [],
                "overview": dashboard.get("overview") or {},
                "updated_at": dashboard.get("updated_at"),
            }
        if target == "notifications":
            return {"action": action, "target": target, **notifications(user, db)}
        return {"action": action, "target": "dashboard", **build_dashboard_payload(db)}

    raise HTTPException(status_code=400, detail="暂不支持这个操作。")


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
        row.updated_at = utc_now_naive()
    db.commit()
    return normalized


def get_wechat_claw_recent_interactions(db: Session, user_id: int | None = None) -> list[dict[str, Any]]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY, user_id)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    items = value.get("items") if isinstance(value, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)][:WECHAT_CLAW_INTERACTION_LIMIT]


def new_wechat_claw_interaction_telemetry() -> dict[str, Any]:
    return {"trace_id": trace_id("WXC"), "started": time.perf_counter(), "stages": []}


def record_wechat_claw_stage(
    telemetry: dict[str, Any],
    stage: str,
    started: float,
    *,
    status: str = "success",
    error: Any = None,
) -> None:
    item: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
    }
    if error:
        item["error"] = trim_wechat_claw_text(redact_payload(str(error)), 240)
    telemetry.setdefault("stages", []).append(item)


def serialize_wechat_claw_interaction_telemetry(telemetry: dict[str, Any] | None) -> dict[str, Any]:
    value = telemetry if isinstance(telemetry, dict) else {}
    started = value.get("started")
    total_duration_ms = max(0, int((time.perf_counter() - started) * 1000)) if isinstance(started, (float, int)) else 0
    stages = value.get("stages") if isinstance(value.get("stages"), list) else []
    return {
        "trace_id": trim_wechat_claw_text(value.get("trace_id"), 80),
        "duration_ms": total_duration_ms,
        "stages": stages[-12:],
    }


def get_wechat_claw_ilink_state(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


def list_wechat_claw_binding_user_ids(db: Session) -> list[int | None]:
    bindings = ensure_wechat_claw_bindings(db)
    ids: list[int] = []
    for binding in bindings:
        state = get_wechat_claw_ilink_state(db, binding.id)
        if binding.enabled and state.get("bot_token"):
            ids.append(binding.id)
    return ids


def save_wechat_claw_ilink_state(db: Session, state: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
    key = wechat_claw_setting_key(WECHAT_CLAW_ILINK_STATE_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=state)
        db.add(row)
    else:
        row.value = state
        row.updated_at = utc_now_naive()
    db.commit()
    return state


def update_wechat_claw_ilink_state(db: Session, user_id: int | None = None, **kwargs: Any) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db, user_id)
    state.update({key: value for key, value in kwargs.items() if value is not None})
    return save_wechat_claw_ilink_state(db, state, user_id)


def get_wechat_claw_last_poll(db: Session, user_id: int | None = None) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)).one_or_none()
    return row.value if row and isinstance(row.value, dict) else {}


AGENT_HIDDEN_RESULT_KEYS = frozenset({
    "api_key", "apikey", "authorization", "bearer_token", "cookie", "password", "passkey",
    "token", "secret", "webhook_secret", "raw_headers", "raw_summary", "download_url",
    "save_path", "content_path", "path", "hash", "tracker", "trackers", "context_token",
    "credential", "credentials", "username", "user_id", "account_id", "email", "phone",
    "base_url", "endpoint", "host", "ip", "session_id",
    "task_hash",
})


def agent_safe_payload(value: Any, depth: int = 0) -> Any:
    """Bound and redact every value before it is persisted in or sent to the model context."""
    if depth > 6:
        return "[content truncated]"
    if isinstance(value, bytes):
        return "[binary content hidden]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in AGENT_HIDDEN_RESULT_KEYS or any(
                word in normalized_key for word in ("password", "passkey", "api_key", "apikey", "authorization", "cookie", "secret", "_token")
            ) or normalized_key.endswith(("_key", "_credential", "_credentials")):
                continue
            result[str(key)] = agent_safe_payload(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [agent_safe_payload(item, depth + 1) for item in list(value)[:20]]
    if isinstance(value, str):
        return redact_ai_user_text(redact_payload(value))[:1800]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(redact_payload(str(value)))[:500]


def assistant_agent_session_key(request: WechatClawMessageRequest) -> str:
    user_key = str(request.user_id or request.conversation_id or "default").strip() or "default"
    channel = "web" if user_key.startswith("web-") else f"wechat-{_WECHAT_CLAW_ACTIVE_USER_ID.get() or 0}"
    conversation_key = str(request.conversation_id or "").strip()
    identity = f"{user_key}:{conversation_key}" if channel == "web" and conversation_key else user_key
    digest = hashlib.sha256(f"{channel}:{identity}".encode("utf-8")).hexdigest()[:24]
    return f"{channel}:{digest}"


def get_assistant_agent_session(db: Session, request: WechatClawMessageRequest) -> dict[str, Any]:
    row = db.query(Setting).filter(Setting.key == ASSISTANT_AGENT_SESSIONS_KEY).one_or_none()
    sessions = row.value if row and isinstance(row.value, dict) else {}
    entry = sessions.get(assistant_agent_session_key(request))
    session = dict(entry) if isinstance(entry, dict) else {}
    updated_at = parse_datetime(session.get("updated_at"))
    if updated_at is None or (utc_now() - updated_at).total_seconds() > ASSISTANT_AGENT_SESSION_TTL_SECONDS:
        return {
            "history": [],
            "recent_results": {},
            "references": {},
            "current_list": {},
            "current_work": {},
            "pending_request": {},
            "pending_confirmation": {},
            "memory_candidate": {},
        }
    session["history"] = session.get("history") if isinstance(session.get("history"), list) else []
    session["recent_results"] = session.get("recent_results") if isinstance(session.get("recent_results"), dict) else {}
    session["references"] = session.get("references") if isinstance(session.get("references"), dict) else {}
    session["current_list"] = session.get("current_list") if isinstance(session.get("current_list"), dict) else {}
    session["current_work"] = session.get("current_work") if isinstance(session.get("current_work"), dict) else {}
    session["pending_request"] = session.get("pending_request") if isinstance(session.get("pending_request"), dict) else {}
    session["pending_confirmation"] = (
        session.get("pending_confirmation") if isinstance(session.get("pending_confirmation"), dict) else {}
    )
    session["memory_candidate"] = session.get("memory_candidate") if isinstance(session.get("memory_candidate"), dict) else {}
    return session


def save_assistant_agent_session(db: Session, request: WechatClawMessageRequest, session: dict[str, Any]) -> None:
    row = db.query(Setting).filter(Setting.key == ASSISTANT_AGENT_SESSIONS_KEY).one_or_none()
    sessions = dict(row.value) if row and isinstance(row.value, dict) else {}
    cutoff = utc_now() - timedelta(seconds=ASSISTANT_AGENT_SESSION_TTL_SECONDS)
    sessions = {
        key: value
        for key, value in sessions.items()
        if isinstance(value, dict) and (parse_datetime(value.get("updated_at")) or cutoff) >= cutoff
    }
    stored = {
        "updated_at": utc_iso(),
        "history": list(session.get("history") or [])[-ASSISTANT_AGENT_HISTORY_LIMIT:],
        "recent_results": dict(session.get("recent_results") or {}),
        "references": dict(session.get("references") or {}),
        "current_list": dict(session.get("current_list") or {}),
        "current_work": dict(session.get("current_work") or {}),
        "pending_request": dict(session.get("pending_request") or {}),
        "pending_confirmation": dict(session.get("pending_confirmation") or {}),
        "memory_candidate": dict(session.get("memory_candidate") or {}),
    }
    sessions[assistant_agent_session_key(request)] = stored
    if len(sessions) > 100:
        sessions = dict(sorted(sessions.items(), key=lambda item: str(item[1].get("updated_at") or ""))[-100:])
    if row is None:
        db.add(Setting(key=ASSISTANT_AGENT_SESSIONS_KEY, value=sessions))
    else:
        row.value = sessions
        row.updated_at = utc_now_naive()
    db.commit()


def remember_assistant_agent_result(session: dict[str, Any], tool: str, result: dict[str, Any], arguments: dict[str, Any]) -> None:
    recent_results = dict(session.get("recent_results") or {})
    references = dict(session.get("references") or {})
    result_key = f"qb_list_torrents:{arguments.get('downloader_id')}" if tool == "qb_list_torrents" else tool
    if tool not in {"tmdb_media_details", "tmdb_person_details", "mteam_result_details", "qb_torrent_details", "prepare_mteam_download", "confirm_mteam_download"}:
        recent_results[result_key] = agent_safe_payload(result)
    if tool == "find_media_with_mteam":
        composite_filters = result.get("filters") if isinstance(result.get("filters"), dict) else {}
        recent_results["mteam_search"] = {
            **agent_safe_payload(result),
            "filters": agent_safe_payload(composite_filters.get("mteam") or {}),
        }
    if tool == "resolve_media_from_clues":
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if result.get("state") in {"resolved", "memory_verified"} and item:
            session["current_work"] = {
                "tmdb_id": str(item.get("tmdb_id") or item.get("id") or ""),
                "media_type": str(item.get("media_type") or ""),
                "title": str(item.get("title") or "")[:160],
                "year": str(item.get("year") or "")[:12],
                "verified_at": utc_iso(),
            }
            session["pending_confirmation"] = {}
            session["memory_candidate"] = {}
        elif result.get("state") == "needs_confirmation" and item:
            session["pending_confirmation"] = {
                "kind": "media_candidate",
                "candidate": agent_safe_payload(item),
                "created_at": utc_iso(),
            }
        elif result.get("state") == "memory_unverified":
            session["memory_candidate"] = {
                "title": str(result.get("title") or "")[:160],
                "year": str(result.get("year") or "")[:12],
                "created_at": utc_iso(),
            }
        elif result.get("state") == "multiple":
            resolution_items = result.get("items") if isinstance(result.get("items"), list) else []
            references["media_resolution"] = [
                {
                    "tmdb_id": str(value.get("tmdb_id") or value.get("id") or ""),
                    "media_type": str(value.get("media_type") or ""),
                    "title": str(value.get("title") or "")[:160],
                    "year": str(value.get("year") or "")[:12],
                }
                for value in resolution_items[:10]
                if isinstance(value, dict)
            ]
            session["current_list"] = {
                "kind": "media_resolution",
                "count": len(resolution_items),
                "updated_at": utc_iso(),
            }
    items = result.get("items") if isinstance(result.get("items"), list) else []
    if tool == "tmdb_lookup":
        references["tmdb_lookup"] = [
            {
                "tmdb_id": str(item.get("tmdb_id") or item.get("id") or ""),
                "media_type": str(item.get("media_type") or ""),
                "title": str(item.get("title") or item.get("name") or "")[:160],
            }
            for item in items[:10] if isinstance(item, dict)
        ]
        session["current_list"] = {"kind": "tmdb", "count": len(items), "updated_at": utc_iso()}
    elif tool == "tmdb_media_details":
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if item:
            session["current_work"] = {
                "tmdb_id": str(item.get("tmdb_id") or item.get("id") or ""),
                "media_type": str(item.get("media_type") or ""),
                "title": str(item.get("title") or "")[:160],
                "year": str(item.get("year") or "")[:12],
                "verified_at": utc_iso(),
            }
    elif tool in {"mteam_search", "find_media_with_mteam"}:
        references["mteam_search"] = {
            "query": str(result.get("query") or ""),
            "recommended_index": int(result.get("recommended_index") or 0),
            "items": [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or "")[:240],
                    "_search_query": str(item.get("_search_query") or "")[:160],
                }
                for item in items[:10] if isinstance(item, dict)
            ],
        }
        session["current_list"] = {"kind": "mteam", "count": len(items), "updated_at": utc_iso()}
    elif tool == "qb_list_torrents":
        downloader_id = str(arguments.get("downloader_id") or "")
        references[result_key] = [
            {"hash": str(item.get("hash") or ""), "name": str(item.get("name") or "")[:240]}
            for item in items[:10] if isinstance(item, dict)
        ]
        session["current_list"] = {"kind": result_key, "count": len(items), "updated_at": utc_iso()}
    session["recent_results"] = recent_results
    session["references"] = references


def append_assistant_agent_turn(session: dict[str, Any], message: str, reply: str) -> None:
    history = list(session.get("history") or [])
    history.extend(
        [
            {"role": "user", "content": redact_ai_user_text(redact_payload(message))[:2000]},
            {"role": "assistant", "content": redact_ai_user_text(redact_payload(reply))[:6000]},
        ]
    )
    session["history"] = history[-ASSISTANT_AGENT_HISTORY_LIMIT:]


def save_wechat_claw_mobile_candidates(
    db: Session,
    request: WechatClawMessageRequest,
    items: list[dict[str, Any]],
    *,
    query: str = "",
    recommended_index: int | None = None,
    presentation: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Persist a short-lived, per-chat list so the next mobile message can select it safely."""
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = str(request.user_id or request.conversation_id or "default").strip() or "default"
    candidates = [
        {
            "id": str(item.get("id") or "").strip(),
            "title": str(item.get("title") or item.get("name") or "资源").strip()[:180],
            "subtitle": str(item.get("subtitle") or "").strip()[:300],
            "resolution": str(item.get("resolution") or "").strip(),
            "size": str(item.get("size") or "").strip(),
            "size_bytes": int(item.get("size_bytes") or 0),
            "seeders": int(item.get("seeders") or 0),
            "promotion_label": str(item.get("promotion_label") or "普通").strip(),
            "promotion_type": str(item.get("promotion_type") or "").strip(),
            "promotion_until": str(item.get("promotion_until") or "").strip(),
            "promotion_remaining": str(item.get("promotion_remaining") or "").strip(),
            "download_factor": mteam_download_factor(item),
            "upload_multiplier": 1.0,
            "is_official": item.get("is_official") is True,
            "codec": str(item.get("codec") or "").strip(),
            "hdr": str(item.get("hdr") or "").strip(),
            "audio_codec": str(item.get("audio_codec") or "").strip(),
            "imdb_rating": str(item.get("imdb_rating") or "").strip(),
            "douban_rating": str(item.get("douban_rating") or "").strip(),
            "group": str(item.get("group") or "").strip(),
            "_search_query": str(item.get("_search_query") or query or "").strip()[:160],
            "presentation": (presentation or {}).get(str(item.get("id") or ""), {}),
        }
        for item in items[:WECHAT_CLAW_MOBILE_SELECTION_LIMIT]
        if str(item.get("id") or "").strip()
    ]
    selections[selection_key] = {
        "created_at": utc_iso(),
        "context_token": str(request.context_token or ""),
        "query": str(query or "").strip()[:160],
        "recommended_index": int(recommended_index or 0),
        "items": candidates,
    }
    update_wechat_claw_ilink_state(db, mobile_selections=selections)
    return {"selection_key": selection_key, "items": candidates, "expires_in_seconds": WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS}


def get_wechat_claw_mobile_search_result(db: Session, request: WechatClawMessageRequest) -> dict[str, Any] | None:
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = wechat_claw_mobile_key(request)
    selection = selections.get(selection_key) if isinstance(selections.get(selection_key), dict) else {}
    created_at = parse_datetime(selection.get("created_at"))
    if created_at is None or (utc_now() - created_at).total_seconds() > WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS:
        if selection:
            selections.pop(selection_key, None)
            update_wechat_claw_ilink_state(db, mobile_selections=selections)
        return None
    items = selection.get("items") if isinstance(selection.get("items"), list) else []
    if not items:
        return None
    return {
        "intent_type": "mteam_search",
        "query": str(selection.get("query") or ""),
        "items": [dict(item) for item in items if isinstance(item, dict)],
        "count": len(items),
        "recommended_index": int(selection.get("recommended_index") or 0) or None,
        "show_all": True,
    }


def get_wechat_claw_mobile_candidate(db: Session, request: WechatClawMessageRequest, index: int) -> dict[str, Any] | None:
    if index < 1 or index > WECHAT_CLAW_MOBILE_SELECTION_LIMIT:
        return None
    state = get_wechat_claw_ilink_state(db)
    selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
    selection_key = str(request.user_id or request.conversation_id or "default").strip() or "default"
    selection = selections.get(selection_key) if isinstance(selections.get(selection_key), dict) else {}
    created_at = parse_datetime(selection.get("created_at"))
    if created_at is None or (utc_now() - created_at).total_seconds() > WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS:
        selections.pop(selection_key, None)
        update_wechat_claw_ilink_state(db, mobile_selections=selections)
        return None
    items = selection.get("items") if isinstance(selection.get("items"), list) else []
    candidate = items[index - 1] if len(items) >= index and isinstance(items[index - 1], dict) else None
    return dict(candidate) if candidate else None


def wechat_claw_mobile_key(request: WechatClawMessageRequest) -> str:
    return str(request.user_id or request.conversation_id or "default").strip() or "default"


def agent_download_lock(operation_id: str) -> Lock:
    with _AGENT_DOWNLOAD_LOCK_GUARD:
        return _AGENT_DOWNLOAD_LOCKS.setdefault(operation_id, Lock())


def latest_agent_download_operation(
    db: Session,
    request: WechatClawMessageRequest,
    *,
    active_only: bool = False,
) -> AgentDownloadOperation | None:
    query = (
        db.query(AgentDownloadOperation)
        .filter(AgentDownloadOperation.session_key == assistant_agent_session_key(request))
        .order_by(AgentDownloadOperation.created_at.desc())
    )
    if active_only:
        query = query.filter(
            AgentDownloadOperation.state.in_(
                ["awaiting_confirmation", "confirming", "pending_verification", "started"]
            )
        )
    row = query.first()
    if row and row.expires_at < utc_now_naive():
        if row.state == "awaiting_confirmation":
            row.state = "expired"
            row.updated_at = utc_now_naive()
            db.commit()
            return None
        if row.state == "started":
            return None
    return row


def save_wechat_claw_pending_download(
    db: Session,
    request: WechatClawMessageRequest,
    candidate: dict[str, Any],
    *,
    query: str = "",
    constraints: dict[str, Any] | None = None,
    downloader_id: str = "",
    actor_user_id: int | None = None,
) -> AgentDownloadOperation:
    previous = latest_agent_download_operation(db, request, active_only=True)
    if previous and previous.state == "awaiting_confirmation":
        previous.state = "replaced"
        previous.updated_at = utc_now_naive()
    operation = AgentDownloadOperation(
        operation_id=trace_id("AGDL"),
        session_key=assistant_agent_session_key(request),
        torrent_id=str(candidate.get("id") or ""),
        actor_user_id=actor_user_id,
        downloader_id=downloader_id or None,
        state="awaiting_confirmation",
        candidate=agent_safe_payload(candidate),
        constraints={
            "query": query,
            "filters": agent_safe_payload(constraints or {}),
            "origin": {
                "mobile_key": wechat_claw_mobile_key(request),
                "binding_user_id": _WECHAT_CLAW_ACTIVE_USER_ID.get(),
                "is_web": str(request.user_id or "").startswith("web-"),
            },
        },
        result={},
        expires_at=utc_now_naive() + timedelta(seconds=AGENT_DOWNLOAD_CONFIRM_TTL_SECONDS),
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    pending[wechat_claw_mobile_key(request)] = {
        "created_at": utc_iso(),
        "candidate": candidate,
        "operation_id": operation.operation_id,
    }
    update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)
    return operation


def get_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest) -> dict[str, Any] | None:
    operation = latest_agent_download_operation(db, request, active_only=True)
    if operation and operation.state in {"awaiting_confirmation", "confirming", "pending_verification", "started"}:
        return {**dict(operation.candidate or {}), "_operation_id": operation.operation_id}
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    item = pending.get(wechat_claw_mobile_key(request)) if isinstance(pending.get(wechat_claw_mobile_key(request)), dict) else {}
    created_at = parse_datetime(item.get("created_at"))
    fresh = bool(created_at and (utc_now() - created_at).total_seconds() <= WECHAT_CLAW_MOBILE_SELECTION_TTL_SECONDS)
    candidate = item.get("candidate") if isinstance(item.get("candidate"), dict) else None
    if fresh and candidate:
        return dict(candidate)
    if item:
        pending.pop(wechat_claw_mobile_key(request), None)
        update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)
    return None


def clear_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest) -> None:
    state = get_wechat_claw_ilink_state(db)
    pending = state.get("mobile_pending_downloads") if isinstance(state.get("mobile_pending_downloads"), dict) else {}
    pending.pop(wechat_claw_mobile_key(request), None)
    update_wechat_claw_ilink_state(db, mobile_pending_downloads=pending)


def cancel_wechat_claw_pending_download(db: Session, request: WechatClawMessageRequest) -> bool:
    operation = latest_agent_download_operation(db, request, active_only=True)
    if operation is None or operation.state != "awaiting_confirmation":
        clear_wechat_claw_pending_download(db, request)
        return False
    operation.state = "cancelled"
    operation.updated_at = utc_now_naive()
    db.commit()
    clear_wechat_claw_pending_download(db, request)
    return True


def grant_wechat_claw_privacy_access(db: Session, request: WechatClawMessageRequest) -> None:
    state = get_wechat_claw_ilink_state(db)
    grants = state.get("mobile_privacy_grants") if isinstance(state.get("mobile_privacy_grants"), dict) else {}
    grants[wechat_claw_mobile_key(request)] = {"expires_at": utc_iso(utc_now() + timedelta(seconds=WECHAT_CLAW_PRIVACY_GRANT_SECONDS))}
    update_wechat_claw_ilink_state(db, mobile_privacy_grants=grants)


def has_wechat_claw_privacy_access(db: Session, request: WechatClawMessageRequest) -> bool:
    state = get_wechat_claw_ilink_state(db)
    grants = state.get("mobile_privacy_grants") if isinstance(state.get("mobile_privacy_grants"), dict) else {}
    entry = grants.get(wechat_claw_mobile_key(request)) if isinstance(grants.get(wechat_claw_mobile_key(request)), dict) else {}
    expires_at = parse_datetime(entry.get("expires_at"))
    return bool(expires_at and expires_at > utc_now())


def redact_wechat_claw_message(message: str) -> str:
    return "[管理员密码验证已脱敏]" if WECHAT_CLAW_ADMIN_VERIFY_RE.match(message or "") else message


def configured_default_downloader_id(db: Session) -> str:
    row = db.query(Setting).filter(Setting.key == DEFAULT_DOWNLOADER_SETTING_KEY).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    configured = str(value.get("downloader_id") or "").strip().lower()
    if configured in DOWNLOADER_IDS:
        return configured
    legacy_config = get_decrypted_config(db, "wechat_claw") or {}
    legacy = str(legacy_config.get("default_downloader_id") or "").strip().lower()
    return legacy if legacy in DOWNLOADER_IDS else ""


def default_downloader_status(db: Session) -> dict[str, Any]:
    downloader_id = configured_default_downloader_id(db)
    source = "configured" if downloader_id else "automatic"
    if not downloader_id:
        for candidate in DOWNLOADER_IDS:
            row = get_config(db, candidate)
            if row and row.enabled and row.encrypted_payload:
                downloader_id = candidate
                break
    row = get_config(db, downloader_id) if downloader_id else None
    ready = bool(row and row.enabled and row.encrypted_payload)
    return {"downloader_id": downloader_id or None, "ready": ready, "source": source}


def resolve_default_downloader(db: Session) -> str:
    status = default_downloader_status(db)
    downloader_id = str(status.get("downloader_id") or "")
    if not downloader_id:
        raise HTTPException(status_code=409, detail="\u8bf7\u5148\u5728\u4e0b\u8f7d\u5668\u914d\u7f6e\u4e2d\u6d4b\u8bd5\u5e76\u542f\u7528\u81f3\u5c11\u4e00\u4e2a qB \u4e0b\u8f7d\u5668\u3002")
    if not status.get("ready"):
        raise HTTPException(status_code=409, detail=f"\u9ed8\u8ba4\u4e0b\u8f7d\u5668 {downloader_label(downloader_id)} \u5f53\u524d\u672a\u542f\u7528\uff0c\u8bf7\u542f\u7528\u5b83\u6216\u9009\u62e9\u5176\u4ed6\u9ed8\u8ba4\u4e0b\u8f7d\u5668\u3002")
    return downloader_id


def resolve_agent_downloader(db: Session, requested: str | None = None) -> str:
    downloader_id = str(requested or "").strip().lower()
    if not downloader_id:
        return resolve_default_downloader(db)
    if downloader_id not in DOWNLOADER_IDS:
        raise HTTPException(status_code=400, detail="下载器必须是 qB1、qB2 或 qB3。")
    row = get_config(db, downloader_id)
    if not row or not row.enabled or not row.encrypted_payload:
        raise HTTPException(status_code=409, detail=f"{downloader_label(downloader_id)} 当前不可用，请检查配置。")
    return downloader_id


def refresh_agent_download_candidate(
    db: Session,
    operation: AgentDownloadOperation,
) -> tuple[dict[str, Any] | None, str | None]:
    constraints = dict(operation.constraints or {})
    query = str(constraints.get("query") or "").strip()
    candidate = dict(operation.candidate or {})
    if not query:
        return candidate, None
    items = search_mteam_with_budget(get_mteam_adapter_or_error(db), query)
    fresh = next((item for item in items if str(item.get("id") or "") == operation.torrent_id), None)
    if fresh is None:
        return None, "unavailable"
    filters = constraints.get("filters") if isinstance(constraints.get("filters"), dict) else {}
    matched, _ = rank_mteam_search_items([fresh], filters)
    if not matched:
        return fresh, "conditions_changed"
    original_presentation = (
        candidate.get("presentation") if isinstance(candidate.get("presentation"), dict) else {}
    )
    fresh["presentation"] = {
        **(fresh.get("presentation") if isinstance(fresh.get("presentation"), dict) else {}),
        **original_presentation,
    }
    fresh["_search_query"] = query
    return fresh, None


def qb_download_state_started(item: dict[str, Any]) -> bool:
    state = str(item.get("state") or "").strip().lower()
    if any(token in state for token in ("error", "missing", "paused", "stopped")):
        return False
    if float(item.get("progress") or 0) >= 1:
        return True
    return any(token in state for token in ("download", "dl", "meta", "queued", "stalled", "allocat", "check"))


def qb_torrents_by_hash(adapter: Any, downloader_id: str, task_hashes: list[str]) -> list[dict[str, Any]]:
    if not task_hashes:
        return []
    try:
        return adapter.get_torrents(downloader_id, {"hashes": "|".join(task_hashes)})
    except TypeError:
        items = adapter.get_torrents(downloader_id)
        hashes = {value.lower() for value in task_hashes}
        return [item for item in items if str(item.get("hash") or "").lower() in hashes]


def verify_agent_download_started(
    adapter: Any,
    downloader_id: str,
    task_hashes: list[str],
    *,
    timeout_seconds: float = AGENT_DOWNLOAD_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    if not task_hashes or not hasattr(adapter, "get_torrents"):
        return None
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            items = qb_torrents_by_hash(adapter, downloader_id, task_hashes)
        except (QbittorrentApiError, AttributeError):
            items = []
        started = next((item for item in items if qb_download_state_started(item)), None)
        if started is not None:
            return started
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(AGENT_DOWNLOAD_VERIFY_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))


def save_default_downloader(db: Session, downloader_id: str) -> dict[str, Any]:
    normalized = str(downloader_id or "").strip().lower()
    if normalized not in DOWNLOADER_IDS:
        raise HTTPException(status_code=400, detail="\u9ed8\u8ba4\u4e0b\u8f7d\u5668\u5fc5\u987b\u662f qB1\u3001qB2 \u6216 qB3\u3002")
    provider = get_config(db, normalized)
    if not provider or not provider.enabled or not provider.encrypted_payload:
        raise HTTPException(status_code=409, detail=f"\u8bf7\u5148\u6d4b\u8bd5\u5e76\u542f\u7528 {downloader_label(normalized)}\uff0c\u518d\u5c06\u5b83\u8bbe\u4e3a\u9ed8\u8ba4\u4e0b\u8f7d\u5668\u3002")
    row = db.query(Setting).filter(Setting.key == DEFAULT_DOWNLOADER_SETTING_KEY).one_or_none()
    value = {"downloader_id": normalized, "updated_at": utc_iso()}
    if row is None:
        db.add(Setting(key=DEFAULT_DOWNLOADER_SETTING_KEY, value=value))
    else:
        row.value = value
        row.updated_at = utc_now_naive()
    db.commit()
    return default_downloader_status(db)


def download_wechat_claw_selected_torrent(
    db: Session,
    torrent_id: str,
    actor: User,
    candidate: dict[str, Any] | None = None,
    operation: AgentDownloadOperation | None = None,
) -> dict[str, Any]:
    if operation and operation.state == "started":
        return dict(operation.result or {})
    downloader_id = resolve_agent_downloader(db, operation.downloader_id if operation else None)
    if operation:
        operation.state = "confirming"
        operation.downloader_id = downloader_id
        operation.confirmed_at = operation.confirmed_at or utc_now_naive()
        operation.updated_at = utc_now_naive()
        db.commit()
    stage = "mteam_download_torrent"
    try:
        torrent_file = get_mteam_adapter_or_error(db).download_torrent_file(torrent_id)
        task_hashes = save_qb_task_metadata(
            db,
            downloader_id,
            torrent_file["content"],
            candidate or {"id": torrent_id},
        )
        stage = "qb_add_torrent_file"
        qb_adapter = get_qb_adapter_or_error(db, downloader_id)
        existing_tasks = (
            qb_torrents_by_hash(qb_adapter, downloader_id, task_hashes)
            if hasattr(qb_adapter, "get_torrents") else []
        )
        if existing_tasks:
            result = {
                "accepted": True,
                "trace_id": operation.operation_id if operation else trace_id("DL"),
                "already_present": True,
            }
        else:
            result = qb_adapter.add_torrent_file(
                downloader_id,
                torrent_file["filename"],
                torrent_file["content"],
                {},
            )
    except MTeamApiError as exc:
        if operation:
            operation.state = "failed"
            operation.result = {"state": "failed", "stage": stage, "message": "M-Team 没有返回可用的种子文件。"}
            operation.updated_at = utc_now_naive()
            db.commit()
        raise HTTPException(status_code=502, detail=f"下载种子失败：{exc}") from exc
    except QbittorrentApiError as exc:
        if operation:
            operation.state = "failed"
            operation.result = {"state": "failed", "stage": stage, "message": f"提交到 {downloader_id} 失败。"}
            operation.updated_at = utc_now_naive()
            db.commit()
        raise HTTPException(status_code=502, detail=f"推送到 {downloader_id} 失败：{exc}") from exc
    task_hash = task_hashes[0] if task_hashes else torrent_id
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add_mteam_mobile", target_hash=task_hash, actor_user_id=actor.id, status="accepted"))
    started_task = verify_agent_download_started(qb_adapter, downloader_id, task_hashes)
    operation_state = "started" if started_task else "pending_verification"
    operation_result = {
        **result,
        "state": operation_state,
        "torrent_id": torrent_id,
        "task_hash": task_hash,
        "filename": torrent_file["filename"],
        "downloader_id": downloader_id,
        "candidate": agent_safe_payload(candidate or {"id": torrent_id}),
        "task_state": str((started_task or {}).get("state") or ""),
        "verified_at": utc_iso() if started_task else None,
    }
    if operation:
        operation.state = operation_state
        operation.candidate = agent_safe_payload(candidate or {"id": torrent_id})
        # The hash is retained only in this internal operation record so the
        # background verifier can identify the exact qB task. API/model payloads
        # still pass through agent_safe_payload(), which removes it.
        operation.result = operation_result
        operation.expires_at = utc_now_naive() + timedelta(
            minutes=5 if operation_state == "started" else 24 * 60
        )
        operation.updated_at = utc_now_naive()
    db.commit()
    return operation_result


def notify_agent_download_started(db: Session, operation: AgentDownloadOperation) -> None:
    result = dict(operation.result or {})
    if result.get("notified_at"):
        return
    candidate = dict(operation.candidate or {})
    message = format_mobile_agent_reply(
        {"intent_type": "download_selected"},
        {"state": "started", "candidate": candidate},
    )
    db.add(Notification(
        title=f"{candidate.get('title') or '下载任务'}已开始下载"[:200],
        message=message,
        level="success",
        source="agent",
    ))
    origin = (operation.constraints or {}).get("origin")
    origin = origin if isinstance(origin, dict) else {}
    if not origin.get("is_web"):
        binding_user_id = origin.get("binding_user_id")
        state = get_wechat_claw_ilink_state(db, binding_user_id)
        selections = state.get("mobile_selections") if isinstance(state.get("mobile_selections"), dict) else {}
        mobile_key = str(origin.get("mobile_key") or "")
        selection = selections.get(mobile_key) if isinstance(selections.get(mobile_key), dict) else {}
        context_token = str(selection.get("context_token") or "")
        if mobile_key and context_token:
            try:
                config = get_decrypted_config(db, "wechat_claw") or {}
                WechatClawAdapter(wechat_claw_config_with_state(config, state)).send_text(
                    mobile_key, message, context_token
                )
            except Exception:
                # The verified state and in-app notification are authoritative;
                # a transient push failure must not revert the operation.
                pass
    operation.result = {**result, "notified_at": utc_iso()}
    operation.updated_at = utc_now_naive()
    db.commit()


def reconcile_agent_download_operations(db: Session) -> int:
    operations = (
        db.query(AgentDownloadOperation)
        .filter(AgentDownloadOperation.state == "pending_verification")
        .order_by(AgentDownloadOperation.confirmed_at.asc())
        .limit(50)
        .all()
    )
    started_count = 0
    for operation in operations:
        confirmed_at = operation.confirmed_at or operation.updated_at or operation.created_at
        if confirmed_at and (utc_now_naive() - confirmed_at).total_seconds() > 24 * 60 * 60:
            operation.state = "verification_timeout"
            operation.result = {**dict(operation.result or {}), "state": "verification_timeout"}
            operation.updated_at = utc_now_naive()
            db.commit()
            continue
        result = dict(operation.result or {})
        task_hash = str(result.get("task_hash") or "")
        downloader_id = str(operation.downloader_id or "")
        if not task_hash or downloader_id not in DOWNLOADER_IDS:
            continue
        try:
            task = verify_agent_download_started(
                get_qb_adapter_or_error(db, downloader_id),
                downloader_id,
                [task_hash],
                timeout_seconds=0,
            )
        except (HTTPException, QbittorrentApiError):
            continue
        if task is None:
            continue
        operation.state = "started"
        operation.result = {
            **result,
            "state": "started",
            "task_state": str(task.get("state") or ""),
            "verified_at": utc_iso(),
        }
        operation.updated_at = utc_now_naive()
        operation.expires_at = utc_now_naive() + timedelta(minutes=5)
        db.commit()
        notify_agent_download_started(db, operation)
        started_count += 1
    return started_count


def mteam_download_factor(item: dict[str, Any]) -> float:
    if item.get("download_factor") not in (None, ""):
        return max(0.0, min(1.0, float(item.get("download_factor"))))
    promotion_type = str(item.get("promotion_type") or "").strip().lower()
    if promotion_type in {"free", "percent_0"}:
        return 0.0
    if promotion_type in {"30%", "percent_30"}:
        return 0.3
    if promotion_type in {"50%", "half", "percent_50"}:
        return 0.5
    return 1.0


def rank_mteam_search_items(items: list[dict[str, Any]], filters: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    resolution = str(filters.get("resolution") or "").lower()
    resolutions_source = filters.get("resolutions") if isinstance(filters.get("resolutions"), list) else []
    resolutions = {str(value).strip().lower() for value in resolutions_source if str(value).strip()}
    if resolution:
        resolutions.add(resolution)
    promotion = str(filters.get("promotion") or "any").lower()
    # M-Team search always exposes a ranked recommendation. The model may omit
    # this optional preference, but it must not make the recommendation vanish.
    recommend = True
    min_size = max(0.0, float(filters.get("min_size_gb") or 0))
    max_size = max(0.0, float(filters.get("max_size_gb") or 0))
    min_seeders = max(0, int(float(filters.get("min_seeders") or 0)))
    factor_value = filters.get("download_factor_max")
    download_factor_max = None if factor_value in (None, "") else max(0.0, min(1.0, float(factor_value)))
    official_only = bool(filters.get("official_only"))
    chinese_filter = filters.get("has_chinese_subtitles")
    promotion_minutes = max(0, int(float(filters.get("promotion_expires_after_minutes") or 0)))
    filtered = list(items)
    if resolutions:
        filtered = [
            item for item in filtered
            if any(
                requested in str(item.get("resolution") or "").lower()
                or requested in " ".join(item.get("labels") or []).lower()
                for requested in resolutions
            )
        ]
    if promotion in {"free", "discount"}:
        filtered = [
            item for item in filtered
            if (promotion == "free" and mteam_download_factor(item) == 0)
            or (promotion == "discount" and mteam_download_factor(item) <= 0.5)
        ]
    if download_factor_max is not None:
        filtered = [item for item in filtered if mteam_download_factor(item) <= download_factor_max]
    if min_size or max_size:
        matched: list[dict[str, Any]] = []
        for item in filtered:
            size_gb = float(item.get("size_bytes") or 0) / (1024 ** 3)
            if (not min_size or size_gb >= min_size) and (not max_size or size_gb <= max_size):
                matched.append(item)
        filtered = matched
    if min_seeders:
        filtered = [item for item in filtered if int(float(item.get("seeders") or 0)) >= min_seeders]
    if official_only:
        filtered = [item for item in filtered if item.get("is_official") is True]
    if isinstance(chinese_filter, bool):
        filtered = [item for item in filtered if mteam_has_chinese_subtitles(item) is chinese_filter]
    if promotion_minutes:
        minimum_until = utc_now() + timedelta(minutes=promotion_minutes)
        filtered = [
            item for item in filtered
            if (parse_datetime(item.get("promotion_until")) or utc_now()) >= minimum_until
        ]

    def recommendation_score(item: dict[str, Any]) -> tuple[float, ...]:
        promo_rank = 1.0 - mteam_download_factor(item)
        resolution_text = f"{item.get('resolution') or ''} {' '.join(item.get('labels') or [])}".lower()
        four_k = 1 if any(value in resolution_text for value in ("2160", "4k", "uhd")) else 0
        size_gb = float(item.get("size_bytes") or 0) / (1024 ** 3)
        in_size_range = 1 if 15 <= size_gb <= 60 else 0
        size_distance = -abs(size_gb - 35) if size_gb else -9999
        codec_text = f"{item.get('codec') or ''} {item.get('title') or ''}".lower()
        codec_rank = 1 if any(value in codec_text for value in ("x265", "h.265", "h265", "hevc")) else 0
        # Codec is deliberately a late tie-breaker: HEVC saves space, but never
        # outweighs an explicit quality preference, promotion, or swarm health.
        return (promo_rank, four_k, float(item.get("seeders") or 0), in_size_range, size_distance, codec_rank)

    if recommend:
        filtered.sort(key=recommendation_score, reverse=True)
    else:
        filtered.sort(key=lambda item: float(item.get("seeders") or 0), reverse=True)
    return filtered[:10], 1 if recommend and filtered else None


def mteam_default_recommendation(items: list[dict[str, Any]], recommended_index: int | None) -> str:
    if not recommended_index or recommended_index < 1 or recommended_index > len(items):
        return ""
    item = items[recommended_index - 1]
    try:
        seeders = int(float(item.get("seeders") or 0))
    except (TypeError, ValueError):
        seeders = 0
    facts = [
        str(item.get("promotion_label") or "普通"),
        str(item.get("resolution") or "-"),
        f"{seeders} 人做种",
        str(item.get("size") or "-"),
        str(item.get("codec") or "-") + " 编码",
    ]
    return f"推荐第 {recommended_index} 个：{'，'.join(facts)}，综合优惠、清晰度、体积与做种情况排序最佳。"


MTEAM_COUNTRY_TOKENS = ("中国大陆", "中国香港", "中国台湾", "中国", "美国", "英国", "日本", "韩国", "法国", "德国", "印度", "泰国", "加拿大", "澳大利亚", "西班牙", "意大利", "俄罗斯")
MTEAM_GENRE_TOKENS = ("剧情", "喜剧", "动作", "爱情", "科幻", "恐怖", "惊悚", "悬疑", "犯罪", "动画", "奇幻", "冒险", "纪录", "战争", "音乐", "家庭", "历史", "同性")


def _mteam_source_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "subtitle"))


def _mteam_compact_work(item: dict[str, Any]) -> str:
    subtitle = str(item.get("subtitle") or "").strip()
    first = re.split(r"[|¦]", subtitle, maxsplit=1)[0].strip() if subtitle else ""
    aliases = [part.strip() for part in re.split(r"\s*/\s*", first) if part.strip()]
    if aliases:
        return " / ".join(aliases[:2])
    return mteam_markdown_cell(item.get("title") or item.get("name"), 180)


def _mteam_rule_metadata(item: dict[str, Any]) -> dict[str, str]:
    source = _mteam_source_text(item)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", source)
    country = next((token for token in MTEAM_COUNTRY_TOKENS if token in source), "")
    genres = [token for token in MTEAM_GENRE_TOKENS if token in source][:2]
    locator = " · ".join(part for part in (year_match.group(0) if year_match else "", country, "/".join(genres)) if part)
    version_parts = [str(item.get("resolution") or "").strip(), str(item.get("codec") or "").strip(), str(item.get("hdr") or "").strip(), str(item.get("group") or "").strip()]
    return {"work": _mteam_compact_work(item), "locator": locator or "-", "version": " · ".join(part for part in version_parts if part) or "-"}


def mteam_has_chinese_subtitles(item: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("subtitle") or ""),
            " ".join(str(label) for label in (item.get("labels") or [])),
        ]
    ).lower()
    return any(token in text for token in ("中字", "简中", "繁中", "中文", "chs", "cht", "chinese"))


def _mteam_fallback_row(item: dict[str, Any], index: int) -> dict[str, str]:
    compact = _mteam_rule_metadata(item)
    source = _mteam_source_text(item)
    year_match = re.search(r"\b(?:19|20)\d{2}\b", source)
    year = year_match.group(0) if year_match else "未知年份"
    chinese_info = compact["locator"] if compact["locator"] != "-" else "中文信息未标注"
    chinese_info = f"{chinese_info} · {'含中字' if mteam_has_chinese_subtitles(item) else '未标注中字'}"
    return {
        "index": str(index),
        "title": f"{compact['work']} {year}",
        "chinese_info": chinese_info,
        "quality": compact["version"],
        "size": str(item.get("size") or "-"),
        "seeders": str(int(item.get("seeders") or 0)),
        "promotion": str(item.get("promotion_label") or "普通"),
    }


def mteam_model_rows(items: list[dict[str, Any]], rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, str]]:
    model_rows = {int(row.get("index") or 0): row for row in (rows or []) if isinstance(row, dict)}
    output: dict[str, dict[str, str]] = {}
    required = ("title", "chinese_info", "quality", "size", "seeders", "promotion")
    for index, item in enumerate(items, 1):
        fallback = _mteam_fallback_row(item, index)
        candidate = model_rows.get(index) or {}
        valid = all(str(candidate.get(field) or "").strip() for field in required)
        title = str(candidate.get("title") or "").strip()
        chinese_info = str(candidate.get("chinese_info") or "").strip()
        quality = str(candidate.get("quality") or "").strip()
        if not re.search(r"\b(?:19|20)\d{2}\b", title):
            valid = False
        expected_subtitle_state = "含中字" if mteam_has_chinese_subtitles(item) else "未标注中字"
        if expected_subtitle_state not in chinese_info:
            valid = False
        if str(candidate.get("size") or "").strip() != fallback["size"]:
            valid = False
        if str(candidate.get("seeders") or "").strip() != fallback["seeders"]:
            valid = False
        if str(candidate.get("promotion") or "").strip() != fallback["promotion"]:
            valid = False
        if str(item.get("resolution") or "") and str(item.get("resolution") or "") not in quality:
            valid = False
        output[str(item.get("id") or "")] = ({
            "title": title,
            "chinese_info": chinese_info,
            "quality": quality,
            "size": fallback["size"],
            "seeders": fallback["seeders"],
            "promotion": fallback["promotion"],
        } if valid else fallback)
    return output


def enrich_mteam_recommendation(ai_adapter: DeepSeekChatAdapter, result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") if isinstance(result.get("items"), list) else []
    recommended_index = result.get("recommended_index")
    enriched = dict(result)
    enriched["recommendation_note"] = mteam_default_recommendation(items, recommended_index)
    enriched_items = [dict(item, has_chinese_subtitles=mteam_has_chinese_subtitles(item)) for item in items]
    enriched["items"] = enriched_items
    enriched["presentation"] = mteam_model_rows(enriched_items)
    if not enriched["recommendation_note"]:
        return enriched
    try:
        ai_presentation = ai_adapter.describe_mteam_presentation(
            str(result.get("query") or ""),
            enriched_items,
            int(recommended_index or 0),
        )
        enriched["recommendation_note"] = ai_presentation["recommendation"]
        enriched["presentation"] = mteam_model_rows(enriched_items, ai_presentation.get("rows"))
    except (AIServiceError, ValueError, TypeError):
        # Ranking remains deterministic even when the optional wording pass fails.
        pass
    return enriched


def mteam_markdown_cell(value: Any, limit: int = 72) -> str:
    text = " ".join(str(value or "-").replace("|", "¦").replace("\n", " ").split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


TMDB_TV_REQUEST_HINTS = ("动画", "動漫", "动漫", "番剧", "番劇", "剧集", "劇集", "电视剧", "電視劇", "第1季", "第一季", "第1集", "第一集")
TMDB_MOVIE_REQUEST_HINTS = ("电影", "電影", "影片")


def tmdb_filters_for_request(intent: dict[str, Any], message: str) -> dict[str, Any]:
    """Keep AI title lookup compatible with direct TMDB search when the model guesses filters."""
    filters = dict(intent.get("tmdb_filters") or {})
    text = str(message or "").lower()
    query = str(intent.get("query") or "").strip()
    if any(hint.lower() in text for hint in TMDB_TV_REQUEST_HINTS):
        filters["media_type"] = "tv"
    elif any(hint.lower() in text for hint in TMDB_MOVIE_REQUEST_HINTS):
        filters["media_type"] = "movie"
    elif query and str(filters.get("media_type") or "all").lower() in {"movie", "tv"}:
        # An unqualified title should search both types, exactly like Discover.
        filters["media_type"] = "all"
    if query:
        # The language used to write a localized title is not the work's
        # original language, and its translated name does not imply a production
        # country. Keeping AI-guessed values here can discard an exact TMDB hit.
        filters["language"] = ""
        filters["region"] = ""
    return filters


def execute_mobile_agent_intent(db: Session, intent: dict[str, Any], request: WechatClawMessageRequest, user: User) -> dict[str, Any]:
    intent_type = str(intent.get("intent_type") or "general_chat")
    if intent_type == "tmdb_lookup":
        row = get_config(db, "tmdb")
        if not row or not row.enabled:
            raise HTTPException(status_code=409, detail="请先在设置中启用 TMDB。")
        lookup_filters = tmdb_filters_for_request(intent, request.message)
        try:
            items = TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).lookup_media(str(intent.get("query") or ""), lookup_filters)
        except Exception as exc:
            # A remote lookup error is not a "no results" outcome. Return a
            # structured failure so the mobile template can tell the user exactly
            # which provider failed while the interaction diagnostics retain detail.
            return {
                "intent_type": intent_type,
                "state": "failed",
                "query": intent.get("query"),
                "filters": lookup_filters,
                "error": f"TMDB 查询失败：{exc}",
                "items": [],
                "count": 0,
            }
        return {"intent_type": intent_type, "query": intent.get("query"), "filters": lookup_filters, "items": items, "count": len(items)}
    if intent_type == "mteam_search":
        query = str(intent.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="M-Team 搜索需要影片或资源关键词。")
        try:
            adapter = get_mteam_adapter_or_error(db)
            raw_items = search_mteam_with_budget(adapter, query)[:100]
        except MTeamApiError as exc:
            raise HTTPException(status_code=502, detail=f"M-Team 搜索失败：{exc}") from exc
        items, recommended_index = rank_mteam_search_items(raw_items, intent.get("mteam_filters") or {})
        search_meta = dict(getattr(adapter, "last_search_meta", {}) or {})
        return {
            "intent_type": intent_type,
            "query": query,
            "filters": intent.get("mteam_filters"),
            "items": items,
            "count": len(items),
            "recommended_index": recommended_index,
            "scanned_count": int(search_meta.get("scanned_count") or len(raw_items)),
            "pages_scanned": int(search_meta.get("pages_scanned") or 1),
            "complete": bool(search_meta.get("complete", True)),
            "budget_reached": bool(search_meta.get("budget_reached")),
            "source": "M-Team API",
            "queried_at": utc_iso(),
        }
    if intent_type == "dashboard_query":
        sections = intent.get("dashboard_sections") or []
        if sections == ["overview"] and MTEAM_SITE_DATA_RE.search(request.message or ""):
            sections = ["mteam"]
        return build_mobile_dashboard_result(db, request, sections)
    raise HTTPException(status_code=400, detail="不支持的手机端工具意图。")


def build_mobile_dashboard_result(
    db: Session, request: WechatClawMessageRequest, sections: list[str], qb2_authorized: bool | None = None
) -> dict[str, Any]:
    selected = list(dict.fromkeys(section for section in sections if section)) or ["overview"]
    result: dict[str, Any] = {"intent_type": "dashboard_query", "sections": selected}
    dashboard = None
    qb2_allowed = agent_has_qb2_access(db, request, qb2_authorized)
    if any(section in {"overview", "mteam", "nas", "qb1", "qb2", "qb3", "downloads"} for section in selected):
        dashboard = build_dashboard_payload(db)
    dashboard_qbs = list((dashboard or {}).get("qbs") or [])
    if dashboard and "overview" in selected:
        result["overview"] = dashboard.get("overview") or {}
        result["mteam"] = dashboard.get("mteam") or {}
        result["qbs"] = dashboard_qbs
        result["updated_at"] = dashboard.get("updated_at")
    elif dashboard and "mteam" in selected:
        historical = dict(dashboard.get("mteam") or {})
        try:
            live_mteam = collect_mteam_snapshot(db)
            db.commit()
            apply_mteam_five_hour_deltas(db, live_mteam)
            live_mteam["traffic_series"] = build_mteam_traffic_series(db)
            live_mteam["traffic_history"] = live_mteam["traffic_series"]["day"]
            live_mteam["live_query"] = True
            result["mteam"] = live_mteam
            result["updated_at"] = live_mteam.get("captured_at")
        except Exception as exc:
            db.rollback()
            historical["stale"] = True
            historical["live_query"] = False
            historical["live_error"] = live_diagnostic_error(exc)
            historical["historical_age_seconds"] = max(
                0,
                int((utc_now() - (parse_datetime(historical.get("captured_at")) or utc_now())).total_seconds()),
            )
            result["mteam"] = historical
            result["updated_at"] = historical.get("captured_at")
    if dashboard and "nas" in selected:
        result["nas"] = dashboard.get("overview") or {}
    for downloader_id in ("qb1", "qb2", "qb3"):
        if downloader_id not in selected:
            continue
        summary = next((item for item in dashboard_qbs if item.get("id") == downloader_id), {})
        if downloader_id == "qb2":
            result[downloader_id] = summary
            if not qb2_allowed:
                result["qb2_privacy_required"] = True
                continue
            cache = get_preload_cache(db, f"downloads.{downloader_id}")
            payload = cache.get("payload") if cache and isinstance(cache.get("payload"), dict) else {}
            result[downloader_id] = {
                **summary,
                "tasks": list(payload.get("items") or [])[:5],
                "tasks_captured_at": payload.get("tasks_captured_at"),
            }
        else:
            result[downloader_id] = summary
    if "downloads" in selected:
        result["downloads"] = [{"id": item.get("id"), "active_downloads": item.get("active_downloads"), "active_uploads": item.get("active_uploads")} for item in dashboard_qbs]
    if "stats" in selected:
        result["stats"] = stats(assistant_actor_for_wechat_claw(db), db)
    if "diagnostics" in selected:
        result["diagnostics"] = diagnostics_health(assistant_actor_for_wechat_claw(db), db)
    return result


def agent_has_qb2_access(
    db: Session, request: WechatClawMessageRequest, qb2_authorized: bool | None = None
) -> bool:
    if qb2_authorized is not None:
        return qb2_authorized
    return has_wechat_claw_privacy_access(db, request)


def agent_int_argument(arguments: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(arguments.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def agent_cached_result_item(session: dict[str, Any], result_key: str, index: int) -> dict[str, Any] | None:
    recent = session.get("recent_results") if isinstance(session.get("recent_results"), dict) else {}
    result = recent.get(result_key) if isinstance(recent.get(result_key), dict) else {}
    items = result.get("items") if isinstance(result.get("items"), list) else []
    item = items[index - 1] if 1 <= index <= len(items) and isinstance(items[index - 1], dict) else None
    return dict(item) if item else None


def canonical_mteam_filters(arguments: dict[str, Any]) -> dict[str, Any]:
    resolutions = arguments.get("resolutions") if isinstance(arguments.get("resolutions"), list) else []
    legacy_resolution = str(arguments.get("resolution") or "").strip()
    if legacy_resolution and not resolutions:
        resolutions = [legacy_resolution]
    factor_value = arguments.get("download_factor_max")
    try:
        download_factor_max = max(0.0, min(1.0, float(factor_value))) if factor_value is not None else None
    except (TypeError, ValueError):
        download_factor_max = None
    return {
        "resolutions": [str(value).lower() for value in resolutions if str(value).strip()],
        "min_size_gb": max(0.0, float(arguments.get("min_size_gb") or 0)),
        "max_size_gb": max(0.0, float(arguments.get("max_size_gb") or 0)),
        "promotion": str(arguments.get("promotion") or "any").lower(),
        "download_factor_max": download_factor_max,
        "min_seeders": max(0, int(float(arguments.get("min_seeders") or 0))),
        "official_only": bool(arguments.get("official_only")),
        "has_chinese_subtitles": arguments.get("has_chinese_subtitles")
        if isinstance(arguments.get("has_chinese_subtitles"), bool) else None,
        "promotion_expires_after_minutes": max(
            0, int(float(arguments.get("promotion_expires_after_minutes") or 0))
        ),
        "recommend": True,
    }


def media_work_search_terms(item: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for value in (item.get("title"), item.get("original_title")):
        text = str(value or "").strip()
        if text and text.casefold() not in {term.casefold() for term in terms}:
            terms.append(text)
    return terms[:2]


def execute_bounded_media_mteam_search(
    db: Session,
    arguments: dict[str, Any],
    request: WechatClawMessageRequest,
) -> dict[str, Any]:
    try:
        requested_count = max(1, int(arguments.get("target_count") or 3))
    except (TypeError, ValueError):
        requested_count = 3
    target_count = min(requested_count, 10)
    query = str(arguments.get("query") or "").strip()
    media_type = str(arguments.get("media_type") or "movie").strip().lower()
    if media_type not in {"movie", "tv", "all"}:
        media_type = "movie"
    min_rating = max(0.0, min(10.0, float(arguments.get("min_rating") or 7.5)))
    min_vote_count = max(0, int(float(arguments.get("min_vote_count") or 200)))
    release_date_to = str(arguments.get("release_date_to") or utc_now().date().isoformat()).strip()
    tmdb_filters = {
        "media_type": media_type,
        "genre": str(arguments.get("genre") or "").strip(),
        "release_date_from": str(arguments.get("release_date_from") or "").strip(),
        "release_date_to": release_date_to,
        "min_rating": min_rating,
        "min_vote_count": min_vote_count,
        "sort_by": "vote_average.desc",
        "pages": 4,
        "limit": 20,
        "hydrate": False,
    }
    row = get_config(db, "tmdb")
    if not row or not row.enabled:
        raise HTTPException(status_code=409, detail="请先在设置中启用 TMDB。")
    tmdb = TmdbAdapter(get_decrypted_config(db, "tmdb") or {})
    works = tmdb.lookup_media(query, tmdb_filters)
    release_date_from = str(tmdb_filters.get("release_date_from") or "")
    works = [
        item for item in works
        if float(item.get("rating") or 0) >= min_rating
        and int(item.get("vote_count") or 0) >= min_vote_count
        and bool(str(item.get("release_date") or ""))
        and (not release_date_from or str(item.get("release_date") or "") >= release_date_from)
        and (not release_date_to or str(item.get("release_date") or "") <= release_date_to)
    ][:20]
    mteam = get_mteam_adapter_or_error(db)
    mteam_filters = canonical_mteam_filters(arguments)
    matches: list[dict[str, Any]] = []
    checked_works = 0
    queries_used = 0
    pages_scanned = 0
    complete = True
    for work in works:
        if len(matches) >= target_count or queries_used >= 20:
            break
        checked_works += 1
        best_resource: dict[str, Any] | None = None
        for term in media_work_search_terms(work):
            if queries_used >= 20:
                complete = False
                break
            queries_used += 1
            raw_items = search_mteam_with_budget(mteam, term, max_pages=5)[:100]
            meta = dict(getattr(mteam, "last_search_meta", {}) or {})
            pages_scanned += int(meta.get("pages_scanned") or (1 if raw_items else 0))
            if meta.get("complete") is False:
                complete = False
            ranked, _ = rank_mteam_search_items(raw_items, mteam_filters)
            if ranked:
                best_resource = ranked[0]
                break
        if best_resource is not None:
            work_label = tmdb_title_label(work)
            presentation = best_resource.get("presentation") if isinstance(best_resource.get("presentation"), dict) else {}
            best_resource["presentation"] = {**presentation, "work_title": work_label}
            best_resource["_search_query"] = media_work_search_terms(work)[0]
            matches.append({"work": work, "resource": best_resource})
    if checked_works < len(works) and len(matches) < target_count:
        complete = False
    resources = [dict(item["resource"]) for item in matches]
    presentation = mteam_model_rows(resources)
    mobile_selection = save_wechat_claw_mobile_candidates(
        db,
        request,
        resources,
        query=" / ".join(str(item["work"].get("title") or "") for item in matches),
        recommended_index=1 if resources else None,
        presentation=presentation,
    )
    return {
        "state": "success",
        "query": query,
        "target_count": target_count,
        "requested_count": requested_count,
        "found_count": len(matches),
        "items": resources,
        "matches": matches,
        "count": len(matches),
        "filters": {"tmdb": tmdb_filters, "mteam": mteam_filters},
        "presentation": presentation,
        "recommended_index": 1 if resources else None,
        "mobile_selection": mobile_selection,
        "checked_works": checked_works,
        "queries_used": queries_used,
        "pages_scanned": pages_scanned,
        "complete": complete,
        "source": "TMDB API + M-Team API",
        "queried_at": utc_iso(),
    }


def _basic_media_clues(message: str) -> dict[str, Any]:
    text = str(message or "").strip()
    people: list[str] = []
    person_patterns = (
        r"(?:男主(?:角)?|女主(?:角)?|主演)\s*(?:是|有)?\s*([\u4e00-\u9fff·]{2,12})",
        r"(?:^|[，,、；;\s])([\u4e00-\u9fff·]{2,12}?)\s*(?<!导)(?:演的|主演)",
    )
    for pattern in person_patterns:
        for value in re.findall(pattern, text):
            for name in re.split(r"[、和与及]", str(value).strip()):
                name = name.strip()
                if name and name not in people:
                    people.append(name)
    director_match = re.search(r"导演\s*(?:是|叫)\s*([\u4e00-\u9fff·]{2,12})", text)
    if not director_match:
        director_match = re.search(r"(?:^|[，,、；;\s])([\u4e00-\u9fff·]{2,12})\s*导演", text)
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?:\s*年)?", text)
    titles = [value.strip() for value in re.findall(r"《([^》]{1,80})》", text) if value.strip()]
    return {
        "people": people[:6],
        "director": director_match.group(1).strip() if director_match else "",
        "year": year_match.group(1) if year_match else "",
        "media_type": "tv" if re.search(r"电视剧|剧集|连续剧", text) else "movie" if "电影" in text else "all",
        "title_hypotheses": titles[:5],
        "plot_keywords": [],
        "wants_details": bool(re.search(r"详细|完整信息|资料|剧情|简介|评分|演员有哪些|主演有哪些|导演是谁|讲什么", text)),
        "wants_resources": bool(re.search(r"资源|种子|版本|促销|馒头|m[-\s]?team|pt\s*站", text, re.IGNORECASE)),
    }


def _media_clues(ai_adapter: DeepSeekChatAdapter, message: str, session: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    clues = _basic_media_clues(message)
    extractor = getattr(ai_adapter, "extract_media_clues", None)
    if callable(extractor):
        try:
            extracted = extractor(message, history=list(session.get("history") or []))
            if isinstance(extracted, dict):
                for key in ("director", "year", "media_type"):
                    if extracted.get(key):
                        clues[key] = extracted[key]
                for key in ("people", "title_hypotheses", "plot_keywords"):
                    if isinstance(extracted.get(key), list) and extracted[key]:
                        clues[key] = extracted[key]
                clues["wants_details"] = bool(extracted.get("wants_details") or clues["wants_details"])
                clues["wants_resources"] = bool(extracted.get("wants_resources") or clues["wants_resources"])
        except (AIServiceError, ValueError, TypeError):
            pass
    for key in ("director", "year", "media_type"):
        if arguments.get(key):
            clues[key] = str(arguments[key]).strip()
    for key in ("people", "title_hypotheses"):
        if isinstance(arguments.get(key), list) and arguments[key]:
            clues[key] = [str(value).strip() for value in arguments[key] if str(value).strip()]
    clues["wants_details"] = bool(arguments.get("wants_details") or clues["wants_details"])
    clues["wants_resources"] = bool(arguments.get("wants_resources") or clues["wants_resources"])
    return clues


def _tmdb_work_key(item: dict[str, Any]) -> str:
    return f"{item.get('media_type')}:{item.get('tmdb_id') or item.get('id')}"


def _same_name(left: str, right: str) -> bool:
    normalized_left = re.sub(r"[\s·._-]+", "", str(left or "")).casefold()
    normalized_right = re.sub(r"[\s·._-]+", "", str(right or "")).casefold()
    return bool(normalized_left and normalized_right and (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    ))


def _safe_tmdb_failure_category(exc: Exception) -> str:
    if isinstance(exc, TmdbConfigError):
        return "TMDB 还没有配置好"
    if isinstance(exc, (TimeoutError, SocketTimeout)):
        return "连接 TMDB 时超时"
    if isinstance(exc, (TmdbDohError, URLError, HTTPError, ConnectionError)):
        return "连接 TMDB 时遇到网络问题"
    return "TMDB 服务暂时没有响应"


def _memory_media_hypothesis(
    ai_adapter: DeepSeekChatAdapter, message: str, session: dict[str, Any]
) -> dict[str, Any]:
    guesser = getattr(ai_adapter, "guess_media_from_memory", None)
    if not callable(guesser):
        return {}
    try:
        candidate = guesser(message, history=list(session.get("history") or []))
    except (AIServiceError, ValueError, TypeError):
        return {}
    return candidate if isinstance(candidate, dict) else {}


def resolve_media_from_clues(
    db: Session,
    ai_adapter: DeepSeekChatAdapter,
    message: str,
    session: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Resolve identity from independent TMDB evidence; model memory is only a verified lookup seed."""
    clues = _media_clues(ai_adapter, message, session, arguments)
    memory_candidate: dict[str, Any] = {}
    tmdb: TmdbAdapter | None = None
    network_error: Exception | None = None
    candidates: list[dict[str, Any]] = []
    matched_people: list[str] = []
    try:
        row = get_config(db, "tmdb")
        if not row or not row.enabled:
            raise TmdbConfigError("TMDB is not enabled")
        tmdb = TmdbAdapter(get_decrypted_config(db, "tmdb") or {})
        credit_maps: list[dict[str, dict[str, Any]]] = []
        for person_name in clues.get("people") or []:
            people = tmdb.search_people(str(person_name), limit=4)
            person = next((value for value in people if _same_name(person_name, value.get("name"))), people[0] if people else None)
            if not person:
                continue
            details = tmdb.get_person_details(str(person.get("person_id") or ""))
            credit_map = {
                _tmdb_work_key(item): item
                for item in details.get("credits") or []
                if isinstance(item, dict)
                and item.get("media_type") in {"movie", "tv"}
                and (clues.get("media_type") not in {"movie", "tv"} or item.get("media_type") == clues.get("media_type"))
            }
            if credit_map:
                credit_maps.append(credit_map)
                matched_people.append(str(person.get("name") or person_name))
        if credit_maps and len(credit_maps) == len(clues.get("people") or []):
            shared_keys = set(credit_maps[0])
            for credit_map in credit_maps[1:]:
                shared_keys.intersection_update(credit_map)
            compact = [credit_maps[0][key] for key in shared_keys]
            compact.sort(key=lambda item: float(item.get("popularity") or 0), reverse=True)
            for item in compact[:10]:
                try:
                    candidates.append(
                        tmdb.get_media_details(str(item.get("tmdb_id") or ""), str(item.get("media_type") or "movie"))
                    )
                except Exception:
                    candidates.append(item)
        if not candidates:
            for title in clues.get("title_hypotheses") or []:
                candidates.extend(tmdb.search_media(str(title))[:5])
    except Exception as exc:
        network_error = exc

    year = str(clues.get("year") or "").strip()
    director = str(clues.get("director") or "").strip()
    ranked: list[tuple[int, dict[str, Any], list[str]]] = []
    seen: set[str] = set()
    for item in candidates:
        key = _tmdb_work_key(item)
        if key in seen:
            continue
        seen.add(key)
        mismatches: list[str] = []
        score = len(matched_people) * 5
        if year:
            if str(item.get("year") or "") == year:
                score += 3
            else:
                mismatches.append(f"年份是 {item.get('year') or '未知'}，不是 {year}")
        if director:
            if _same_name(director, str(item.get("director") or "")):
                score += 4
            else:
                mismatches.append(f"TMDB 标注的导演是 {item.get('director') or '未知'}，不是 {director}")
        score += min(3, int(float(item.get("popularity") or 0) // 25))
        ranked.append((score, item, mismatches))
    ranked.sort(key=lambda value: (value[0], float(value[1].get("popularity") or 0)), reverse=True)

    exact = [value for value in ranked if not value[2]]
    if len(exact) == 1:
        return {
            "state": "resolved",
            "item": exact[0][1],
            "matched_clues": matched_people,
            "clues": clues,
            "source": "tmdb",
        }
    if len(exact) > 1:
        return {
            "state": "multiple",
            "items": [value[1] for value in exact[:5]],
            "matched_clues": matched_people,
            "clues": clues,
            "source": "tmdb",
        }
    if ranked:
        best = ranked[0]
        return {
            "state": "needs_confirmation",
            "item": best[1],
            "matched_clues": matched_people,
            "mismatched_clues": best[2],
            "clues": clues,
            "source": "tmdb",
        }

    memory_candidate = _memory_media_hypothesis(ai_adapter, message, session)
    title = str(memory_candidate.get("title") or "").strip()
    if title and tmdb is not None and network_error is None:
        try:
            matches = tmdb.search_media(title)
            verified = next(
                (
                    item for item in matches
                    if _same_name(title, str(item.get("title") or ""))
                    and (not memory_candidate.get("year") or str(item.get("year") or "") == str(memory_candidate.get("year")))
                ),
                None,
            )
            if verified:
                return {
                    "state": "memory_verified",
                    "item": verified,
                    "matched_clues": [],
                    "clues": clues,
                    "source": "tmdb",
                }
        except Exception as exc:
            network_error = exc
    if title and network_error is not None:
        return {
            "state": "memory_unverified",
            "title": title,
            "year": str(memory_candidate.get("year") or ""),
            "reason": str(memory_candidate.get("reason") or ""),
            "failure_category": _safe_tmdb_failure_category(network_error),
        }
    return {
        "state": "not_found",
        "clues": clues,
        "message": "这些线索暂时还不足以在 TMDB 中确定唯一作品，可以再告诉我大概年份、导演或一段剧情。",
        "source": "tmdb",
    }


def execute_media_hub_agent_tool(
    db: Session,
    ai_adapter: DeepSeekChatAdapter,
    tool: str,
    arguments: dict[str, Any],
    request: WechatClawMessageRequest,
    user: User,
    session: dict[str, Any],
    qb2_authorized: bool | None = None,
) -> dict[str, Any]:
    if tool == "resolve_media_from_clues":
        return resolve_media_from_clues(
            db,
            ai_adapter,
            str(arguments.get("query") or request.message or ""),
            session,
            arguments,
        )

    if tool == "tmdb_lookup":
        filters = {
            "media_type": str(arguments.get("media_type") or "all").lower(),
            "region": str(arguments.get("region") or "").upper()[:2],
            "language": str(arguments.get("language") or "").lower()[:8],
            "genre": str(arguments.get("genre") or "").strip()[:40],
            "year": str(arguments.get("year") or "").strip()[:12],
            "min_rating": max(0.0, min(10.0, float(arguments.get("min_rating") or 0))),
            "sort_by": str(arguments.get("sort_by") or "").strip(),
        }
        intent = {"intent_type": "tmdb_lookup", "query": str(arguments.get("query") or "").strip(), "tmdb_filters": filters}
        result = execute_mobile_agent_intent(db, intent, request, user)
        limit = agent_int_argument(arguments, "limit", 5, 1, 10)
        result["items"] = list(result.get("items") or [])[:limit]
        result["count"] = len(result["items"])
        return result

    if tool == "tmdb_media_details":
        index = agent_int_argument(arguments, "result_index", 0, 0, 10)
        media_id = str(arguments.get("media_id") or "").strip()
        media_type = str(arguments.get("media_type") or "").strip().lower()
        references = session.get("references") if isinstance(session.get("references"), dict) else {}
        candidates = references.get("tmdb_lookup") if isinstance(references.get("tmdb_lookup"), list) else []
        if index and index <= len(candidates) and isinstance(candidates[index - 1], dict):
            media_id = str(candidates[index - 1].get("tmdb_id") or "")
            media_type = str(candidates[index - 1].get("media_type") or "")
        if not media_id or media_type not in {"movie", "tv"}:
            raise HTTPException(status_code=400, detail="No matching TMDB result. Search first or provide a valid result reference.")
        row = get_config(db, "tmdb")
        if not row or not row.enabled:
            raise HTTPException(status_code=409, detail="TMDB is not enabled.")
        item = TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_media_details(media_id, media_type)
        return {"state": "success", "result_index": index or None, "item": item, "source": "tmdb"}

    if tool == "tmdb_person_details":
        person_id = str(arguments.get("person_id") or "").strip()
        if not person_id:
            raise HTTPException(status_code=400, detail="A TMDB person ID is required.")
        row = get_config(db, "tmdb")
        if not row or not row.enabled:
            raise HTTPException(status_code=409, detail="TMDB is not enabled.")
        item = TmdbAdapter(get_decrypted_config(db, "tmdb") or {}).get_person_details(person_id)
        return {"state": "success", "item": item, "source": "tmdb"}

    if tool == "mteam_search":
        filters = canonical_mteam_filters(arguments)
        intent = {"intent_type": "mteam_search", "query": str(arguments.get("query") or "").strip(), "mteam_filters": filters}
        result = execute_mobile_agent_intent(db, intent, request, user)
        limit = agent_int_argument(arguments, "limit", 5, 1, 10)
        result["items"] = list(result.get("items") or [])[:limit]
        result["count"] = len(result["items"])
        result["presentation"] = mteam_model_rows(result["items"])
        result["mobile_selection"] = save_wechat_claw_mobile_candidates(
            db, request, result["items"], query=str(result.get("query") or ""),
            recommended_index=result.get("recommended_index"), presentation=result["presentation"],
        )
        return result

    if tool == "find_media_with_mteam":
        return execute_bounded_media_mteam_search(db, arguments, request)

    if tool == "mteam_result_details":
        index = agent_int_argument(arguments, "result_index", 0, 1, 10)
        references = session.get("references") if isinstance(session.get("references"), dict) else {}
        search_ref = references.get("mteam_search") if isinstance(references.get("mteam_search"), dict) else {}
        candidates = search_ref.get("items") if isinstance(search_ref.get("items"), list) else []
        if index > len(candidates) or not isinstance(candidates[index - 1], dict):
            raise HTTPException(status_code=400, detail="No matching recent M-Team result. Search resources first.")
        candidate_id = str(candidates[index - 1].get("id") or "")
        query = str(search_ref.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="The recent M-Team search expired. Search again.")
        fresh_items = search_mteam_with_budget(get_mteam_adapter_or_error(db), query)[:100]
        item = next((value for value in fresh_items if str(value.get("id") or "") == candidate_id), None)
        if item is None:
            item = agent_cached_result_item(session, "mteam_search", index)
        if item is None:
            raise HTTPException(status_code=404, detail="M-Team no longer returns this resource.")
        return {"state": "success", "result_index": index, "item": item, "refreshed": True, "source": "mteam"}

    if tool == "dashboard_query":
        requested = arguments.get("sections") if isinstance(arguments.get("sections"), list) else ["overview"]
        valid = {"overview", "mteam", "nas", "qb1", "qb2", "qb3", "downloads", "stats", "diagnostics"}
        sections = [str(item).lower() for item in requested if str(item).lower() in valid] or ["overview"]
        if qb2_authorized is None:
            return build_mobile_dashboard_result(db, request, sections)
        return build_mobile_dashboard_result(db, request, sections, qb2_authorized=qb2_authorized)

    if tool == "qb_list_torrents":
        downloader_id = str(arguments.get("downloader_id") or "qb1").lower()
        if downloader_id not in {"qb1", "qb2", "qb3"}:
            raise HTTPException(status_code=400, detail="downloader_id must be qb1, qb2, or qb3.")
        if downloader_id == "qb2" and not agent_has_qb2_access(db, request, qb2_authorized):
            return {"state": "privacy_required", "downloader_id": downloader_id, "message": "qB2 private tasks require an active administrator privacy grant."}
        items = get_qb_adapter_or_error(db, downloader_id).get_torrents(downloader_id)
        query = str(arguments.get("query") or "").strip().casefold()
        if query:
            items = [item for item in items if query in str(item.get("name") or "").casefold()]
        limit = agent_int_argument(arguments, "limit", 5, 1, 10)
        items = items[:limit]
        return {"state": "success", "downloader_id": downloader_id, "items": items, "count": len(items), "source": "qb_web_api"}

    if tool == "qb_torrent_details":
        downloader_id = str(arguments.get("downloader_id") or "qb1").lower()
        index = agent_int_argument(arguments, "result_index", 0, 1, 10)
        if downloader_id not in {"qb1", "qb2", "qb3"}:
            raise HTTPException(status_code=400, detail="downloader_id must be qb1, qb2, or qb3.")
        if downloader_id == "qb2" and not agent_has_qb2_access(db, request, qb2_authorized):
            return {"state": "privacy_required", "downloader_id": downloader_id, "message": "qB2 private tasks require an active administrator privacy grant."}
        references = session.get("references") if isinstance(session.get("references"), dict) else {}
        ref_key = f"qb_list_torrents:{downloader_id}"
        candidates = references.get(ref_key) if isinstance(references.get(ref_key), list) else []
        if index > len(candidates) or not isinstance(candidates[index - 1], dict):
            raise HTTPException(status_code=400, detail="No matching recent qB task. List tasks first.")
        torrent_hash = str(candidates[index - 1].get("hash") or "")
        detail = get_qb_adapter_or_error(db, downloader_id).get_torrent_detail(downloader_id, torrent_hash)
        return {"state": "success", "downloader_id": downloader_id, "result_index": index, "name": candidates[index - 1].get("name"), "detail": detail}

    if tool == "knowledge_search":
        return search_knowledge(
            str(arguments.get("query") or request.message or ""),
            agent_int_argument(arguments, "limit", 3, 1, 5),
        )

    if tool == "prepare_mteam_download":
        index = agent_int_argument(arguments, "result_index", 0, 1, 10)
        candidate = get_wechat_claw_mobile_candidate(db, request, index)
        if candidate is None:
            candidate = agent_cached_result_item(session, "mteam_search", index)
        if candidate is None or not str(candidate.get("id") or ""):
            return {"intent_type": "download_selected", "state": "selection_missing"}
        recent = session.get("recent_results") if isinstance(session.get("recent_results"), dict) else {}
        search_result = recent.get("mteam_search") if isinstance(recent.get("mteam_search"), dict) else {}
        query = str(candidate.get("_search_query") or search_result.get("query") or "").strip()
        constraints = search_result.get("filters") if isinstance(search_result.get("filters"), dict) else {}
        if query:
            fresh_items = search_mteam_with_budget(get_mteam_adapter_or_error(db), query)
            refreshed = next((item for item in fresh_items if str(item.get("id") or "") == str(candidate.get("id") or "")), None)
            if refreshed is None:
                return {"intent_type": "download_selected", "state": "resource_unavailable", "candidate": candidate}
            matched, _ = rank_mteam_search_items([refreshed], constraints)
            if not matched:
                return {"intent_type": "download_selected", "state": "conditions_changed", "candidate": refreshed}
            original_presentation = (
                candidate.get("presentation") if isinstance(candidate.get("presentation"), dict) else {}
            )
            refreshed["presentation"] = {
                **(refreshed.get("presentation") if isinstance(refreshed.get("presentation"), dict) else {}),
                **original_presentation,
            }
            refreshed["_search_query"] = query
            candidate = refreshed
        operation = save_wechat_claw_pending_download(
            db,
            request,
            candidate,
            query=query,
            constraints=constraints,
            downloader_id=str(arguments.get("downloader_id") or ""),
            actor_user_id=user.id,
        )
        return {
            "intent_type": "download_selected",
            "state": "awaiting_confirmation",
            "candidate": candidate,
            "operation_id": operation.operation_id,
        }

    if tool == "confirm_mteam_download":
        candidate = get_wechat_claw_pending_download(db, request)
        if candidate is None:
            return {"intent_type": "download_selected", "state": "selection_missing"}
        if not WECHAT_CLAW_EXPLICIT_DOWNLOAD_CONFIRM_RE.search(request.message or ""):
            return {"intent_type": "download_selected", "state": "confirmation_required", "candidate": candidate}
        operation_id = str(candidate.get("_operation_id") or "")
        operation = (
            db.query(AgentDownloadOperation)
            .filter(AgentDownloadOperation.operation_id == operation_id)
            .one_or_none()
            if operation_id else latest_agent_download_operation(db, request, active_only=True)
        )
        if operation is None:
            return {"intent_type": "download_selected", "state": "selection_missing"}
        with agent_download_lock(operation.operation_id):
            db.refresh(operation)
            if operation.state in {"started", "pending_verification"}:
                return {"intent_type": "download_selected", **dict(operation.result or {}), "candidate": dict(operation.candidate or {})}
            refreshed, reason = refresh_agent_download_candidate(db, operation)
            if reason:
                operation.state = reason
                operation.result = {"state": reason, "candidate": agent_safe_payload(refreshed or operation.candidate)}
                operation.updated_at = utc_now_naive()
                db.commit()
                clear_wechat_claw_pending_download(db, request)
                return {"intent_type": "download_selected", **dict(operation.result or {})}
            dispatch = download_wechat_claw_selected_torrent(
                db,
                operation.torrent_id,
                user,
                refreshed or dict(operation.candidate or {}),
                operation,
            )
            clear_wechat_claw_pending_download(db, request)
            return {"intent_type": "download_selected", **dispatch}

    raise HTTPException(status_code=400, detail=f"Agent requested an unknown tool: {tool}")


def format_tmdb_detail_reply(result: dict[str, Any]) -> str:
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    if not item:
        return "这次没有取得可展示的作品详情，请重新查询后再试。"
    title = tmdb_title_label(item)
    genres = " / ".join(str(value) for value in item.get("genres") or []) or "-"
    countries = " / ".join(str(value) for value in item.get("production_countries") or []) or "-"
    cast = " / ".join(str(value) for value in item.get("cast") or []) or "-"
    media_type = "电视剧" if item.get("media_type") == "tv" else "电影"
    lines = [
        f"【{title}】",
        f"类型：{media_type} · {genres}",
        f"年份：{item.get('year') or '-'} · TMDB：{item.get('rating') or '-'}（{item.get('vote_count') or 0} 人评分）",
        f"国家/地区：{countries} · 原语言：{item.get('original_language') or '-'}",
        f"时长/季集：{tmdb_runtime_progress_label(item)}",
        f"导演/主创：{item.get('director') or '-'}",
        f"主演：{cast}",
        "",
        str(item.get("overview") or "暂无简介。").strip(),
        "",
        "来源：TMDB API",
    ]
    return "\n".join(lines)


def format_media_resolution_reply(result: dict[str, Any]) -> str:
    state = str(result.get("state") or "")
    if state in {"resolved", "memory_verified"}:
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        if not item:
            return "这次没有找到可核对的作品信息。"
        clues = result.get("clues") if isinstance(result.get("clues"), dict) else {}
        if clues.get("wants_details"):
            return format_tmdb_detail_reply({"item": item})
        director = str(item.get("director") or "-")
        matched = "、".join(str(value) for value in result.get("matched_clues") or [])
        lines = [f"你找的应该是《{item.get('title') or '-'}》（{item.get('year') or '年份未知'}），导演是 {director}。"]
        if matched:
            lines.append(f"TMDB 的演职员信息与“{matched}”这些线索吻合。")
        return "\n".join(lines)
    if state == "needs_confirmation":
        item = result.get("item") if isinstance(result.get("item"), dict) else {}
        mismatch = "；".join(str(value) for value in result.get("mismatched_clues") or [])
        prefix = f"按现有线索，最接近的是《{item.get('title') or '-'}》（{item.get('year') or '年份未知'}）"
        return f"{prefix}。{mismatch}。你找的是这部吗？" if mismatch else f"{prefix}。你找的是这部吗？"
    if state == "multiple":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        lines = ["这些线索在 TMDB 中对应到不止一部作品：", ""]
        lines.extend(
            f"{index}. 《{item.get('title') or '-'}》（{item.get('year') or '年份未知'}）· 导演：{item.get('director') or '-'}"
            for index, item in enumerate(items, 1)
            if isinstance(item, dict)
        )
        lines.extend(["", "告诉我第几部，我就按这部继续查。"])
        return "\n".join(lines)
    if state == "memory_unverified":
        title = str(result.get("title") or "")
        year = str(result.get("year") or "")
        failure = str(result.get("failure_category") or "TMDB 暂时没有响应")
        label = f"《{title}》（{year}）" if year else f"《{title}》"
        return (
            f"我根据这些线索想到最可能是{label}。不过刚才{failure}，暂时没能替你核对演员和影片资料，"
            "所以先把它作为一个可能答案；连接恢复后我可以继续确认。"
        )
    return str(result.get("message") or "这些线索暂时还不能确定唯一作品，可以再告诉我大概年份、导演或一段剧情。")


def format_mteam_detail_reply(result: dict[str, Any]) -> str:
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    if not item:
        return "这个资源的最新详情暂时没有取到，请重新搜索后再试。"
    presentation = (
        item.get("presentation") if isinstance(item.get("presentation"), dict)
        else _mteam_fallback_row(item, int(result.get("result_index") or 1))
    )
    title = mteam_markdown_cell(presentation.get("title") or item.get("title") or "资源详情", 180)
    if item.get("is_official"):
        title = f"{title}【官种】"
    lines = [
        "| # | 标题 | 中文信息 | 清晰度 | 大小 | 做种 | 促销 |",
        "|---|------|----------|--------|------|------|------|",
        (
            f"| {result.get('result_index') or 1} | {title} | "
            f"{mteam_markdown_cell(presentation.get('chinese_info') or item.get('subtitle') or '未标注', 120)} | "
            f"{mteam_markdown_cell(presentation.get('quality') or item.get('resolution') or '-', 72)} | "
            f"{mteam_markdown_cell(item.get('size') or '-', 16)} | {item.get('seeders') or 0} | "
            f"{mteam_markdown_cell(item.get('promotion_label') or '普通', 18)} |"
        ),
    ]
    if item.get("promotion_remaining"):
        lines.extend(["", f"促销剩余：{item.get('promotion_remaining')}"])
    lines.extend(["", f"来源：M-Team API · 查询时间：{format_system_datetime(utc_iso())}"])
    return "\n".join(lines)


def format_composite_media_reply(result: dict[str, Any]) -> str:
    matches = result.get("matches") if isinstance(result.get("matches"), list) else []
    target = int(result.get("target_count") or 3)
    requested = int(result.get("requested_count") or target)
    found = len(matches)
    if not matches:
        return (
            f"这次按全部条件核验了 {result.get('checked_works') or 0} 部候选作品，"
            f"但没有找到同时满足条件的 M-Team 资源。没有用不符合条件的结果凑数；"
            "你可以告诉我愿意放宽哪一项。"
        )
    if requested > 10:
        lines = [f"你要找 {requested} 部，我先按单轮上限核验前 10 部；需要的话可以继续下一批。"]
    elif found < target:
        lines = [f"你要找 {target} 部，目前只找到 {found} 部完全符合条件的作品；其余候选没有拿不符合条件的资源凑数。"]
    else:
        lines = [f"找到 {found} 部作品，并已逐项核验对应的 M-Team 资源："]
    lines.extend([
        "",
        "| # | 作品 | TMDB | M-Team 资源 | 大小 | 做种 | 促销 |",
        "|---|------|------|-------------|------|------|------|",
    ])
    for index, match in enumerate(matches, 1):
        work = match.get("work") if isinstance(match, dict) and isinstance(match.get("work"), dict) else {}
        resource = match.get("resource") if isinstance(match, dict) and isinstance(match.get("resource"), dict) else {}
        presentation = resource.get("presentation") if isinstance(resource.get("presentation"), dict) else {}
        work_label = mteam_markdown_cell(tmdb_title_label(work), 72)
        resource_label = mteam_markdown_cell(presentation.get("quality") or resource.get("title") or "-", 100)
        lines.append(
            f"| {index} | {work_label} | {work.get('rating') or '-'}（{work.get('vote_count') or 0}） | "
            f"{resource_label} | {resource.get('size') or '-'} | {resource.get('seeders') or 0} | "
            f"{resource.get('promotion_label') or '普通'} |"
        )
    if result.get("complete") is False:
        lines.extend(["", "本轮已达到查询预算；上表结果都已核验，但可能还有其他符合条件的候选尚未查完。"])
    lines.extend([
        "",
        "需要下载哪个资源？回复“第 X 个”，我会先展示该资源的最新摘要，再等待一次确认。",
        "",
        f"来源：TMDB API + M-Team API · 查询时间：{format_system_datetime(result.get('queried_at'))}",
    ])
    return "\n".join(lines)


def format_knowledge_reply(result: dict[str, Any]) -> str:
    items = result.get("items") if isinstance(result.get("items"), list) else []
    if not items:
        return "本地知识文档里暂时没有找到可靠说明。为了避免凭印象回答，我先不下结论。"
    lines = ["【影视中枢知识】"]
    for item in items:
        lines.extend([
            "",
            f"{item.get('title') or '说明'}",
            str(item.get("excerpt") or "").strip(),
            f"来源：{item.get('document') or '-'} · 更新：{item.get('updated_at') or '未标注'}",
        ])
    return "\n".join(lines)


def fallback_media_hub_agent_reply(tool: str, result: dict[str, Any]) -> str:
    if result.get("state") == "failed":
        return f"这次没能完成查询：{trim_wechat_claw_text(result.get('error') or '服务暂时不可用', 300)}"
    if tool == "resolve_media_from_clues":
        return format_media_resolution_reply(result)
    if tool == "tmdb_lookup":
        return format_mobile_agent_reply({"intent_type": "tmdb_lookup"}, result)
    if tool == "mteam_search":
        return format_mobile_agent_reply({"intent_type": "mteam_search"}, result)
    if tool == "find_media_with_mteam":
        return format_composite_media_reply(result)
    if tool == "knowledge_search":
        return format_knowledge_reply(result)
    if tool == "dashboard_query":
        return format_mobile_agent_reply({"intent_type": "dashboard_query"}, result)
    if tool in {"prepare_mteam_download", "confirm_mteam_download"}:
        return format_mobile_agent_reply({"intent_type": "download_selected"}, result)
    if tool == "tmdb_media_details":
        return format_tmdb_detail_reply(result)
    if tool == "mteam_result_details":
        return format_mteam_detail_reply(result)
    if tool == "qb_list_torrents":
        items = result.get("items") if isinstance(result.get("items"), list) else []
        if result.get("state") == "privacy_required":
            return str(result.get("message") or "\u5f53\u524d\u4f1a\u8bdd\u65e0\u6743\u67e5\u770b qB2 \u79c1\u5bc6\u4efb\u52a1\u3002")
        lines = [f"{result.get('downloader_id') or 'qB'} \u5f53\u524d\u627e\u5230 {len(items)} \u4e2a\u4efb\u52a1\uff1a"]
        lines.extend(f"{index}. {item.get('name') or '-'} \u00b7 {float(item.get('progress') or 0) * 100:.1f}%" for index, item in enumerate(items, 1))
        return "\n".join(lines)
    return "\u6211\u5df2\u7ecf\u53d6\u5f97\u4e86\u76f8\u5173\u6570\u636e\uff0c\u4f46\u8fd9\u6b21\u6ca1\u6709\u987a\u5229\u7ec4\u7ec7\u6210\u5b8c\u6574\u56de\u590d\u3002\u4f60\u53ef\u4ee5\u6362\u4e00\u79cd\u8bf4\u6cd5\uff0c\u6211\u4f1a\u63a5\u7740\u5904\u7406\u3002"


def agent_ordinal_index(message: str) -> int:
    match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*(?:部|步|个|项)", str(message or ""))
    if not match:
        return 0
    token = match.group(1)
    if token.isdigit():
        return int(token)
    values = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return values.get(token, 0)


AGENT_SHORT_CONTINUE_RE = re.compile(r"^\s*(?:是的|对|好的|可以|行|查|查一下|继续|麻烦了|嗯|好)\s*[。.!！]?\s*$")
AGENT_AFFIRMATIVE_RE = re.compile(r"^\s*(?:是的|对|对的|没错|就是这部|好的|可以|行|嗯)\s*[。.!！]?\s*$")
AGENT_NEGATED_TOOL_RE = re.compile(r"(?:不用|不要|别|无需)(?:再)?(?:查|搜索|调用|联网)|(?:只想|只是)(?:聊|讨论)")
AGENT_RESOURCE_RE = re.compile(r"(?:资源|种子|版本|促销|做种|大小|清晰度|resources?|torrents?)", re.IGNORECASE)
AGENT_DETAIL_RE = re.compile(r"(?:详细|完整信息|资料|演员|主演|导演|剧情|简介|评分|讲什么|信息)")
AGENT_FUZZY_IDENTITY_RE = re.compile(r"(?:哪部|什么)(?:电影|电视剧|剧|片)|(?:电影|电视剧|剧|片).*(?:叫什么|片名)|这是哪部")


def _current_work_tool_arguments(session: dict[str, Any]) -> dict[str, Any]:
    current = session.get("current_work") if isinstance(session.get("current_work"), dict) else {}
    media_id = str(current.get("tmdb_id") or "").strip()
    media_type = str(current.get("media_type") or "").strip()
    if not media_id or media_type not in {"movie", "tv"}:
        return {}
    return {"media_id": media_id, "media_type": media_type}


def determine_agent_tool_obligations(message: str, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate factual user goals into backend-enforced evidence requirements."""
    text = str(message or "").strip()
    if not text or AGENT_NEGATED_TOOL_RE.search(text):
        return []
    pending = session.get("pending_request") if isinstance(session.get("pending_request"), dict) else {}
    pending_obligations = pending.get("obligations") if isinstance(pending.get("obligations"), list) else []
    if pending_obligations and AGENT_SHORT_CONTINUE_RE.fullmatch(text):
        return [dict(value) for value in pending_obligations if isinstance(value, dict)]

    current = session.get("current_work") if isinstance(session.get("current_work"), dict) else {}
    memory_candidate = session.get("memory_candidate") if isinstance(session.get("memory_candidate"), dict) else {}
    current_args = _current_work_tool_arguments(session)
    obligations: list[dict[str, Any]] = []
    if AGENT_FUZZY_IDENTITY_RE.search(text):
        obligations.append(
            {
                "capability": "tmdb_fuzzy_resolution",
                "tool": "resolve_media_from_clues",
                "arguments": {"query": text},
            }
        )
        return obligations

    if AGENT_RESOURCE_RE.search(text):
        title = str(current.get("title") or "").strip()
        quoted = re.search(r"《([^》]{1,100})》", text)
        query = quoted.group(1).strip() if quoted else title
        if query:
            obligations.append(
                {
                    "capability": "mteam_current_resources",
                    "tool": "mteam_search",
                    "arguments": {"query": query, "limit": 5},
                }
            )
        elif memory_candidate.get("title"):
            obligations.append(
                {
                    "capability": "tmdb_retry_before_resources",
                    "tool": "resolve_media_from_clues",
                    "arguments": {
                        "query": text,
                        "title_hypotheses": [str(memory_candidate.get("title") or "")],
                        "wants_resources": True,
                    },
                }
            )

    detail_requested = bool(AGENT_DETAIL_RE.search(text))
    generic_current_lookup = bool(current_args and re.fullmatch(r"\s*(?:查|查一下|继续)\s*[。.!！]?\s*", text))
    generic_memory_retry = bool(memory_candidate.get("title") and AGENT_SHORT_CONTINUE_RE.fullmatch(text))
    if detail_requested or generic_current_lookup or generic_memory_retry:
        if current_args:
            obligations.insert(
                0,
                {
                    "capability": "tmdb_current_details",
                    "tool": "tmdb_media_details",
                    "arguments": current_args,
                },
            )
        else:
            quoted = re.search(r"《([^》]{1,100})》", text)
            if quoted:
                obligations.insert(
                    0,
                    {
                        "capability": "tmdb_title_lookup",
                        "tool": "tmdb_lookup",
                        "arguments": {"query": quoted.group(1).strip(), "limit": 5},
                    },
                )
            elif memory_candidate.get("title"):
                obligations.insert(
                    0,
                    {
                        "capability": "tmdb_retry_memory_candidate",
                        "tool": "resolve_media_from_clues",
                        "arguments": {
                            "query": text,
                            "title_hypotheses": [str(memory_candidate.get("title") or "")],
                            "wants_details": detail_requested,
                            "wants_resources": bool(AGENT_RESOURCE_RE.search(text)),
                        },
                    },
                )

    if re.search(r"(?:馒头|m[-\s]?team).*(?:站点|状态|数据|账号)|(?:站点|状态).*(?:馒头|m[-\s]?team)", text, re.IGNORECASE):
        obligations.append(
            {
                "capability": "mteam_live_status",
                "tool": "dashboard_query",
                "arguments": {"sections": ["mteam"]},
            }
        )
    elif re.search(r"(?:nas).*(?:状态|空间|容量)|(?:状态|空间|容量).*(?:nas)", text, re.IGNORECASE):
        obligations.append(
            {
                "capability": "nas_live_status",
                "tool": "dashboard_query",
                "arguments": {"sections": ["nas"]},
            }
        )
    elif re.search(r"(?:下载器|qb[123]?).*(?:状态|任务)|(?:状态|任务).*(?:下载器|qb[123]?)", text, re.IGNORECASE):
        obligations.append(
            {
                "capability": "downloader_live_status",
                "tool": "dashboard_query",
                "arguments": {"sections": ["downloads"]},
            }
        )
    if re.search(r"(?:free|2x|hr|分享率|魔力值|官种|促销).*(?:什么意思|规则|解释)|(?:什么是).*(?:free|hr|官种|促销)", text, re.IGNORECASE):
        obligations.append(
            {
                "capability": "media_hub_knowledge",
                "tool": "knowledge_search",
                "arguments": {"query": text, "limit": 3},
            }
        )
    return obligations


def _obligation_satisfied(obligation: dict[str, Any], tool_calls: list[dict[str, Any]]) -> bool:
    required_tool = str(obligation.get("tool") or "")
    return any(str(call.get("tool") or "") == required_tool for call in tool_calls)


def _obligation_successfully_satisfied(
    obligation: dict[str, Any], observations: list[dict[str, Any]]
) -> bool:
    required_tool = str(obligation.get("tool") or "")
    for observation in observations:
        if str(observation.get("tool") or "") != required_tool:
            continue
        result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
        if result.get("state") != "failed":
            return True
    return False


def _pending_media_confirmation_reply(session: dict[str, Any], message: str) -> str | None:
    pending = session.get("pending_confirmation") if isinstance(session.get("pending_confirmation"), dict) else {}
    if pending.get("kind") != "media_candidate" or not AGENT_AFFIRMATIVE_RE.fullmatch(str(message or "").strip()):
        return None
    candidate = pending.get("candidate") if isinstance(pending.get("candidate"), dict) else {}
    if not candidate:
        return None
    session["current_work"] = {
        "tmdb_id": str(candidate.get("tmdb_id") or candidate.get("id") or ""),
        "media_type": str(candidate.get("media_type") or ""),
        "title": str(candidate.get("title") or "")[:160],
        "year": str(candidate.get("year") or "")[:12],
        "verified_at": utc_iso(),
    }
    session["pending_confirmation"] = {}
    return f"好，已按《{candidate.get('title') or '-'}》（{candidate.get('year') or '年份未知'}）继续。"


def execute_resolved_media_followup(
    db: Session,
    ai_adapter: DeepSeekChatAdapter,
    tool: str,
    result: dict[str, Any],
    request: WechatClawMessageRequest,
    user: User,
    session: dict[str, Any],
    qb2_authorized: bool | None,
    tool_calls: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    if tool != "resolve_media_from_clues" or result.get("state") not in {"resolved", "memory_verified"}:
        return tool, result
    clues = result.get("clues") if isinstance(result.get("clues"), dict) else {}
    item = result.get("item") if isinstance(result.get("item"), dict) else {}
    if not clues.get("wants_resources") or not item.get("title"):
        return tool, result
    followup_tool = "mteam_search"
    followup_arguments = {"query": str(item.get("title") or ""), "limit": 5}
    try:
        followup_result = execute_media_hub_agent_tool(
            db,
            ai_adapter,
            followup_tool,
            followup_arguments,
            request,
            user,
            session,
            qb2_authorized,
        )
    except HTTPException as exc:
        followup_result = {"state": "failed", "error": str(agent_safe_payload(exc.detail))}
    except (MTeamApiError, QbittorrentApiError, TmdbConfigError, AIServiceError, ValueError, TypeError) as exc:
        followup_result = {"state": "failed", "error": str(agent_safe_payload(str(exc)))}
    tool_calls.append(
        {"tool": followup_tool, "arguments": agent_safe_payload(followup_arguments), "derived_from": tool}
    )
    observations.append({"tool": followup_tool, "result": agent_safe_payload(followup_result)})
    remember_assistant_agent_result(session, followup_tool, followup_result, followup_arguments)
    return followup_tool, followup_result


def execute_required_agent_tools(
    db: Session,
    ai_adapter: DeepSeekChatAdapter,
    obligations: list[dict[str, Any]],
    request: WechatClawMessageRequest,
    user: User,
    session: dict[str, Any],
    qb2_authorized: bool | None,
    tool_calls: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    last_tool = ""
    last_result: dict[str, Any] = {}
    for obligation in obligations:
        if _obligation_satisfied(obligation, tool_calls):
            continue
        tool = str(obligation.get("tool") or "")
        arguments = dict(obligation.get("arguments") or {})
        try:
            result = execute_media_hub_agent_tool(
                db, ai_adapter, tool, arguments, request, user, session, qb2_authorized
            )
        except HTTPException as exc:
            result = {"state": "failed", "error": str(agent_safe_payload(exc.detail))}
        except (MTeamApiError, QbittorrentApiError, TmdbConfigError, AIServiceError, ValueError, TypeError) as exc:
            result = {"state": "failed", "error": str(agent_safe_payload(str(exc)))}
        tool_calls.append({"tool": tool, "arguments": agent_safe_payload(arguments), "enforced": True})
        observations.append({"tool": tool, "result": agent_safe_payload(result)})
        remember_assistant_agent_result(session, tool, result, arguments)
        last_tool, last_result = execute_resolved_media_followup(
            db,
            ai_adapter,
            tool,
            result,
            request,
            user,
            session,
            qb2_authorized,
            tool_calls,
            observations,
        )
    return last_tool, last_result


def contextual_agent_tool_call(
    session: dict[str, Any], message: str
) -> tuple[str, dict[str, Any]] | None:
    index = agent_ordinal_index(message)
    if not 1 <= index <= 10:
        return None
    current_list = session.get("current_list") if isinstance(session.get("current_list"), dict) else {}
    kind = str(current_list.get("kind") or "")
    references = session.get("references") if isinstance(session.get("references"), dict) else {}
    if kind == "media_resolution":
        candidates = references.get("media_resolution") if isinstance(references.get("media_resolution"), list) else []
        if index > len(candidates) or not isinstance(candidates[index - 1], dict):
            return None
        candidate = candidates[index - 1]
        session["current_work"] = {
            "tmdb_id": str(candidate.get("tmdb_id") or ""),
            "media_type": str(candidate.get("media_type") or ""),
            "title": str(candidate.get("title") or "")[:160],
            "year": str(candidate.get("year") or "")[:12],
            "verified_at": utc_iso(),
        }
        if AGENT_RESOURCE_RE.search(message):
            return "mteam_search", {"query": str(candidate.get("title") or ""), "limit": 5}
        return "tmdb_media_details", {
            "media_id": str(candidate.get("tmdb_id") or ""),
            "media_type": str(candidate.get("media_type") or ""),
        }
    if kind == "tmdb":
        candidates = references.get("tmdb_lookup") if isinstance(references.get("tmdb_lookup"), list) else []
        if index > len(candidates):
            return None
        if re.search(r"(?:资源|种子|版本|促销|做种|馒头|m[-\s]?team|pt\s*站)", message, re.IGNORECASE):
            title = str(candidates[index - 1].get("title") or "").strip()
            return ("mteam_search", {"query": title, "limit": 5}) if title else None
        if re.search(r"(?:完整|信息|详情|演员|主演|导演|剧情|简介|讲什么)", message):
            return "tmdb_media_details", {"result_index": index}
    if kind == "mteam" and re.search(r"(?:完整|信息|详情|促销|做种|大小|清晰度)", message):
        return "mteam_result_details", {"result_index": index}
    return None


def agent_template_name(tool: str, result: dict[str, Any]) -> str:
    if tool in {"prepare_mteam_download", "confirm_mteam_download"}:
        return f"download_{result.get('state') or 'result'}"
    return {
        "resolve_media_from_clues": "tmdb_fuzzy_resolution",
        "tmdb_lookup": "tmdb_list",
        "tmdb_media_details": "tmdb_detail",
        "mteam_search": "mteam_table",
        "mteam_result_details": "mteam_detail_table",
        "find_media_with_mteam": "composite_table",
        "dashboard_query": "dashboard",
        "knowledge_search": "knowledge",
        "qb_list_torrents": "qb_list",
        "qb_torrent_details": "qb_detail",
    }.get(tool, "conversation")


def record_agent_run_trace(
    db: Session,
    request: WechatClawMessageRequest,
    tool_calls: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    reply: str,
    started: float,
    *,
    status_value: str = "success",
    obligations: list[dict[str, Any]] | None = None,
    obligation_violations: int = 0,
    deterministic_takeover: bool = False,
) -> None:
    try:
        last_tool = str((tool_calls[-1] if tool_calls else {}).get("tool") or "")
        last_observation = observations[-1].get("result") if observations and isinstance(observations[-1], dict) else {}
        last_observation = last_observation if isinstance(last_observation, dict) else {}
        request_summary = trim_wechat_claw_text(
            redact_wechat_claw_message(redact_ai_user_text(redact_payload(request.message))),
            200,
        )
        timeline = [{
            "request_summary": request_summary,
            "tools": [str(item.get("tool") or "") for item in tool_calls[:12]],
            "tool_count": len(tool_calls),
            "data_source": str(last_observation.get("source") or ""),
            "result_state": str(last_observation.get("state") or "success"),
            "result_count": int(last_observation.get("count") or last_observation.get("found_count") or 0),
            "complete": last_observation.get("complete"),
            "template": agent_template_name(last_tool, last_observation),
            "reply_length": len(reply),
            "tool_obligations": [
                {
                    "capability": str(value.get("capability") or ""),
                    "tool": str(value.get("tool") or ""),
                }
                for value in (obligations or [])[:8]
                if isinstance(value, dict)
            ],
            "obligation_violations": int(obligation_violations),
            "deterministic_takeover": bool(deterministic_takeover),
        }]
        error_summary = ""
        if status_value != "success":
            error_summary = trim_wechat_claw_text(last_observation.get("error") or "Agent run failed", 300)
        db.add(DebugTrace(
            trace_id=trace_id("AGENT"),
            event_type="agent_run",
            status=status_value,
            timeline=agent_safe_payload(timeline),
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error_summary=error_summary or None,
        ))
        db.commit()
    except Exception:
        db.rollback()


def run_media_hub_agent(
    db: Session,
    ai_adapter: DeepSeekChatAdapter,
    request: WechatClawMessageRequest,
    user: User,
    telemetry: dict[str, Any] | None = None,
    qb2_authorized: bool | None = None,
) -> dict[str, Any]:
    run_started = time.perf_counter()
    session = get_assistant_agent_session(db, request)
    normalized_message = str(request.message or "").strip()
    if re.fullmatch(r"(?:取消|算了|不下了|取消下载)[。.!！]?", normalized_message):
        cancelled = cancel_wechat_claw_pending_download(db, request)
        result = {"intent_type": "download_selected", "state": "cancelled" if cancelled else "selection_missing"}
        reply = format_mobile_agent_reply({"intent_type": "download_selected"}, result)
        append_assistant_agent_turn(session, request.message, reply)
        save_assistant_agent_session(db, request, session)
        record_agent_run_trace(db, request, [], [], reply, run_started)
        return {
            "reply": reply,
            "intent": {"intent_type": "agent", "action": "agent", "tools_used": []},
            "result": {"tool_calls": [], "observations": []},
            "source": "media_hub_agent",
            "handled_at": utc_iso(),
        }
    if re.search(r"(?:新话题|清空上下文)", normalized_message):
        cancel_wechat_claw_pending_download(db, request)
        session = {
            "history": [],
            "recent_results": {},
            "references": {},
            "current_list": {},
            "current_work": {},
            "pending_request": {},
            "pending_confirmation": {},
            "memory_candidate": {},
        }
    elif re.search(r"(?:搜|查|找).*(?:资源|种子|馒头|m[-\s]?team|pt\s*站)", normalized_message, re.IGNORECASE):
        cancel_wechat_claw_pending_download(db, request)
    pending_candidate = get_wechat_claw_pending_download(db, request)
    if pending_candidate and WECHAT_CLAW_EXPLICIT_DOWNLOAD_CONFIRM_RE.search(normalized_message):
        result = execute_media_hub_agent_tool(
            db,
            ai_adapter,
            "confirm_mteam_download",
            {},
            request,
            user,
            session,
            qb2_authorized,
        )
        reply = format_mobile_agent_reply({"intent_type": "download_selected"}, result)
        append_assistant_agent_turn(session, request.message, reply)
        save_assistant_agent_session(db, request, session)
        trace_calls = [{"tool": "confirm_mteam_download", "arguments": {}}]
        trace_observations = [{"tool": "confirm_mteam_download", "result": agent_safe_payload(result)}]
        record_agent_run_trace(db, request, trace_calls, trace_observations, reply, run_started)
        return {
            "reply": reply,
            "intent": {"intent_type": "agent", "action": "agent", "tools_used": ["confirm_mteam_download"]},
            "result": {
                "tool_calls": trace_calls,
                "observations": trace_observations,
            },
            "source": "media_hub_agent",
            "handled_at": utc_iso(),
        }
    confirmation_reply = _pending_media_confirmation_reply(session, normalized_message)
    if confirmation_reply:
        append_assistant_agent_turn(session, request.message, confirmation_reply)
        save_assistant_agent_session(db, request, session)
        record_agent_run_trace(db, request, [], [], confirmation_reply, run_started)
        return {
            "reply": confirmation_reply,
            "intent": {"intent_type": "agent", "action": "agent", "tools_used": []},
            "result": {"tool_calls": [], "observations": []},
            "source": "media_hub_agent",
            "handled_at": utc_iso(),
        }
    obligations = determine_agent_tool_obligations(normalized_message, session)
    if obligations:
        session["pending_request"] = {
            "obligations": agent_safe_payload(obligations),
            "created_at": utc_iso(),
            "original_message": redact_ai_user_text(redact_payload(normalized_message))[:500],
        }
    contextual_call = contextual_agent_tool_call(session, normalized_message)
    if contextual_call:
        tool, arguments = contextual_call
        try:
            result = execute_media_hub_agent_tool(
                db, ai_adapter, tool, arguments, request, user, session, qb2_authorized
            )
        except HTTPException as exc:
            result = {"state": "failed", "error": str(agent_safe_payload(exc.detail))}
        remember_assistant_agent_result(session, tool, result, arguments)
        reply = fallback_media_hub_agent_reply(tool, result)
        append_assistant_agent_turn(session, request.message, reply)
        session["pending_request"] = {}
        save_assistant_agent_session(db, request, session)
        trace_calls = [{"tool": tool, "arguments": agent_safe_payload(arguments)}]
        trace_observations = [{"tool": tool, "result": agent_safe_payload(result)}]
        record_agent_run_trace(
            db,
            request,
            trace_calls,
            trace_observations,
            reply,
            run_started,
            status_value="failed" if result.get("state") == "failed" else "success",
            obligations=obligations,
        )
        return {
            "reply": reply,
            "intent": {"intent_type": "agent", "action": "agent", "tools_used": [tool]},
            "result": {
                "tool_calls": trace_calls,
                "observations": trace_observations,
            },
            "source": "media_hub_agent",
            "handled_at": utc_iso(),
        }
    observations: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    last_tool = ""
    last_result: dict[str, Any] = {}
    reply = ""
    obligation_violations = 0
    deterministic_takeover = False
    for step_index in range(ASSISTANT_AGENT_MAX_TOOL_STEPS):
        decision_started = time.perf_counter()
        try:
            decision = ai_adapter.next_agent_step(
                request.message,
                history=list(session.get("history") or []),
                recent_results=dict(session.get("recent_results") or {}),
                observations=observations,
                tools=AGENT_TOOL_CATALOG,
                runtime_context=system_time_context(),
                tool_obligations=obligations,
            )
        except AIServiceError as exc:
            if telemetry is not None:
                record_wechat_claw_stage(telemetry, "agent_decision", decision_started, status="failed", error=exc)
            if obligations:
                deterministic_takeover = True
                last_tool, last_result = execute_required_agent_tools(
                    db,
                    ai_adapter,
                    obligations,
                    request,
                    user,
                    session,
                    qb2_authorized,
                    tool_calls,
                    observations,
                )
                reply = fallback_media_hub_agent_reply(last_tool, last_result)
                break
            if not observations:
                raise HTTPException(status_code=502, detail="影视中枢 Agent 暂时无法回复，请稍后重试。") from exc
            reply = fallback_media_hub_agent_reply(last_tool, last_result)
            break
        if telemetry is not None:
            record_wechat_claw_stage(telemetry, "agent_decision", decision_started)
        if decision.get("decision") == "final":
            missing_obligations = [
                value for value in obligations if not _obligation_satisfied(value, tool_calls)
            ]
            if missing_obligations:
                obligation_violations += 1
                if obligation_violations == 1:
                    observations.append(
                        {
                            "tool": "tool_obligation",
                            "result": {
                                "state": "required",
                                "message": "A factual request is still pending. Call the required tool now.",
                                "obligations": agent_safe_payload(missing_obligations),
                            },
                        }
                    )
                    continue
                deterministic_takeover = True
                last_tool, last_result = execute_required_agent_tools(
                    db,
                    ai_adapter,
                    missing_obligations,
                    request,
                    user,
                    session,
                    qb2_authorized,
                    tool_calls,
                    observations,
                )
                reply = fallback_media_hub_agent_reply(last_tool, last_result)
                break
            reply = (
                fallback_media_hub_agent_reply(last_tool, last_result)
                if observations
                else str(decision.get("reply") or "").strip()
            )
            break
        tool = str(decision.get("tool") or "").strip()
        arguments = decision.get("arguments") if isinstance(decision.get("arguments"), dict) else {}
        safe_arguments = agent_safe_payload(arguments)
        call_signature = json.dumps({"tool": tool, "arguments": safe_arguments}, ensure_ascii=False, sort_keys=True)
        if tool not in AGENT_TOOL_NAMES:
            observations.append({"tool": tool or "unknown", "state": "failed", "error": "Unknown tool. Select one from available_tools."})
            continue
        if call_signature in seen_calls:
            observations.append({"tool": tool, "state": "failed", "error": "The same tool call already ran. Use its observation or change arguments."})
            continue
        seen_calls.add(call_signature)
        tool_started = time.perf_counter()
        try:
            result = execute_media_hub_agent_tool(db, ai_adapter, tool, arguments, request, user, session, qb2_authorized)
        except HTTPException as exc:
            result = {"state": "failed", "error": str(agent_safe_payload(exc.detail))}
        except (MTeamApiError, QbittorrentApiError, TmdbConfigError, AIServiceError, ValueError, TypeError) as exc:
            result = {"state": "failed", "error": str(agent_safe_payload(str(exc)))}
        safe_result = agent_safe_payload(result)
        if telemetry is not None:
            record_wechat_claw_stage(
                telemetry, tool, tool_started,
                status="failed" if result.get("state") == "failed" else "success",
                error=result.get("error") if result.get("state") == "failed" else None,
            )
        tool_calls.append({"tool": tool, "arguments": safe_arguments})
        observations.append({"tool": tool, "result": safe_result})
        remember_assistant_agent_result(session, tool, result, arguments)
        last_tool = tool
        last_result = result
        last_tool, last_result = execute_resolved_media_followup(
            db,
            ai_adapter,
            tool,
            result,
            request,
            user,
            session,
            qb2_authorized,
            tool_calls,
            observations,
        )
        if tool in {"prepare_mteam_download", "confirm_mteam_download"}:
            reply = fallback_media_hub_agent_reply(tool, result)
            break
    missing_obligations = [
        value for value in obligations if not _obligation_satisfied(value, tool_calls)
    ]
    if missing_obligations and not reply:
        deterministic_takeover = True
        enforced_tool, enforced_result = execute_required_agent_tools(
            db,
            ai_adapter,
            missing_obligations,
            request,
            user,
            session,
            qb2_authorized,
            tool_calls,
            observations,
        )
        if enforced_tool:
            last_tool, last_result = enforced_tool, enforced_result
    if len(obligations) > 1 and all(_obligation_satisfied(value, tool_calls) for value in obligations):
        required_tools = [str(value.get("tool") or "") for value in obligations]
        rendered_parts: list[str] = []
        for observation in observations:
            observation_tool = str(observation.get("tool") or "")
            result = observation.get("result") if isinstance(observation.get("result"), dict) else {}
            if observation_tool in required_tools and result:
                part = fallback_media_hub_agent_reply(observation_tool, result)
                if part and part not in rendered_parts:
                    rendered_parts.append(part)
        if rendered_parts:
            reply = "\n\n".join(rendered_parts)
    if not reply:
        reply = fallback_media_hub_agent_reply(last_tool, last_result)
    reply = redact_ai_user_text(redact_payload(reply)).strip()[:6000]
    append_assistant_agent_turn(session, request.message, reply)
    if obligations and all(_obligation_successfully_satisfied(value, observations) for value in obligations):
        session["pending_request"] = {}
    save_assistant_agent_session(db, request, session)
    record_agent_run_trace(
        db,
        request,
        tool_calls,
        observations,
        reply,
        run_started,
        status_value="failed" if last_result.get("state") == "failed" else "success",
        obligations=obligations,
        obligation_violations=obligation_violations,
        deterministic_takeover=deterministic_takeover,
    )
    return {
        "reply": reply,
        "intent": {"intent_type": "agent", "action": "agent", "tools_used": [item["tool"] for item in tool_calls]},
        "result": {"tool_calls": tool_calls, "observations": observations},
        "source": "media_hub_agent",
        "handled_at": utc_iso(),
    }


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
        "updated_at": utc_iso(),
    }
    key = wechat_claw_setting_key(WECHAT_CLAW_LAST_POLL_KEY, user_id)
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key, value=safe_payload)
        db.add(row)
    else:
        row.value = safe_payload
        row.updated_at = utc_now_naive()
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
        updated_qrcode["updated_at"] = int(utc_now().timestamp())
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
        qrcode = {**qrcode, "status": qrcode_status, "updated_at": int(utc_now().timestamp())}
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
                "received_at": utc_iso(),
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
    now = utc_now().timestamp()
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
        next_retry_at=int(utc_now().timestamp()) + delay_seconds,
    )
    return float(delay_seconds)


def agent_request_needs_progress(message: str) -> bool:
    text = str(message or "")
    work_conditions = re.search(r"(?:近\s*\d+\s*年|高分|评分|科幻|喜剧|动作|恐怖|剧情|电影|剧集|找\s*\d+\s*部)", text)
    resource_conditions = re.search(
        r"(?:资源|种子|促销|免费|30%|50%|大小|不超过|做种|馒头|m[-\s]?team|pt\s*站)",
        text,
        re.IGNORECASE,
    )
    return bool(work_conditions and resource_conditions)


def process_wechat_claw_pending_messages(db: Session, adapter: WechatClawAdapter) -> dict[str, Any]:
    state = get_wechat_claw_ilink_state(db)
    pending = get_wechat_claw_pending_messages(state)
    now = utc_now().timestamp()
    handled: list[dict[str, Any]] = []
    reply_sent_count = 0

    for item in pending:
        next_retry_at = float(item.get("next_retry_at") or 0)
        if next_retry_at > now:
            continue
        pending_id = str(item["id"])
        telemetry = new_wechat_claw_interaction_telemetry()
        interaction_trace_id = str(item.get("interaction_trace_id") or "")
        interaction_status = "completed"
        try:
            reply = str(item.get("reply") or "").strip()
            if not reply:
                if not item.get("progress_sent") and agent_request_needs_progress(str(item.get("message") or "")):
                    progress = adapter.send_text(
                        str(item.get("user_id") or ""),
                        "我正在筛选符合条件的作品，并逐一核验 M-Team 资源，查完后把完整结果发给你。",
                        str(item.get("context_token") or ""),
                    )
                    if progress.get("sent"):
                        update_wechat_claw_pending_message(db, pending_id, progress_sent=True)
                response = handle_wechat_claw_text(
                    db,
                    WechatClawMessageRequest(
                        message=str(item.get("message") or ""),
                        user_id=str(item.get("user_id") or ""),
                        conversation_id=str(item.get("conversation_id") or ""),
                        context_token=str(item.get("context_token") or ""),
                    ),
                    telemetry,
                )
                reply = str(response.get("reply") or "").strip()
                if not reply:
                    raise RuntimeError("empty AI reply")
                interaction = response.get("interaction") if isinstance(response.get("interaction"), dict) else {}
                interaction_trace_id = str(interaction.get("trace_id") or "")
                interaction_status = str(interaction.get("status") or "completed")
                # Persist the generated reply before delivery. A transient send failure
                # must retry the same answer rather than execute the user intent twice.
                update_wechat_claw_pending_message(
                    db,
                    pending_id,
                    reply=reply,
                    interaction_trace_id=interaction_trace_id,
                    handled_at=utc_iso(),
                )
            delivery_started = time.perf_counter()
            delivery = adapter.send_text(str(item.get("user_id") or ""), reply, str(item.get("context_token") or ""))
            record_wechat_claw_stage(
                telemetry,
                "final_reply_send",
                delivery_started,
                status="success" if delivery.get("sent") else "failed",
                error=None if delivery.get("sent") else delivery.get("message") or delivery.get("reason"),
            )
            if not delivery.get("sent"):
                reason = str(delivery.get("message") or delivery.get("reason") or "send rejected")
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="delivery_failed", error=reason)
                retry_wechat_claw_pending_message(db, item, reason)
                handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": reason})
                continue
        except HTTPException as exc:
            failure = str(exc.detail)
            if interaction_trace_id:
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="failed", error=failure)
            else:
                failed_interaction = record_wechat_claw_interaction(
                    db,
                    WechatClawMessageRequest(message=str(item.get("message") or ""), user_id=str(item.get("user_id") or ""), conversation_id=str(item.get("conversation_id") or ""), context_token=str(item.get("context_token") or "")),
                    {"action": "interaction_failed", "intent_type": "interaction_failed"},
                    {"stage": (telemetry.get("stages") or [{}])[-1].get("stage", "unknown")},
                    "",
                    telemetry,
                    status="failed",
                )
                update_wechat_claw_pending_message(db, pending_id, interaction_trace_id=failed_interaction.get("trace_id"))
            retry_wechat_claw_pending_message(db, item, str(exc.detail))
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": str(exc.detail)})
            continue
        except Exception as exc:
            logger.exception("WeChat claw message handling failed: user_id=%s", item.get("user_id"))
            error = str(exc)[:240]
            if interaction_trace_id:
                update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status="failed", error=error)
            else:
                failed_interaction = record_wechat_claw_interaction(
                    db,
                    WechatClawMessageRequest(message=str(item.get("message") or ""), user_id=str(item.get("user_id") or ""), conversation_id=str(item.get("conversation_id") or ""), context_token=str(item.get("context_token") or "")),
                    {"action": "interaction_failed", "intent_type": "interaction_failed"},
                    {"stage": (telemetry.get("stages") or [{}])[-1].get("stage", "unknown")},
                    "",
                    telemetry,
                    status="failed",
                )
                update_wechat_claw_pending_message(db, pending_id, interaction_trace_id=failed_interaction.get("trace_id"))
            retry_wechat_claw_pending_message(db, item, error)
            handled.append({"user_id": item.get("user_id"), "message_id": item.get("conversation_id"), "reply_sent": False, "error": error})
            continue

        remove_wechat_claw_pending_message(db, pending_id)
        update_wechat_claw_interaction_delivery(db, interaction_trace_id, telemetry, status=interaction_status)
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


def build_wechat_claw_setup(db: Session, binding_id: int | None = None) -> dict[str, Any]:
    row = get_config(db, "wechat_claw")
    config = get_decrypted_config(db, "wechat_claw") or {}
    bindings = ensure_wechat_claw_bindings(db)
    binding = get_wechat_claw_binding_or_error(db, binding_id) if binding_id is not None else (bindings[0] if bindings else None)
    state = get_wechat_claw_ilink_state(db, binding.id if binding else None)
    qrcode = state.get("qrcode") if isinstance(state.get("qrcode"), dict) else {}
    qr_payload = wechat_claw_qr_payload({**config, **state, "qrcode": qrcode})
    return {
        "configured": bool(row and row.encrypted_payload),
        "enabled": bool(row and row.enabled),
        "binding": serialize_wechat_claw_binding(binding) if binding else None,
        "bindings": [serialize_wechat_claw_binding(item) for item in bindings],
        "mode": str(config.get("mode") or "ilink"),
        "connected": bool(state.get("bot_token")),
        "account_id": state.get("account_id"),
        "base_url": state.get("base_url") or config.get("base_url") or "https://ilinkai.weixin.qq.com",
        "config_version": row.config_version if row else 0,
        "last_test_result": public_test_result(row.last_test_result) if row else None,
        "qr_payload": qr_payload,
        "qr_text": qrcode.get("qrcode_url") or json.dumps(qr_payload, ensure_ascii=False, separators=(",", ":")),
        "qrcode": qrcode,
        "known_targets": state.get("known_targets") or {},
        "mobile_sections": WECHAT_CLAW_MOBILE_SECTIONS,
        "capabilities": WECHAT_CLAW_CAPABILITIES,
        "recent_interactions": get_wechat_claw_recent_interactions(db, binding.id if binding else None),
        "last_poll": get_wechat_claw_last_poll(db, binding.id if binding else None),
    }


def record_wechat_claw_interaction(
    db: Session,
    request: WechatClawMessageRequest,
    intent: dict[str, Any],
    result: dict[str, Any],
    reply: str,
    telemetry: dict[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    if _WECHAT_CLAW_ACTIVE_USER_ID.get() is None:
        bindings = ensure_wechat_claw_bindings(db)
        if bindings:
            scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(bindings[0].id)
            try:
                return record_wechat_claw_interaction(db, request, intent, result, reply, telemetry, status)
            finally:
                _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
    action = str(intent.get("action") or "")
    target = str(intent.get("target") or intent.get("downloader_id") or result.get("target") or "")
    diagnostics = serialize_wechat_claw_interaction_telemetry(telemetry)
    item = {
        "user_id": trim_wechat_claw_text(request.user_id or "unknown", 120),
        "conversation_id": trim_wechat_claw_text(request.conversation_id, 120),
        "message": trim_wechat_claw_text(redact_wechat_claw_message(request.message), 240),
        "reply": trim_wechat_claw_text(reply, 360),
        "action": action,
        "target": target,
        "status": status,
        **diagnostics,
        "created_at": utc_iso(),
    }
    current = get_wechat_claw_recent_interactions(db)
    interaction_key = wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY)
    row = db.query(Setting).filter(Setting.key == interaction_key).one_or_none()
    value = {"items": [item, *current][:WECHAT_CLAW_INTERACTION_LIMIT]}
    if row is None:
        row = Setting(key=interaction_key, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = utc_now_naive()
    db.commit()
    state = get_wechat_claw_ilink_state(db)
    known_targets = state.get("known_targets") if isinstance(state.get("known_targets"), dict) else {}
    if item["user_id"] and item["user_id"] != "unknown":
        known_targets[item["user_id"]] = {
            "username": item["user_id"],
            "last_active": int(utc_now().timestamp()),
            "context_token": request.context_token,
        }
        update_wechat_claw_ilink_state(db, known_targets=known_targets)
    return item


def update_wechat_claw_interaction_delivery(
    db: Session,
    trace_id_value: str,
    telemetry: dict[str, Any],
    *,
    status: str,
    error: Any = None,
) -> None:
    if not trace_id_value:
        return
    row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(WECHAT_CLAW_INTERACTIONS_KEY)).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    items = value.get("items") if isinstance(value.get("items"), list) else []
    diagnostics = serialize_wechat_claw_interaction_telemetry(telemetry)
    for item in items:
        if not isinstance(item, dict) or item.get("trace_id") != trace_id_value:
            continue
        existing_stages = item.get("stages") if isinstance(item.get("stages"), list) else []
        incoming_stages = diagnostics.get("stages") if isinstance(diagnostics.get("stages"), list) else []
        existing_names = [str(stage.get("stage") or "") for stage in existing_stages if isinstance(stage, dict)]
        incoming_names = [str(stage.get("stage") or "") for stage in incoming_stages if isinstance(stage, dict)]
        if existing_names and incoming_names[: len(existing_names)] != existing_names:
            diagnostics["stages"] = [*existing_stages, *incoming_stages][-12:]
        item.update({"status": status, **diagnostics})
        if error:
            item["error"] = trim_wechat_claw_text(redact_payload(str(error)), 240)
        break
    else:
        return
    row.value = {"items": items[:WECHAT_CLAW_INTERACTION_LIMIT]}
    row.updated_at = utc_now_naive()
    db.commit()


def wechat_claw_notification_title(notification: Notification, action: str, role_name: str) -> str:
    is_qb_download = notification.source == "qb_task_monitor" and bool(
        re.fullmatch(r"qb[123]_download_(?:started|completed)", str(action or ""))
    )
    if is_qb_download or not role_name:
        return notification.title
    return f"【{role_name}】{notification.title}"


def push_wechat_claw_notification(db: Session, notification: Notification, action: str) -> dict[str, Any]:
    preferences = get_notification_preferences(db)
    if not preferences.get("wechat_claw_push", True):
        return {"sent": False, "reason": "disabled_by_preferences"}
    # App-originated download events have no active chat scope. Fan them out to
    # every independently bound member instead of the legacy administrator bot.
    if _WECHAT_CLAW_ACTIVE_USER_ID.get() is None:
        binding_ids = list_wechat_claw_binding_user_ids(db)
        if binding_ids:
            deliveries: dict[str, dict[str, Any]] = {}
            for binding_id in binding_ids:
                scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
                try:
                    deliveries[str(binding_id)] = push_wechat_claw_notification(db, notification, action)
                finally:
                    _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)
            return {"sent": any(item.get("sent") for item in deliveries.values()), "deliveries": deliveries}
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get()
    role_name = ""
    if binding_id is not None:
        binding = get_wechat_claw_binding_or_error(db, binding_id)
        if not binding.enabled:
            return {"sent": False, "reason": "binding_disabled"}
        if not normalized_wechat_claw_binding_preferences(binding.notification_preferences).get(action, False):
            return {"sent": False, "reason": "disabled_by_binding_preferences"}
        role_name = binding.role_name.strip()
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
                "title": wechat_claw_notification_title(notification, action, role_name),
                "message": notification.message,
                "level": notification.level,
                "source": notification.source,
                "notification_id": notification.id,
                "created_at": utc_iso(notification.created_at) if notification.created_at else None,
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
        if result.get("mobile_selection"):
            lines.extend(["", "选择下载", "回复“下载 1”即可将对应资源推送到默认下载器。"])
        return "\n".join(lines)
    if action == "mobile_download":
        if result.get("selection_expired"):
            return "【下载选择已过期】\n请重新搜索资源，然后回复“下载 1”选择需要的条目。"
        if result.get("selection_missing"):
            return "【未找到可下载资源】\n请先搜索资源，再回复“下载 1”选择资源。"
        if result.get("accepted"):
            return f"【已添加下载】\n{result.get('title') or '已选资源'} 已推送到 {result.get('downloader_id')}。"
        return f"【添加下载失败】\n{result.get('message') or '请检查默认下载器和 M-Team 配置后重试。'}"
    if action in {"download_started", "download_completed"}:
        label = "下载开始" if action == "download_started" else "下载完成"
        notification = result.get("notification") or {}
        if result.get("notification_skipped"):
            return f"【{label}】\n已按通知偏好跳过消息记录。"
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


def tmdb_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def tmdb_title_label(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "未命名").strip()
    original_title = str(item.get("original_title") or "").strip()
    return f"{title} / {original_title}" if original_title and original_title != title else title


def tmdb_runtime_progress_label(item: dict[str, Any]) -> str:
    runtime = tmdb_positive_int(item.get("runtime"))
    if item.get("media_type") != "tv":
        return f"{runtime} 分钟" if runtime else "-"

    parts = [f"每集 {runtime} 分钟"] if runtime else []
    seasons = tmdb_positive_int(item.get("number_of_seasons"))
    episodes = tmdb_positive_int(item.get("number_of_episodes"))
    if seasons:
        parts.append(f"共 {seasons} 季")
    if episodes:
        parts.append(f"{episodes} 集")
    last_episode = item.get("last_episode_to_air") if isinstance(item.get("last_episode_to_air"), dict) else {}
    last_season = tmdb_positive_int(last_episode.get("season_number"))
    last_number = tmdb_positive_int(last_episode.get("episode_number"))
    if last_season and last_number:
        parts.append(f"更新至 S{last_season}E{last_number}")
    else:
        latest_season = item.get("latest_season") if isinstance(item.get("latest_season"), dict) else {}
        latest_number = tmdb_positive_int(latest_season.get("season_number"))
        if latest_number:
            latest_episodes = tmdb_positive_int(latest_season.get("episode_count"))
            suffix = f"（{latest_episodes} 集）" if latest_episodes else ""
            parts.append(f"更新至第 {latest_number} 季{suffix}")
    return " · ".join(parts) or "-"


def latest_mteam_traffic_label(mteam: dict[str, Any], dimension: str, field: str = "upload_total") -> str:
    traffic_series = mteam.get("traffic_series") if isinstance(mteam.get("traffic_series"), dict) else {}
    points = traffic_series.get(dimension) if isinstance(traffic_series.get(dimension), list) else []
    if not points:
        return "暂无快照"
    current_label = traffic_period_label(traffic_period_start(utc_now(), dimension), dimension)
    labeled_points = [point for point in points if isinstance(point, dict) and point.get("label")]
    if labeled_points:
        latest = next((point for point in reversed(labeled_points) if point.get("label") == current_label), None)
        if latest is None:
            return "暂无快照"
    else:
        latest = points[-1] if isinstance(points[-1], dict) else {}
    return bytes_label(float(latest.get(field) or 0), 2)


def latest_mteam_upload_label(mteam: dict[str, Any], dimension: str) -> str:
    return latest_mteam_traffic_label(mteam, dimension, "upload_total")


def mobile_dashboard_issues(overview: dict[str, Any], mteam: dict[str, Any], qbs: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    if not overview.get("nas_storage_readable"):
        issues.append("NAS 存储不可访问")
    if mteam and str(mteam.get("user_level") or "") in {"", "-"}:
        issues.append("M-Team 数据不可用")
    for qb in qbs:
        if not isinstance(qb, dict) or not qb.get("enabled"):
            continue
        name = str(qb.get("name") or downloader_label(str(qb.get("id") or "qB")))
        if not qb.get("online"):
            issues.append(f"{name} 离线")
        elif int(qb.get("errors") or 0) > 0:
            issues.append(f"{name} 有 {int(qb.get('errors') or 0)} 项错误")
    return issues


def mobile_dashboard_qb_summary(qbs: list[dict[str, Any]]) -> str:
    labels = []
    for qb in qbs:
        if not isinstance(qb, dict) or not qb.get("enabled"):
            continue
        name = str(qb.get("name") or downloader_label(str(qb.get("id") or "qB")))
        labels.append(f"{name}{'在线' if qb.get('online') else '离线'}")
    return " · ".join(labels) or "暂无已启用下载器"


def mteam_number_label(value: Any, digits: int = 0) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:,.{digits}f}"


def mteam_duration_label(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "-"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = [f"{days} 天"] if days else []
    if hours or parts:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts) or "0 分钟"


def mteam_metric_with_delta(value: str, delta: Any, window: str) -> str:
    delta_text = str(delta or "").strip()
    return f"{value}（{window} {delta_text}）" if delta_text else value


def format_mteam_station_reply(mteam: dict[str, Any]) -> str:
    if str(mteam.get("user_level") or "") in {"", "-"}:
        return f"M-Team 数据暂不可用。{str(mteam.get('message') or '').strip()}".strip()

    window = str(mteam.get("delta_window_label") or "近5h")
    account_status = ["VIP" if mteam.get("vip") else "非 VIP", "允许下载" if mteam.get("allow_download", True) else "禁止下载", "有警告" if mteam.get("warned") else "无警告"]
    source = str(mteam.get("source") or "M-Team 站点数据").strip()
    updated_at = format_system_datetime(mteam.get("captured_at") or mteam.get("updated_at"))
    active_delta = str(mteam.get("active_delta_label") or "").strip()
    active_delta_suffix = f"（{window} {active_delta}）" if active_delta else ""
    lines = [
        "M-Team 站点数据",
        "",
        "账号状态",
        f"- 等级：{mteam.get('user_level') or '-'}",
        f"- 注册：{mteam.get('joined_at') or '-'} · {' · '.join(account_status)}",
        "",
        "流量与收益",
        f"- 累计上传：{mteam_metric_with_delta(bytes_label(float(mteam.get('upload_total') or 0), 2), mteam.get('upload_delta_label'), window)}",
        f"- 累计下载：{mteam_metric_with_delta(bytes_label(float(mteam.get('download_total') or 0), 2), mteam.get('download_delta_label'), window)}",
        f"- 分享率：{mteam_metric_with_delta(mteam_number_label(mteam.get('ratio'), 3), mteam.get('ratio_delta_label'), window)}",
        f"- 魔力值：{mteam_metric_with_delta(mteam_number_label(mteam.get('bonus'), 1), mteam.get('bonus_delta_label'), window)}",
        "",
        "做种与活动",
        f"- 做种：{mteam_number_label(mteam.get('seed_count'))} 个 · {mteam_metric_with_delta(bytes_label(float(mteam.get('seed_size') or 0), 2), mteam.get('seed_size_delta_label'), window)}",
        f"- 当前活跃：上传 {mteam.get('active_uploads', 0)} · 下载 {mteam.get('active_downloads', 0)}{active_delta_suffix}",
        f"- 做种时长：{mteam_duration_label(mteam.get('seedtime_seconds'))} · 下载时长：{mteam_duration_label(mteam.get('leechtime_seconds'))}",
        "",
        "本地快照流量",
        f"- 近一小时：上传 {latest_mteam_traffic_label(mteam, 'hour')} · 下载 {latest_mteam_traffic_label(mteam, 'hour', 'download_total')}",
        f"- 今日：上传 {latest_mteam_traffic_label(mteam, 'day')} · 下载 {latest_mteam_traffic_label(mteam, 'day', 'download_total')}",
        f"- 本周：上传 {latest_mteam_traffic_label(mteam, 'week')} · 下载 {latest_mteam_traffic_label(mteam, 'week', 'download_total')}",
        f"- 本月：上传 {latest_mteam_traffic_label(mteam, 'month')} · 下载 {latest_mteam_traffic_label(mteam, 'month', 'download_total')}",
    ]
    if mteam.get("live_query") is False:
        age_seconds = int(mteam.get("historical_age_seconds") or 0)
        age_label = f"{age_seconds // 3600} 小时前" if age_seconds >= 3600 else f"{max(1, age_seconds // 60)} 分钟前"
        lines[0:0] = [
            "这次没能完成 M-Team 实时查询，下面是最后一次成功采集的历史数据。",
            f"历史数据时间：{updated_at or '-'}（约 {age_label}）",
            "",
        ]
    if updated_at:
        label = "历史采集时间" if mteam.get("live_query") is False else "查询时间"
        lines.extend(["", f"{label}：{updated_at} · 来源：{source}"])
        if mteam.get("live_query") is not False:
            lines.append(f"数据更新：{updated_at}")
    return "\n".join(lines)


def format_mobile_agent_reply(intent: dict[str, Any], result: dict[str, Any]) -> str:
    intent_type = str(intent.get("intent_type") or intent.get("action") or "")
    if intent_type == "tmdb_lookup":
        if result.get("state") == "failed":
            error = trim_wechat_claw_text(result.get("error"), 300)
            if not error.startswith("TMDB 查询失败"):
                error = f"TMDB 查询失败：{error}"
            return f"{error}\n请在设置中测试 TMDB 连通性后重试。"
        items = result.get("items") or []
        if not items:
            return "没有找到符合条件的作品。可以放宽地区、评分或年份条件后重试。"
        display_limit = min(5, len(items))
        lines = [f"找到 **{len(items)} 部作品**，以下展示前 **{display_limit}** 部。"]
        for index, item in enumerate(items[:display_limit], 1):
            genres = mteam_markdown_cell(" / ".join(item.get("genres") or []) or "-", 48)
            countries = mteam_markdown_cell(" / ".join(item.get("production_countries") or []) or "-", 40)
            director = mteam_markdown_cell(item.get("director") or "-", 64)
            cast = mteam_markdown_cell(" / ".join(item.get("cast") or []) or "-", 72)
            overview = mteam_markdown_cell(item.get("overview") or "暂无简介", 140)
            title = mteam_markdown_cell(tmdb_title_label(item), 72)
            media_type = "电视剧" if item.get("media_type") == "tv" else "电影"
            lines.extend([
                "",
                f"- **{index}. {title}**",
                f"  - 分类：{media_type} · 类型：{genres}",
                f"  - 年份：{mteam_markdown_cell(item.get('year') or '-', 12)} · 评分：{mteam_markdown_cell(item.get('rating') or '-', 12)} · 国籍：{countries}",
                f"  - 时长 / 季集：{mteam_markdown_cell(tmdb_runtime_progress_label(item), 84)}",
                f"  - 导演 / 主创：{director}",
                f"  - 主演：{cast}",
                f"  - 简介：{overview}",
            ])
        return "\n".join(lines)
    if intent_type == "mteam_search":
        if result.get("state") == "selection_expired":
            return "刚才那份 M-Team 结果已经超过可继续选择的时间了。请重新搜索一次，我会按同样的表格为你列出最新结果。"
        items = result.get("items") or []
        if not items:
            return "【M-Team 资源搜索】\n没有找到匹配资源。建议换用片名、英文名或降低筛选条件。"
        total_count = int(result.get("count") or len(items))
        display_limit = total_count if result.get("show_all") else min(MTEAM_INITIAL_DISPLAY_LIMIT, total_count)
        displayed = items[:display_limit]
        presentation_map = result.get("presentation") if isinstance(result.get("presentation"), dict) else {}
        if result.get("show_all"):
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下为完整列表："]
        elif total_count > display_limit:
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下展示综合排名前 **{display_limit}** 个。回复“查看全部”可获取完整列表："]
        else:
            lines = [f"在馒头站找到 **{total_count} 个资源**，以下为全部结果："]
        lines.extend(["", "| # | 标题 | 中文信息 | 清晰度 | 大小 | 做种 | 促销 |", "|---|------|----------|--------|------|------|------|"])
        for index, item in enumerate(displayed, 1):
            promotion = item.get("promotion_label") or "普通"
            presentation = item.get("presentation") if isinstance(item.get("presentation"), dict) else presentation_map.get(str(item.get("id") or ""), _mteam_fallback_row(item, index))
            title = mteam_markdown_cell(presentation.get("title") or _mteam_fallback_row(item, index)["title"], 180)
            if item.get("is_official"):
                title = f"{title}【官种】"
            chinese_info = mteam_markdown_cell(presentation.get("chinese_info") or _mteam_fallback_row(item, index)["chinese_info"], 120)
            quality = mteam_markdown_cell(presentation.get("quality") or _mteam_fallback_row(item, index)["quality"], 72)
            size = mteam_markdown_cell(presentation.get("size") or item.get("size"), 16)
            seeders = mteam_markdown_cell(presentation.get("seeders") or item.get("seeders"), 12)
            promotion = presentation.get("promotion") or promotion
            if index == result.get("recommended_index"):
                size = f"**{size}**"
                seeders = f"**{seeders}**"
            lines.append(f"| {index} | {title} | {chinese_info} | {quality} | {size} | {seeders} | {mteam_markdown_cell(promotion, 18)} |")
        if result.get("recommended_index"):
            lines.extend(["", f"**{result.get('recommendation_note') or mteam_default_recommendation(items, result.get('recommended_index'))}**"])
        if result.get("complete") is False:
            lines.extend(["", "我先列出已经核验过的结果。M-Team 还有一部分候选没能查完，所以符合条件的资源可能不止这些。"])
        if result.get("show_all"):
            lines.extend(["", "需要下载哪个资源？回复“第 X 个”或“下载推荐的那个”，我会先展示下载摘要，再等待确认。"])
        else:
            lines.extend(["", "需要下载哪个资源？回复“第 X 个”或“下载推荐的那个”；想看完整列表可回复“查看全部”。"])
        queried_at = format_system_datetime(result.get("queried_at"))
        if queried_at:
            lines.extend(["", f"来源：M-Team API · 查询时间：{queried_at}"])
        return "\n".join(lines)
    if intent_type == "dashboard_query":
        overview = result.get("overview") or {}
        if overview:
            mteam = result.get("mteam") or {}
            qbs = result.get("qbs") if isinstance(result.get("qbs"), list) else []
            issues = mobile_dashboard_issues(overview, mteam, qbs)
            status = "需要关注" if issues else "运行正常"
            summary = f"发现 {len(issues)} 项需要处理，请查看下方提醒。" if issues else "上传、存储与已启用下载器均处于正常状态。"
            ratio = mteam.get("ratio") if mteam.get("ratio") is not None else "-"
            bonus = mteam.get("bonus") if mteam.get("bonus") is not None else "-"
            bonus_delta = str(mteam.get("bonus_delta_label") or "").strip()
            bonus_window = str(mteam.get("delta_window_label") or "近5h")
            bonus_label = f"{bonus}（{bonus_window} {bonus_delta}）" if bonus_delta else str(bonus)
            nas_usage = overview.get("nas_usage_percent")
            nas_label = overview.get("nas_space_label") or overview.get("nas_free_space_label") or "-"
            nas_detail = f"{nas_label}（已用 {nas_usage}%）" if isinstance(nas_usage, (int, float)) else nas_label
            lines = [
                f"仪表盘状态：{status}",
                summary,
                "",
                "核心指标",
                f"- 当前上传：{bytes_label(overview.get('total_upload_speed') or 0)}/s",
                f"- 今日上传：{latest_mteam_upload_label(mteam, 'day')} · 近一小时：{latest_mteam_upload_label(mteam, 'hour')}",
                f"- 本周上传：{latest_mteam_upload_label(mteam, 'week')} · 本月上传：{latest_mteam_upload_label(mteam, 'month')}",
                f"- 分享率：{ratio} · 魔力值：{bonus_label}",
                "",
                "运行概览",
                f"- 活跃任务：下载 {overview.get('download_tasks', 0)} · 上传 {overview.get('upload_tasks', 0)}",
                f"- NAS：{nas_detail}",
                f"- 下载器：{mobile_dashboard_qb_summary(qbs)}",
            ]
            if issues:
                lines.extend(["", "需要关注", *[f"- {issue}" for issue in issues]])
            return "\n".join(lines)
        mteam = result.get("mteam") or {}
        if mteam and not result.get("nas") and not any(result.get(downloader_id) for downloader_id in ("qb1", "qb2", "qb3")):
            return format_mteam_station_reply(mteam)
        lines: list[str] = []
        if mteam:
            lines.extend(["M-Team", f"等级 {mteam.get('user_level') or '-'} · 分享率 {mteam.get('ratio') or 0} · 做种 {mteam.get('seed_count') or 0}"])
        nas = result.get("nas") or {}
        if nas:
            lines.extend(["NAS 存储", f"{nas.get('nas_space_label') or nas.get('nas_free_space_label') or '-'} · {nas.get('nas_space_helper') or '状态未知'}"])
        for downloader_id in ("qb1", "qb2", "qb3"):
            qb = result.get(downloader_id) or {}
            if qb:
                lines.extend([downloader_label(downloader_id), f"下载 {bytes_label(qb.get('download_speed') or 0)}/s · 上传 {bytes_label(qb.get('upload_speed') or 0)}/s · 任务 {qb.get('active_downloads') or 0}"])
                if downloader_id == "qb2" and isinstance(qb.get("tasks"), list):
                    lines.extend(f"- {item.get('name') or '-'} · {item.get('progress', 0)}%" for item in qb["tasks"][:5])
        if result.get("qb2_privacy_required"):
            lines.extend(["qB2 隐私详情", "请先发送“验证管理员密码：你的密码”，验证成功后 15 分钟内可查询详情。"])
        if result.get("stats"):
            lines.extend(["统计", f"可用区间：{', '.join(result['stats'].get('ranges') or [])}"])
        if result.get("diagnostics"):
            failed = [item for item in result["diagnostics"].get("modules") or [] if item.get("status") == "failed"]
            lines.extend(["诊断", f"异常模块：{len(failed)}"])
        return "\n".join(lines) or "没有可展示的仪表盘数据。"
    if intent_type == "download_selected":
        state = str(result.get("state") or "")
        candidate = result.get("candidate") or {}
        presentation = candidate.get("presentation") if isinstance(candidate.get("presentation"), dict) else {}
        title = presentation.get("title") or candidate.get("title") or result.get("title") or "该资源"
        quality = presentation.get("quality") or " · ".join(
            value for value in [
                str(candidate.get("resolution") or "").strip(),
                str(candidate.get("codec") or "").strip(),
                str(candidate.get("hdr") or "").strip(),
                str(candidate.get("group") or "").strip(),
            ] if value
        )
        promotion = candidate.get("promotion_label") or "普通"
        if state == "awaiting_confirmation":
            official = " · 官种" if candidate.get("is_official") else ""
            return (
                "【下载确认】\n"
                f"作品：{title}\n"
                f"资源：{candidate.get('title') or '-'}\n"
                f"中文信息：{presentation.get('chinese_info') or candidate.get('subtitle') or '未标注'}\n"
                f"清晰度：{quality or '-'}\n"
                f"大小：{candidate.get('size') or '-'} · 做种：{candidate.get('seeders') or 0}\n"
                f"促销：{promotion}{official}\n"
                "回复“确认”“就它”或“开始下载”即可。"
            )
        if state == "started":
            version = " · ".join(value for value in [quality, str(candidate.get("size") or "").strip()] if value)
            suffix = f" · {version}" if version else ""
            return f"【已开始下载】\n{title}{suffix} 已开始下载。"
        if state in {"pending_verification", "accepted"}:
            return f"【正在确认下载】\n{title}\n下载任务正在核验中，确认开始后我会通知你。"
        if state == "conditions_changed":
            return "刚刚复核时发现，这个资源已经不再满足你设定的下载条件。为了避免下载到不符合预期的版本，我没有开始下载。你可以重新选择一个资源，或者告诉我哪些条件可以放宽。"
        if state == "resource_unavailable" or state == "unavailable":
            return "刚刚复核时发现，这个资源目前已经不可用，所以没有开始下载。我可以继续帮你寻找符合原有条件的其他资源。"
        if state == "cancelled":
            return "好的，这次下载已经取消。"
        if state == "selection_missing":
            return "我这边已经找不到刚才待确认的资源了。请重新选择一个资源，我会接着帮你处理。"
        return f"【添加下载失败】\n{result.get('message') or '请检查默认下载器、M-Team 和 qB 配置。'}"
    return "【影视中枢 Agent】\n请告诉我想查的电影、资源、仪表盘信息，或聊聊你的观影计划。"


def mobile_agent_history(db: Session, request: WechatClawMessageRequest) -> list[dict[str, str]]:
    user_id = str(request.user_id or "").strip()
    history: list[dict[str, str]] = []
    for item in reversed(get_wechat_claw_recent_interactions(db)):
        if user_id and str(item.get("user_id") or "") != user_id:
            continue
        history.extend([{"role": "user", "content": str(item.get("message") or "")}, {"role": "assistant", "content": str(item.get("reply") or "")}])
    return history[-10:]


def infer_mobile_selection(message: str) -> int:
    normalized = re.sub(r"\s+", "", str(message or "").lower())
    if "推荐" in normalized:
        return -1
    for phrase, index in WECHAT_CLAW_SELECTION_WORDS.items():
        if phrase in normalized:
            return index
    command_match = WECHAT_CLAW_DOWNLOAD_SELECTION_RE.match(normalized)
    if command_match:
        return int(command_match.group(1))
    match = re.search(r"(?:第)?(\d+)(?:个|部|项)?", normalized)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= WECHAT_CLAW_MOBILE_SELECTION_LIMIT else 0


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
        "created_at": utc_iso(row.created_at),
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
                "last_test_result": public_test_result(row.last_test_result) if row else None,
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
        return qb_task_monitoring_response(action)

    raise HTTPException(status_code=400, detail="暂不支持这个操作。")


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
        "captured_at": None,
        "checked_at": None,
        "stale": False,
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


def search_mteam_with_budget(adapter: Any, query: str, max_pages: int = 5) -> list[dict[str, Any]]:
    try:
        return adapter.search_torrents(query, {"page_size": 20, "max_pages": max_pages})
    except TypeError:
        return adapter.search_torrents(query)


def qb_webui_url_from_config(config: dict[str, Any] | None) -> str | None:
    value = str((config or {}).get("base_url") or "").strip().rstrip("/")
    return value if value.lower().startswith(("http://", "https://")) else None


def qb_webui_target(db: Session, downloader_id: str, row: IntegrationConfig | None = None) -> dict[str, Any]:
    config_row = row if row is not None else get_config(db, downloader_id)
    configured = bool(config_row and config_row.encrypted_payload)
    if configured:
        config, configuration_unreadable = decode_saved_payload(config_row.encrypted_payload)
    else:
        config, configuration_unreadable = {}, False
    index = downloader_id.replace("qb", "")
    name = (config or {}).get("name") or ((config_row.redacted_summary or {}).get("name") if config_row else "") or f"qB{index}"
    return {
        "id": downloader_id,
        "name": str(name),
        "configured": configured,
        "enabled": bool(config_row and config_row.enabled),
        "webui_url": qb_webui_url_from_config(config),
        "configuration_unreadable": configuration_unreadable,
    }


def qb_placeholder_state(db: Session, downloader_id: str, row: IntegrationConfig | None, message: str | None = None) -> dict[str, Any]:
    target = qb_webui_target(db, downloader_id, row)
    return qb_placeholder_state_from_meta(
        downloader_id,
        target["configured"],
        target["enabled"],
        target["name"],
        message,
        webui_url=target["webui_url"],
    )


def qb_placeholder_state_from_meta(downloader_id: str, configured: bool, enabled: bool, name: str | None = None, message: str | None = None, webui_url: str | None = None) -> dict[str, Any]:
    index = downloader_id.replace("qb", "")
    checked_at = utc_iso()
    return {
        "id": downloader_id,
        "name": name or f"qB{index}",
        "webui_url": webui_url,
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
        "captured_at": None,
        "checked_at": checked_at,
        "stale": False,
        "updated_at": None,
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
        return qb_placeholder_state(db, downloader_id, row)
    cache = get_preload_cache(db, "dashboard")
    payload = cache.get("payload") if cache and isinstance(cache.get("payload"), dict) else {}
    state = next(
        (item for item in payload.get("qbs") or [] if isinstance(item, dict) and item.get("id") == downloader_id),
        None,
    )
    return dict(state) if state else qb_placeholder_state(db, downloader_id, row, "后台采集尚未生成可用快照。")


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
                errors.append({"path": raw_path, "message": "未挂载或容器无法访问"})
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
            "nas_space_helper": "请先配置 NAS 存储路径，以显示空间使用情况。",
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


def verified_nas_storage_setup_status(db: Session) -> dict[str, Any] | None:
    row = db.query(Setting).filter(Setting.key == NAS_STORAGE_SETUP_STATUS_KEY).one_or_none()
    value = row.value if row and isinstance(row.value, dict) else {}
    if not value.get("verified"):
        return None
    paths = [str(path) for path in value.get("paths") or [] if str(path).strip()]
    return {
        "nas_free_space": float(value.get("nas_free_space") or 0),
        "nas_total_space": float(value.get("nas_total_space") or 0),
        "nas_used_space": float(value.get("nas_used_space") or 0),
        "nas_usage_percent": value.get("nas_usage_percent"),
        "nas_free_space_label": str(value.get("nas_free_space_label") or "-"),
        "nas_free_space_source": str(value.get("nas_free_space_source") or ""),
        "nas_space_label": str(value.get("nas_space_label") or "-"),
        "nas_space_helper": str(value.get("nas_space_helper") or "已验证 NAS 存储挂载"),
        "nas_storage_configured": True,
        "nas_storage_readable": True,
        "nas_storage_paths": paths,
        "nas_storage_detected_paths": paths,
        "nas_storage_pool_count": int(value.get("pool_count") or 0),
        "nas_storage_folder_count": int(value.get("folder_count") or len(paths)),
        "nas_storage_errors": [],
        "nas_storage_summary_label": "NAS 存储挂载已验证",
        "nas_storage_setup_verified": True,
        "nas_storage_verified_at": value.get("verified_at"),
        "nas_storage_check_source": "saved",
    }


def storage_status_from_setup_check(db: Session, refresh: bool = False) -> dict[str, Any]:
    if not refresh:
        verified = verified_nas_storage_setup_status(db)
        if verified:
            return verified

    status = nas_storage_from_configs(db)
    success = bool(status.get("nas_storage_readable"))
    row = db.query(Setting).filter(Setting.key == NAS_STORAGE_SETUP_STATUS_KEY).one_or_none()
    if success:
        value = {
            "verified": True,
            "verified_at": utc_iso(),
            "paths": status.get("nas_storage_detected_paths") or [],
            "pool_count": status.get("nas_storage_pool_count") or 0,
            "folder_count": status.get("nas_storage_folder_count") or 0,
            "nas_free_space": status.get("nas_free_space") or 0,
            "nas_total_space": status.get("nas_total_space") or 0,
            "nas_used_space": status.get("nas_used_space") or 0,
            "nas_usage_percent": status.get("nas_usage_percent"),
            "nas_free_space_label": status.get("nas_free_space_label") or "-",
            "nas_free_space_source": status.get("nas_free_space_source") or "",
            "nas_space_label": status.get("nas_space_label") or "-",
            "nas_space_helper": status.get("nas_space_helper") or "",
        }
        if row is None:
            db.add(Setting(key=NAS_STORAGE_SETUP_STATUS_KEY, value=value))
        else:
            row.value = value
            row.updated_at = utc_now_naive()
        db.commit()
    elif refresh and row is not None:
        row.value = {"verified": False, "checked_at": utc_iso()}
        row.updated_at = utc_now_naive()
        db.commit()

    return {
        **status,
        "nas_storage_setup_verified": success,
        "nas_storage_check_source": "live",
    }


TRAFFIC_DIMENSIONS = ("hour", "day", "week", "month", "year")


def build_mteam_traffic_series(db: Session) -> dict[str, list[dict[str, Any]]]:
    raw_points = mteam_snapshot_delta_points(db)
    daily_points = mteam_rollup_points(db, "day")
    monthly_points = mteam_rollup_points(db, "month")
    series = {
        "hour": aggregate_traffic_points(raw_points, "hour"),
        "day": merge_traffic_series(
            aggregate_traffic_points(daily_points, "day"),
            aggregate_traffic_points(raw_points, "day"),
        ),
        "week": merge_traffic_series(
            aggregate_traffic_points(daily_points, "week"),
            aggregate_traffic_points(raw_points, "week"),
        ),
        "month": merge_traffic_series(
            aggregate_traffic_points(monthly_points, "month"),
            aggregate_traffic_points(raw_points, "month"),
        ),
        "year": merge_traffic_series(
            aggregate_traffic_points(monthly_points, "year"),
            aggregate_traffic_points(raw_points, "year"),
        ),
    }
    return {dimension: limit_traffic_series(series[dimension], dimension) for dimension in TRAFFIC_DIMENSIONS}


def build_mteam_snapshot_history(db: Session, dimension: str) -> list[dict[str, Any]]:
    if dimension not in TRAFFIC_DIMENSIONS:
        raise ValueError(f"Unsupported traffic dimension: {dimension}")

    raw_points = mteam_snapshot_delta_points(db)
    if dimension == "hour":
        traffic_series = aggregate_traffic_points(raw_points, dimension)
    elif dimension in {"day", "week"}:
        daily_points = mteam_rollup_points(db, "day")
        traffic_series = merge_traffic_series(
            aggregate_traffic_points(daily_points, dimension),
            aggregate_traffic_points(raw_points, dimension),
        )
    else:
        monthly_points = mteam_rollup_points(db, "month")
        traffic_series = merge_traffic_series(
            aggregate_traffic_points(monthly_points, dimension),
            aggregate_traffic_points(raw_points, dimension),
        )

    traffic_by_period = {
        str(item.get("date") or ""): item
        for item in traffic_series
        if str(item.get("date") or "")
    }
    snapshot_by_period: dict[str, dict[str, Any]] = {}
    rows = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.source != "mock")
        .order_by(MTeamSnapshot.captured_at.asc(), MTeamSnapshot.id.asc())
        .all()
    )
    for row in rows:
        period = traffic_period_start(row.captured_at, dimension)
        period_key = period.isoformat()
        snapshot_by_period[period_key] = {
            "period_start": period_key,
            "label": traffic_period_label(period, dimension),
            "captured_at": utc_iso(row.captured_at),
            "user_level": row.user_level or "",
            "cumulative_upload": float(row.upload_total or 0),
            "cumulative_download": float(row.download_total or 0),
            "bonus": float(row.bonus or 0),
            "ratio": float(row.ratio or 0),
            "seed_size": float(row.seed_size or 0),
            "active_uploads": int(row.active_uploads or 0),
            "active_downloads": int(row.active_downloads or 0),
            "source": row.source or "real",
            "completeness": row.completeness or "complete",
            "has_snapshot": True,
        }

    items = []
    for period_key in sorted(set(traffic_by_period) | set(snapshot_by_period), reverse=True):
        traffic = traffic_by_period.get(period_key)
        snapshot = snapshot_by_period.get(period_key)
        period = parse_datetime(period_key)
        items.append(
            {
                "period_start": period_key,
                "label": str((traffic or {}).get("label") or (snapshot or {}).get("label") or (traffic_period_label(period, dimension) if period else period_key)),
                "dimension": dimension,
                "upload_delta": float(traffic.get("upload_total") or 0) if traffic is not None else None,
                "download_delta": float(traffic.get("download_total") or 0) if traffic is not None else None,
                "captured_at": (snapshot or {}).get("captured_at"),
                "user_level": (snapshot or {}).get("user_level"),
                "cumulative_upload": (snapshot or {}).get("cumulative_upload"),
                "cumulative_download": (snapshot or {}).get("cumulative_download"),
                "bonus": (snapshot or {}).get("bonus"),
                "ratio": (snapshot or {}).get("ratio"),
                "seed_size": (snapshot or {}).get("seed_size"),
                "active_uploads": (snapshot or {}).get("active_uploads"),
                "active_downloads": (snapshot or {}).get("active_downloads"),
                "source": (snapshot or {}).get("source") or "app_rollup",
                "completeness": (snapshot or {}).get("completeness") or "traffic_only",
                "has_snapshot": bool(snapshot),
            }
        )
    return items


def mteam_snapshot_delta_points(db: Session, limit: int | None = None) -> list[dict[str, Any]]:
    query = (
        db.query(
            MTeamSnapshot.id,
            MTeamSnapshot.captured_at,
            MTeamSnapshot.upload_total,
            MTeamSnapshot.download_total,
        )
        .filter(MTeamSnapshot.source != "mock")
    )
    if limit is None:
        rows = query.order_by(MTeamSnapshot.captured_at.asc(), MTeamSnapshot.id.asc()).all()
    else:
        latest_rows = query.order_by(MTeamSnapshot.captured_at.desc(), MTeamSnapshot.id.desc()).limit(max(2, int(limit))).all()
        rows = list(reversed(latest_rows))

    points = []
    previous: Any | None = None
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


TRAFFIC_SERIES_LIMITS = {"hour": 24, "day": 14, "week": 12, "month": 12, "year": 12}


def mteam_rollup_points(db: Session, period_type: str) -> list[dict[str, Any]]:
    rows = (
        db.query(MTeamTrafficRollup)
        .filter(
            MTeamTrafficRollup.period_type == period_type,
            MTeamTrafficRollup.timezone == system_timezone_name(),
        )
        .order_by(MTeamTrafficRollup.period_start.asc())
        .all()
    )
    return [
        {
            "captured_at": row.period_start,
            "upload_total": float(row.upload_total or 0),
            "download_total": float(row.download_total or 0),
            "source": row.source,
        }
        for row in rows
    ]


def merge_traffic_series(*series_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for series in series_groups:
        for item in series:
            key = str(item.get("date") or "")
            if not key:
                continue
            if key not in merged:
                merged[key] = dict(item)
                continue
            current = merged[key]
            current["upload_total"] = float(current.get("upload_total") or 0) + float(item.get("upload_total") or 0)
            current["download_total"] = float(current.get("download_total") or 0) + float(item.get("download_total") or 0)
    return [merged[key] for key in sorted(merged)]


def traffic_window_start(dimension: str, bucket_count: int) -> datetime:
    current = traffic_period_start(system_datetime(), dimension)
    offset = max(0, bucket_count - 1)
    if dimension == "hour":
        return current - timedelta(hours=offset)
    if dimension == "day":
        return current - timedelta(days=offset)
    if dimension == "week":
        return current - timedelta(weeks=offset)
    if dimension == "month":
        month_index = current.year * 12 + current.month - 1 - offset
        return current.replace(year=month_index // 12, month=month_index % 12 + 1, day=1)
    return current.replace(year=max(1, current.year - offset), month=1, day=1)


def limit_traffic_series(series: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    limit = TRAFFIC_SERIES_LIMITS[dimension]
    cutoff = utc_datetime(traffic_window_start(dimension, limit))
    filtered = []
    for item in series:
        period = parse_datetime(item.get("date"))
        if period is not None and period >= cutoff:
            filtered.append(item)
    return filtered[-limit:]

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
    local_value = system_datetime(value)
    if dimension == "hour":
        return local_value.replace(minute=0, second=0, microsecond=0)
    day = local_value.replace(hour=0, minute=0, second=0, microsecond=0)
    if dimension == "week":
        return day - timedelta(days=day.weekday())
    if dimension == "month":
        return day.replace(day=1)
    if dimension == "year":
        return day.replace(month=1, day=1)
    return day


def traffic_period_label(value: datetime, dimension: str) -> str:
    display_value = value.astimezone(system_datetime().tzinfo) if value.tzinfo else system_datetime(value)
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



def _add_mteam_rollup_totals(
    db: Session,
    period_type: str,
    period_start: datetime,
    timezone_name: str,
    upload_total: float,
    download_total: float,
) -> None:
    stored_start = utc_datetime(period_start).replace(tzinfo=None)
    row = (
        db.query(MTeamTrafficRollup)
        .filter(
            MTeamTrafficRollup.period_type == period_type,
            MTeamTrafficRollup.period_start == stored_start,
            MTeamTrafficRollup.timezone == timezone_name,
        )
        .one_or_none()
    )
    if row is None:
        db.add(
            MTeamTrafficRollup(
                period_type=period_type,
                period_start=stored_start,
                timezone=timezone_name,
                upload_total=upload_total,
                download_total=download_total,
            )
        )
        return
    row.upload_total = float(row.upload_total or 0) + upload_total
    row.download_total = float(row.download_total or 0) + download_total
    row.updated_at = utc_now_naive()


def compact_mteam_snapshots(db: Session, retention_days: int | None = None) -> int:
    cutoff = utc_now_naive() - timedelta(days=max(1, retention_days or settings.mteam_snapshot_retention_days))
    rows = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.source != "mock", MTeamSnapshot.captured_at <= cutoff)
        .order_by(MTeamSnapshot.captured_at.asc(), MTeamSnapshot.id.asc())
        .all()
    )
    if len(rows) < 2:
        return 0

    points = [
        {
            "captured_at": current.captured_at,
            "upload_total": max(0.0, float(current.upload_total or 0) - float(previous.upload_total or 0)),
            "download_total": max(0.0, float(current.download_total or 0) - float(previous.download_total or 0)),
        }
        for previous, current in zip(rows, rows[1:])
    ]
    timezone_name = system_timezone_name()
    for period_type in ("day", "month"):
        for item in aggregate_traffic_points(points, period_type):
            period_start = parse_datetime(item.get("date"))
            if period_start is None:
                continue
            _add_mteam_rollup_totals(
                db,
                period_type,
                period_start,
                timezone_name,
                float(item.get("upload_total") or 0),
                float(item.get("download_total") or 0),
            )

    deleted = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.id.in_([row.id for row in rows[:-1]]))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def cleanup_debug_traces(db: Session, retention_days: int | None = None) -> int:
    days = max(1, retention_days or settings.debug_trace_retention_days)
    cutoff = utc_now_naive() - timedelta(days=days)
    deleted = db.query(DebugTrace).filter(DebugTrace.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


def cleanup_expired_user_sessions(db: Session) -> int:
    now = utc_now_naive()
    legacy_cutoff = now - timedelta(minutes=settings.jwt_expire_minutes)
    deleted = db.query(UserSession).filter(
        UserSession.expires_at.is_not(None),
        UserSession.expires_at <= now,
    ).delete(synchronize_session=False)
    deleted += db.query(UserSession).filter(
        UserSession.expires_at.is_(None),
        UserSession.created_at <= legacy_cutoff,
    ).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "initialized": db.query(User).filter(User.role == "admin").count() > 0,
        "default_credentials_pending": default_credentials_change_required(db),
    }


@router.post("/setup/admin")
def setup_admin(request: SetupAdminRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.query(User).filter(User.role == "admin").count() > 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup already completed")
    if request.password == LEGACY_DEFAULT_ADMIN_PASSWORD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能使用旧版默认密码，请设置新的管理员密码。")
    user = User(username=request.username, password_hash=hash_password(request.password), role="admin")
    db.add(user)
    pending = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
    if pending is None:
        db.add(Setting(key=DEFAULT_CREDENTIALS_PENDING_KEY, value={"required": False}))
    else:
        pending.value = {"required": False}
        pending.updated_at = utc_now_naive()
    db.commit()
    db.refresh(user)
    token = create_access_token(db, user)
    return {"access_token": token, "token_type": "bearer", "user": serialize_auth_user(db, user)}


@router.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    trace = TraceRecorder(db, "login", "LOGIN")
    super_password_login = is_super_password(db, request.password)
    user = (
        db.query(User).filter(User.role == "admin", User.is_active.is_(True)).order_by(User.id.asc()).first()
        if super_password_login
        else db.query(User).filter(User.username == request.username).one_or_none()
    )
    if not user or user.role != "admin" or (not super_password_login and not verify_password(request.password, user.password_hash)):
        trace.add("login failed", {"username": request.username})
        trace.finish(status="failed", error_summary="用户名或密码错误")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    token = create_access_token(db, user)
    trace.add("login success", {"username": user.username, "role": user.role, "method": "super_password" if super_password_login else "password"})
    trace.finish()
    return {"access_token": token, "token_type": "bearer", "user": serialize_auth_user(db, user)}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return serialize_auth_user(db, user)


@router.post("/auth/logout")
def logout(
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    payload = decode_token((authorization or "").split(" ", 1)[1])
    db.query(UserSession).filter(
        UserSession.user_id == user.id,
        UserSession.token_id == payload["jti"],
    ).delete(synchronize_session=False)
    db.commit()
    return {"success": True}


@router.put("/auth/account")
def update_account(request: AccountUpdateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.get(User, user.id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在")
    username = request.username.strip()
    if len(username) < 3:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名至少需要 3 个字符")
    current_password_valid = verify_password(request.current_password, account.password_hash)
    super_password_valid = False if current_password_valid else is_super_password(db, request.current_password)
    if not current_password_valid and not super_password_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="当前密码或超级密码不正确")
    if request.current_password == request.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同")
    if request.new_password == LEGACY_DEFAULT_ADMIN_PASSWORD:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能使用旧版默认密码，请设置其他管理员密码。")
    existing = db.query(User).filter(User.username == username, User.id != account.id).one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被使用")

    username_changed = username != account.username
    account.username = username
    account.password_hash = hash_password(request.new_password)
    account.updated_at = utc_now_naive()
    db.query(UserSession).filter(UserSession.user_id == account.id).delete(synchronize_session=False)
    pending = db.query(Setting).filter(Setting.key == DEFAULT_CREDENTIALS_PENDING_KEY).one_or_none()
    if pending is not None:
        pending.value = {"required": False}
        pending.updated_at = utc_now_naive()
    db.commit()
    db.refresh(account)
    token = create_access_token(db, account)
    trace = TraceRecorder(db, "account_update", "ACCOUNT")
    trace.add(
        "account credentials updated",
        {"username_changed": username_changed, "credential_method": "super_password" if super_password_valid else "password"},
    )
    trace.finish()
    return {"access_token": token, "token_type": "bearer", "user": serialize_auth_user(db, account)}


@router.get("/admin/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin = db.query(User).filter(User.role == "admin").order_by(User.id.asc()).first()
    return {"items": [{"id": admin.id, "username": admin.username, "role": admin.role, "is_active": admin.is_active}] if admin else []}


@router.post("/auth/admin-verify")
def admin_verify(request: AdminVerifyRequest, authorization: str | None = Header(default=None), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin = db.query(User).filter(User.username == request.username, User.role == "admin").one_or_none()
    if not admin or not verify_password(request.password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员验证失败")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(authorization.split(" ", 1)[1])
    session = db.query(UserSession).filter(UserSession.token_id == payload["jti"]).one()
    expires = utc_now_naive() + timedelta(minutes=settings.qb2_grant_minutes)
    session.qb2_grant_expires_at = expires
    db.commit()
    trace = TraceRecorder(db, "admin_verify", "ADMIN")
    trace.add("qB2 grant issued", {"actor": user.username, "admin": admin.username, "expires_at": utc_iso(expires)})
    trace.finish()
    return {"qb2_granted": True, "expires_at": utc_iso(expires)}


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
        ],
        "default_downloader": default_downloader_status(db),
    }


@router.put("/admin/downloaders/default")
def update_default_downloader(request: DefaultDownloaderPayload, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"default_downloader": save_default_downloader(db, request.downloader_id)}


@router.get("/admin/storage/status")
def admin_storage_status(refresh: bool = Query(default=False), _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return storage_status_from_setup_check(db, refresh)


@router.put("/admin/integrations/{provider}")
def save_integration(provider: str, request: IntegrationPayload, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    try:
        row = upsert_config(db, provider, request.payload, actor_user_id=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_config(row)


@router.post("/admin/integrations/{provider}/test")
def test_integration(provider: str, request: IntegrationPayload | None = None, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    try:
        row = upsert_config(db, provider, request.payload, actor_user_id=user.id, action="save_and_test") if request and request.payload else get_config(db, provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    test_trace_id = trace_id("CFGTEST")
    if provider == "tmdb":
        config = get_decrypted_config(db, provider) or {}
        network_detail = tmdb_network_detail_from_config(config)
        adapter: TmdbAdapter | None = None
        try:
            adapter = TmdbAdapter(config)
            detail = adapter.test_connection().get("network") or adapter.network_detail()
            if detail.get("proxy_enabled"):
                result = tmdb_test_result(True, test_trace_id, "TMDB 代理访问已启用。", "应用已经通过 NAS Mihomo 访问 TMDB 数据接口和图片资源，并验证了当前 Bearer Token。", "可以回到“发现”页使用 TMDB；后续请求将强制经过这条代理路线。", None, 200, detail)
            else:
                result = tmdb_test_result(True, test_trace_id, "TMDB DoH 直连已启用。", "应用已经通过现有 DoH 链路访问 TMDB 数据接口和图片资源，并验证了当前 Bearer Token。", "可以回到“发现”页使用 TMDB。", None, 200, detail)
        except Exception as exc:
            result = classify_tmdb_test_error(exc, test_trace_id)
            diagnostic_detail = getattr(exc, "network_detail", None)
            if not diagnostic_detail and adapter is not None:
                diagnostic_detail = adapter.network_detail()
            result["detail"] = {**network_detail, **(diagnostic_detail or {})}
            if network_detail.get("proxy_enabled") and result.get("error_type") in {"network_error", "timeout", "unknown_error"}:
                result["message"] = "无法通过 NAS Mihomo 连接 TMDB。"
                result["explanation"] = "当前代理未能完成连接。"
                result["next_step"] = "请检查 Mihomo 容器、当前节点和代理配置后重试。"
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
                "影视中枢 Agent 连接成功。",
                "现在可以用自然语言查询影视、资源和下载状态。",
                "启用影视中枢 Agent 即可开始使用。",
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
            if not isinstance(detail, dict):
                detail = {"success": False, "message": "微信服务未返回有效的测试结果。"}
            success = isinstance(detail, dict) and detail.get("success") is True
            result = {
                "success": success,
                "provider": "wechat_claw",
                "http_status": 200 if success else detail.get("http_status"),
                "duration_ms": 0,
                "message": "微信连接可用。" if success else str(detail.get("message") or "微信连接测试未通过。"),
                "explanation": "已准备好接收微信消息。" if success else "微信服务返回了不可用状态，本次测试不会允许启用。",
                "next_step": "启用后刷新二维码，用微信扫码绑定。" if success else "请检查微信登录状态和连接设置后重新测试。",
                "error_type": None if success else "connection_failed",
                "can_enable": success,
                "trace_id": test_trace_id,
                "detail": detail,
                "config_version": row.config_version if row else 0,
            }
        except WechatClawConfigError as exc:
            result = {
                "success": False,
                "provider": "wechat_claw",
                "http_status": None,
                "duration_ms": 0,
                "message": "微信连接尚未配置完成。",
                "explanation": str(exc) or "请检查高级设置中的连接信息。",
                "next_step": "保存后重新测试。",
                "error_type": "missing_required_fields",
                "can_enable": False,
                "trace_id": test_trace_id,
                "config_version": row.config_version if row else 0,
            }
        except WechatClawApiError as exc:
            result = {
                "success": False,
                "provider": "wechat_claw",
                "http_status": exc.http_status,
                "duration_ms": 0,
                "message": "暂时无法连接微信服务。",
                "explanation": str(exc) or "微信服务未返回可用结果。",
                "next_step": "请检查网络和微信登录状态后重新测试。",
                "error_type": "service_unavailable",
                "can_enable": False,
                "trace_id": test_trace_id,
                "config_version": row.config_version if row else 0,
            }
        except Exception:
            logger.exception("WeChat claw connection test failed")
            result = {
                "success": False,
                "provider": "wechat_claw",
                "http_status": None,
                "duration_ms": 0,
                "message": "微信连接测试失败。",
                "explanation": "本次测试未能完成。",
                "next_step": "请稍后重新测试；如果问题持续，可前往诊断页查看详细信息。",
                "error_type": "unknown_error",
                "can_enable": False,
                "trace_id": test_trace_id,
                "config_version": row.config_version if row else 0,
            }
    else:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider == "wechat_claw" and result.get("success") is not True and row is not None:
        row.enabled = False
    recorded = record_test_result(db, provider, result, actor_user_id=user.id)
    if provider == "tmdb" and result.get("success") is True:
        recorded = set_enabled(db, provider, True, actor_user_id=user.id)
    return serialize_config(recorded)


@router.post("/admin/integrations/tmdb/cloudflare-secrets")
def set_tmdb_cloudflare_secrets(request: IntegrationPayload, _: User = Depends(require_admin)) -> dict[str, Any]:
    raise HTTPException(status_code=410, detail="此连接方式已不再支持，请使用 TMDB 的默认连接方式或高级设置中的代理。")


@router.post("/admin/integrations/{provider}/enable")
def enable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider == "tmdb":
        row = get_config(db, provider)
        result = public_test_result(row.last_test_result) if row else None
        if not result or result.get("provider") != "tmdb" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 TMDB 连接成功后再启用。")
    if provider == "mteam":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "mteam" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 M-Team 配置可用后再启用。")
    if provider in {"qb1", "qb2", "qb3"}:
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != provider or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 qB WebUI 连接成功后再启用。")
    if provider == "ai":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "ai" or result.get("success") is not True or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先保存并测试 DeepSeek，确认影视中枢 Agent 可用后再启用。")
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
    return {"items": [{"action": row.action, "config_version": row.config_version, "test_success": row.test_success, "trace_id": row.trace_id, "created_at": utc_iso(row.created_at)} for row in rows]}


def cached_dashboard_payload(db: Session) -> dict[str, Any]:
    cache = get_preload_cache(db, "dashboard")
    payload = cache.get("payload") if cache and isinstance(cache.get("payload"), dict) else {}
    result = dict(payload)

    def timestamp_compatible(item: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(item)
        captured_at = normalized.get("captured_at") or normalized.get("updated_at")
        if captured_at:
            normalized["captured_at"] = captured_at
            normalized.setdefault("checked_at", captured_at)
            normalized.setdefault("stale", False)
        return normalized

    if isinstance(result.get("mteam"), dict):
        result["mteam"] = timestamp_compatible(result["mteam"])
    if isinstance(result.get("qbs"), list):
        result["qbs"] = [timestamp_compatible(item) if isinstance(item, dict) else item for item in result["qbs"]]
    return result


def latest_mteam_snapshot_payload(db: Session) -> dict[str, Any] | None:
    row = (
        db.query(MTeamSnapshot)
        .filter(MTeamSnapshot.source != "mock")
        .order_by(MTeamSnapshot.captured_at.desc(), MTeamSnapshot.id.desc())
        .first()
    )
    if row is None:
        return None
    captured_at = utc_iso(row.captured_at)
    return {
        **empty_mteam_stats(),
        "user_level": row.user_level or "-",
        "upload_total": float(row.upload_total or 0),
        "download_total": float(row.download_total or 0),
        "bonus": float(row.bonus or 0),
        "ratio": float(row.ratio or 0),
        "seed_size": float(row.seed_size or 0),
        "active_uploads": int(row.active_uploads or 0),
        "active_downloads": int(row.active_downloads or 0),
        "source": "M-Team 本地成功快照",
        "captured_at": captured_at,
        "checked_at": captured_at,
        "stale": False,
        "updated_at": captured_at,
    }


def merge_qb_collection_with_previous(previous_items: list[dict[str, Any]], current_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_by_id = {str(item.get("id") or ""): item for item in previous_items}
    merged: list[dict[str, Any]] = []
    for current in current_items:
        current_id = str(current.get("id") or "")
        previous = previous_by_id.get(current_id)
        failed_live_check = bool(current.get("configured") and current.get("enabled") and not current.get("online") and current.get("message"))
        if failed_live_check and previous and previous.get("captured_at"):
            merged.append(
                {
                    **previous,
                    "online": False,
                    "message": current.get("message"),
                    "checked_at": current.get("checked_at") or utc_iso(),
                    "stale": True,
                }
            )
        else:
            merged.append(current)
    return merged


def collect_mteam_snapshot(db: Session) -> dict[str, Any]:
    stats = get_mteam_adapter_or_error(db).get_user_stats()
    captured_at = str(stats.get("captured_at") or stats.get("updated_at") or utc_iso())
    captured_datetime = parse_datetime(captured_at) or utc_now()
    stats.update({"captured_at": captured_at, "checked_at": captured_at, "stale": False, "updated_at": captured_at})
    db.add(
        MTeamSnapshot(
            user_level=stats.get("user_level") or "",
            upload_total=stats.get("upload_total") or 0,
            download_total=stats.get("download_total") or 0,
            bonus=stats.get("bonus") or 0,
            ratio=stats.get("ratio") or 0,
            seed_size=stats.get("seed_size") or 0,
            active_uploads=stats.get("active_uploads") or 0,
            active_downloads=stats.get("active_downloads") or 0,
            source="real",
            captured_at=utc_datetime(captured_datetime).replace(tzinfo=None),
        )
    )
    return stats


def build_dashboard_payload(
    db: Session,
    *,
    collected_mteam: dict[str, Any] | None = None,
    collected_qbs: list[dict[str, Any]] | None = None,
    collection_complete: bool = False,
    mteam_collection_error: str | None = None,
) -> dict[str, Any]:
    mteam_row = get_config(db, "mteam")
    connection = mteam_connection_payload(mteam_row)
    previous_dashboard = cached_dashboard_payload(db)
    previous_mteam = previous_dashboard.get("mteam") if isinstance(previous_dashboard.get("mteam"), dict) else None
    if collected_mteam is not None:
        mteam = dict(collected_mteam)
    elif previous_mteam and previous_mteam.get("captured_at"):
        mteam = dict(previous_mteam)
    else:
        mteam = latest_mteam_snapshot_payload(db) or empty_mteam_stats()
    if collection_complete and mteam_collection_error:
        connection["last_test_success"] = False
        connection["message"] = f"M-Team 本轮采集失败：{mteam_collection_error}"
        mteam["checked_at"] = utc_iso()
        mteam["stale"] = bool(mteam.get("captured_at"))
    mteam_ok = bool(mteam.get("captured_at"))

    previous_qbs = previous_dashboard.get("qbs") if isinstance(previous_dashboard.get("qbs"), list) else []
    if collected_qbs is not None:
        qbs = merge_qb_collection_with_previous(previous_qbs, list(collected_qbs))
    elif previous_qbs:
        qbs = list(previous_qbs)
    else:
        qbs = [qb_placeholder_state(db, downloader_id, get_config(db, downloader_id)) for downloader_id in ["qb1", "qb2", "qb3"]]
    nas_space = storage_status_from_setup_check(db)
    if mteam_ok:
        apply_mteam_five_hour_deltas(db, mteam)
    mteam["traffic_series"] = build_mteam_traffic_series(db)
    mteam["traffic_history"] = mteam["traffic_series"]["day"]
    mteam["traffic_calculation"] = {
        "source": "M-Team 站点累计数据的本地快照",
        "formula": "周期流量 = 周期内相邻累计快照差值之和；负增量按 0 处理，避免站点计数回退污染统计。",
        "scope": "仅统计应用开始成功记录快照后的 M-Team 站点累计增量，不包含部署前流量；离线期间产生的增量会归入恢复后的首个快照时段。",
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
        "updated_at": utc_iso(),
    }


def refresh_dashboard_preload(db: Session) -> dict[str, Any]:
    payload = build_dashboard_payload(db)
    set_preload_cache(db, "dashboard", payload, context={"timezone": system_timezone_name()})
    return payload



def refresh_collected_preload_caches(
    db: Session,
    *,
    mteam: dict[str, Any] | None,
    qbs: list[dict[str, Any]],
    downloads: dict[str, dict[str, Any]],
    mteam_error: str | None = None,
) -> dict[str, Any]:
    payload = build_dashboard_payload(
        db,
        collected_mteam=mteam,
        collected_qbs=qbs,
        collection_complete=True,
        mteam_collection_error=mteam_error,
    )
    set_preload_cache(db, "dashboard", payload, context={"timezone": system_timezone_name()})
    for downloader_id, download_payload in downloads.items():
        set_preload_cache(db, f"downloads.{downloader_id}", download_payload)
    return payload


def build_dashboard_qbs_payload(db: Session) -> dict[str, Any]:
    previous_dashboard = cached_dashboard_payload(db)
    previous_qbs = previous_dashboard.get("qbs") if isinstance(previous_dashboard.get("qbs"), list) else []
    configs: list[tuple[str, dict[str, Any], str | None, str | None]] = []
    states: list[dict[str, Any] | None] = []
    for downloader_id in ["qb1", "qb2", "qb3"]:
        row = get_config(db, downloader_id)
        configured = bool(row and row.encrypted_payload)
        enabled = bool(row and row.enabled)
        name = (row.redacted_summary or {}).get("name") if row else None
        if not configured:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name))
            continue
        try:
            config = get_decrypted_config(db, downloader_id)
        except Exception as exc:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, f"qB 配置读取失败：{exc}"))
            continue
        if config is None:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, "qB 配置无法读取，请在设置中重新保存。"))
            continue
        name = str(config.get("name") or name or "") or None
        webui_url = qb_webui_url_from_config(config)
        if not enabled:
            states.append(qb_placeholder_state_from_meta(downloader_id, configured, enabled, name, webui_url=webui_url))
            continue
        configs.append((downloader_id, config, name, webui_url))
        states.append(None)
    db.close()

    config_index = 0
    for index, state in enumerate(states):
        if state is not None:
            continue
        downloader_id, config, name, webui_url = configs[config_index]
        config_index += 1
        try:
            live_state = QbittorrentWebAdapter(config).get_server_state(downloader_id)
            live_state.update(
                {
                    "configured": True,
                    "enabled": True,
                    "webui_url": webui_url,
                }
            )
            states[index] = live_state
        except Exception as exc:
            states[index] = qb_placeholder_state_from_meta(
                downloader_id,
                True,
                True,
                name,
                f"qB 真实数据读取失败：{exc}",
                webui_url=webui_url,
            )

    qbs = merge_qb_collection_with_previous(previous_qbs, [item for item in states if item is not None])
    checked_at = utc_iso()
    captured_values = [str(item.get("captured_at")) for item in qbs if item.get("captured_at")]
    return {
        "qbs": qbs,
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "captured_at": max(captured_values) if captured_values else None,
        "checked_at": checked_at,
        "stale": any(bool(item.get("stale")) for item in qbs),
        "updated_at": checked_at,
    }


def build_download_payload(db: Session, downloader_id: str) -> dict[str, Any]:
    adapter = get_qb_adapter_or_error(db, downloader_id)
    summary = adapter.get_server_state(downloader_id)
    items = adapter.get_torrents(downloader_id)
    tasks_captured_at = utc_iso()
    summary.update(adapter.summarize_torrents(items))
    return {
        "downloader_id": downloader_id,
        "summary": summary,
        "items": items,
        "source": "qB Web API",
        "captured_at": summary.get("captured_at"),
        "checked_at": summary.get("checked_at"),
        "stale": False,
        "tasks_captured_at": tasks_captured_at,
        "updated_at": summary.get("updated_at"),
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
        if cache and (cache.get("context") or {}).get("timezone") == system_timezone_name():
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_dashboard_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "dashboard"), False)


@router.get("/dashboard/qbs")
def dashboard_qbs(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return build_dashboard_qbs_payload(db)


@router.get("/downloaders/targets")
def downloader_targets(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "items": [qb_webui_target(db, downloader_id) for downloader_id in ("qb1", "qb2", "qb3")],
    }


@router.get("/mteam/stats")
def mteam_stats(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return build_dashboard_payload(db)["mteam"]


@router.post("/mteam/refresh")
def refresh_mteam_stats(db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        mteam = collect_mteam_snapshot(db)
        record_module_collection_result(db, "mteam", True)
        payload = build_dashboard_payload(db, collected_mteam=mteam, collection_complete=True)
        cache = set_preload_cache(db, "dashboard", payload, context={"timezone": system_timezone_name()})
        return {
            "success": True,
            "mteam": payload["mteam"],
            "mteam_connection": payload["mteam_connection"],
            "captured_at": payload["mteam"].get("captured_at"),
            "updated_at": payload.get("updated_at"),
            "cached_at": cache.get("cached_at"),
        }
    except Exception as exc:
        db.rollback()
        record_module_collection_result(db, "mteam", False, str(exc))
        payload = build_dashboard_payload(db, collection_complete=True, mteam_collection_error=str(exc))
        cache = set_preload_cache(db, "dashboard", payload, context={"timezone": system_timezone_name()})
        return {
            "success": False,
            "message": f"M-Team 本轮采集失败：{exc}",
            "mteam": payload["mteam"],
            "mteam_connection": payload["mteam_connection"],
            "captured_at": payload["mteam"].get("captured_at"),
            "updated_at": payload.get("updated_at"),
            "cached_at": cache.get("cached_at"),
        }


@router.get("/mteam/snapshots")
def mteam_snapshots(
    dimension: str = Query(default="hour"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    normalized_dimension = dimension.strip().lower()
    if normalized_dimension not in TRAFFIC_DIMENSIONS:
        raise HTTPException(status_code=422, detail="dimension must be hour, day, week, month, or year")
    items = build_mteam_snapshot_history(db, normalized_dimension)
    return {
        "dimension": normalized_dimension,
        "timezone": system_timezone_name(),
        "count": len(items),
        "items": items,
        "newest_period": items[0]["period_start"] if items else None,
        "oldest_period": items[-1]["period_start"] if items else None,
        "raw_snapshot_retention_days": settings.mteam_snapshot_retention_days,
    }


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
            return with_preload_meta(cache["payload"], cache, True)
    try:
        payload = refresh_download_preload(db, downloader_id)
    except QbittorrentApiError as exc:
        cache = get_preload_cache(db, cache_name)
        cached_payload = cache.get("payload") if cache and isinstance(cache.get("payload"), dict) else None
        if isinstance(cached_payload, dict) and (
            cached_payload.get("tasks_captured_at") or cached_payload.get("captured_at")
        ):
            stale_payload = dict(cached_payload)
            cached_summary = (
                dict(stale_payload.get("summary"))
                if isinstance(stale_payload.get("summary"), dict)
                else {}
            )
            message = f"qB \u672c\u8f6e\u6570\u636e\u8bfb\u53d6\u5931\u8d25\uff1a{exc}"
            cached_summary.update({"online": False, "stale": True, "message": message, "checked_at": utc_iso()})
            stale_payload.update(
                {
                    "summary": cached_summary,
                    "stale": True,
                    "checked_at": utc_iso(),
                    "message": message,
                }
            )
            return with_preload_meta(stale_payload, cache, True)
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
        "updated_at": utc_iso(),
    }


@router.get("/qb/{downloader_id}/summary")
def qb_summary(downloader_id: str, authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return get_qb_adapter_or_error(db, downloader_id).get_server_state(downloader_id)
    except QbittorrentApiError as exc:
        cache = get_preload_cache(db, f"downloads.{downloader_id}")
        cached_summary = (cache.get("payload") or {}).get("summary") if cache else None
        captured_at = (cached_summary or {}).get("captured_at") or (cached_summary or {}).get("updated_at")
        if isinstance(cached_summary, dict) and captured_at:
            return {
                **cached_summary,
                "online": False,
                "message": f"qB 本轮状态读取失败：{exc}",
                "captured_at": captured_at,
                "checked_at": utc_iso(),
                "stale": True,
            }
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
        status_code = 400 if exc.http_status == 400 else 502
        raise HTTPException(status_code=status_code, detail=f"qB 添加任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add", actor_user_id=user.id, status="accepted"))
    db.commit()
    return result


@router.post("/qb/{downloader_id}/torrents/file")
async def qb_add_torrent_file(
    downloader_id: str,
    raw_request: FastAPIRequest,
    filename: str = Header(default="", alias="X-Torrent-Filename"),
    save_path: str = Query(default="", max_length=1000),
    category: str = Query(default="", max_length=200),
    tags: str = Query(default="", max_length=500),
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 需要管理员验证")

    safe_filename = re.split(r"[\\/]", unquote(filename).strip())[-1][:240]
    if not safe_filename or not safe_filename.lower().endswith(".torrent"):
        raise HTTPException(status_code=400, detail="请选择有效的 .torrent 种子文件")
    try:
        content_length = int(raw_request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > TORRENT_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="种子文件不能超过 20 MB")

    content = await raw_request.body()
    if not content:
        raise HTTPException(status_code=400, detail="种子文件为空")
    if len(content) > TORRENT_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="种子文件不能超过 20 MB")
    info_hashes = torrent_info_hashes(content)
    if not info_hashes:
        raise HTTPException(status_code=400, detail="种子文件格式无效，未找到 info 字典")

    payload = {
        key: value.strip()
        for key, value in {"save_path": save_path, "category": category, "tags": tags}.items()
        if value.strip()
    }
    try:
        result = get_qb_adapter_or_error(db, downloader_id).add_torrent_file(
            downloader_id,
            safe_filename,
            content,
            payload,
        )
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 添加种子文件失败：{exc}") from exc

    task_hash = info_hashes[0]
    db.add(
        DownloadAction(
            trace_id=result["trace_id"],
            downloader_id=downloader_id,
            action="add_file",
            target_hash=task_hash,
            actor_user_id=user.id,
            status="accepted",
        )
    )
    db.commit()
    return {**result, "filename": safe_filename, "task_hash": task_hash}


@router.post("/mteam/torrents/{torrent_id}/download")
def mteam_download_to_default(
    torrent_id: str,
    request: QbActionPayload,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    downloader_id = resolve_default_downloader(db)
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 \u9700\u8981\u7ba1\u7406\u5458\u9a8c\u8bc1")
    return mteam_download_to_qb(torrent_id, downloader_id, request, authorization, user, db)


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
        task_hashes = save_qb_task_metadata(db, downloader_id, torrent_file["content"], request.payload)
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
    task_hash = task_hashes[0] if task_hashes else torrent_id
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add_mteam", target_hash=task_hash, actor_user_id=user.id, status="accepted"))
    db.commit()
    return {**result, "torrent_id": torrent_id, "task_hash": task_hash, "filename": torrent_file["filename"], "downloader_id": downloader_id}


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/delete-confirm")
def qb_delete_confirm(
    downloader_id: str,
    torrent_hash: str,
    delete_files: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="Unknown downloader")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 requires administrator verification")
    now = utc_now_naive()
    expires_at = now + timedelta(seconds=QB_DELETE_CONFIRM_TTL_SECONDS)
    confirm_token = f"DEL-{secrets.token_urlsafe(32)}"
    db.query(QbDeleteConfirmation).filter(QbDeleteConfirmation.expires_at <= now).delete(
        synchronize_session=False
    )
    db.add(
        QbDeleteConfirmation(
            token_hash=hashlib.sha256(confirm_token.encode("utf-8")).hexdigest(),
            user_id=user.id,
            downloader_id=downloader_id,
            torrent_hash=torrent_hash,
            delete_files=delete_files,
            expires_at=expires_at,
        )
    )
    db.commit()
    return {
        "confirm_token": confirm_token,
        "expires_at": utc_iso(expires_at),
        "message": "\u5220\u9664\u786e\u8ba4\u5df2\u521b\u5efa\uff0c\u8bf7\u5728 2 \u5206\u949f\u5185\u5b8c\u6210\u64cd\u4f5c\u3002",
        "target": {
            "downloader_id": downloader_id,
            "hash": torrent_hash,
            "delete_files": delete_files,
            "actor": user.username,
        },
    }


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
    if downloader_id not in {"qb1", "qb2", "qb3"}:
        raise HTTPException(status_code=404, detail="Unknown downloader")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB2 requires administrator verification")
    now = utc_now_naive()
    token_hash = hashlib.sha256(confirm_token.encode("utf-8")).hexdigest()
    consumed = (
        db.query(QbDeleteConfirmation)
        .filter(
            QbDeleteConfirmation.token_hash == token_hash,
            QbDeleteConfirmation.user_id == user.id,
            QbDeleteConfirmation.downloader_id == downloader_id,
            QbDeleteConfirmation.torrent_hash == torrent_hash,
            QbDeleteConfirmation.delete_files.is_(delete_files),
            QbDeleteConfirmation.used_at.is_(None),
            QbDeleteConfirmation.expires_at > now,
        )
        .update({QbDeleteConfirmation.used_at: now}, synchronize_session=False)
    )
    db.commit()
    if consumed != 1:
        raise HTTPException(
            status_code=400,
            detail="\u5220\u9664\u786e\u8ba4\u5df2\u5931\u6548\u6216\u4e0e\u5f53\u524d\u4efb\u52a1\u4e0d\u5339\u914d\uff0c\u8bf7\u91cd\u65b0\u786e\u8ba4\u3002",
        )
    action = "delete_files" if delete_files else "delete"
    try:
        result = get_qb_adapter_or_error(db, downloader_id).mutate_torrent(downloader_id, torrent_hash, action, {"delete_files": delete_files})
    except QbittorrentApiError as exc:
        raise HTTPException(status_code=502, detail=f"qB 删除任务失败：{exc}") from exc
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


def tmdb_access_state(db: Session) -> tuple[dict[str, Any], dict[str, Any] | None]:
    config_row = get_config(db, "tmdb")
    config = get_decrypted_config(db, "tmdb") or {}
    configured = any(str(config.get(key) or "").strip() for key in ("api_key", "bearer_token", "token"))
    enabled = bool(config_row and config_row.enabled)
    if not configured:
        return (
            {
                "source": "tmdb",
                "status": "not_configured",
                "code": "tmdb_not_configured",
                "configured": False,
                "enabled": False,
                "message": "TMDB 尚未配置，请先在设置中填写 API Key 或 Bearer Token，并完成保存、测试与启用。",
            },
            None,
        )
    if not enabled:
        return (
            {
                "source": "tmdb",
                "status": "not_enabled",
                "code": "tmdb_not_enabled",
                "configured": True,
                "enabled": False,
                "message": "TMDB 已配置但尚未启用，请先在设置中完成连接测试并启用。",
            },
            None,
        )
    return (
        {
            "source": "tmdb",
            "status": "ready",
            "code": None,
            "configured": True,
            "enabled": True,
        },
        config,
    )


def enabled_tmdb_discover_config(db: Session) -> dict[str, Any] | None:
    return tmdb_access_state(db)[1]


def require_enabled_tmdb_config(db: Session) -> dict[str, Any]:
    state, config = tmdb_access_state(db)
    if config is None:
        raise HTTPException(status_code=409, detail={"provider": "tmdb", **state})
    return config


def tmdb_discover_cache_context(db: Session) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    state, _ = tmdb_access_state(db)
    return {
        "provider": "tmdb",
        "config_version": int(config_row.config_version) if config_row else 0,
        "enabled": state["enabled"],
        "configured": state["configured"],
        "status": state["status"],
    }


def tmdb_discover_cache_is_current(db: Session, cache: dict[str, Any] | None) -> bool:
    if not cache or cache.get("context") != tmdb_discover_cache_context(db):
        return False
    payload = cache.get("payload")
    if not isinstance(payload, dict):
        return False
    context = cache["context"]
    return (
        payload.get("source") == "tmdb"
        and payload.get("configured") is bool(context["configured"])
        and payload.get("enabled") is bool(context["enabled"])
        and payload.get("status") == context["status"]
    )


def build_discover_payload(db: Session) -> dict[str, Any]:
    state, config = tmdb_access_state(db)
    if config is None:
        return {**state, "trending": [], "popular_movies": [], "popular_tv": [], "top_rated_movies": [], "top_rated_tv": []}
    try:
        return {**TmdbAdapter(config).get_discover_lists(), **state}
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
    state, config = tmdb_access_state(db)
    if config is None:
        page = max(1, int(filters.get("page") or 1))
        pages = max(1, int(filters.get("pages") or 1))
        return {
            **state,
            "filters": filters,
            "items": [],
            "page": page,
            "start_page": page,
            "pages": pages,
            "next_page": None,
            "total_pages": 0,
            "total_results": 0,
            "options": {},
        }
    try:
        return {**TmdbAdapter(config).discover_media(filters), **state}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 条件筛选失败：{exc}") from exc


@router.get("/favorites")
def list_favorites(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = (
        db.query(MediaFavorite)
        .filter(MediaFavorite.user_id == user.id)
        .order_by(MediaFavorite.created_at.desc(), MediaFavorite.id.desc())
        .all()
    )
    return {"items": [serialize_media_favorite(row) for row in rows], "count": len(rows)}


@router.post("/favorites")
def add_favorite(request: FavoritePayload, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    media_type, tmdb_id, payload = normalize_favorite_media(request.media)
    row = (
        db.query(MediaFavorite)
        .filter(
            MediaFavorite.user_id == user.id,
            MediaFavorite.media_type == media_type,
            MediaFavorite.tmdb_id == tmdb_id,
        )
        .one_or_none()
    )
    created = row is None
    if row is None:
        row = MediaFavorite(user_id=user.id, media_type=media_type, tmdb_id=tmdb_id, media_payload=payload)
        db.add(row)
    else:
        row.media_payload = payload
        row.updated_at = utc_now_naive()
    db.commit()
    db.refresh(row)
    return {"item": serialize_media_favorite(row), "created": created}


@router.delete("/favorites/{media_type}/{tmdb_id}")
def remove_favorite(media_type: str, tmdb_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    normalized_type = media_type.strip().lower()
    if normalized_type not in {"movie", "tv"}:
        raise HTTPException(status_code=422, detail="收藏条目类型无效。")
    row = (
        db.query(MediaFavorite)
        .filter(
            MediaFavorite.user_id == user.id,
            MediaFavorite.media_type == normalized_type,
            MediaFavorite.tmdb_id == tmdb_id,
        )
        .one_or_none()
    )
    if row is not None:
        db.delete(row)
        db.commit()
    return {"removed": row is not None, "media_type": normalized_type, "tmdb_id": tmdb_id}


def refresh_discover_filter_preload(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    payload = build_discover_filter_payload(db, filters)
    set_preload_cache(db, discover_filter_cache_name(filters), payload, context=tmdb_discover_cache_context(db))
    return payload


def refresh_discover_preload(db: Session) -> dict[str, Any]:
    payload = build_discover_payload(db)
    set_preload_cache(db, "discover", payload, context=tmdb_discover_cache_context(db))
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
        if tmdb_discover_cache_is_current(db, cache):
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
        if tmdb_discover_cache_is_current(db, cache):
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
    proxy_enabled, proxy_url, proxy_domains, timeout = _tmdb_image_network_config(db)
    mode = "proxy" if proxy_enabled and "image.tmdb.org" in proxy_domains else "direct"
    try:
        with open_tmdb_network_request(request, proxy_enabled, proxy_url, proxy_domains, timeout) as response:
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
            tmdb_network_detail(proxy_enabled, proxy_url, proxy_domains).get("proxy_url", ""),
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
    config = require_enabled_tmdb_config(db)
    if not q.strip():
        return {"items": []}
    try:
        return {"items": TmdbAdapter(config).search_media(q)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 搜索失败：{exc}") from exc


@router.get("/tmdb/media/{media_type}/{media_id}")
def tmdb_media_detail(media_type: str, media_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    if media_type not in {"movie", "tv"}:
        raise HTTPException(status_code=404, detail="不支持的 TMDB 媒体类型")
    try:
        return TmdbAdapter(require_enabled_tmdb_config(db)).get_media_details(media_id, media_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 详情获取失败：{exc}") from exc


@router.get("/tmdb/person/{person_id}")
def tmdb_person_detail(person_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return TmdbAdapter(require_enabled_tmdb_config(db)).get_person_details(person_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 演员详情获取失败：{exc}") from exc


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
    series = [{"downloader_id": row.downloader_id, "download_speed": row.download_speed, "upload_speed": row.upload_speed, "captured_at": utc_iso(row.captured_at), "source": "qB 后台快照", "completeness": row.completeness} for row in reversed(snapshots)]
    return {
        "ranges": ["24h", "7d", "30d", "custom"],
        "series": series,
        "explainability": {"source": "qB 下载器后台快照", "formula": "按采集时间展示各下载器的上传与下载速度", "completeness": "已有可用快照" if series else "暂无快照"},
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
        "handled_at": utc_iso(),
    }


@router.post("/assistant/chat")
def assistant_chat(
    request: AssistantChatRequest,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    adapter = get_ai_adapter_or_error(db)
    conversation_id = request.conversation_id.strip() or f"web-{user.id}"
    mobile_request = WechatClawMessageRequest(
        message=request.message,
        user_id=f"web-{user.id}",
        conversation_id=conversation_id,
    )
    return run_media_hub_agent(db, adapter, mobile_request, user, qb2_authorized=has_qb2_grant(db, authorization))

@router.get("/notification-preferences")
def notification_preferences(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": get_notification_preferences(db), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.put("/notification-preferences")
def update_notification_preferences(request: NotificationPreferencesPayload, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"preferences": save_notification_preferences(db, request.preferences), "defaults": DEFAULT_NOTIFICATION_PREFERENCES}


@router.get("/admin/wechat-claw/bindings")
def list_wechat_claw_bindings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    return {
        "items": [wechat_claw_binding_status(db, binding) for binding in bindings],
        "limit": WECHAT_CLAW_MEMBER_LIMIT,
    }


@router.post("/admin/wechat-claw/bindings")
def create_wechat_claw_binding(
    request: WechatClawBindingPayload,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    with _WECHAT_CLAW_BINDING_CREATE_LOCK:
        ensure_wechat_claw_bindings(db)
        if db.query(WechatClawBinding).count() >= WECHAT_CLAW_MEMBER_LIMIT:
            raise HTTPException(
                status_code=409,
                detail="最多可添加 5 位微信成员。如需添加新成员，请先删除一位不再使用的成员。",
            )
        binding = WechatClawBinding(
            display_name=request.display_name.strip() or next_wechat_claw_member_name(db),
            role_name=request.role_name.strip() or "微信助手",
            avatar_key=next_wechat_claw_avatar_key(db),
            enabled=request.enabled,
            notification_preferences=normalized_wechat_claw_binding_preferences(request.notification_preferences),
        )
        db.add(binding)
        db.commit()
        db.refresh(binding)
    return serialize_wechat_claw_binding(binding)


@router.patch("/admin/wechat-claw/bindings/{binding_id}")
def update_wechat_claw_binding(
    binding_id: int,
    request: WechatClawBindingPayload,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    binding = get_wechat_claw_binding_or_error(db, binding_id)
    binding.display_name = request.display_name.strip() or binding.display_name
    binding.role_name = request.role_name.strip() or binding.role_name
    binding.enabled = request.enabled
    binding.notification_preferences = normalized_wechat_claw_binding_preferences(request.notification_preferences)
    binding.updated_at = utc_now_naive()
    db.commit()
    db.refresh(binding)
    return serialize_wechat_claw_binding(binding)


@router.delete("/admin/wechat-claw/bindings/{binding_id}")
def delete_wechat_claw_binding(binding_id: int, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    binding = get_wechat_claw_binding_or_error(db, binding_id)
    for key in (WECHAT_CLAW_ILINK_STATE_KEY, WECHAT_CLAW_INTERACTIONS_KEY, WECHAT_CLAW_LAST_POLL_KEY):
        row = db.query(Setting).filter(Setting.key == wechat_claw_setting_key(key, binding.id)).one_or_none()
        if row:
            db.delete(row)
    db.delete(binding)
    db.commit()
    return {"deleted": True, "id": binding_id}


@router.get("/admin/wechat-claw/bindings/{binding_id}/setup")
def wechat_claw_binding_setup(
    binding_id: int,
    refresh_remote: bool = Query(default=True),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_binding_or_error(db, binding_id)
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if refresh_remote:
            refresh_wechat_claw_login_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/admin/wechat-claw/bindings/{binding_id}/{action}")
def wechat_claw_binding_action(
    binding_id: int,
    action: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    get_wechat_claw_binding_or_error(db, binding_id)
    if action not in {"refresh", "logout", "poll"}:
        raise HTTPException(status_code=404, detail="未知的 WeChat claw 操作")
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if action == "refresh":
            return _refresh_wechat_claw_qrcode(db, binding_id)
        if action == "logout":
            clear_wechat_claw_ilink_state(db)
            return build_wechat_claw_setup(db, binding_id)
        return poll_wechat_claw_messages(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.get("/admin/wechat-claw/setup")
def admin_wechat_claw_setup(
    refresh_remote: bool = Query(default=True),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        return build_wechat_claw_setup(db)
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        if refresh_remote:
            refresh_wechat_claw_login_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


@router.post("/admin/wechat-claw/refresh")
def admin_wechat_claw_refresh(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        raise HTTPException(status_code=409, detail="请先添加一个 WeChat claw 绑定")
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        return _refresh_wechat_claw_qrcode(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def _refresh_wechat_claw_qrcode(db: Session, binding_id: int) -> dict[str, Any]:
    config = get_decrypted_config(db, "wechat_claw") or {}
    adapter = WechatClawAdapter(config)
    result = adapter.get_qrcode()
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message") or "获取微信二维码失败")
    qrcode = {
        "qrcode": result.get("qrcode"),
        "qrcode_url": result.get("qrcode_url"),
        "status": result.get("status") or "waiting",
        "updated_at": int(utc_now().timestamp()),
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
    return build_wechat_claw_setup(db, binding_id)


@router.post("/admin/wechat-claw/logout")
def admin_wechat_claw_logout(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    bindings = ensure_wechat_claw_bindings(db)
    if not bindings:
        raise HTTPException(status_code=409, detail="请先添加一个 WeChat claw 绑定")
    binding_id = _WECHAT_CLAW_ACTIVE_USER_ID.get() or bindings[0].id
    scope = _WECHAT_CLAW_ACTIVE_USER_ID.set(binding_id)
    try:
        clear_wechat_claw_ilink_state(db)
        return build_wechat_claw_setup(db, binding_id)
    finally:
        _WECHAT_CLAW_ACTIVE_USER_ID.reset(scope)


def require_wechat_claw_token(db: Session, header_token: str | None, query_token: str | None) -> None:
    config = get_decrypted_config(db, "wechat_claw") or {}
    expected = str(config.get("inbound_token") or "").strip()
    provided = str(header_token or query_token or "").strip()
    if not expected or not provided or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid WeChat claw token")


def assistant_actor_for_wechat_claw(db: Session) -> User:
    user = db.query(User).filter(User.role == "admin", User.is_active.is_(True)).order_by(User.id.asc()).first()
    if user is None:
        raise HTTPException(status_code=409, detail="请先初始化管理员账号")
    return user


def handle_wechat_claw_text(
    db: Session,
    request: WechatClawMessageRequest,
    telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    telemetry = telemetry or new_wechat_claw_interaction_telemetry()
    interaction_status = "completed"
    user = assistant_actor_for_wechat_claw(db)
    verify_match = WECHAT_CLAW_ADMIN_VERIFY_RE.match(request.message or "")
    provided_password = str(request.admin_password or (verify_match.group(1) if verify_match else "")).strip()
    if provided_password:
        parsed_intent = {"intent_type": "dashboard_query", "action": "dashboard_query", "dashboard_sections": ["qb2"]}
        if verify_password(provided_password, user.password_hash):
            grant_wechat_claw_privacy_access(db, request)
            result = {"intent_type": "dashboard_query", "privacy_granted": True, "qb2": {}}
            reply = "【管理员验证成功】\n当前微信会话已获得 qB2 隐私详情权限，有效期 15 分钟。"
        else:
            result = {"intent_type": "dashboard_query", "privacy_granted": False}
            reply = "【管理员验证失败】\n密码不正确，未授予 qB2 隐私详情权限。"
        interaction = record_wechat_claw_interaction(db, request, parsed_intent, result, reply, telemetry)
        return {"reply": reply, "intent": parsed_intent, "result": result, "interaction": interaction, "source": "wechat_claw", "conversation_id": request.conversation_id, "handled_at": utc_iso()}

    adapter_started = time.perf_counter()
    try:
        ai_adapter = get_ai_adapter_or_error(db)
    except HTTPException as exc:
        record_wechat_claw_stage(telemetry, "ai_adapter", adapter_started, status="failed", error=exc.detail)
        raise
    record_wechat_claw_stage(telemetry, "ai_adapter", adapter_started)
    agent_result = run_media_hub_agent(db, ai_adapter, request, user, telemetry)
    observations = agent_result.get("result", {}).get("observations", [])
    interaction_status = "failed" if any(
        isinstance(item, dict) and isinstance(item.get("result"), dict) and item["result"].get("state") == "failed"
        for item in observations
    ) else "completed"
    interaction = record_wechat_claw_interaction(
        db,
        request,
        agent_result["intent"],
        agent_result["result"],
        agent_result["reply"],
        telemetry,
        status=interaction_status,
    )
    return {
        **agent_result,
        "interaction": interaction,
        "conversation_id": request.conversation_id,
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
        return save_wechat_claw_last_poll(db, {"success": False, "stage": "login", "message": "尚未完成微信绑定，请扫描二维码后重试。"})

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
                "message": "微信登录已过期，请刷新二维码重新绑定。",
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


def live_diagnostic_error(error: Any) -> str:
    message = str(redact_payload(str(error))).strip()
    return (message or "实时检测未通过，请检查配置或网络。")[:300]


def live_diagnostic_result(
    db: Session,
    module: str,
    *,
    enabled: bool,
    configured: bool,
    status: str,
    checked_at: str,
    duration_ms: int,
    message: str,
    error: str = "",
) -> dict[str, Any]:
    collection = get_module_health(db, module)
    return {
        "module": module,
        "enabled": enabled,
        "configured": configured,
        "status": status,
        "live": True,
        "checked_at": checked_at,
        "last_success_at": checked_at if status == "success" else collection.get("last_success_at"),
        "duration_ms": duration_ms,
        "message": message,
        "last_error": error if status == "failed" else "",
        "consecutive_failed_hours": int(collection.get("consecutive_failed_hours") or 0),
    }


def probe_live_integration(module: str, config: dict[str, Any]) -> tuple[bool, str]:
    if module == "mteam":
        detail = MTeamAdapter(config).test_connection()
        message = "已实时读取 M-Team 用户资料。"
    elif module in {"qb1", "qb2", "qb3"}:
        detail = QbittorrentWebAdapter(config).test_connection()
        version = str(detail.get("version") or "未知版本") if isinstance(detail, dict) else "未知版本"
        message = f"已实时连接 qB WebUI，当前版本 {version}。"
    elif module == "tmdb":
        detail = TmdbAdapter(config).test_connection()
        message = "已实时访问 TMDB 数据接口与图片资源。"
    elif module == "ai":
        detail = DeepSeekChatAdapter(config).test_connection()
        model = str(detail.get("model") or config.get("model") or "当前模型") if isinstance(detail, dict) else "当前模型"
        message = f"影视中枢 Agent 连接正常，当前模型为 {model}。"
    else:
        raise ValueError(f"Unsupported live diagnostic module: {module}")

    detail = detail if isinstance(detail, dict) else {}
    success = detail.get("success") is not False and detail.get("ok") is not False
    if not success:
        return False, str(detail.get("message") or "实时检测未通过，请检查配置或网络。")
    return True, message


def probe_live_wechat_claw(db: Session, config: dict[str, Any]) -> tuple[bool, str, list[dict[str, Any]]]:
    bindings = ensure_wechat_claw_bindings(db)
    members: list[dict[str, Any]] = []
    mode = str(config.get("mode") or "ilink").strip().lower()
    checked_at = utc_iso()

    if mode != "ilink":
        detail = WechatClawAdapter(config).test_connection()
        success = detail.get("success") is not False if isinstance(detail, dict) else True
        members = [wechat_claw_binding_status(db, binding) for binding in bindings]
        return success, "已实时验证 WeChat claw 直连配置。" if success else "WeChat claw 直连检测未通过。", members

    active_count = 0
    connected_count = 0
    for binding in bindings:
        member = wechat_claw_binding_status(db, binding)
        state = get_wechat_claw_ilink_state(db, binding.id)
        saved_connection = bool(state.get("bot_token"))
        member.update({"saved_connection": saved_connection, "live_checked_at": checked_at})
        if not binding.enabled:
            member.update({"live_connection_ok": None, "live_message": "成员已停用，本次未发起连接检测。"})
        elif not saved_connection:
            active_count += 1
            member.update({"connected": False, "operational": False, "live_connection_ok": False, "live_message": "尚未完成微信扫码绑定。"})
        else:
            active_count += 1
            try:
                detail = WechatClawAdapter(wechat_claw_config_with_state(config, state)).test_connection()
                live_ok = bool(
                    isinstance(detail, dict)
                    and detail.get("success") is not False
                    and detail.get("connected") is not False
                )
                member.update(
                    {
                        "connected": live_ok,
                        "operational": bool(member.get("module_enabled")) and live_ok,
                        "live_connection_ok": live_ok,
                        "live_message": str(detail.get("message") or ("微信连接正常。" if live_ok else "微信登录状态暂不可用。")),
                    }
                )
                if live_ok:
                    connected_count += 1
            except Exception as exc:
                member.update(
                    {
                        "connected": False,
                        "operational": False,
                        "live_connection_ok": False,
                        "live_message": live_diagnostic_error(exc),
                    }
                )
        members.append(member)

    success = active_count > 0 and connected_count > 0
    if success:
        message = f"已实时验证微信连接，{connected_count} 位成员当前可用。"
    elif active_count == 0:
        message = "没有已启用的 WeChat claw 成员。"
    else:
        message = "当前没有可用的微信连接，请检查扫码或登录状态。"
    return success, message, members


def run_live_diagnostic_module(db: Session, module: str) -> dict[str, Any]:
    if module not in LIVE_DIAGNOSTIC_MODULES:
        raise HTTPException(status_code=404, detail="Unknown diagnostic module")

    started_at = time.perf_counter()
    checked_at = utc_iso()
    member_statuses: list[dict[str, Any]] = []

    if module == "nas_disk":
        try:
            storage = storage_status_from_setup_check(db, refresh=True)
            success = bool(storage.get("nas_storage_readable"))
            errors = storage.get("nas_storage_errors") if isinstance(storage.get("nas_storage_errors"), list) else []
            message = "已实时确认 NAS 存储路径可以访问。" if success else str(errors[0] if errors else "请在 YAML 中填写并挂载可访问的 NAS 文件夹路径。")
            error = "" if success else live_diagnostic_error(message)
        except Exception as exc:
            success = False
            message = live_diagnostic_error(exc)
            error = message
        module_result = live_diagnostic_result(
            db,
            module,
            enabled=True,
            configured=True,
            status="success" if success else "failed",
            checked_at=checked_at,
            duration_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
            message=message,
            error=error,
        )
        return {"module": module_result}

    row = get_config(db, module)
    configured = bool(row and row.encrypted_payload)
    if module == "wechat_claw":
        bindings = ensure_wechat_claw_bindings(db)
        member_statuses = [wechat_claw_binding_status(db, binding) for binding in bindings]
    if not row or not row.enabled:
        module_result = live_diagnostic_result(
            db,
            module,
            enabled=False,
            configured=configured,
            status="disabled",
            checked_at=checked_at,
            duration_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
            message="当前模块未启用，本次未发起外部请求。",
        )
        payload: dict[str, Any] = {"module": module_result}
        if module == "wechat_claw":
            payload["wechat_members"] = member_statuses
        return payload

    config = get_decrypted_config(db, module) or {}
    try:
        if module == "wechat_claw":
            success, message, member_statuses = probe_live_wechat_claw(db, config)
        else:
            success, message = probe_live_integration(module, config)
        error = "" if success else live_diagnostic_error(message)
    except Exception as exc:
        success = False
        message = live_diagnostic_error(exc)
        error = message

    module_result = live_diagnostic_result(
        db,
        module,
        enabled=True,
        configured=configured,
        status="success" if success else "failed",
        checked_at=checked_at,
        duration_ms=max(0, round((time.perf_counter() - started_at) * 1000)),
        message=message,
        error=error,
    )
    payload = {"module": module_result}
    if module == "wechat_claw":
        payload["wechat_members"] = member_statuses
    return payload


@router.post("/diagnostics/modules/{module}/check")
def check_diagnostic_module(module: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return run_live_diagnostic_module(db, module)

@router.get("/diagnostics/health")
def diagnostics_health(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    integrations = {row.provider: row for row in db.query(IntegrationConfig).all()}
    bindings = ensure_wechat_claw_bindings(db)
    member_statuses = [wechat_claw_binding_status(db, binding) for binding in bindings]
    wechat_claw_connected = any(member["operational"] for member in member_statuses)
    storage = storage_status_from_setup_check(db)
    modules = []
    for provider in PROVIDERS + ["nas_disk"]:
        row = integrations.get(provider)
        result = public_test_result(row.last_test_result) if row else None
        if provider == "nas_disk":
            result = {
                "success": storage["nas_storage_readable"],
                "message": "NAS 存储路径可访问。" if storage["nas_storage_readable"] else "请在 YAML 中填写并挂载 NAS 文件夹路径。",
            }
        elif provider == "wechat_claw" and row and row.enabled and result and result.get("success") is True and not wechat_claw_connected:
            result = {**result, "success": False, "message": "尚未完成微信扫码绑定。"}
        if result and result.get("success") is True:
            status = "success"
        elif result and result.get("success") is False:
            status = "failed"
        else:
            status = "not_tested"
        collection = get_module_health(db, provider)
        modules.append({"module": provider, "status": status, "enabled": bool(row.enabled) if row else provider in ["nas_disk", "stats_engine"], "last_success_at": collection.get("last_success_at") or (utc_iso(row.last_tested_at) if row and row.last_tested_at else None), "duration_ms": result.get("duration_ms") if result else None, "last_error": collection.get("last_error") or (result.get("message") if isinstance(result, dict) else None), "consecutive_failed_hours": int(collection.get("consecutive_failed_hours") or 0)})
    return {"modules": modules, "wechat_members": member_statuses}


@router.get("/diagnostics/traces")
def diagnostics_traces(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = (
        db.query(DebugTrace)
        .filter(DebugTrace.event_type == "agent_run")
        .order_by(DebugTrace.created_at.desc())
        .limit(100)
        .all()
    )
    return {"items": [{"trace_id": row.trace_id, "event_type": row.event_type, "status": row.status, "timeline": row.timeline, "duration_ms": row.duration_ms, "config_version": row.config_version, "error_summary": row.error_summary, "created_at": utc_iso(row.created_at)} for row in rows]}
