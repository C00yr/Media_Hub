export type ApiError = Error & { status?: number };

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export function getToken(): string | null {
  return localStorage.getItem("ptmh_token");
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem("ptmh_token", token);
  else localStorage.removeItem("ptmh_token");
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type") && options.body && !(options.body instanceof FormData) && !(options.body instanceof Blob)) {
    headers.set("Content-Type", "application/json");
  }
  const clientTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (clientTimezone) headers.set("X-Client-Timezone", clientTimezone);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const responseText = await response.text();
    if (response.status === 401 && authErrorCode(responseText) === "auth_session_invalid" && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("ptmh:auth-session-invalid"));
    }
    const error = new Error(normalizeApiErrorMessage(responseText, response.status)) as ApiError;
    error.status = response.status;
    throw error;
  }
  return response.json() as Promise<T>;
}

function authErrorCode(value: string): string {
  try {
    const payload = JSON.parse(value);
    return typeof payload?.detail?.code === "string" ? payload.detail.code : "";
  } catch {
    return "";
  }
}

export function normalizeApiErrorMessage(error: unknown, status?: number): string {
  const raw = error instanceof Error ? error.message : String(error ?? "");
  const fallback = httpStatusMessage(status);
  if (!raw.trim()) return fallback;
  try {
    const parsed = JSON.parse(raw);
    return normalizeErrorPayload(parsed, fallback);
  } catch {
    return looksLikeJson(raw) ? fallback : raw;
  }
}

function normalizeErrorPayload(payload: any, fallback: string): string {
  if (typeof payload === "string") return payload || fallback;
  if (!payload || typeof payload !== "object") return fallback;
  if ("detail" in payload) return normalizeDetail(payload.detail, fallback);
  if (typeof payload.message === "string" && payload.message.trim()) return payload.message;
  if (typeof payload.error === "string" && payload.error.trim()) return payload.error;
  return fallback;
}

function normalizeDetail(detail: any, fallback: string): string {
  if (typeof detail === "string") return detail || fallback;
  if (Array.isArray(detail)) return normalizeValidationErrors(detail, fallback);
  if (!detail || typeof detail !== "object") return fallback;

  const message = stringValue(detail.message) || stringValue(detail.error) || stringValue(detail.reason);
  const provider = providerLabel(stringValue(detail.provider));
  const target = stringValue(detail.title) || stringValue(detail.name) || stringValue(detail.torrent_id);

  if (message && (provider || target)) {
    const parts = [`操作没有完成：${message}`];
    if (provider) parts.push(`服务：${provider}`);
    if (target) parts.push(`对象：${target}`);
    return parts.join("\n");
  }
  if (message) return message;

  const readableValues = Object.entries(detail)
    .filter(([key]) => !["trace_id", "duration_ms", "stage", "stages", "stack", "payload"].includes(key))
    .map(([key, value]) => readablePair(key, value))
    .filter(Boolean);
  return readableValues.length ? readableValues.slice(0, 4).join("\n") : fallback;
}

function normalizeValidationErrors(items: any[], fallback: string): string {
  const messages = items
    .map((item) => {
      if (typeof item === "string") return item;
      const field = Array.isArray(item?.loc) ? item.loc.filter((part: unknown) => part !== "body").join(".") : "";
      const message = stringValue(item?.msg) || stringValue(item?.message);
      if (!message) return "";
      return field ? `${field}：${message}` : message;
    })
    .filter(Boolean);
  return messages.length ? `填写内容有误：${messages.slice(0, 3).join("；")}` : fallback;
}

function readablePair(key: string, value: unknown): string {
  if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return "";
  const labelMap: Record<string, string> = {
    path: "路径",
    status: "状态",
    code: "错误码",
    http_status: "HTTP 状态码",
    next_step: "下一步",
    explanation: "说明",
  };
  return `${labelMap[key] || key}：${String(value)}`;
}

function stringValue(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function providerLabel(value: string): string {
  const labels: Record<string, string> = {
    mteam: "M-Team",
    qb1: "qB1",
    qb2: "qB2",
    qb3: "qB3",
    tmdb: "TMDB",
  };
  return labels[value.toLowerCase()] || value;
}


function httpStatusMessage(status?: number): string {
  if (status === 400) return "请求内容有误，请检查填写的信息。";
  if (status === 401) return "登录状态已失效，请重新登录。";
  if (status === 403) return "当前账号没有权限执行这个操作。";
  if (status === 404) return "请求的内容不存在，可能是服务尚未更新或地址不正确。";
  if (status === 409) return "当前功能还没有准备好，请先完成设置并启用。";
  if (status === 422) return "填写内容有误，请检查表单后重试。";
  if (status && status >= 500) return "服务暂时不可用，请稍后重试；如问题持续，可前往诊断页查看详情。";
  return "请求失败，请稍后重试。";
}

function looksLikeJson(value: string): boolean {
  const trimmed = value.trim();
  return trimmed.startsWith("{") || trimmed.startsWith("[");
}

export function formatBytes(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let current = value;
  for (const unit of units) {
    if (current < 1024 || unit === units[units.length - 1]) return `${current.toFixed(1)} ${unit}`;
    current /= 1024;
  }
  return `${current.toFixed(1)} PB`;
}

export function formatSpeed(value: number): string {
  return `${formatBytes(value)}/s`;
}
