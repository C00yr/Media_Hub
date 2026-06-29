from datetime import datetime, timedelta
from socket import timeout as SocketTimeout
from typing import Any
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.mock import MockMetadataAdapter, MockQbittorrentAdapter, MockTrackerAdapter
from app.adapters.tmdb import TmdbAdapter
from app.adapters.tmdb.client import TmdbConfigError
from app.api.deps import get_current_user, has_qb2_grant, require_admin
from app.auth.security import create_access_token, hash_password, verify_password
from app.config.settings import get_settings
from app.db.session import get_db
from app.diagnostics.tracing import TraceRecorder
from app.models.entities import (
    ConfigAuditLog,
    DebugTrace,
    DiagnosticExport,
    DownloadAction,
    IntegrationConfig,
    Notification,
    QbSnapshot,
    Setting,
    StatResult,
    User,
    UserSession,
)
from app.services.integrations import (
    PROVIDERS,
    get_decrypted_config,
    get_config,
    record_test_result,
    serialize_config,
    set_enabled,
    upsert_config,
)
from app.utils.ids import trace_id
from app.utils.redaction import redact_payload


router = APIRouter()
settings = get_settings()


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


def bytes_label(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.1f} {unit}"
        current /= 1024
    return f"{current:.1f} PB"


def tmdb_test_result(success: bool, trace: str, message: str, explanation: str, next_step: str, error_type: str | None = None, http_status: int | None = None) -> dict[str, Any]:
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
            None,
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
            return tmdb_test_result(
                False,
                trace,
                "TMDB 请求太频繁。",
                "TMDB 暂时限制了请求频率，密钥本身不一定有问题。",
                "请等待几分钟后再点击“保存并测试”。",
                "rate_limited",
                status_code,
            )
        if status_code >= 500:
            return tmdb_test_result(
                False,
                trace,
                "TMDB 服务暂时不可用。",
                "TMDB 服务器返回了异常状态，通常需要稍后重试。",
                "请稍后再测试。如果一直失败，再检查网络或代理设置。",
                "tmdb_service_error",
                status_code,
            )
        return tmdb_test_result(
            False,
            trace,
            "TMDB 返回了无法识别的错误。",
            f"TMDB 返回 HTTP {status_code}，应用无法确认具体原因。",
            "请检查密钥、语言/地区设置和网络环境，然后重试。",
            "http_error",
            status_code,
        )
    if isinstance(exc, (TimeoutError, SocketTimeout)):
        return tmdb_test_result(
            False,
            trace,
            "连接 TMDB 超时。",
            "应用等待 TMDB 响应太久，可能是当前网络访问 TMDB 较慢或被阻断。",
            "请检查网络、代理、Docker/NAS 出网能力，或把超时时间调大后再测试。",
            "timeout",
            None,
        )
    if isinstance(exc, URLError):
        reason = str(getattr(exc, "reason", exc))
        return tmdb_test_result(
            False,
            trace,
            "无法连接到 TMDB。",
            "应用没有连上 TMDB，常见原因是 DNS、代理、NAS/Docker 出网或防火墙问题。",
            f"请先确认这台机器能访问 api.themoviedb.org。系统底层提示：{reason}",
            "network_error",
            None,
        )
    return tmdb_test_result(
        False,
        trace,
        "TMDB 测试失败。",
        "应用遇到了未能自动识别的问题。",
        "请检查填写内容后重试；如果仍然失败，可以把这条提示截图发给开发者排查。",
        "unknown_error",
        None,
    )


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    initialized = db.query(User).count() > 0
    return {"initialized": initialized}


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


@router.post("/auth/admin-verify")
def admin_verify(
    request: AdminVerifyRequest,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    admin = db.query(User).filter(User.username == request.username, User.role == "admin").one_or_none()
    if not admin or not verify_password(request.password, admin.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="管理员验证失败")
    payload = authorization.split(" ", 1)[1]
    token_payload = __import__("app.auth.security", fromlist=["decode_token"]).decode_token(payload)
    session = db.query(UserSession).filter(UserSession.token_id == token_payload["jti"]).one()
    expires = datetime.utcnow() + timedelta(minutes=settings.qb2_grant_minutes)
    session.qb2_grant_expires_at = expires
    db.commit()
    trace = TraceRecorder(db, "admin_verify", "ADMIN")
    trace.add("qB 2 grant issued", {"actor": user.username, "admin": admin.username, "expires_at": expires.isoformat()})
    trace.finish()
    return {"qb2_granted": True, "expires_at": expires.isoformat()}


@router.get("/admin/integrations")
def list_integrations(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    existing = {row.provider: row for row in db.query(IntegrationConfig).all()}
    return {
        "providers": [
            serialize_config(existing[provider])
            if provider in existing
            else {
                "provider": provider,
                "config_version": 0,
                "enabled": False,
                "redacted_summary": {},
                "last_tested_at": None,
                "last_test_result": None,
                "updated_at": None,
            }
            for provider in PROVIDERS
        ]
    }


@router.put("/admin/integrations/{provider}")
def save_integration(
    provider: str,
    request: IntegrationPayload,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    row = upsert_config(db, provider, request.payload, actor_user_id=user.id)
    return serialize_config(row)


@router.post("/admin/integrations/{provider}/test")
def test_integration(
    provider: str,
    request: IntegrationPayload | None = None,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if request and request.payload:
        row = upsert_config(db, provider, request.payload, actor_user_id=user.id, action="save_and_test")
    else:
        row = get_config(db, provider)
    test_trace_id = trace_id("CFGTEST")
    if provider == "tmdb":
        try:
            config = get_decrypted_config(db, provider) or {}
            adapter = TmdbAdapter(config)
            adapter.get_discover_lists()
            result = tmdb_test_result(
                True,
                test_trace_id,
                "TMDB 连接成功。",
                "应用已经成功访问 TMDB，并拿到了发现页需要的片单数据。",
                "你现在可以点击“启用”，然后回到“发现”页查看真实 TMDB 数据。",
                None,
                200,
            )
        except Exception as exc:
            result = classify_tmdb_test_error(exc, test_trace_id)
        result["config_version"] = row.config_version if row else 0
    else:
        result = {
            "success": True,
            "provider": provider,
            "mode": "mock",
            "http_status": 200,
            "duration_ms": 120,
            "message": "Mock 只读连接测试成功。",
            "trace_id": test_trace_id,
            "config_version": row.config_version if row else 0,
        }
    saved = record_test_result(db, provider, result, actor_user_id=user.id)
    return serialize_config(saved)


@router.post("/admin/integrations/{provider}/enable")
def enable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if provider == "tmdb":
        row = get_config(db, provider)
        result = row.last_test_result if row else None
        if not result or result.get("provider") != "tmdb" or result.get("mode") != "real" or result.get("can_enable") is not True:
            raise HTTPException(status_code=409, detail="请先点击“保存并测试”，确认 TMDB 连接成功后再启用。")
    return serialize_config(set_enabled(db, provider, True, actor_user_id=user.id))


@router.post("/admin/integrations/{provider}/disable")
def disable_integration(provider: str, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return serialize_config(set_enabled(db, provider, False, actor_user_id=user.id))


@router.get("/admin/integrations/{provider}/audit")
def integration_audit(provider: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = (
        db.query(ConfigAuditLog)
        .filter(ConfigAuditLog.provider == provider)
        .order_by(ConfigAuditLog.created_at.desc())
        .limit(30)
        .all()
    )
    return {
        "items": [
            {
                "action": row.action,
                "config_version": row.config_version,
                "test_success": row.test_success,
                "trace_id": row.trace_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/dashboard")
def dashboard(authorization: str | None = Header(default=None), db: Session = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    tracker = MockTrackerAdapter()
    qb = MockQbittorrentAdapter()
    mteam = tracker.get_user_stats()
    qb2_allowed = has_qb2_grant(db, authorization)
    qbs = []
    for downloader_id in ["qb1", "qb2", "qb3"]:
        if downloader_id == "qb2" and not qb2_allowed:
            qbs.append({"id": "qb2", "name": "qB 2", "locked": True, "message": "私有下载器需要管理员验证。"})
        else:
            qbs.append(qb.get_server_state(downloader_id))
    return {
        "overview": {
            "total_download_speed": sum(item.get("download_speed", 0) for item in qbs),
            "total_upload_speed": sum(item.get("upload_speed", 0) for item in qbs),
            "nas_free_space": 3.5 * 1024**4,
            "nas_free_space_label": bytes_label(3.5 * 1024**4),
            "download_tasks": sum(item.get("active_downloads", 0) for item in qbs),
            "upload_tasks": sum(item.get("active_uploads", 0) for item in qbs),
        },
        "mteam": mteam,
        "qbs": qbs,
    }


@router.get("/mteam/stats")
def mteam_stats(_: User = Depends(get_current_user)) -> dict[str, Any]:
    return MockTrackerAdapter().get_user_stats()


@router.get("/qb/{downloader_id}/summary")
def qb_summary(
    downloader_id: str,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB 2 需要管理员验证")
    return MockQbittorrentAdapter().get_server_state(downloader_id)


@router.get("/qb/{downloader_id}/torrents")
def qb_torrents(
    downloader_id: str,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB 2 需要管理员验证")
    return {"items": MockQbittorrentAdapter().get_torrents(downloader_id)}


@router.post("/qb/{downloader_id}/torrents")
def qb_add_torrent(
    downloader_id: str,
    request: QbActionPayload,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB 2 需要管理员验证")
    result = MockQbittorrentAdapter().add_torrent(downloader_id, request.payload)
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action="add", actor_user_id=user.id, status="accepted"))
    db.commit()
    return result


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/{action}")
def qb_mutate(
    downloader_id: str,
    torrent_hash: str,
    action: str,
    request: QbActionPayload,
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    allowed = {"pause", "resume", "limits", "category", "tags"}
    if action not in allowed:
        raise HTTPException(status_code=404, detail="不支持的操作")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB 2 需要管理员验证")
    result = MockQbittorrentAdapter().mutate_torrent(downloader_id, torrent_hash, action, request.payload)
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/delete-confirm")
def qb_delete_confirm(downloader_id: str, torrent_hash: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {
        "confirm_token": trace_id("DEL"),
        "message": "用户确认风险后，请在 DELETE 请求中提交这个确认令牌。",
        "target": {"downloader_id": downloader_id, "hash": torrent_hash, "actor": user.username},
    }


@router.delete("/qb/{downloader_id}/torrents/{torrent_hash}")
def qb_delete(
    downloader_id: str,
    torrent_hash: str,
    confirm_token: str = Query(min_length=8),
    delete_files: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not confirm_token.startswith("DEL-"):
        raise HTTPException(status_code=400, detail="需要服务端删除确认令牌")
    if downloader_id == "qb2" and not has_qb2_grant(db, authorization):
        raise HTTPException(status_code=403, detail="qB 2 需要管理员验证")
    action = "delete_files" if delete_files else "delete"
    result = MockQbittorrentAdapter().mutate_torrent(downloader_id, torrent_hash, action, {"delete_files": delete_files})
    db.add(DownloadAction(trace_id=result["trace_id"], downloader_id=downloader_id, action=action, target_hash=torrent_hash, actor_user_id=user.id))
    db.commit()
    return result


@router.get("/discover/lists")
def discover_lists(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    config_row = get_config(db, "tmdb")
    if not config_row or not config_row.enabled:
        return {
            "source": "tmdb",
            "configured": False,
            "message": "请先在设置中保存并启用 TMDB API Key 或 Bearer Token。",
            "trending": [],
            "popular_movies": [],
            "popular_tv": [],
            "top_rated_movies": [],
            "top_rated_tv": [],
        }
    try:
        config = get_decrypted_config(db, "tmdb") or {}
        return TmdbAdapter(config).get_discover_lists()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"TMDB 数据获取失败：{exc}") from exc


@router.get("/search/media")
def search_media(q: str = Query(default=""), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"items": MockMetadataAdapter().search_media(q)}


@router.get("/search/mteam")
def search_mteam(q: str = Query(default=""), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"items": MockTrackerAdapter().search_torrents(q)}


@router.get("/stats")
def stats(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    snapshots = db.query(QbSnapshot).order_by(QbSnapshot.captured_at.desc()).limit(18).all()
    if not snapshots:
        snapshots = []
    series = [
        {
            "downloader_id": row.downloader_id,
            "download_speed": row.download_speed,
            "upload_speed": row.upload_speed,
            "captured_at": row.captured_at.isoformat(),
            "source": row.source,
            "completeness": row.completeness,
        }
        for row in reversed(snapshots)
    ]
    return {
        "ranges": ["24h", "7d", "30d", "custom"],
        "series": series,
        "explainability": {
            "source": "下载器原始数据与应用计算数据（Mock）",
            "formula": "周期增量 = 当前有效快照 - 起始有效快照",
            "completeness": "完整" if series else "部分缺失",
        },
    }


@router.get("/notifications")
def notifications(_: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(Notification).order_by(Notification.created_at.desc()).limit(50).all()
    if not rows:
        rows = [
            Notification(title="示例通知", message="下载监控已在线。", level="info", source="mock"),
            Notification(title="示例阈值", message="NAS 剩余空间状态健康。", level="success", source="mock"),
        ]
    return {
        "items": [
            {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "level": row.level,
                "read": row.read,
                "source": row.source,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get("/diagnostics/health")
def diagnostics_health(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    integrations = {row.provider: row for row in db.query(IntegrationConfig).all()}
    modules = []
    for provider in PROVIDERS + ["nas_disk", "stats_engine"]:
        row = integrations.get(provider)
        result = row.last_test_result if row else None
        modules.append(
            {
                "module": provider,
                "status": "success" if (result or provider in ["nas_disk", "stats_engine"]) else "not_tested",
                "enabled": bool(row.enabled) if row else provider in ["nas_disk", "stats_engine"],
                "last_success_at": row.last_tested_at.isoformat() if row and row.last_tested_at else None,
                "duration_ms": result.get("duration_ms") if result else None,
                "last_error": None,
            }
        )
    return {"modules": modules}


@router.get("/diagnostics/traces")
def diagnostics_traces(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.query(DebugTrace).order_by(DebugTrace.created_at.desc()).limit(50).all()
    return {
        "items": [
            {
                "trace_id": row.trace_id,
                "event_type": row.event_type,
                "status": row.status,
                "timeline": row.timeline,
                "duration_ms": row.duration_ms,
                "config_version": row.config_version,
                "error_summary": row.error_summary,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


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
