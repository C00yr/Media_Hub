import json
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


INTENT_SYSTEM_PROMPT = """
You are the intent parser for PT Media Hub. Output strict JSON only.
Convert the user's Chinese or English message into one executable command.

Allowed JSON shape:
{
  "action": "resource_search" | "download_started" | "download_completed" | "status_query",
  "query": "movie or torrent keywords, empty when not needed",
  "downloader_id": "qb1" | "qb2" | "qb3" | "all",
  "target": "dashboard" | "mteam" | "qb" | "notifications" | "downloads" | "stats" | "diagnostics" | "discover",
  "torrent_id": "optional torrent id",
  "torrent_hash": "optional qB hash",
  "limit": 5,
  "message": "short notification message when action is download_started/download_completed"
}

Rules:
- For resource lookup, use action "resource_search" and put searchable terms in query.
- For "started downloading", "start notification", or similar, use "download_started".
- For "download finished", "completed", or similar, use "download_completed".
- For status questions about qB, M-Team, NAS, dashboard, notifications, or downloads, use "status_query".
- downloader_id defaults to "all" unless the user says qB1/qB2/qB3.
- target defaults to "dashboard" for general status, "qb" for qB status, "mteam" for M-Team status, "stats" for statistics, "diagnostics" for health checks, and "discover" for TMDB/discovery status.
- limit must be between 1 and 10.
""".strip()


SUMMARY_SYSTEM_PROMPT = """
You are PT Media Hub's mobile response composer for WeChat claw and the in-app assistant.
Use the execution result JSON as the only data source. Output strict JSON only.

Allowed JSON shape:
{
  "title": "short Chinese title, max 18 chars",
  "summary": "one concise Chinese sentence",
  "sections": [
    {"heading": "Chinese heading, max 8 chars", "items": ["short item text", "..."]}
  ],
  "actions": ["optional next action text"],
  "footer": "optional short safety or source note"
}

Mobile formatting rules:
- Keep section order stable: summary first, then sections, then actions, then footer.
- Use 1 to 4 sections and at most 5 items per section.
- Do not use markdown tables, HTML, code blocks, raw JSON, or long paragraphs.
- For resource search, list title, size, resolution, seeders, and torrent id when present.
- For status queries, summarize M-Team, qB downloaders, NAS space, and errors if present.
- For download notifications, confirm whether a notification was created or skipped, and include notification id when present.
- Never expose API keys, cookies, tokens, raw headers, passwords, or inbound webhook secrets.
""".strip()


class AIConfigError(ValueError):
    pass


class AIServiceError(RuntimeError):
    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class DeepSeekChatAdapter:
    def __init__(self, config: dict[str, Any]):
        self.api_key = str(config.get("api_key") or config.get("deepseek_api_key") or "").strip()
        self.base_url = str(config.get("base_url") or DEEPSEEK_BASE_URL).strip().rstrip("/")
        self.model = str(config.get("model") or DEFAULT_MODEL).strip()
        self.timeout = int(config.get("timeout") or 30)
        self.max_tokens = int(config.get("max_tokens") or 1200)
        self.temperature = float(config.get("temperature") or 0.1)
        self.thinking = str(config.get("thinking") or "disabled").strip().lower()
        self.reasoning_effort = str(config.get("reasoning_effort") or "high").strip().lower()
        if not self.api_key:
            raise AIConfigError("DeepSeek API Key is missing")
        if not self.base_url.startswith(("http://", "https://")):
            raise AIConfigError("DeepSeek base_url must start with http:// or https://")
        if not self.model:
            raise AIConfigError("DeepSeek model is missing")
        self.opener = build_opener()

    def parse_intent(self, text: str) -> dict[str, Any]:
        user_text = str(text or "").strip()
        if not user_text:
            raise AIServiceError("User message is empty")
        content = self._chat(
            [
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": f"Return JSON for this request:\n{user_text}"},
            ],
            json_mode=True,
            max_tokens=700,
        )
        payload = _json_loads(content)
        if not isinstance(payload, dict):
            raise AIServiceError("AI did not return a JSON object")
        return normalize_assistant_intent(payload)

    def summarize_result(self, user_text: str, intent: dict[str, Any], result: dict[str, Any]) -> str:
        content = self._chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": user_text,
                            "structured_intent": intent,
                            "execution_result": result,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            json_mode=True,
            max_tokens=self.max_tokens,
        )
        payload = _json_loads(content)
        if not isinstance(payload, dict):
            raise AIServiceError("AI did not return a mobile reply JSON object")
        return render_mobile_reply(payload)

    def test_connection(self) -> dict[str, Any]:
        content = self._chat(
            [
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": 'Return json exactly like {"ok":true,"name":"pt-media-hub"}'},
            ],
            json_mode=True,
            max_tokens=120,
        )
        payload = _json_loads(content)
        return {
            "success": bool(isinstance(payload, dict) and payload.get("ok") is True),
            "model": self.model,
            "base_url": self.base_url,
            "checked_at": datetime.utcnow().isoformat(),
        }

    def _chat(self, messages: list[dict[str, str]], json_mode: bool, max_tokens: int) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.thinking in {"enabled", "disabled"}:
            body["thinking"] = {"type": self.thinking}
        if self.reasoning_effort in {"high", "max"}:
            body["reasoning_effort"] = self.reasoning_effort
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "PT-Media-Hub",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise AIServiceError(detail or exc.reason, exc.code) from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AIServiceError(f"DeepSeek request failed: {exc}") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("DeepSeek response did not contain message content") from exc
        if not str(content or "").strip():
            raise AIServiceError("DeepSeek returned empty content")
        return str(content)


def normalize_assistant_intent(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "status_query").strip()
    if action not in {"resource_search", "download_started", "download_completed", "status_query"}:
        action = "status_query"
    downloader_id = str(payload.get("downloader_id") or "all").strip().lower()
    if downloader_id not in {"qb1", "qb2", "qb3", "all"}:
        downloader_id = "all"
    target = str(payload.get("target") or "").strip().lower()
    if target not in {"dashboard", "mteam", "qb", "notifications", "downloads", "stats", "diagnostics", "discover"}:
        target = "qb" if downloader_id != "all" else "dashboard"
    try:
        limit = int(payload.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    return {
        "action": action,
        "query": str(payload.get("query") or "").strip(),
        "downloader_id": downloader_id,
        "target": target,
        "torrent_id": str(payload.get("torrent_id") or "").strip(),
        "torrent_hash": str(payload.get("torrent_hash") or "").strip(),
        "limit": max(1, min(10, limit)),
        "message": str(payload.get("message") or "").strip(),
    }


def _json_loads(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def render_mobile_reply(payload: dict[str, Any]) -> str:
    title = str(payload.get("title") or "处理结果").strip()[:18]
    summary = str(payload.get("summary") or "").strip()
    lines = [f"【{title}】"]
    if summary:
        lines.append(summary)
    sections = payload.get("sections") or []
    if isinstance(sections, list):
        for section in sections[:4]:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "详情").strip()[:8]
            items = section.get("items") or []
            if not isinstance(items, list) or not items:
                continue
            lines.append("")
            lines.append(f"{heading}")
            for item in items[:5]:
                text = str(item or "").strip()
                if text:
                    lines.append(f"- {text[:120]}")
    actions = payload.get("actions") or []
    if isinstance(actions, list) and actions:
        lines.append("")
        lines.append("下一步")
        for action in actions[:3]:
            text = str(action or "").strip()
            if text:
                lines.append(f"- {text[:100]}")
    footer = str(payload.get("footer") or "").strip()
    if footer:
        lines.append("")
        lines.append(footer[:120])
    return "\n".join(lines).strip()
