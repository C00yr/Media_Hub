import base64
import json
import random
import secrets
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


class WechatClawConfigError(ValueError):
    pass


class WechatClawApiError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class WechatClawAdapter:
    channel_version = "1.0.2"

    def __init__(self, config: dict[str, Any]):
        self.mode = str(config.get("mode") or "ilink").strip() or "ilink"
        self.name = str(config.get("name") or "通知1").strip() or "通知1"
        self.base_url = str(config.get("base_url") or "https://ilinkai.weixin.qq.com").strip().rstrip("/")
        self.default_target = str(config.get("default_target") or "").strip()
        self.admin_user_ids = str(config.get("admin_user_ids") or config.get("admins") or "").strip()
        self.poll_timeout = int(config.get("poll_timeout") or config.get("timeout") or 25)
        self.bot_token = str(config.get("bot_token") or "").strip()
        self.account_id = str(config.get("account_id") or "").strip()
        self.sync_buf = str(config.get("sync_buf") or "").strip()
        self.known_targets = config.get("known_targets") if isinstance(config.get("known_targets"), dict) else {}
        self.public_base_url = str(config.get("public_base_url") or "").strip().rstrip("/")
        self.inbound_token = str(config.get("inbound_token") or "").strip()
        self.webhook_url = str(config.get("webhook_url") or "").strip()
        self.webhook_secret = str(config.get("webhook_secret") or "").strip()
        self.timeout = int(config.get("timeout") or 10)
        if self.mode not in {"ilink", "direct"}:
            self.mode = "ilink"
        if self.mode == "ilink" and not self.base_url.startswith(("http://", "https://")):
            raise WechatClawConfigError("base_url must start with http:// or https://")
        if self.mode == "direct":
            if not self.public_base_url.startswith(("http://", "https://")):
                raise WechatClawConfigError("public_base_url must start with http:// or https://")
            if not self.inbound_token:
                raise WechatClawConfigError("inbound_token is required")
        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            raise WechatClawConfigError("webhook_url must start with http:// or https://")
        self.opener = build_opener()

    @staticmethod
    def _ok(payload: dict[str, Any]) -> bool:
        if not payload:
            return False
        for key in ("success", "ok", "is_success"):
            if isinstance(payload.get(key), bool):
                return bool(payload[key])
        code = payload.get("errcode", payload.get("code", payload.get("ret", 0)))
        try:
            return int(str(code)) == 0
        except (TypeError, ValueError):
            return str(code).strip().lower() in {"0", "ok", "success", "succeed"}

    @staticmethod
    def _normalize_qrcode_url(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        if raw.lower().startswith("data:image/"):
            return raw
        if raw.startswith("//"):
            return f"https:{raw}"
        if len(raw) >= 128 and all(char.isalnum() or char in "+/=_-" for char in raw):
            return f"data:image/png;base64,{raw}"
        return raw

    @staticmethod
    def _pick_recursive(value: Any, keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if key in value and value[key] not in (None, ""):
                    return value[key]
            for item in value.values():
                found = WechatClawAdapter._pick_recursive(item, keys)
                if found not in (None, ""):
                    return found
        if isinstance(value, list):
            for item in value:
                found = WechatClawAdapter._pick_recursive(item, keys)
                if found not in (None, ""):
                    return found
        return None

    @staticmethod
    def _wechat_uin() -> str:
        return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")

    def _headers(self, auth_required: bool = True) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "PT-Media-Hub-Wechat-Claw/1.0",
        }
        if auth_required and self.bot_token:
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {self.bot_token}"
            headers["X-WECHAT-UIN"] = self._wechat_uin()
        return headers

    def _with_base_info(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(body or {})
        base_info = payload.get("base_info") if isinstance(payload.get("base_info"), dict) else {}
        base_info.setdefault("channel_version", self.channel_version)
        payload["base_info"] = base_info
        return payload

    @staticmethod
    def _delivery_succeeded(payload: dict[str, Any]) -> bool:
        # The official sendmessage response may be an empty JSON object on HTTP 200.
        if not payload:
            return True
        return WechatClawAdapter._ok(payload)

    def _json_request(self, method: str, url: str, body: dict[str, Any] | None = None, auth_required: bool = True, timeout: int | None = None) -> dict[str, Any]:
        data = None if method == "GET" else json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, headers=self._headers(auth_required=auth_required), method=method)
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
                try:
                    return json.loads(text) if text else {}
                except json.JSONDecodeError:
                    return {"success": False, "message": text[:500], "http_status": response.status}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise WechatClawApiError(detail or exc.reason, exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise WechatClawApiError(f"WeChat claw iLink request failed: {exc}") from exc

    def test_connection(self) -> dict[str, Any]:
        if self.mode == "ilink":
            if not self.bot_token:
                return {
                    "success": True,
                    "mode": "ilink",
                    "connected": False,
                    "base_url": self.base_url,
                    "message": "iLink 配置可用，等待扫码登录。",
                    "checked_at": datetime.utcnow().isoformat(),
                }
            payload = self._json_request("POST", f"{self.base_url}/ilink/bot/getconfig", {}, timeout=max(self.timeout, 20))
            ok = self._ok(payload) or "ilink_user_id required" in str(payload.get("message") or payload.get("errmsg") or "").lower()
            return {
                "success": ok,
                "mode": "ilink",
                "connected": ok,
                "base_url": self.base_url,
                "message": "iLink 登录态可用。" if ok else str(payload.get("message") or payload.get("errmsg") or "iLink 登录态不可用"),
                "checked_at": datetime.utcnow().isoformat(),
            }
        return {
            "success": True,
            "mode": "direct",
            "public_base_url": self.public_base_url,
            "webhook_configured": bool(self.webhook_url),
            "mobile_app_url": self.public_base_url,
            "mobile_chat_url": f"{self.public_base_url}/api/wechat-claw/message",
            "capabilities_url": f"{self.public_base_url}/api/wechat-claw/capabilities",
            "checked_at": datetime.utcnow().isoformat(),
        }

    def get_qrcode(self) -> dict[str, Any]:
        payload = self._json_request("GET", f"{self.base_url}/ilink/bot/get_bot_qrcode?bot_type=3", auth_required=False, timeout=max(self.timeout, 20))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("result") if isinstance(payload.get("result"), dict) else payload
        qrcode = data.get("qrcode") or data.get("qr_code") or data.get("qrcode_id") or data.get("ticket")
        qrcode_url = self._normalize_qrcode_url(
            data.get("qrcode_url") or data.get("url") or data.get("qrcodeUrl") or data.get("qr_url") or data.get("qrcode_img_content") or data.get("qrcode_img_url") or data.get("qr_img")
        )
        if not qrcode_url and qrcode:
            qrcode_url = f"https://liteapp.weixin.qq.com/q/7GiQu1?{urlencode({'qrcode': qrcode, 'bot_type': 3})}"
        return {
            "success": self._ok(payload) and bool(qrcode or qrcode_url),
            "qrcode": qrcode,
            "qrcode_url": qrcode_url,
            "status": "waiting",
            "message": payload.get("errmsg") or payload.get("message"),
            "raw": payload,
        }

    def get_qrcode_status(self, qrcode: str) -> dict[str, Any]:
        url = f"{self.base_url}/ilink/bot/get_qrcode_status?{urlencode({'qrcode': qrcode})}"
        payload = self._json_request("GET", url, auth_required=False, timeout=max(self.timeout, 20))
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload.get("result") if isinstance(payload.get("result"), dict) else payload
        token = data.get("bot_token") or data.get("token") or data.get("access_token") or self._pick_recursive(data, {"bot_token", "access_token", "token", "jwt", "auth_token"})
        account_id = data.get("account_id") or data.get("ilink_bot_id") or data.get("wxid") or data.get("uid") or data.get("user_id") or self._pick_recursive(data, {"account_id", "ilink_bot_id", "wxid", "uid", "user_id", "from_user", "from_uid"})
        status = data.get("status") or data.get("state") or payload.get("status") or payload.get("state") or self._pick_recursive(data, {"status", "state", "scan_status"}) or "waiting"
        return {
            "success": self._ok(payload),
            "status": str(status).lower(),
            "token": token,
            "account_id": str(account_id) if account_id else "",
            "base_url": data.get("baseurl") or data.get("base_url") or payload.get("baseurl") or payload.get("base_url") or "",
            "qrcode_url": self._normalize_qrcode_url(data.get("qrcode_url") or data.get("url") or data.get("qrcodeUrl") or data.get("qr_url") or data.get("qrcode_img_content") or data.get("qrcode_img_url") or data.get("qr_img")),
            "message": payload.get("errmsg") or payload.get("message"),
            "raw": payload,
        }

    def poll_updates(self, timeout_seconds: int | None = None) -> dict[str, Any]:
        if not self.bot_token:
            return {"success": False, "message": "bot_token is missing", "messages": [], "sync_buf": self.sync_buf}
        payload = self._json_request(
            "POST",
            f"{self.base_url}/ilink/bot/getupdates",
            self._with_base_info({"get_updates_buf": self.sync_buf or ""}),
            timeout=(timeout_seconds or self.poll_timeout) + 10,
        )
        messages = payload.get("msgs") if isinstance(payload.get("msgs"), list) else []
        sync_buf = payload.get("get_updates_buf", payload.get("sync_buf", payload.get("syncBuf", self.sync_buf)))
        parsed = [self._parse_update(item) for item in messages]
        return {
            "success": bool(payload) and (self._ok(payload) or isinstance(payload.get("msgs"), list)),
            "messages": [item for item in parsed if item],
            "sync_buf": str(sync_buf or ""),
            "raw_count": len(messages),
            "parsed_count": len([item for item in parsed if item]),
            "message": payload.get("errmsg") or payload.get("message"),
        }

    def _parse_update(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        if int(item.get("message_type") or 0) not in {0, 1}:
            return None
        user_id = str(item.get("from_user_id") or "").strip()
        context_token = str(item.get("context_token") or "").strip()
        message_id = item.get("message_id") or item.get("client_id") or item.get("seq") or ""
        text = ""
        item_list = item.get("item_list") if isinstance(item.get("item_list"), list) else []
        for content_item in item_list:
            if not isinstance(content_item, dict) or int(content_item.get("type") or 0) != 1:
                continue
            text_item = content_item.get("text_item") if isinstance(content_item.get("text_item"), dict) else {}
            text = str(text_item.get("text") or "").strip()
            if text:
                break
        if not user_id or not text or not context_token:
            return None
        return {
            "user_id": user_id,
            "username": user_id,
            "message_id": str(message_id or ""),
            "text": text,
            "context_token": context_token,
            "raw": item,
        }

    def send_text(self, to_user: str, text: str, context_token: str = "") -> dict[str, Any]:
        if not self.bot_token or not to_user or not text:
            return {"sent": False, "reason": "missing_credentials_or_target"}
        if not context_token:
            return {"sent": False, "reason": "context_token_missing"}
        message = {
            "from_user_id": self.account_id,
            "to_user_id": to_user,
            "client_id": f"pt-media-hub:{int(datetime.utcnow().timestamp() * 1000)}:{secrets.token_hex(4)}",
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        try:
            payload = self._json_request(
                "POST",
                f"{self.base_url}/ilink/bot/sendmessage",
                self._with_base_info({"msg": message}),
                timeout=max(self.timeout, 20),
            )
        except WechatClawApiError as exc:
            return {"sent": False, "reason": "send_request_failed", "message": str(exc), "http_status": exc.http_status}
        sent = self._delivery_succeeded(payload)
        return {"sent": sent, "reason": None if sent else "send_rejected", "response": payload}

    def send_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mode == "ilink":
            target = str(payload.get("to_user") or self.default_target or "").strip()
            if not target and self.known_targets:
                latest = sorted(self.known_targets.items(), key=lambda item: (item[1] or {}).get("last_active", 0), reverse=True)
                target = latest[0][0] if latest else ""
            if not target:
                return {"sent": False, "reason": "target_not_configured"}
            text = str(payload.get("message") or payload.get("text") or payload.get("title") or "").strip()
            if payload.get("title") and payload.get("message"):
                text = f"{payload.get('title')}\n{payload.get('message')}"
            target_state = self.known_targets.get(target) if isinstance(self.known_targets.get(target), dict) else {}
            context_token = str(payload.get("context_token") or target_state.get("context_token") or "").strip()
            delivery = self.send_text(target, text, context_token)
            return {"mode": "ilink", "target": target, **delivery}
        if not self.webhook_url:
            return {"sent": False, "reason": "webhook_not_configured"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PT-Media-Hub-Wechat-Claw",
        }
        if self.webhook_secret:
            headers["X-Wechat-Claw-Secret"] = self.webhook_secret
        request = Request(self.webhook_url, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", "replace")
                return {"sent": True, "http_status": response.status, "response": text[:500]}
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise WechatClawApiError(detail or exc.reason, exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise WechatClawApiError(f"WeChat claw webhook request failed: {exc}") from exc
