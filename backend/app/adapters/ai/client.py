import json
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from app.utils.time import utc_iso


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


INTENT_SYSTEM_PROMPT = """
You are the intent parser for PT Media Hub. Output strict JSON only.
Convert the user's Chinese or English message into exactly one normalized intent.

Allowed JSON shape:
{
  "intent_type": "tmdb_lookup" | "mteam_search" | "dashboard_query" | "download_selected" | "general_chat",
  "query": "search keywords, empty when not needed",
  "tmdb_filters": {"media_type":"movie"|"tv"|"all", "region":"ISO country code or empty", "language":"ISO language code or empty", "genre":"Chinese genre name or empty", "year":"year/decade or empty", "min_rating":0, "sort_by":"vote_average.desc"|"popularity.desc"|"release_date.desc"|""},
  "mteam_filters": {"resolution":"2160p"|"1080p"|"" , "min_size_gb":0, "max_size_gb":0, "promotion":"free"|"discount"|"any", "recommend":true|false},
  "dashboard_sections":["overview"|"mteam"|"nas"|"qb1"|"qb2"|"qb3"|"downloads"|"stats"|"diagnostics"],
  "selection_index": 0,
  "selection_reference": "first|recommended|empty",
  "download_confirmation": true|false,
  "limit": 5,
  "message": "optional short clarification"
}

Rules:
- Use tmdb_lookup for movie/TV information, recommendations, title lookup, or condition discovery. For vague "high score", set min_rating=8.0. Convert Korea to KR and Korean to ko when relevant. Treat 动画、动漫、番剧、动画剧集、季、集 as media_type="tv"; treat 电影、影片 as media_type="movie". For a title lookup with no format cue, use media_type="all" rather than guessing movie.
- Use mteam_search only for private-tracker resource lookup. Set recommend=true when the user asks for a recommendation or gives no precise release constraints.
- Use dashboard_query for dashboard, M-Team, NAS, qB, download task, stats, or diagnostic requests. A generic dashboard request uses ["overview"]. A request for M-Team or 馒头站 account/site data must use ["mteam"].
- Use download_selected only when the user refers to a recent M-Team candidate and expresses an affirmative intent. Resolve "1", "the first", and "the recommended one" into selection_index/selection_reference when possible. Set download_confirmation=true only for clear confirmation.
- Use general_chat for every other request. Never turn a general movie discussion into a tool call.
- limit must be between 1 and 10.
""".strip()


GENERAL_AGENT_SYSTEM_PROMPT = """
你是 PT Media Hub 的影视中枢 Agent：专业、友好、克制，擅长影视发现、观影建议、片单与家庭媒体管理。
请自然地延续用户的对话语境。不要声称查询过 M-Team、TMDB、qB 或 NAS，除非当前消息已经提供了对应工具结果。
不要泄露或猜测 API Key、Cookie、Token、密码、内部路径、下载器私密任务信息。回答使用简洁中文，不使用 Markdown 表格。
""".strip()


MTEAM_RECOMMENDATION_SYSTEM_PROMPT = """
You are the final presentation editor for ranked M-Team search results.
The backend has already ranked candidates and selected the recommended index; never change ranking, indexes, sizes, seeders, or promotions.
Return strict JSON only:
{"recommendation":"...","rows":[{"index":1,"title":"","chinese_info":"","quality":"","size":"","seeders":"","promotion":""}]}
Every row must contain exactly these six display fields. Keep every field compact, clean, and complete:
- title must contain the work title and a four-digit year.
- chinese_info must contain useful Chinese/alias information and explicitly say either "含中字" or "未标注中字".
- quality must combine resolution, video codec, HDR/DV when present, and release group when present, using " · ".
- size, seeders, and promotion must copy the supplied values exactly.
Use only supplied candidate data. Do not include cast, director, raw long release names, IDs, or extra fields. Never invent availability, quality, episode count, or technical claims.
The recommendation must mention the selected number and 2-4 supplied facts.
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
        # Mobile conversations send an immediate acknowledgement, so preserve the
        # complete answer instead of cutting a slow model off at the old 30s limit.
        self.timeout = max(90, int(config.get("timeout") or 90))
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
        self.opener = build_opener(ProxyHandler({}))

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

    def answer_general(self, user_text: str, history: list[dict[str, str]] | None = None) -> str:
        messages = [{"role": "system", "content": GENERAL_AGENT_SYSTEM_PROMPT}]
        for item in (history or [])[-10:]:
            role = "assistant" if item.get("role") == "assistant" else "user"
            content = str(item.get("content") or "").strip()
            if content:
                messages.append({"role": role, "content": content[:800]})
        messages.append({"role": "user", "content": user_text})
        return self._chat(messages, json_mode=False, max_tokens=min(self.max_tokens, 900))

    def describe_mteam_presentation(self, query: str, items: list[dict[str, Any]], recommended_index: int | None) -> dict[str, Any]:
        if not recommended_index or recommended_index < 1 or recommended_index > len(items):
            return {"recommendation": "", "metadata": []}
        candidates = [
            {
                "index": index,
                "raw_title": str(item.get("title") or ""),
                "raw_subtitle": str(item.get("subtitle") or ""),
                "resolution": item.get("resolution"),
                "size": item.get("size"),
                "seeders": item.get("seeders"),
                "promotion": item.get("promotion_label") or "普通",
                "codec": item.get("codec") or "-",
                "hdr": item.get("hdr") or "",
                "group": item.get("group") or "",
                "labels": item.get("labels") or [],
                "has_chinese_subtitles": bool(item.get("has_chinese_subtitles")),
            }
            for index, item in enumerate(items[:10], 1)
        ]
        content = self._chat(
            [
                {"role": "system", "content": MTEAM_RECOMMENDATION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"query": query, "recommended_index": recommended_index, "candidates": candidates}, ensure_ascii=False)},
            ],
            json_mode=True,
            max_tokens=min(self.max_tokens, 1400),
        )
        payload = _json_loads(content)
        recommendation = str(payload.get("recommendation") or "").strip() if isinstance(payload, dict) else ""
        if not recommendation or len(recommendation) > 220:
            raise AIServiceError("AI recommendation output was invalid")
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        return {"recommendation": recommendation, "rows": [item for item in rows if isinstance(item, dict)][:len(candidates)]}

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
            "checked_at": utc_iso(),
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
    legacy_action = str(payload.get("action") or "").strip()
    intent_type = str(payload.get("intent_type") or "").strip().lower()
    legacy_map = {"resource_search": "mteam_search", "status_query": "dashboard_query", "mobile_download": "download_selected"}
    intent_type = legacy_map.get(intent_type or legacy_action, intent_type or legacy_action)
    if intent_type not in {"tmdb_lookup", "mteam_search", "dashboard_query", "download_selected", "general_chat"}:
        intent_type = "general_chat"
    try:
        limit = int(payload.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    tmdb_source = payload.get("tmdb_filters") if isinstance(payload.get("tmdb_filters"), dict) else {}
    mteam_source = payload.get("mteam_filters") if isinstance(payload.get("mteam_filters"), dict) else {}
    sections = payload.get("dashboard_sections") if isinstance(payload.get("dashboard_sections"), list) else []
    valid_sections = {"overview", "mteam", "nas", "qb1", "qb2", "qb3", "downloads", "stats", "diagnostics"}
    normalized_sections = [str(item).lower() for item in sections if str(item).lower() in valid_sections]
    if intent_type == "dashboard_query" and not normalized_sections:
        normalized_sections = ["overview"]
    try:
        min_rating = float(tmdb_source.get("min_rating") or 0)
    except (TypeError, ValueError):
        min_rating = 0
    try:
        selection_index = int(payload.get("selection_index") or 0)
    except (TypeError, ValueError):
        selection_index = 0
    try:
        min_size_gb = float(mteam_source.get("min_size_gb") or 0)
    except (TypeError, ValueError):
        min_size_gb = 0
    try:
        max_size_gb = float(mteam_source.get("max_size_gb") or 0)
    except (TypeError, ValueError):
        max_size_gb = 0
    return {
        "intent_type": intent_type,
        "action": intent_type,
        "query": str(payload.get("query") or "").strip(),
        "tmdb_filters": {
            "media_type": str(tmdb_source.get("media_type") or "all").lower() if str(tmdb_source.get("media_type") or "all").lower() in {"movie", "tv", "all"} else "all",
            "region": str(tmdb_source.get("region") or "").upper()[:2],
            "language": str(tmdb_source.get("language") or "").lower()[:8],
            "genre": str(tmdb_source.get("genre") or "").strip()[:40],
            "year": str(tmdb_source.get("year") or "").strip()[:12],
            "min_rating": max(0, min(10, min_rating)),
            "sort_by": str(tmdb_source.get("sort_by") or "").strip(),
        },
        "mteam_filters": {
            "resolution": str(mteam_source.get("resolution") or "").lower(),
            "min_size_gb": max(0, min_size_gb),
            "max_size_gb": max(0, max_size_gb),
            "promotion": str(mteam_source.get("promotion") or "any").lower(),
            "recommend": bool(mteam_source.get("recommend")),
        },
        "dashboard_sections": normalized_sections,
        "selection_index": max(0, min(10, selection_index)),
        "selection_reference": str(payload.get("selection_reference") or "").lower(),
        "download_confirmation": bool(payload.get("download_confirmation")),
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
