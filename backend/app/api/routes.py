import shutil
import subprocess
import os
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from re import fullmatch
from socket import timeout as SocketTimeout
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.mock import MockMetadataAdapter
from app.adapters.mteam import MTeamAdapter, MTeamApiError, MTeamConfigError
from app.adapters.qbittorrent import QbittorrentApiError, QbittorrentConfigError, QbittorrentWebAdapter
from app.adapters.tmdb import TmdbAdapter
from app.adapters.tmdb.client import TmdbConfigError, TmdbDohError, TmdbGatewayError
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
PRELOAD_PREFIX = "preload."


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


def _wrangler_cwd() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    candidates = [
        repo_root / "tmdb-gateway",
        repo_root / "cloudflare" / "tmdb-gateway",
    ]
    for candidate in candidates:
        if (candidate / "package.json").exists() or (candidate / "wrangler.jsonc").exists() or (candidate / "wrangler.toml").exists():
            return candidate
    return repo_root


def _worker_name_from_gateway_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.split(":", 1)[0].strip().lower()
    suffix = ".workers.dev"
    if not host.endswith(suffix):
        return ""
    parts = host[: -len(suffix)].split(".")
    return parts[0] if len(parts) >= 2 else ""


def _normalize_worker_name(payload: dict[str, Any]) -> str:
    worker_name = str(payload.get("worker_name") or "").strip()
    if not worker_name:
        worker_name = _worker_name_from_gateway_url(str(payload.get("gateway_url") or "").strip())
    if not worker_name:
        raise HTTPException(status_code=400, detail="请填写 Worker 名称，或先填写完整的 workers.dev 地址。")
    if not fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", worker_name):
        raise HTTPException(status_code=400, detail="Worker 名称只能包含小写字母、数字和连字符，且不能以连字符开头或结尾。")
    return worker_name


def _wrangler_command() -> str | None:
    return shutil.which("npx.cmd") or shutil.which("npx")


def _sanitize_wrangler_output(value: str) -> str:
    lines = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "https://dash.cloudflare.com/oauth2/auth" in stripped:
            lines.append("Wrangler 需要重新登录 Cloudflare。")
            continue
        lines.append(stripped)
    return "\n".join(lines[-8:])


def _put_worker_secret(cwd: Path, worker_name: str, secret_name: str, secret_value: str) -> tuple[bool, str]:
    command = _wrangler_command()
    if not command:
        return False, "当前运行环境没有找到 npx，无法调用 Wrangler CLI。"
    try:
        result = subprocess.run(
            [command, "wrangler", "secret", "put", secret_name, "--name", worker_name],
            cwd=str(cwd),
            input=f"{secret_value}\n",
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"写入 {secret_name} 超时。请确认 Wrangler 已登录，网络可以访问 Cloudflare。"
    except OSError as exc:
        return False, f"无法启动 Wrangler CLI：{exc}"
    if result.returncode == 0:
        return True, ""
    detail = _sanitize_wrangler_output(f"{result.stdout}\n{result.stderr}")
    return False, detail or f"Wrangler 写入 {secret_name} 失败。"


def bytes_label(value: float, digits: int = 1) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.{digits}f} {unit}"
        current /= 1024
    return f"{current:.{digits}f} PB"


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


def with_preload_meta(payload: dict[str, Any], cache: dict[str, Any] | None, preloaded: bool) -> dict[str, Any]:
    result = dict(payload)
    result["_preload"] = {
        "preloaded": preloaded,
        "cached_at": cache.get("cached_at") if cache else None,
        "refreshing": preloaded,
    }
    return result


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
    if isinstance(exc, TmdbConfigError):
        return tmdb_test_result(False, trace, "TMDB Worker 网关配置不完整。", "系统需要 Cloudflare Worker 地址和 Gateway Key 才能通过网关访问 TMDB。", "请在设置页填写 Worker 地址，并使用自动生成的 Gateway Key 设置 Cloudflare Worker Secret。", "gateway_config_missing")
    if isinstance(exc, TmdbGatewayError):
        return tmdb_test_result(False, trace, "Cloudflare Worker 健康检查失败。", "NAS 已连接到 Worker 域名，但 Worker 返回的健康检查内容不符合预期。", "请检查 Worker 是否部署了本项目提供的 TMDB Gateway 代码。", exc.error_type, exc.http_status)
    if isinstance(exc, HTTPError):
        status_code = int(exc.code)
        try:
            gateway_error = str(exc.headers.get("X-TMDB-Gateway-Error") or "")
        except Exception:
            gateway_error = ""
        if status_code in (401, 403) and gateway_error in {"invalid_gateway_key", "missing_gateway_key"}:
            return tmdb_test_result(False, trace, "Gateway Key 校验失败。", "NAS 后端已经访问到 Cloudflare Worker，但 X-Gateway-Key 与 Worker Secret 不一致，或 Worker 还没有设置 GATEWAY_KEY。", "请把设置页里的 Gateway Key 复制到 Cloudflare Worker Secret：GATEWAY_KEY，然后重新部署/测试。", "gateway_key_invalid", status_code)
        if gateway_error == "missing_tmdb_token":
            return tmdb_test_result(False, trace, "Worker 缺少 TMDB Bearer Token。", "NAS 已经访问到 Cloudflare Worker，但 Worker 还没有设置 TMDB_BEARER_TOKEN Secret。", "请把 TMDB Bearer Token 设置到 Cloudflare Worker Secret：TMDB_BEARER_TOKEN，然后重新部署/测试。", "gateway_tmdb_token_missing", status_code)
        if gateway_error == "tmdb_auth_error":
            return tmdb_test_result(False, trace, "Worker 中的 TMDB Token 无效。", "Cloudflare Worker 已收到请求，但转发到 TMDB 时被 TMDB 拒绝。", "请在 Cloudflare Worker Secret 中重新设置 TMDB_BEARER_TOKEN，不要把它填成 API Key。", "gateway_tmdb_token_invalid", status_code)
        if gateway_error == "tmdb_network_error":
            return tmdb_test_result(False, trace, "Worker 到 TMDB 的链路异常。", "NAS 可以访问 Cloudflare Worker，但 Worker 连接 TMDB 失败。", "请查看 Cloudflare Worker 日志，确认 Worker 出网和 TMDB 服务状态。", "gateway_upstream_network_error", status_code)
        if status_code >= 500:
            return tmdb_test_result(False, trace, "Worker 到 TMDB 的链路异常。", "NAS 可以访问 Cloudflare Worker，但 Worker 转发到 TMDB 时失败。", "请查看 Cloudflare Worker 日志，确认 TMDB_BEARER_TOKEN 和 Worker 出网状态。", "gateway_upstream_error", status_code)
        return tmdb_test_result(False, trace, "TMDB Worker 网关返回异常。", f"Worker 返回 HTTP {status_code}，应用无法确认具体原因。", "请检查 Worker Secret、路由和 Cloudflare Worker 日志。", "gateway_http_error", status_code)
    if isinstance(exc, (TimeoutError, SocketTimeout, URLError)):
        reason = str(getattr(exc, "reason", exc))
        if phase == "health":
            return tmdb_test_result(False, trace, "NAS 无法访问 Cloudflare Worker。", "后端容器连不上 Worker 的 /health 地址，因此还没有进入 TMDB Token 校验阶段。", f"请先确认 NAS 容器能访问你的 Worker 域名。底层提示：{reason}", "gateway_network_error")
        return tmdb_test_result(False, trace, "Worker 到 TMDB 的链路异常。", "NAS 已能访问 Worker，但 Worker API 请求没有成功完成。", f"请检查 Worker 日志和 TMDB Secret。底层提示：{reason}", "gateway_upstream_network_error")
    return tmdb_test_result(False, trace, "TMDB Worker 网关测试失败。", "应用遇到了未能自动识别的网关问题。", "请检查 Worker URL、Gateway Key 和 Cloudflare Worker 日志。", "gateway_unknown_error")


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


def _unique_storage_paths_from_configs(db: Session) -> list[str]:
    paths: list[str] = []
    for provider in ("qb1", "qb2", "qb3"):
        config = get_decrypted_config(db, provider) or {}
        for value in config.get("nas_mount_paths") or []:
            if str(value).strip():
                paths.append(str(value).strip())
        for mapping in config.get("path_mappings") or []:
            if isinstance(mapping, dict) and str(mapping.get("to") or "").strip():
                paths.append(str(mapping.get("to")).strip())
    unique: list[str] = []
    seen = set()
    for path in paths:
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def nas_storage_from_configs(db: Session) -> dict[str, Any]:
    configured_paths = _unique_storage_paths_from_configs(db)
    if not configured_paths:
        return {
            "nas_free_space": 0,
            "nas_total_space": 0,
            "nas_used_space": 0,
            "nas_usage_percent": None,
            "nas_free_space_label": "-",
            "nas_free_space_source": "",
            "nas_space_label": "-",
            "nas_space_helper": "请在设置的下载器配置中填写 NAS/本机挂载路径",
            "nas_storage_configured": False,
            "nas_storage_readable": False,
            "nas_storage_paths": [],
            "nas_storage_errors": [],
        }

    disks_seen: set[Any] = set()
    total_space = 0.0
    free_space = 0.0
    readable_paths: list[str] = []
    errors: list[dict[str, str]] = []
    for raw_path in configured_paths:
        path = Path(raw_path).expanduser()
        try:
            if not path.exists():
                errors.append({"path": raw_path, "message": "路径不存在或后端容器无法访问"})
                continue
            disk_key: Any
            try:
                disk_key = path.stat().st_dev
            except OSError:
                disk_key = os.path.normcase(path.anchor or str(path))
            if disk_key in disks_seen:
                readable_paths.append(raw_path)
                continue
            usage = shutil.disk_usage(path)
            disks_seen.add(disk_key)
            readable_paths.append(raw_path)
            total_space += float(usage.total)
            free_space += float(usage.free)
        except Exception as exc:
            errors.append({"path": raw_path, "message": str(exc)})

    if total_space <= 0:
        return {
            "nas_free_space": 0,
            "nas_total_space": 0,
            "nas_used_space": 0,
            "nas_usage_percent": None,
            "nas_free_space_label": "-",
            "nas_free_space_source": "",
            "nas_space_label": "-",
            "nas_space_helper": "已配置挂载路径，但后端无法访问，请检查路径或容器挂载",
            "nas_storage_configured": True,
            "nas_storage_readable": False,
            "nas_storage_paths": readable_paths,
            "nas_storage_errors": errors,
        }

    used_space = max(0.0, total_space - free_space)
    usage_percent = round((used_space / total_space) * 100, 1)
    return {
        "nas_free_space": free_space,
        "nas_total_space": total_space,
        "nas_used_space": used_space,
        "nas_usage_percent": usage_percent,
        "nas_free_space_label": bytes_label(free_space, 2),
        "nas_free_space_source": "NAS/本机挂载路径",
        "nas_space_label": f"{bytes_label(used_space, 2)}/{bytes_label(total_space, 2)}",
        "nas_space_helper": f"已使用 {usage_percent}% · {len(disks_seen)} 个磁盘",
        "nas_storage_configured": True,
        "nas_storage_readable": True,
        "nas_storage_paths": readable_paths,
        "nas_storage_errors": errors,
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
        gateway_phase = "api"
        try:
            config = get_decrypted_config(db, provider) or {}
            adapter = TmdbAdapter(config)
            if adapter.mode == "gateway":
                gateway_phase = "health"
                adapter.test_gateway_health()
                gateway_phase = "api"
            adapter.test_connection()
            result = tmdb_test_result(True, test_trace_id, "TMDB 连接成功。", "应用已经成功访问 TMDB，并验证了当前 Bearer Token 可以使用。", "你现在可以点击“启用”，然后回到“发现”页查看真实 TMDB 数据。", None, 200)
        except Exception as exc:
            config = get_decrypted_config(db, provider) or {}
            if str(config.get("mode") or settings.tmdb_mode or "direct").strip().lower() == "gateway":
                result = classify_tmdb_gateway_test_error(exc, test_trace_id, gateway_phase)
            else:
                result = classify_tmdb_test_error(exc, test_trace_id)
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
    else:
        result = mock_test_result(provider, test_trace_id, row)
    return serialize_config(record_test_result(db, provider, result, actor_user_id=user.id))


@router.post("/admin/integrations/tmdb/cloudflare-secrets")
def set_tmdb_cloudflare_secrets(request: IntegrationPayload, _: User = Depends(require_admin)) -> dict[str, Any]:
    payload = request.payload or {}
    gateway_key = str(payload.get("gateway_key") or "").strip()
    bearer_token = str(payload.get("bearer_token") or payload.get("token") or "").strip()
    if not gateway_key:
        raise HTTPException(status_code=400, detail="请先填写 Gateway Key。")
    if not bearer_token:
        raise HTTPException(status_code=400, detail="请先填写 TMDB v4 Bearer Token。")
    worker_name = _normalize_worker_name(payload)
    cwd = _wrangler_cwd()

    written: list[str] = []
    for secret_name, secret_value in (("GATEWAY_KEY", gateway_key), ("TMDB_BEARER_TOKEN", bearer_token)):
        ok, detail = _put_worker_secret(cwd, worker_name, secret_name, secret_value)
        if not ok:
            partial = f"已写入：{', '.join(written)}。" if written else ""
            raise HTTPException(status_code=502, detail=f"{partial}写入 {secret_name} 失败：{detail}")
        written.append(secret_name)

    return {
        "success": True,
        "provider": "tmdb",
        "mode": "real",
        "can_enable": False,
        "message": "Cloudflare Worker Secrets 已写入。",
        "explanation": f"GATEWAY_KEY 和 TMDB_BEARER_TOKEN 已通过本机 Wrangler CLI 写入 Worker：{worker_name}。",
        "next_step": "现在点击“保存并测试”，确认 Worker 可以访问 TMDB；测试成功后再启用 TMDB。",
        "written": written,
        "worker_name": worker_name,
    }


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
            background_tasks.add_task(refresh_dashboard_preload_task)
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_dashboard_preload(db)
    return with_preload_meta(payload, get_preload_cache(db, "dashboard"), False)


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
            background_tasks.add_task(refresh_download_preload_task, downloader_id)
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


@router.post("/qb/{downloader_id}/torrents/{torrent_hash}/delete-confirm")
def qb_delete_confirm(downloader_id: str, torrent_hash: str, user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"confirm_token": trace_id("DEL"), "message": "用户确认风险后，请在 DELETE 请求中提交这个确认令牌。", "target": {"downloader_id": downloader_id, "hash": torrent_hash, "actor": user.username}}


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
        "pages": 4,
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
            background_tasks.add_task(refresh_discover_preload_task)
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
    }
    cache_name = discover_filter_cache_name(filters)
    if cached and not refresh:
        cache = get_preload_cache(db, cache_name)
        if cache:
            background_tasks.add_task(refresh_discover_filter_preload_task, filters)
            return with_preload_meta(cache["payload"], cache, True)
    payload = refresh_discover_filter_preload(db, filters)
    return with_preload_meta(payload, get_preload_cache(db, cache_name), False)


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
        modules.append({"module": provider, "status": "success" if (result or provider in ["nas_disk", "stats_engine"]) else "not_tested", "enabled": bool(row.enabled) if row else provider in ["nas_disk", "stats_engine"], "last_success_at": row.last_tested_at.isoformat() if row and row.last_tested_at else None, "duration_ms": result.get("duration_ms") if result else None, "last_error": None})
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
