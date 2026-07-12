import {
  Activity,
  ArrowUp,
  Bell,
  CalendarDays,
  Check,
  Clock3,
  Coins,
  Copy,
  Database,
  Download,
  Eye,
  EyeOff,
  Film,
  Gauge,
  HardDrive,
  Lock,
  LogOut,
  MessageSquare,
  Plus,
  Percent,
  Radar,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Star,
  Trash2,
  Upload,
  UserRound,
  Users,
  Wrench
} from "lucide-react";
import QRCode from "qrcode";
import { FormEvent, KeyboardEvent, MouseEvent, PointerEvent, ReactNode, RefObject, SyntheticEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, formatBytes, formatSpeed, getToken, normalizeApiErrorMessage, setToken } from "../api/client";

type User = { username: string; role: string };
type NavKey = "discover" | "dashboard" | "downloads" | "notifications" | "settings" | "diagnostics";
type TrafficDimension = "year" | "month" | "week" | "day" | "hour";
type DiscoverMode = "home" | "dual" | "mteam";
type DiscoverBrowseMode = "casual" | "filter";
type DiscoverFilters = {
  media_type: "movie" | "tv";
  sort_by: string;
  genre: string;
  region: string;
  language: string;
  year: string;
  min_rating: string;
};

const DEFAULT_DISCOVER_FILTERS: DiscoverFilters = {
  media_type: "movie",
  sort_by: "popularity.desc",
  genre: "",
  region: "",
  language: "",
  year: "",
  min_rating: "0",
};

type IntegrationTestResult = {
  success?: boolean;
  provider?: string;
  mode?: string;
  message?: string;
  explanation?: string;
  next_step?: string;
  error_type?: string | null;
  http_status?: number | null;
  can_enable?: boolean;
  trace_id?: string;
  detail?: {
    network_mode?: string;
    route_label?: string;
    proxy_enabled?: boolean;
    proxy_url?: string;
    non_tmdb_policy?: string;
    image_host?: string;
    image_probe?: {
      checked?: boolean;
      ok?: boolean;
      error?: string;
      reason?: string;
    };
  };
};

type TmdbForm = {
  mode: string;
  api_key: string;
  bearer_token: string;
  proxy_url: string;
  language: string;
  region: string;
  timeout: string;
  endpoint: string;
};

type PushNoticeState = {
  status: "running" | "success" | "error";
  title: string;
  step: string;
  detail?: string;
};

type MTeamForm = {
  base_url: string;
  api_key: string;
  cookie: string;
  user_agent: string;
  authorization: string;
  passkey: string;
  timeout: string;
};

type QbForm = {
  base_url: string;
  username: string;
  password: string;
  timeout: string;
};

type AiForm = {
  base_url: string;
  api_key: string;
  model: string;
  timeout: string;
  max_tokens: string;
  temperature: string;
  thinking: "enabled" | "disabled";
  reasoning_effort: "high" | "max";
};

type WechatClawForm = {
  mode: "ilink" | "direct";
  name: string;
  base_url: string;
  default_target: string;
  admin_user_ids: string;
  poll_timeout: string;
  public_base_url: string;
  inbound_token: string;
  webhook_url: string;
  webhook_secret: string;
  default_downloader_id: "all" | "qb1" | "qb2" | "qb3";
  timeout: string;
};

type WechatClawInteraction = {
  user_id?: string;
  conversation_id?: string;
  message?: string;
  reply?: string;
  action?: string;
  target?: string;
  status?: string;
  trace_id?: string;
  duration_ms?: number;
  error?: string;
  stages?: Array<{ stage?: string; status?: string; duration_ms?: number; error?: string }>;
  created_at?: string;
};

function wechatClawStageLabel(stage?: string): string {
  const labels: Record<string, string> = {
    progress_ack: "即时回应",
    ai_adapter: "AI 配置检查",
    ai_recommendation: "AI 推荐说明",
    ai_intent: "AI 意图识别",
    ai_general_answer: "AI 生成回复",
    tmdb_lookup: "TMDB 查询",
    mteam_search: "M-Team 搜索",
    dashboard_query: "仪表盘查询",
    download_selection: "资源选择",
    download_selected: "下载提交",
    reply_render: "回复整理",
    final_reply_send: "微信最终发送",
  };
  return labels[String(stage || "")] || String(stage || "未知阶段");
}

function renderReplyInline(text: string): ReactNode {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    return part;
  });
}

function parseMarkdownTableRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function AssistantReply({ reply }: { reply: string }) {
  const lines = String(reply || "").split("\n");
  const headerIndex = lines.findIndex((line, index) => line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1] || ""));
  if (headerIndex < 0) return <span className="assistant-reply-text">{lines.map((line, index) => <span key={index}>{renderReplyInline(line)}{index < lines.length - 1 && <br />}</span>)}</span>;
  const rows: string[][] = [];
  let afterIndex = headerIndex + 2;
  while (afterIndex < lines.length && lines[afterIndex].trim().startsWith("|")) {
    rows.push(parseMarkdownTableRow(lines[afterIndex]));
    afterIndex += 1;
  }
  return (
    <div className="assistant-markdown-reply">
      {lines.slice(0, headerIndex).map((line, index) => <div className="assistant-reply-line" key={`before-${index}`}>{renderReplyInline(line)}</div>)}
      <div className="assistant-markdown-table-wrap">
        <table className="assistant-markdown-table">
          <thead><tr>{parseMarkdownTableRow(lines[headerIndex]).map((cell, index) => <th key={index}>{renderReplyInline(cell)}</th>)}</tr></thead>
          <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderReplyInline(cell)}</td>)}</tr>)}</tbody>
        </table>
      </div>
      {lines.slice(afterIndex).map((line, index) => <div className="assistant-reply-line" key={`after-${index}`}>{renderReplyInline(line)}</div>)}
    </div>
  );
}

type NotificationPreferenceKey = "download_started" | "download_completed" | "resource_search" | "status_query" | "wechat_claw_push";

const navItems: { key: NavKey; label: string; icon: typeof Film; admin?: boolean }[] = [
  { key: "discover", label: "发现", icon: Film },
  { key: "dashboard", label: "仪表盘", icon: Gauge },
  { key: "downloads", label: "下载", icon: Download },
  { key: "notifications", label: "通知", icon: Bell },
  { key: "settings", label: "设置", icon: Settings },
  { key: "diagnostics", label: "诊断", icon: Wrench, admin: true }
];

const pageDescriptions: Record<NavKey, string> = {
  discover: "从 TMDB 获取流行趋势、热门内容和高分片单。",
  dashboard: "查看站点、下载器和 NAS 的核心运行指标。",
  downloads: "查看和管理多个 qB 下载器中的任务。",
  notifications: "集中查看系统提醒和任务通知。",
  settings: "管理运行时凭据，敏感信息只在后端加密保存。",
  diagnostics: "查看核心模块是否正常运行，并导出脱敏诊断信息。"
};

function useLoad<T>(loader: () => Promise<T>, deps: unknown[], initialData: T | null = null, clearOnLoad = true) {
  const [data, setData] = useState<T | null>(initialData);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    if (clearOnLoad) setData(null);
    loader()
      .then((value) => alive && setData(value))
      .catch((err) => alive && setError((err as Error).message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, deps);

  return {
    data,
    error,
    loading,
    reload: () => {
      return loader()
        .then((value) => {
          setData(value);
          setError("");
          return value;
        })
        .catch((err) => {
          setError((err as Error).message);
          setData(null);
          throw err;
        });
    },
    setData
  };
}

const SEARCH_HISTORY_STORAGE_KEY = "ptmh_search_history";
const LOCAL_SNAPSHOT_PREFIX = "ptmh_snapshot:v2:";
const LOCAL_SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024;
const REVALIDATE_DELAY_MS = 3000;
const TMDB_DIRECT_IMAGE_RE = /https:\/\/image\.tmdb\.org\/t\/p\/(w\d+|original)\/([A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp))/gi;
const IMAGE_FALLBACK_SRC = `data:image/svg+xml;utf8,${encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="342" height="513" viewBox="0 0 342 513"><rect width="342" height="513" fill="#e5e9f0"/><path d="M96 208h150v97H96z" fill="#cbd5e1"/><circle cx="130" cy="238" r="17" fill="#94a3b8"/><path d="M107 288l44-45 31 31 21-22 34 36z" fill="#94a3b8"/><text x="171" y="350" text-anchor="middle" font-family="Arial,sans-serif" font-size="24" font-weight="700" fill="#64748b">No Image</text></svg>'
)}`;

function rewriteTmdbImageUrl(value: string) {
  return value.replace(TMDB_DIRECT_IMAGE_RE, (_match, size: string, imagePath: string) => `/api/tmdb/image/${size}/${imagePath.replace(/^\/+/, "")}`);
}

function rewriteTmdbImageUrls<T>(payload: T): T {
  if (typeof payload === "string") return rewriteTmdbImageUrl(payload) as T;
  if (Array.isArray(payload)) return payload.map((item) => rewriteTmdbImageUrls(item)) as T;
  if (payload && typeof payload === "object") {
    return Object.fromEntries(Object.entries(payload).map(([key, value]) => [key, rewriteTmdbImageUrls(value)])) as T;
  }
  return payload;
}

function handleImageError(event: SyntheticEvent<HTMLImageElement>) {
  const image = event.currentTarget;
  if (image.dataset.fallbackApplied === "1") return;
  image.dataset.fallbackApplied = "1";
  image.src = IMAGE_FALLBACK_SRC;
}

function readLocalSnapshot<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(`${LOCAL_SNAPSHOT_PREFIX}${key}`);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return rewriteTmdbImageUrls(parsed?.payload ?? null);
  } catch {
    return null;
  }
}

function writeLocalSnapshot(key: string, payload: unknown) {
  try {
    const value = JSON.stringify({ saved_at: new Date().toISOString(), payload: rewriteTmdbImageUrls(payload) });
    if (new Blob([value]).size > LOCAL_SNAPSHOT_MAX_BYTES) return;
    localStorage.setItem(`${LOCAL_SNAPSHOT_PREFIX}${key}`, value);
  } catch {
    // Local cache is opportunistic; quota or privacy errors should not affect the app.
  }
}

function eagerPosterLimit() {
  return window.matchMedia("(max-width: 720px)").matches ? 4 : 8;
}

function isMobilePosterInteraction() {
  return window.matchMedia("(hover: none), (pointer: coarse), (max-width: 720px)").matches;
}

function shouldRevalidateFromCache(payload: any) {
  return Boolean(payload?._preload?.preloaded);
}

function readSearchHistory(): string[] {
  try {
    const value = localStorage.getItem(SEARCH_HISTORY_STORAGE_KEY);
    const parsed = value ? JSON.parse(value) : [];
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string").slice(0, 12) : [];
  } catch {
    return [];
  }
}

function writeSearchHistory(keyword: string): string[] {
  const normalized = keyword.trim();
  if (!normalized) return readSearchHistory();
  const next = [normalized, ...readSearchHistory().filter((item) => item.toLowerCase() !== normalized.toLowerCase())].slice(0, 12);
  localStorage.setItem(SEARCH_HISTORY_STORAGE_KEY, JSON.stringify(next));
  return next;
}

function removeSearchHistory(keyword: string): string[] {
  const next = readSearchHistory().filter((item) => item !== keyword);
  localStorage.setItem(SEARCH_HISTORY_STORAGE_KEY, JSON.stringify(next));
  return next;
}

function clearSearchHistory(): string[] {
  localStorage.removeItem(SEARCH_HISTORY_STORAGE_KEY);
  return [];
}

export function App() {
  const [initialized, setInitialized] = useState<boolean | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [active, setActive] = useState<NavKey>("dashboard");
  const [discoverResetToken, setDiscoverResetToken] = useState(0);
  const [selectedDownloader, setSelectedDownloader] = useState("qb1");

  useEffect(() => {
    api<{ initialized: boolean }>("/api/setup/status").then((data) => setInitialized(data.initialized));
    if (getToken()) {
      api<User>("/api/auth/me").then(setUser).catch(() => setToken(null));
    }
  }, []);

  if (initialized === null) return <Splash />;
  if (!initialized) return <SetupPage onDone={(nextUser) => { setInitialized(true); setUser(nextUser); }} />;
  if (!user) return <LoginPage onLogin={setUser} />;

  const visibleNav = navItems.filter((item) => !item.admin || user.role === "admin");
  const ActivePage = pages[active];

  function openNav(key: NavKey) {
    if (key === "discover" && active === "discover") {
      setDiscoverResetToken((value) => value + 1);
    }
    setActive(key);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <BrandLogo subtitle="媒体中枢" />
        <nav>
          {visibleNav.map((item) => {
            const Icon = item.icon;
            return (
              <button className={active === item.key ? "nav-item active" : "nav-item"} onClick={() => openNav(item.key)} key={item.key}>
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <button className="nav-item logout" onClick={() => { setToken(null); setUser(null); }}>
          <LogOut size={18} />
          <span>退出登录</span>
        </button>
      </aside>
      <main>
        <header className={active === "downloads" ? "topbar topbar-compact" : "topbar"}>
          {active !== "downloads" && (
            <div>
              <h1>{navItems.find((item) => item.key === active)?.label}</h1>
              <p>{pageDescriptions[active]}</p>
            </div>
          )}
          <div className="user-pill">
            <ShieldCheck size={16} />
            {user.username} / {user.role === "admin" ? "管理员" : "用户"}
          </div>
        </header>
        <ActivePage
          user={user}
          resetToken={active === "discover" ? discoverResetToken : 0}
          selectedDownloader={selectedDownloader}
          onNavigate={openNav}
          onOpenDownloader={(downloaderId) => {
            setSelectedDownloader(downloaderId);
            setActive("downloads");
          }}
        />
      </main>
      <nav className="bottom-nav">
        {visibleNav.filter((item) => ["discover", "dashboard", "downloads", "notifications", "settings", "diagnostics"].includes(item.key)).map((item) => {
          const Icon = item.icon;
          return (
            <button className={active === item.key ? "active" : ""} onClick={() => openNav(item.key)} key={item.key} aria-label={item.label}>
              <Icon size={20} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}

function Splash() {
  return <div className="auth-shell"><div className="auth-card"><h1>PT Media Hub</h1><p>正在启动...</p></div></div>;
}

function BrandLogo({ subtitle, large = false }: { subtitle: string; large?: boolean }) {
  return (
    <div className={large ? "brand large" : "brand"}>
      <span className="brand-mark" aria-hidden="true">
        <svg className="brand-popcorn" viewBox="0 0 48 48" role="img">
          <path className="brand-popcorn-kernel" d="M14.9 14.7c-2.7-1.1-4.1-4.3-2.8-6.9 1.4-2.8 5-3.5 7.2-1.5 1.2-3 5.1-4.1 7.7-2.2 1.8 1.3 2.5 3.5 1.8 5.5 2.8-.5 5.5 1.7 5.6 4.6.1 2.9-2.2 5.3-5.1 5.3H17.4c-2.7 0-4.2-2.4-2.5-4.8Z" />
          <path className="brand-popcorn-cup" d="M12.5 18.5h23l-3.4 22H16l-3.5-22Z" />
          <path className="brand-popcorn-fold" d="M20.4 19l1.1 21M27.7 19.1l-1.2 20.8" />
          <path className="brand-popcorn-smile" d="M20.3 28.4c2.3 1.8 5.2 1.8 7.4 0" />
        </svg>
      </span>
      <div className="brand-copy">
        <strong>Media Hub</strong>
        <small>{subtitle}</small>
      </div>
    </div>
  );
}

function storageDisplay(source: any) {
  const total = Number(source?.total_space ?? source?.nas_total_space ?? 0);
  const free = Number(source?.free_space ?? source?.nas_free_space ?? 0);
  const used = Number(source?.used_space ?? source?.nas_used_space ?? (total > 0 ? Math.max(0, total - free) : 0));
  const rawPercent = typeof source?.nas_usage_percent === "number" ? Number(source.nas_usage_percent) : total > 0 ? (used / total) * 100 : 0;
  const percent = total > 0 ? Math.max(0, Math.min(100, rawPercent)) : 0;
  if (total > 0) {
    return {
      percent,
      helper: `${formatBytesFixed(used, 1)} / ${formatBytesFixed(total, 1)}`,
      value: `${numberLabel(percent, 0)}%`,
    };
  }
  if (free > 0) {
    return {
      percent: 0,
      helper: "请先配置 NAS 存储空间",
      value: formatBytesFixed(free, 2),
    };
  }
  return {
    percent: 0,
    helper: "请先配置 NAS 存储空间",
    value: "-",
  };
}

function SetupPage({ onDone }: { onDone: (user: User) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const result = await api<{ access_token: string; user: User }>("/api/setup/admin", {
        method: "POST",
        body: JSON.stringify({ username, password })
      });
      setToken(result.access_token);
      onDone(result.user);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return <AuthForm title="创建管理员" username={username} password={password} error={error} submitLabel="初始化" onUsername={setUsername} onPassword={setPassword} onSubmit={submit} />;
}

function LoginPage({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const result = await api<{ access_token: string; user: User }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password })
      });
      setToken(result.access_token);
      onLogin(result.user);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return <AuthForm title="登录" username={username} password={password} error={error} submitLabel="登录" onUsername={setUsername} onPassword={setPassword} onSubmit={submit} />;
}

function AuthForm(props: {
  title: string;
  username: string;
  password: string;
  error: string;
  submitLabel: string;
  onUsername: (value: string) => void;
  onPassword: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
}) {
  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={props.onSubmit}>
        <BrandLogo subtitle="面向 NAS 的媒体管理应用" large />
        <h1>{props.title}</h1>
        <label>用户名<input value={props.username} onChange={(event) => props.onUsername(event.target.value)} /></label>
        <label>密码<input type="password" value={props.password} onChange={(event) => props.onPassword(event.target.value)} /></label>
        {props.error && <p className="error">{props.error}</p>}
        <button className="primary">{props.submitLabel}</button>
      </form>
    </div>
  );
}

function mergeDashboardQbs(current: any, realtime: any) {
  if (!current || !realtime) return current;
  return {
    ...current,
    qbs: realtime.qbs ?? current.qbs,
    overview: {
      ...current.overview,
      total_download_speed: realtime.overview?.total_download_speed ?? current.overview?.total_download_speed,
      total_upload_speed: realtime.overview?.total_upload_speed ?? current.overview?.total_upload_speed,
      download_tasks: realtime.overview?.download_tasks ?? current.overview?.download_tasks,
      upload_tasks: realtime.overview?.upload_tasks ?? current.overview?.upload_tasks,
    },
    updated_at: realtime.updated_at ?? current.updated_at,
  };
}

function DashboardPage({ onOpenDownloader }: { onOpenDownloader?: (downloaderId: string) => void }) {
  const initialDashboard = useMemo(() => readLocalSnapshot<any>("dashboard"), []);
  const { data, loading, setData } = useLoad<any>(() => api("/api/dashboard?cached=true"), [], initialDashboard, false);
  const [refreshingMTeam, setRefreshingMTeam] = useState(false);
  const [testingMTeam, setTestingMTeam] = useState(false);
  const [testingQbId, setTestingQbId] = useState("");
  const [mteamStatusOverride, setMteamStatusOverride] = useState<{ success: boolean; message: string } | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const delayedRefreshKey = useRef("");

  async function loadDashboard(url: string, silent = false) {
    try {
      const value = await api<any>(url);
      setData(value);
      writeLocalSnapshot("dashboard", value);
      if (!silent) setDashboardError("");
      return value;
    } catch (err) {
      if (!silent) setDashboardError((err as Error).message);
      return null;
    }
  }

  useEffect(() => {
    if (data) writeLocalSnapshot("dashboard", data);
  }, [data]);

  useEffect(() => {
    if (!shouldRevalidateFromCache(data)) return;
    const key = String(data?._preload?.cached_at || data?.updated_at || "dashboard");
    if (delayedRefreshKey.current === key) return;
    delayedRefreshKey.current = key;
    const timer = window.setTimeout(() => {
      if (!document.hidden) loadDashboard("/api/dashboard?refresh=true", true);
    }, REVALIDATE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [data?._preload?.cached_at, data?._preload?.preloaded, data?.updated_at]);

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    async function loadCachedDashboard() {
      if (document.hidden) return;
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await api<any>("/api/dashboard?cached=true");
        if (alive) {
          setData(value);
          writeLocalSnapshot("dashboard", value);
          setDashboardError("");
        }
      } catch (err) {
        if (alive) setDashboardError((err as Error).message);
      } finally {
        inFlight = false;
      }
    }
    const timer = window.setInterval(() => {
      loadCachedDashboard();
    }, 60000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let inFlight = false;
    async function loadQbRealtime() {
      if (document.hidden) return;
      if (inFlight) return;
      inFlight = true;
      try {
        const value = await api<any>("/api/dashboard/qbs");
        if (alive) setData((current: any) => mergeDashboardQbs(current, value));
      } catch {
        // Keep the last successful qB snapshot visible; dashboard-level errors are handled by full refresh.
      } finally {
        inFlight = false;
      }
    }
    loadQbRealtime();
    const timer = window.setInterval(loadQbRealtime, 5000);
    const onVisibilityChange = () => {
      if (!document.hidden) loadQbRealtime();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  async function refreshDashboard() {
    if (refreshingMTeam) return;
    setRefreshingMTeam(true);
    setDashboardError("");
    try {
      setData(await api("/api/dashboard?refresh=true"));
      setMteamStatusOverride(null);
    } catch (err) {
      setDashboardError((err as Error).message);
    } finally {
      setRefreshingMTeam(false);
    }
  }

  async function testMTeamConnection() {
    if (testingMTeam) return;
    setTestingMTeam(true);
    setDashboardError("");
    try {
      const result = await api<{ success: boolean; message: string }>("/api/mteam/test", { method: "POST" });
      setMteamStatusOverride({ success: result.success, message: result.message });
    } catch (err) {
      setMteamStatusOverride({ success: false, message: (err as Error).message });
      setDashboardError((err as Error).message);
    } finally {
      setTestingMTeam(false);
    }
  }

  async function testQbConnection(downloaderId: string) {
    if (testingQbId) return;
    setTestingQbId(downloaderId);
    setDashboardError("");
    try {
      const result = await api<{ success: boolean; message: string }>(`/api/qb/${downloaderId}/test`, { method: "POST" });
      if (!result.success) setDashboardError(result.message);
      const realtime = await api<any>("/api/dashboard/qbs");
      setData((current: any) => mergeDashboardQbs(current, realtime));
    } catch (err) {
      setDashboardError((err as Error).message);
    } finally {
      setTestingQbId("");
    }
  }

  if (!data) return <Panel title="仪表盘"><p>正在加载运行数据...</p></Panel>;

  return (
    <div className="grid-page dashboard-page">
      <div className="dashboard-composite">
        <section className="metric-grid dashboard-overview">
          <Metric icon={Download} title="总下载速度" value={formatSpeed(data.overview.total_download_speed)} source="" />
          <Metric icon={Upload} title="总上传速度" value={formatSpeed(data.overview.total_upload_speed)} source="" />
          <Metric icon={Activity} title="活跃上传/下载" value={<ActiveTransferCounts upload={data.overview.upload_tasks ?? 0} download={data.overview.download_tasks ?? 0} />} source="" />
          <StorageMetric overview={data.overview} />
        </section>
        <section className="dashboard-downloaders">
          <div className="cards-row downloader-card-grid">
            {data.qbs.map((qb: any) => qb.locked ? <LockedCard key={qb.id} title={downloaderDashboardTitle(qb)} message={qb.message} onOpen={() => onOpenDownloader?.(qb.id)} /> : <DownloaderCard key={qb.id} qb={qb} onOpen={onOpenDownloader} onTestConnection={testQbConnection} testingConnection={testingQbId === qb.id} />)}
          </div>
        </section>
      </div>

      <MTeamSnapshotPanel
        mteam={data.mteam}
        connection={data.mteam_connection}
        onRefresh={refreshDashboard}
        refreshing={refreshingMTeam}
        onTestConnection={testMTeamConnection}
        testingConnection={testingMTeam}
        statusOverride={mteamStatusOverride}
        updatedAt={data.updated_at}
      />
      {dashboardError && <p className="error">{dashboardError}</p>}
    </div>
  );
}

function MTeamSnapshotPanel({
  mteam,
  connection,
  onRefresh,
  refreshing,
  onTestConnection,
  testingConnection,
  statusOverride,
  updatedAt
}: {
  mteam: any;
  connection: any;
  onRefresh: () => void;
  refreshing: boolean;
  onTestConnection: () => void;
  testingConnection: boolean;
  statusOverride: { success: boolean; message: string } | null;
  updatedAt?: string;
}) {
  const [trafficDimension, setTrafficDimension] = useState<TrafficDimension>("hour");
  const history = limitTrafficHistory(mteam.traffic_series?.[trafficDimension] ?? mteam.traffic_history ?? [], trafficDimension);
  const connected = statusOverride ? statusOverride.success : Boolean(connection?.enabled && connection?.last_test_success);
  const statusLabel = testingConnection ? "正在测试" : refreshing ? "正在刷新" : connected ? "连接正常" : "连接异常";
  const statusTitle = statusOverride?.message ?? connection?.message ?? statusLabel;
  const deltas = mteamDeltaLabels(mteam);

  return (
    <section className="panel">
      <div className="mteam-panel-header">
        <h2><a className="mteam-title-link" href="https://kp.m-team.cc/index" target="_blank" rel="noreferrer">站点用户数据 - M-Team</a></h2>
        <div className="mteam-status-tools" title={statusTitle}>
          <button className="status-dot-button" onClick={onTestConnection} disabled={testingConnection} title="测试 M-Team 连通性" aria-label="测试 M-Team 连通性">
            <span className={connected ? "status-dot online" : "status-dot offline"} />
          </button>
          <span className={connected ? "status-text online" : "status-text offline"}>{statusLabel}</span>
          <button className={refreshing ? "refresh-icon-button spinning" : "refresh-icon-button"} onClick={onRefresh} disabled={refreshing} title="重新抓取站点数据" aria-label="重新抓取站点数据">
            <RefreshCw size={17} />
          </button>
          <small className="last-refresh">数据更新于：{formatDateLabel(updatedAt || mteam.updated_at)}</small>
        </div>
      </div>
      <div className="mteam-stat-grid">
        <InfoTile icon={UserRound} label="用户等级" value={mteam.user_level ?? "User"} />
        <InfoTile icon={Coins} label="魔力值" value={numberLabel(mteam.bonus)} delta={deltas.bonus} />
        <InfoTile icon={Percent} label="分享率" value={numberLabel(mteam.ratio, 3)} delta={deltas.ratio} negative={String(deltas.ratio ?? "").includes("-")} />
        <InfoTile icon={Upload} label="总上传量" value={formatBytesFixed(mteam.upload_total, 2)} delta={deltas.upload} />
        <InfoTile icon={Download} label="总下载量" value={formatBytes(mteam.download_total)} delta={deltas.download} />
        <InfoTile icon={Activity} label="当前活跃上传/下载" value={<ActiveTransferCounts upload={mteam.active_uploads ?? 0} download={mteam.active_downloads ?? 0} />} />
        <InfoTile icon={Database} label="总做种体积" value={formatBytes(mteam.seed_size ?? 0)} delta={deltas.seed_size} negative={String(deltas.seed_size ?? "").includes("-")} />
        <InfoTile icon={CalendarDays} label="加入时间" value={mteam.joined_at ?? "-"} delta={joinedDurationLabel(mteam.joined_at)} />
      </div>
      <div className="traffic-chart">
        <div className="traffic-chart-header">
          <h3>历史流量</h3>
          <div className="traffic-dimension-tools" aria-label="统计维度">
            <span>维度</span>
            <div className="segmented compact">
              {[
                ["hour", "小时"],
                ["year", "年"],
                ["month", "月"],
                ["week", "周"],
                ["day", "天"]
              ].map(([value, label]) => (
                <button
                  className={trafficDimension === value ? "active" : ""}
                  type="button"
                  onClick={() => setTrafficDimension(value as TrafficDimension)}
                  key={value}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <TrafficLineChart history={history} dimension={trafficDimension} />
        <div className="legend"><span className="dot upload" />上传量<span className="dot download" />下载量</div>
      </div>
    </section>
  );
}

function TrafficLineChart({ history, dimension }: { history: any[]; dimension: TrafficDimension }) {
  const points = (history ?? [])
    .map((point) => ({
      date: trafficPointLabel(point),
      upload: Number(point.upload_total ?? 0),
      download: Number(point.download_total ?? 0),
    }))
    .filter((point) => point.date);
  if (!points.length) return <div className="traffic-empty">暂无{trafficDimensionLabel(dimension)}维度历史流量数据</div>;

  const width = 760;
  const height = 180;
  const padding = { top: 12, right: 22, bottom: 30, left: 60 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const maxTraffic = Math.max(1, ...points.map((point) => Math.max(point.upload, point.download)));
  const xFor = (index: number) => padding.left + (points.length === 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const yFor = (value: number) => padding.top + plotHeight - (value / maxTraffic) * plotHeight;
  const lineFor = (key: "upload" | "download") => points.map((point, index) => `${xFor(index)},${yFor(point[key])}`).join(" ");
  const yTicks = [1, 0.5, 0].map((ratio) => ({
    y: padding.top + plotHeight * (1 - ratio),
    label: formatBytes(maxTraffic * ratio),
  }));
  const baseline = padding.top + plotHeight;
  const areaFor = (key: "upload" | "download") => {
    const line = points.map((point, index) => `${xFor(index)},${yFor(point[key])}`).join(" ");
    return `${xFor(0)},${baseline} ${line} ${xFor(points.length - 1)},${baseline}`;
  };

  return (
    <div className="traffic-line-chart">
      <svg className="traffic-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="上传量和下载量历史折线图">
        {yTicks.map((tick) => (
          <g key={tick.y}>
            <line className="traffic-grid-line" x1={padding.left} y1={tick.y} x2={width - padding.right} y2={tick.y} />
            <text className="traffic-axis-label" x={padding.left - 10} y={tick.y + 4} textAnchor="end">{tick.label}</text>
          </g>
        ))}
        <polygon className="traffic-area upload" points={areaFor("upload")} />
        <polygon className="traffic-area download" points={areaFor("download")} />
        <polyline className="traffic-line upload" points={lineFor("upload")} />
        <polyline className="traffic-line download" points={lineFor("download")} />
        {points.map((point, index) => (
          <g key={`point-${point.date}-${index}`}>
            <circle className="traffic-point upload" cx={xFor(index)} cy={yFor(point.upload)} r={4} />
            <circle className="traffic-point download" cx={xFor(index)} cy={yFor(point.download)} r={4} />
          </g>
        ))}
        {points.map((point, index) => {
          const x = xFor(index);
          const uploadY = yFor(point.upload);
          const downloadY = yFor(point.download);
          const tooltipX = x > width - 235 ? x - 222 : x + 12;
          const tooltipY = Math.max(8, Math.min(uploadY, downloadY) - 64);
          return (
            <g className="traffic-hover-group" key={`${point.date}-${index}`}>
              <line className="traffic-crosshair" x1={x} y1={padding.top} x2={x} y2={padding.top + plotHeight} />
              <rect className="traffic-hit" x={x - 14} y={padding.top} width="28" height={plotHeight} />
              <g className="traffic-tooltip" transform={`translate(${tooltipX} ${tooltipY})`}>
                <rect width="210" height="64" rx="8" />
                <text x="12" y="20">{point.date}</text>
                <text className="upload" x="12" y="40">上传 {formatBytes(point.upload)}</text>
                <text className="download" x="112" y="40">下载 {formatBytes(point.download)}</text>
              </g>
            </g>
          );
        })}
        {points.map((point, index) => {
          if (points.length > 8 && index % Math.ceil(points.length / 8) !== 0 && index !== points.length - 1) return null;
          return (
            <text className="traffic-axis-label" x={xFor(index)} y={height - 10} textAnchor="middle" key={`label-${point.date}-${index}`}>
              {compactTrafficLabel(point.date)}
            </text>
          );
        })}
      </svg>
    </div>
  );
}

function limitTrafficHistory(history: any[], dimension: TrafficDimension): any[] {
  const limits: Partial<Record<TrafficDimension, number>> = {
    hour: 24,
    day: 14,
    week: 12,
    month: 12,
  };
  const limit = limits[dimension];
  if (!limit) return history ?? [];
  return (history ?? []).slice(-limit);
}

function trafficPointLabel(point: any): string {
  if (point?.label) return String(point.label);
  const raw = String(point?.date ?? point?.captured_at ?? "");
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function compactTrafficLabel(label: string): string {
  const hourMatch = label.match(/^(\d{2}\/\d{2})\s+(\d{2}):00$/);
  if (hourMatch) return `${hourMatch[2]}:00`;
  if (label.includes("~")) return label;
  if (/^\d{4}-\d{2}-\d{2}$/.test(label)) return label.slice(5).replace("-", "/");
  if (label.includes(":")) return label.slice(-5);
  return label.length > 10 ? label.slice(0, 10) : label;
}

function trafficDimensionLabel(dimension: TrafficDimension): string {
  return ({ year: "年", month: "月", week: "周", day: "天", hour: "小时" } as Record<TrafficDimension, string>)[dimension];
}

function mteamDeltaLabels(mteam: any): Record<string, string | undefined> {
  if (mteam?.delta_preview) {
    return {
      bonus: "近期 +816.6",
      ratio: "近期 +0.021",
      upload: "近期 +204 GB",
      download: "近期 +26 GB",
      seed_size: "近期 +118 GB",
    };
  }
  const prefix = (value?: string) => value ? `近5h ${value}` : undefined;
  return {
    bonus: prefix(mteam?.bonus_delta_label),
    ratio: prefix(mteam?.ratio_delta_label),
    upload: prefix(mteam?.upload_delta_label),
    download: prefix(mteam?.download_delta_label),
    seed_size: prefix(mteam?.seed_size_delta_label),
  };
}

function joinedDurationLabel(value: string): string | undefined {
  if (!value || value === "-") return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  const days = Math.max(0, Math.floor((Date.now() - date.getTime()) / 86400000));
  return `已加入 ${days} 天`;
}

function ActiveTransferCounts({ upload, download }: { upload: number; download: number }) {
  return (
    <span className="transfer-counts" aria-label={`活跃上传 ${upload}，活跃下载 ${download}`}>
      <span className="transfer-count upload" title="活跃上传">
        <Upload size={13} />
        {upload}
      </span>
      <span className="transfer-count download" title="活跃下载">
        <Download size={13} />
        {download}
      </span>
    </span>
  );
}

function InfoTile({ icon: Icon, label, value, delta, negative, className }: { icon: typeof Film; label: string; value: ReactNode; delta?: string; negative?: boolean; className?: string }) {
  return (
    <div className={className ? `info-tile ${className}` : "info-tile"}>
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
        {delta && <span className={negative ? "delta negative" : "delta"}>{delta}</span>}
      </div>
      <span className="tile-icon"><Icon size={15} /></span>
    </div>
  );
}

function DiscoverPage({ resetToken = 0 }: { resetToken?: number }) {
  const initialDiscover = useMemo(() => readLocalSnapshot<any>("discover.lists"), []);
  const { data, error, loading, setData } = useLoad<any>(() => api("/api/discover/lists?cached=true"), [], initialDiscover, false);
  const [query, setQuery] = useState("");
  const [media, setMedia] = useState<any[]>([]);
  const [torrents, setTorrents] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [discoverMode, setDiscoverMode] = useState<DiscoverMode>("home");
  const [browseMode, setBrowseMode] = useState<DiscoverBrowseMode>("casual");
  const [searchHistory, setSearchHistory] = useState<string[]>(() => readSearchHistory());
  const [expandedDiscoverTitle, setExpandedDiscoverTitle] = useState<string | null>(null);
  const [selectedMedia, setSelectedMedia] = useState<any | null>(null);
  const [selectedPerson, setSelectedPerson] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [resourceSort, setResourceSort] = useState("seeders");
  const [resourceSortDirection, setResourceSortDirection] = useState<"asc" | "desc">("desc");
  const [discoverFilters, setDiscoverFilters] = useState<DiscoverFilters>(DEFAULT_DISCOVER_FILTERS);
  const [filterPayload, setFilterPayload] = useState<any | null>(null);
  const [filterItems, setFilterItems] = useState<any[]>([]);
  const [filterLoading, setFilterLoading] = useState(false);
  const [filterLoadingMore, setFilterLoadingMore] = useState(false);
  const [filterError, setFilterError] = useState("");
  const [filterNextPage, setFilterNextPage] = useState<number | null>(null);
  const [filterHasMore, setFilterHasMore] = useState(false);
  const filterRequestId = useRef(0);
  const filterKeyRef = useRef("");
  const filterSentinelRef = useRef<HTMLDivElement | null>(null);
  const filterLoadingPageRef = useRef<number | null>(null);
  const lists = data ? [
    { title: "流行趋势", items: data.trending },
    { title: "热门电影", items: data.popular_movies },
    { title: "热门剧集", items: data.popular_tv },
    { title: "Top Rated 电影", items: data.top_rated_movies },
    { title: "Top Rated 剧集", items: data.top_rated_tv }
  ] : [];
  const expandedDiscoverList = expandedDiscoverTitle ? lists.find((list) => list.title === expandedDiscoverTitle) : null;
  const filterViewActive = discoverMode === "home" && browseMode === "filter" && !selectedPerson && !selectedMedia && !expandedDiscoverTitle;
  const sortedTorrents = useMemo(() => sortResources(torrents, resourceSort, resourceSortDirection), [torrents, resourceSort, resourceSortDirection]);
  const discoverRefreshKey = useRef("");
  const discoverGenreRefreshKey = useRef("");

  useEffect(() => {
    if (data) writeLocalSnapshot("discover.lists", data);
  }, [data]);

  useEffect(() => {
    if (!shouldRevalidateFromCache(data)) return;
    const key = String(data?._preload?.cached_at || "discover");
    if (discoverRefreshKey.current === key) return;
    discoverRefreshKey.current = key;
    const timer = window.setTimeout(() => {
      if (!document.hidden) {
        api<any>("/api/discover/lists?refresh=true").then((value) => {
          setData(value);
          writeLocalSnapshot("discover.lists", value);
        }).catch(() => undefined);
      }
    }, REVALIDATE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [data?._preload?.cached_at, data?._preload?.preloaded]);

  useEffect(() => {
    if (!data?.configured || !discoverListsNeedGenres(data)) return;
    const key = String(data?._preload?.cached_at || data?.updated_at || "missing-genres");
    if (discoverGenreRefreshKey.current === key) return;
    discoverGenreRefreshKey.current = key;
    const timer = window.setTimeout(() => {
      if (!document.hidden) {
        api<any>("/api/discover/lists?refresh=true").then((value) => {
          setData(value);
          writeLocalSnapshot("discover.lists", value);
        }).catch(() => undefined);
      }
    }, 300);
    return () => window.clearTimeout(timer);
  }, [data]);

  useEffect(() => {
    resetDiscoverHome();
  }, [resetToken]);

  useEffect(() => {
    if (discoverMode !== "home" || browseMode !== "filter") return;
    const filterKey = discoverFilterKey(discoverFilters);
    filterKeyRef.current = filterKey;
    const timer = window.setTimeout(() => {
      loadDiscoverFilter(discoverFilters, { page: 1, pages: 1, cached: true }).then((result) => {
        if (filterKeyRef.current === filterKey && result?._preload?.preloaded) {
          window.setTimeout(() => {
            if (!document.hidden) loadDiscoverFilter(discoverFilters, { page: 1, pages: 1, refresh: true, silent: true }).catch(() => undefined);
          }, REVALIDATE_DELAY_MS);
        }
      });
    }, 220);
    return () => window.clearTimeout(timer);
  }, [discoverMode, browseMode, discoverFilters]);

  useEffect(() => {
    if (!filterViewActive) return;
    const node = filterSentinelRef.current;
    if (!node || !filterHasMore || filterLoading || filterLoadingMore || !filterNextPage) return;
    const nextPage = filterNextPage;
    let cancelled = false;
    function loadNextPage() {
      if (cancelled || filterLoadingPageRef.current === nextPage) return;
      loadDiscoverFilter(discoverFilters, { append: true, page: nextPage, pages: 2 }).catch(() => undefined);
    }
    const frame = window.requestAnimationFrame(() => {
      const rect = node.getBoundingClientRect();
      if (rect.top <= window.innerHeight + 1200 && rect.bottom >= -1200) {
        loadNextPage();
      }
    });
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        loadNextPage();
      }
    }, { rootMargin: "1200px 0px" });
    observer.observe(node);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [filterViewActive, discoverFilters, filterHasMore, filterLoading, filterLoadingMore, filterNextPage]);

  function resetDiscoverHome() {
    setQuery("");
    setMedia([]);
    setTorrents([]);
    setSearching(false);
    setSearchError("");
    setDiscoverMode("home");
    setBrowseMode("casual");
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailError("");
    setFilterError("");
  }

  function switchBrowseMode(nextMode: DiscoverBrowseMode) {
    setBrowseMode(nextMode);
    setDiscoverMode("home");
    setMedia([]);
    setTorrents([]);
    setSearchError("");
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailError("");
  }

  function enterSearchMode() {
    if (discoverMode !== "dual") {
      setDiscoverMode("dual");
      setSelectedMedia(null);
      setSelectedPerson(null);
      setExpandedDiscoverTitle(null);
    }
  }

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    await runSearchKeyword(query);
  }

  async function runSearchKeyword(value: string) {
    const keyword = value.trim();
    if (!keyword) return;
    setQuery(keyword);
    setSearching(true);
    setSearchError("");
    setDiscoverMode("dual");
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setMedia([]);
    setTorrents([]);
    setSearchHistory(writeSearchHistory(keyword));
    const [mediaResult, torrentResult] = await Promise.allSettled([
      api<{ items: any[] }>(`/api/search/media?q=${encodeURIComponent(keyword)}`),
      api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(keyword)}`)
    ]);
    if (mediaResult.status === "fulfilled") setMedia(mediaResult.value.items);
    else setMedia([]);
    if (torrentResult.status === "fulfilled") setTorrents(torrentResult.value.items);
    else setTorrents([]);
    const errors = [mediaResult, torrentResult]
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => (result.reason as Error).message);
    setSearchError(errors.join("\n"));
    setSearching(false);
  }

  async function openMediaDetail(item: any) {
    const tmdbId = mediaTmdbId(item);
    if (!tmdbId || !item.media_type) return;
    setSelectedPerson(null);
    setSelectedMedia(item);
    setExpandedDiscoverTitle(null);
    setDetailLoading(true);
    setDetailError("");
    try {
      setSelectedMedia(await api<any>(`/api/tmdb/media/${item.media_type}/${tmdbId}`));
    } catch (err) {
      setDetailError((err as Error).message);
    } finally {
      setDetailLoading(false);
    }
  }

  async function openPersonDetail(person: any) {
    const personId = person?.person_id ?? person?.id;
    if (!personId) return;
    setSelectedPerson(person);
    setDetailLoading(true);
    setDetailError("");
    try {
      setSelectedPerson(await api<any>(`/api/tmdb/person/${personId}`));
    } catch (err) {
      setDetailError((err as Error).message);
    } finally {
      setDetailLoading(false);
    }
  }

  function closeMediaDetail() {
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailError("");
  }

  async function searchMTeamFromMedia(item: any) {
    const keyword = String(item?.title || item?.original_title || "").trim();
    if (!keyword) return;
    setQuery(keyword);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setExpandedDiscoverTitle(null);
    setMedia([]);
    setTorrents([]);
    setBrowseMode("casual");
    setDiscoverMode("mteam");
    setSearching(true);
    setSearchError("");
    try {
      setSearchHistory(writeSearchHistory(keyword));
      const result = await api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(keyword)}`);
      setTorrents(result.items);
    } catch (err) {
      setSearchError((err as Error).message);
    } finally {
      setSearching(false);
    }
  }

  async function loadDiscoverFilter(filters: DiscoverFilters, options: { append?: boolean; page?: number; pages?: number; cached?: boolean; refresh?: boolean; silent?: boolean } = {}) {
    const append = Boolean(options.append);
    const page = options.page ?? 1;
    const pages = options.pages ?? (append ? 2 : 1);
    const snapshotKey = `discover.filter:${discoverFilterKey(filters)}:${page}:${pages}`;
    const localSnapshot = !append && options.cached ? readLocalSnapshot<any>(snapshotKey) : null;
    if (append) {
      if (filterLoadingPageRef.current === page) return;
      filterLoadingPageRef.current = page;
    } else {
      filterLoadingPageRef.current = null;
    }
    const requestId = append ? filterRequestId.current : filterRequestId.current + 1;
    if (!append) filterRequestId.current = requestId;
    if (append) setFilterLoadingMore(true);
    else if (localSnapshot) {
      setFilterPayload(localSnapshot);
      setFilterItems(localSnapshot?.items ?? []);
      setFilterNextPage(localSnapshot?.next_page ?? null);
      setFilterHasMore(Boolean(localSnapshot?.next_page));
      setFilterLoading(false);
    }
    else if (!options.silent) {
      setFilterLoading(true);
      setFilterItems([]);
      setFilterNextPage(null);
      setFilterHasMore(false);
    }
    if (!append) setFilterError("");
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (key === "min_rating" && Number(value || 0) <= 0) return;
      if (value) params.set(key, value);
    });
    params.set("page", String(page));
    params.set("pages", String(pages));
    if (append || ((options.refresh || options.silent) && filterPayload?.options)) params.set("include_options", "false");
    if (options.cached) params.set("cached", "true");
    if (options.refresh) params.set("refresh", "true");
    try {
      const result = await api<any>(`/api/discover/filter?${params.toString()}`);
      if (filterRequestId.current === requestId) {
        setFilterPayload((current: any) => append ? { ...(current ?? {}), ...result, options: result?.options ?? current?.options } : { ...result, options: result?.options ?? current?.options });
        setFilterNextPage(result?.next_page ?? null);
        setFilterHasMore(Boolean(result?.next_page));
        setFilterItems((current) => append ? mergeMediaItems(current, result?.items ?? []) : (result?.items ?? []));
        if (!append && page === 1) writeLocalSnapshot(snapshotKey, result);
      }
      return result;
    } catch (err) {
      if (filterRequestId.current === requestId) {
        if (!append) {
          setFilterPayload(null);
          setFilterItems([]);
          setFilterNextPage(null);
          setFilterHasMore(false);
        }
        setFilterError((err as Error).message);
      }
    } finally {
      if (append) {
        filterLoadingPageRef.current = null;
        setFilterLoadingMore(false);
      }
      else if (filterRequestId.current === requestId) setFilterLoading(false);
    }
  }

  function updateDiscoverFilters(patch: Partial<DiscoverFilters>) {
    setDiscoverFilters((current) => {
      const next = { ...current, ...patch };
      if (patch.media_type && patch.media_type !== current.media_type) next.genre = "";
      return next;
    });
  }

  return (
    <div className="grid-page">
      <button className="discover-scroll-top" type="button" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })} aria-label="回到顶部" title="回到顶部">
        <ArrowUp size={20} />
      </button>
      <form className="searchbar" onSubmit={runSearch}>
        <input value={query} onFocus={enterSearchMode} onChange={(event) => setQuery(event.target.value)} placeholder="搜索电影、剧集、年份、制作组或关键词" />
        <button className="primary" disabled={searching}><Search size={18} /> {searching ? "搜索中..." : "搜索"}</button>
      </form>
      {searchHistory.length > 0 && (
        <div className="search-history-strip" aria-label="历史搜索">
          <span>最近搜索</span>
          {searchHistory.map((keyword) => (
            <span className="search-history-chip" key={keyword}>
              <button type="button" className="search-history-keyword" onClick={() => runSearchKeyword(keyword)} disabled={searching}>
                {keyword}
              </button>
              <button type="button" className="search-history-remove" onClick={() => setSearchHistory(removeSearchHistory(keyword))} aria-label={`删除最近搜索 ${keyword}`}>
                ×
              </button>
            </span>
          ))}
          <button type="button" className="search-history-clear" onClick={() => setSearchHistory(clearSearchHistory())}>清空记录</button>
        </div>
      )}
      {!selectedPerson && !selectedMedia && !expandedDiscoverList && discoverMode !== "mteam" && (
        <DiscoverModeTabs mode={browseMode} onChange={switchBrowseMode} />
      )}
      {searchError && <p className="error">{searchError}</p>}
      {selectedPerson ? (
        <PersonDetailPage person={selectedPerson} loading={detailLoading} error={detailError} onBack={() => setSelectedPerson(null)} onMediaSelect={openMediaDetail} />
      ) : selectedMedia ? (
        <MediaDetailPage item={selectedMedia} loading={detailLoading} error={detailError} onBack={closeMediaDetail} onPersonSelect={openPersonDetail} onMediaSelect={openMediaDetail} onMTeamSearch={searchMTeamFromMedia} mteamSearching={searching} />
      ) : expandedDiscoverList ? (
        <DiscoverCollectionPage title={expandedDiscoverList.title} items={expandedDiscoverList.items} onBack={() => setExpandedDiscoverTitle(null)} onSelect={openMediaDetail} />
      ) : (
        <>
          {discoverMode === "dual" || discoverMode === "mteam" ? (
            <div className="discover-search-results">
              {discoverMode === "dual" && <MediaSearchResults items={media} onSelect={openMediaDetail} loading={searching} />}
              <MTeamResourceResults
                items={sortedTorrents}
                loading={searching}
                sortBy={resourceSort}
                sortDirection={resourceSortDirection}
                onSortBy={setResourceSort}
                onSortDirection={setResourceSortDirection}
                onBack={resetDiscoverHome}
              />
            </div>
          ) : null}
          {discoverMode === "home" && browseMode === "casual" && (
            <>
              {loading && !data && <Panel title="发现"><p>正在从 TMDB 加载片单...</p></Panel>}
              {error && <Panel title="TMDB 获取失败"><p className="error">{error}</p></Panel>}
              {data && !data.configured && <Panel title="需要配置 TMDB"><p>{data.message}</p><p className="muted">进入“设置”，在 TMDB 配置里填写 API Key 或 Bearer Token，保存并启用后再回到发现页。</p></Panel>}
              {lists.map((list, index) => <PosterRail title={list.title} items={list.items} eagerLimit={index === 0 ? eagerPosterLimit() : 0} onMore={() => setExpandedDiscoverTitle(list.title)} onSelect={openMediaDetail} key={list.title} />)}
            </>
          )}
          {discoverMode === "home" && browseMode === "filter" && (
            <DiscoverFilterPage
              filters={discoverFilters}
              payload={filterPayload}
              items={filterItems}
              loading={filterLoading}
              loadingMore={filterLoadingMore}
              error={filterError}
              hasMore={filterHasMore}
              sentinelRef={filterSentinelRef}
              onChange={updateDiscoverFilters}
              onSelect={openMediaDetail}
            />
          )}
        </>
      )}
    </div>
  );
}

function DiscoverModeTabs({ mode, onChange }: { mode: DiscoverBrowseMode; onChange: (mode: DiscoverBrowseMode) => void }) {
  return (
    <div className="discover-mode-tabs" role="tablist" aria-label="发现模式">
      <button className={mode === "casual" ? "active" : ""} type="button" role="tab" aria-selected={mode === "casual"} onClick={() => onChange("casual")}>
        <Film size={16} /> 随便看看
      </button>
      <button className={mode === "filter" ? "active" : ""} type="button" role="tab" aria-selected={mode === "filter"} onClick={() => onChange("filter")}>
        <SlidersHorizontal size={16} /> 条件筛选
      </button>
    </div>
  );
}

function DiscoverFilterPage({
  filters,
  payload,
  items,
  loading,
  loadingMore,
  error,
  hasMore,
  sentinelRef,
  onChange,
  onSelect
}: {
  filters: DiscoverFilters;
  payload: any;
  items: any[];
  loading: boolean;
  loadingMore: boolean;
  error: string;
  hasMore: boolean;
  sentinelRef: RefObject<HTMLDivElement | null>;
  onChange: (patch: Partial<DiscoverFilters>) => void;
  onSelect: (item: any) => void;
}) {
  const options = payload?.options ?? {};
  const genreOptions = options?.genres?.[filters.media_type] ?? [];
  const sortOptions = options?.sorts ?? [
    { value: "popularity.desc", label: "综合排序" },
    { value: "release_date.desc", label: "首播时间" },
    { value: "vote_average.desc", label: "高分优先" },
    { value: "vote_count.desc", label: "讨论热度" },
  ];
  const regionOptions = options?.regions ?? [
    { value: "", label: "不限地区" },
    { value: "CN", label: "中国大陆" },
    { value: "HK", label: "中国香港" },
    { value: "TW", label: "中国台湾" },
    { value: "US", label: "美国" },
    { value: "JP", label: "日本" },
    { value: "KR", label: "韩国" },
  ];
  const languageOptions = options?.languages ?? [
    { value: "", label: "不限语言" },
    { value: "zh", label: "中文" },
    { value: "en", label: "英语" },
    { value: "ja", label: "日语" },
    { value: "ko", label: "韩语" },
  ];
  const yearOptions = discoverYearOptions();
  const loadedCount = items.length;
  const minRating = Number(filters.min_rating || 0);

  return (
    <section className="discover-filter-page">
      <div className="discover-filter-panel">
        <div className="discover-filter-topline">
          <div className="segmented compact">
            <button className={filters.media_type === "movie" ? "active" : ""} type="button" onClick={() => onChange({ media_type: "movie" })}>电影</button>
            <button className={filters.media_type === "tv" ? "active" : ""} type="button" onClick={() => onChange({ media_type: "tv" })}>电视剧</button>
          </div>
          <button type="button" onClick={() => onChange({ ...DEFAULT_DISCOVER_FILTERS })}>重置条件</button>
        </div>
        <FilterChipGroup label="排序" value={filters.sort_by} options={sortOptions} onChange={(value) => onChange({ sort_by: value })} />
        <FilterChipGroup label="题材" value={filters.genre} options={[{ value: "", label: "不限题材" }, ...genreOptions.map((genre: any) => ({ value: String(genre.id), label: genre.name }))]} onChange={(value) => onChange({ genre: value })} />
        <FilterChipGroup label="地区" value={filters.region} options={regionOptions} onChange={(value) => onChange({ region: value })} />
        <FilterChipGroup label="年代" value={filters.year} options={yearOptions} onChange={(value) => onChange({ year: value })} />
        <FilterChipGroup label="语言" value={filters.language} options={languageOptions} onChange={(value) => onChange({ language: value })} />
        <RatingSlider value={filters.min_rating} onChange={(value) => onChange({ min_rating: value })} />
      </div>

      {error && <p className="error">{error}</p>}
      {payload?.message && <p className="muted">{payload.message}</p>}

      <div className="discover-filter-results-head">
        <div>
          <h2>筛选结果</h2>
          <span>{loading ? "正在刷新" : `已加载 ${loadedCount} 部${payload?.total_results ? ` / 约 ${payload.total_results} 个匹配条目` : ""}${minRating > 0 ? ` · ${minRating.toFixed(1)} 分以上` : ""}`}</span>
        </div>
      </div>
      <div className="poster-grid discover-filter-grid">
        {loading && <SearchLoadingState title="正在筛选 TMDB" detail="正在按类型、题材、地区和评分刷新海报墙" />}
        {items.map((item: any, index: number) => <DiscoverPosterCard item={item} eager={index < eagerPosterLimit()} onSelect={onSelect} key={item.id} />)}
        {!loading && !items.length && <p className="muted">没有符合条件的条目，可以放宽题材、地区或评分。</p>}
      </div>
      <div className="discover-load-sentinel" ref={sentinelRef}>
        {loadingMore ? <SearchLoadingState title="正在加载中" detail="请稍候" /> : hasMore ? <span>向下滚动加载更多</span> : loadedCount > 0 ? <span>已经到底了</span> : null}
      </div>
    </section>
  );
}

function RatingSlider({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const rating = Number(value || 0);
  return (
    <div className="filter-chip-group rating-filter">
      <span>评分</span>
      <div className="rating-slider-wrap">
        <input type="range" min="0" max="10" step="0.1" value={rating} onChange={(event) => onChange(event.target.value)} aria-label="最低评分" />
        <strong>{rating <= 0 ? "不限评分" : `${rating.toFixed(1)} 分以上`}</strong>
      </div>
    </div>
  );
}

function FilterChipGroup({ label, value, options, onChange }: { label: string; value: string; options: { value: string; label: string }[]; onChange: (value: string) => void }) {
  return (
    <div className="filter-chip-group">
      <span>{label}</span>
      <div>
        {options.map((option) => (
          <button className={value === option.value ? "active" : ""} type="button" onClick={() => onChange(option.value)} key={`${label}-${option.value || "all"}`}>
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function discoverYearOptions() {
  const currentYear = new Date().getFullYear();
  const years = Array.from({ length: 6 }, (_, index) => {
    const year = currentYear - index;
    return { value: String(year), label: String(year) };
  });
  return [
    { value: "", label: "不限年代" },
    ...years,
    { value: "2020s", label: "2020年代" },
    { value: "2010s", label: "2010年代" },
    { value: "2000s", label: "2000年代" },
    { value: "1990s", label: "90年代" },
    { value: "1980s", label: "80年代" },
    { value: "1970s", label: "70年代" },
  ];
}

function mergeMediaItems(current: any[], incoming: any[]) {
  const seen = new Set(current.map((item) => String(item.id ?? item.tmdb_id)));
  const merged = [...current];
  incoming.forEach((item) => {
    const key = String(item.id ?? item.tmdb_id);
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(item);
    }
  });
  return merged;
}

function discoverListsNeedGenres(payload: any): boolean {
  const groups = [
    payload?.trending,
    payload?.popular_movies,
    payload?.popular_tv,
    payload?.top_rated_movies,
    payload?.top_rated_tv,
  ];
  return groups.some((group) => Array.isArray(group) && group.some((item) => !Array.isArray(item?.genres) || item.genres.length === 0));
}

function discoverFilterKey(filters: DiscoverFilters) {
  return [
    filters.media_type,
    filters.sort_by,
    filters.genre,
    filters.region,
    filters.language,
    filters.year,
    filters.min_rating,
  ].join("|");
}

function DownloadsPage({ selectedDownloader = "qb1" }: { selectedDownloader?: string }) {
  const [downloader, setDownloader] = useState(selectedDownloader);
  const [grantOpen, setGrantOpen] = useState(false);
  const { data, error, loading, setData } = useLoad<any>(() => api(`/api/downloads/${downloader}/overview?cached=true`), [downloader]);
  const downloadRequestId = useRef(0);
  const downloadOverviewInFlight = useRef(false);
  const [qb2Authorized, setQb2Authorized] = useState(false);
  const [refreshingDownload, setRefreshingDownload] = useState(false);

  useEffect(() => {
    if (selectedDownloader && selectedDownloader !== downloader) {
      setDownloader(selectedDownloader);
    }
  }, [selectedDownloader]);

  useEffect(() => {
    refreshDownloadOverview(downloader);
    const timer = window.setInterval(() => {
      refreshDownloadOverview(downloader);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [downloader]);

  function refreshDownloadOverview(target = downloader, refresh = true) {
    if (downloadOverviewInFlight.current) return Promise.resolve(undefined);
    downloadOverviewInFlight.current = true;
    const requestId = downloadRequestId.current + 1;
    downloadRequestId.current = requestId;
    return api<any>(`/api/downloads/${target}/overview?${refresh ? "refresh=true" : "cached=true"}`)
      .then((value) => {
        if (downloadRequestId.current === requestId && value?.downloader_id === target) setData(value);
        if (target === "qb2") setQb2Authorized(true);
        return value;
      })
      .catch(() => undefined)
      .finally(() => {
        downloadOverviewInFlight.current = false;
      });
  }

  async function manualRefreshDownloadOverview() {
    if (refreshingDownload) return;
    setRefreshingDownload(true);
    try {
      await refreshDownloadOverview(downloader);
    } finally {
      setRefreshingDownload(false);
    }
  }

  async function revokeQb2Grant() {
    await api("/api/auth/qb2-grant/revoke", { method: "POST" });
    setQb2Authorized(false);
    if (downloader === "qb2") {
      setData(null);
      setGrantOpen(false);
    }
  }

  const visibleData = data?.downloader_id === downloader ? data : null;
  const qb2Locked = downloader === "qb2" && !qb2Authorized && (Boolean(error) || (!loading && !visibleData));
  const summary = visibleData?.summary;
  const items = visibleData?.items ?? [];

  return (
    <div className="grid-page download-page">
      <div className="download-heading">
        <div>
          <h1>下载</h1>
          <p>查看和管理多个 qB 下载器中的真实任务。</p>
        </div>
        <div className="segmented">
          {["qb1", "qb2", "qb3"].map((id) => <button className={downloader === id ? "active" : ""} onClick={() => setDownloader(id)} key={id}>{downloaderShortLabel(id)}</button>)}
        </div>
      </div>
      {summary && !qb2Locked && <QbSummaryCards qb={summary} count={items.length} onRefresh={manualRefreshDownloadOverview} refreshing={refreshingDownload} />}
      {downloader === "qb2" && qb2Authorized && !qb2Locked && <div className="qb-grant-strip"><span>qB2 隐私授权已开启</span><button type="button" onClick={revokeQb2Grant}>退出授权</button></div>}
      {qb2Locked && <Panel title="qB2 已锁定"><p>私有下载器需要管理员验证。</p><button className="primary" onClick={() => setGrantOpen(true)}><Lock size={16} /> 验证管理员</button></Panel>}
      {error && downloader !== "qb2" && <p className="error">{error}</p>}
      {grantOpen && <AdminGrant onDone={() => { setGrantOpen(false); setQb2Authorized(true); setData(null); refreshDownloadOverview("qb2"); }} />}
      {loading && !visibleData && (!error || qb2Authorized) && <Panel title="下载器"><p>正在读取 qB 真实数据...</p></Panel>}
      {visibleData && !qb2Locked && <QbTorrentTable key={downloader} items={items} downloader={downloader} onChanged={() => { refreshDownloadOverview(); }} />}
    </div>
  );
}

function QbSummaryCards({ qb, count, onRefresh, refreshing }: { qb: any; count: number; onRefresh: () => void; refreshing: boolean }) {
  return (
    <div className="qb-summary-cards">
      <SummaryCard
        icon={Database}
        label="当前下载器"
        value={downloaderDisplayName(qb)}
        helper={qb.online ? "在线" : "离线"}
        tone="mint"
        action={(
          <button className={refreshing ? "refresh-icon-button spinning" : "refresh-icon-button"} type="button" onClick={onRefresh} disabled={refreshing} title="刷新当前下载器" aria-label="刷新当前下载器">
            <RefreshCw size={15} />
          </button>
        )}
      />
      <SummaryCard icon={Download} label="下载速度" value={formatSpeed(qb.download_speed ?? 0)} helper="qB 实时速度" tone="teal" />
      <SummaryCard icon={Upload} label="上传速度" value={formatSpeed(qb.upload_speed ?? 0)} helper="qB 实时速度" tone="orange" />
      <SummaryCard icon={Activity} label="活跃上传/下载" value={<ActiveTransferCounts upload={qb.active_uploads ?? 0} download={qb.active_downloads ?? 0} />} helper={`活跃共 ${(qb.active_downloads ?? 0) + (qb.active_uploads ?? 0)} 个 / 总 ${count} 个任务`} tone="blue" />
      <span><strong>{qb.name}</strong>{qb.online ? "在线" : "离线"}</span>
      <span><Download size={14} /> {formatSpeed(qb.download_speed)}</span>
      <span><Upload size={14} /> {formatSpeed(qb.upload_speed)}</span>
      <span>活跃 <ActiveTransferCounts upload={qb.active_uploads ?? 0} download={qb.active_downloads ?? 0} /></span>
      <span>剩余 {formatBytes(qb.free_space ?? 0)}</span>
    </div>
  );
}

function SummaryCard({ icon: Icon, label, value, helper, tone, action }: { icon: typeof Film; label: string; value: ReactNode; helper: string; tone: "mint" | "teal" | "orange" | "blue"; action?: ReactNode }) {
  return (
    <article className={`summary-card ${tone}`}>
      <div className="summary-card-icon"><Icon size={24} /></div>
      <div>
        <span>{label}{action}</span>
        <strong>{value}</strong>
        <small>{helper}</small>
      </div>
    </article>
  );
}

function QbTorrentTable({ items, downloader, onChanged }: { items: any[]; downloader: string; onChanged: () => void }) {
  const [selectedHash, setSelectedHash] = useState("");
  const [detail, setDetail] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; item: any } | null>(null);
  const detailRequestId = useRef(0);
  const selectedItem = items.find((item) => item.hash === selectedHash) ?? null;

  useEffect(() => {
    if (selectedHash && !items.some((item) => item.hash === selectedHash)) {
      setSelectedHash("");
      setDetail(null);
      setDetailLoading(false);
      setDetailError("");
    }
  }, [items, selectedHash]);

  useEffect(() => {
    function closeMenu() {
      setContextMenu(null);
    }
    window.addEventListener("click", closeMenu);
    window.addEventListener("blur", closeMenu);
    return () => {
      window.removeEventListener("click", closeMenu);
      window.removeEventListener("blur", closeMenu);
    };
  }, []);

  async function loadDetail(hash = selectedHash) {
    if (!hash) return;
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setDetail(null);
    setDetailError("");
    setDetailLoading(true);
    try {
      const value = await api(`/api/qb/${downloader}/torrents/${encodeURIComponent(hash)}/detail`);
      if (detailRequestId.current === requestId) setDetail(value);
    } catch (err) {
      if (detailRequestId.current === requestId) {
        setDetail(null);
        setDetailError((err as Error).message);
      }
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  }

  async function mutateTorrent(item: any, action: "resume" | "pause") {
    await api(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}/${action}`, { method: "POST", body: JSON.stringify({ payload: {} }) });
    setContextMenu(null);
    onChanged();
    if (item.hash === selectedHash) loadDetail(item.hash);
  }

  async function deleteTorrent(item: any) {
    const confirmResult = await api<{ confirm_token: string }>(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}/delete-confirm`, { method: "POST" });
    const confirmed = window.confirm(`确认从 ${downloaderShortLabel(downloader)} 删除这个任务？\n\n${item.name}\n\n默认不会删除本地文件。`);
    if (!confirmed) return;
    await api(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}?confirm_token=${encodeURIComponent(confirmResult.confirm_token)}&delete_files=false`, { method: "DELETE" });
    setContextMenu(null);
    setDetail(null);
    setSelectedHash("");
    onChanged();
  }

  async function changeFilePriority(fileId: number, priority: number) {
    if (!selectedHash) return;
    await api(`/api/qb/${downloader}/torrents/${encodeURIComponent(selectedHash)}/files/${fileId}/priority`, {
      method: "POST",
      body: JSON.stringify({ payload: { priority } })
    });
    await loadDetail(selectedHash);
  }

  function openTaskMenu(item: any, x: number, y: number) {
    const menuWidth = 136;
    const menuHeight = 132;
    setSelectedHash(item.hash);
    setContextMenu({
      x: Math.max(8, Math.min(x, window.innerWidth - menuWidth - 8)),
      y: Math.max(8, Math.min(y, window.innerHeight - menuHeight - 8)),
      item,
    });
  }

  return (
    <>
      <section className="qb-table-panel">
        <div className="qb-table-header">
          <div>
            <h2>{downloaderShortLabel(downloader)} 任务</h2>
            <small>手机端长按任务可操作</small>
          </div>
          <span>{items.length} 条任务</span>
        </div>
        <div className="qb-task-list-wrap">
          <div className="qb-task-grid qb-task-head" role="row">
            <span>名称</span>
            <span>大小</span>
            <span>进度</span>
            <span>状态</span>
            <span>种子</span>
            <span>用户</span>
            <span>下载</span>
            <span>上传</span>
            <span>剩余</span>
            <span>比率</span>
            <span>流行度</span>
            <span>分类</span>
            <span>标签</span>
            <span>添加于</span>
            <span>路径</span>
          </div>
          <div className="qb-task-list">
            {items.map((item) => (
              <QbTorrentTableRow
                item={item}
                selected={item.hash === selectedHash}
                onSelect={() => {
                  setSelectedHash(item.hash);
                  loadDetail(item.hash);
                }}
                onContextMenu={(event) => {
                  event.preventDefault();
                  openTaskMenu(item, event.clientX, event.clientY);
                }}
                onLongPress={(point) => openTaskMenu(item, point.x, point.y)}
                key={item.hash}
              />
            ))}
            {!items.length && <div className="qb-empty">当前下载器没有任务</div>}
          </div>
        </div>
      </section>
      {contextMenu && (
        <div className="qb-context-menu" style={{ left: contextMenu.x, top: contextMenu.y }} onClick={(event) => event.stopPropagation()}>
          <button onClick={() => mutateTorrent(contextMenu.item, "resume")}>启动</button>
          <button onClick={() => mutateTorrent(contextMenu.item, "pause")}>暂停</button>
          <button className="danger" onClick={() => deleteTorrent(contextMenu.item)}>删除</button>
        </div>
      )}
      {selectedItem && detailLoading && <QbDetailLoading item={selectedItem} />}
      {(detail || detailError) && !detailLoading && (
        <QbTorrentDetailPanel item={selectedItem} detail={detail} error={detailError} onPriorityChange={changeFilePriority} />
      )}
    </>
  );
}

function AdminGrant({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function verify() {
    try {
      await api("/api/auth/admin-verify", { method: "POST", body: JSON.stringify({ username, password }) });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return <Panel title="管理员验证"><div className="form-grid"><input value={username} onChange={(event) => setUsername(event.target.value)} /><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /><button className="primary" onClick={verify}>授权 15 分钟</button>{error && <p className="error">{error}</p>}</div></Panel>;
}

function NotificationsPage() {
  const { data } = useLoad<any>(() => api("/api/notifications"), []);
  return <Panel title="通知中心"><div className="table-list">{(data?.items ?? []).map((item: any) => <div className="row" key={`${item.title}-${item.created_at}`}><strong>{item.title}</strong><span>{item.message}</span><small>{item.level} / {item.source}</small></div>)}</div></Panel>;
}

function NotificationsAssistantPage({ onNavigate }: { onNavigate?: (key: NavKey) => void }) {
  const { data, reload } = useLoad<any>(() => api("/api/notifications"), []);
  const [mode, setMode] = useState<"chat" | "json">("chat");
  const [message, setMessage] = useState("帮我查一下 qB 下载器状态");
  const [jsonIntent, setJsonIntent] = useState('{"action":"status_query","target":"dashboard","downloader_id":"all","limit":5}');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<any | null>(null);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    setBusy(true);
    setError("");
    try {
      const payload = mode === "chat" ? { message } : { message, intent: JSON.parse(jsonIntent) };
      const endpoint = mode === "chat" ? "/api/assistant/chat" : "/api/assistant/execute";
      const response = await api<any>(endpoint, { method: "POST", body: JSON.stringify(payload) });
      setResult(response);
      if (["download_started", "download_completed"].includes(response.intent?.action)) reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid-page notifications-page">
      <MemberManagementCard onNavigate={onNavigate} />
      <Panel title="AI 通知助手">
        <form className="assistant-box" onSubmit={submit}>
          <div className="notice info">
            <strong>通过 DeepSeek 把自然语言转换成后端可执行 JSON</strong>
            <span>先在设置页填写 DeepSeek API Key、模型名和 base_url。当前支持资源查询、下载开始通知、下载完成通知、各板块状态查询。</span>
          </div>
          <div className="segmented compact">
            <button type="button" className={mode === "chat" ? "active" : ""} onClick={() => setMode("chat")}><MessageSquare size={16} />自然语言</button>
            <button type="button" className={mode === "json" ? "active" : ""} onClick={() => setMode("json")}><Database size={16} />结构化 JSON</button>
          </div>
          {mode === "chat" ? (
            <label>输入请求
              <textarea value={message} onChange={(event) => setMessage(event.target.value)} placeholder="例如：帮我搜一下沙丘 2160p；qB1 现在下载速度多少；记录下载完成通知" />
            </label>
          ) : (
            <label>后端可执行 JSON
              <textarea value={jsonIntent} onChange={(event) => setJsonIntent(event.target.value)} />
            </label>
          )}
          <div className="actions">
            <button className="primary" disabled={busy}>{busy ? "处理中..." : "执行"}</button>
          </div>
          {error && <p className="error">{error}</p>}
          {result && (
            <div className="assistant-result">
              <div className="result-card success">
                <strong>回复</strong>
                <AssistantReply reply={result.reply} />
              </div>
              <details>
                <summary>查看结构化意图与后端结果</summary>
                <pre>{JSON.stringify({ intent: result.intent, result: result.result }, null, 2)}</pre>
              </details>
            </div>
          )}
        </form>
      </Panel>
      <Panel title="通知中心">
        <div className="table-list">{(data?.items ?? []).map((item: any) => <div className="row" key={`${item.title}-${item.created_at}`}><strong>{item.title}</strong><span>{item.message}</span><small>{item.level} / {item.source}</small></div>)}</div>
      </Panel>
    </div>
  );
}

function NotificationPreferencesCard() {
  const { data, reload } = useLoad<any>(() => api("/api/notification-preferences"), []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const preferences: Record<NotificationPreferenceKey, boolean> = {
    download_started: true,
    download_completed: true,
    resource_search: false,
    status_query: false,
    wechat_claw_push: true,
    ...(data?.preferences ?? {}),
  };
  const labels: Record<NotificationPreferenceKey, { title: string; help: string }> = {
    download_started: { title: "下载开始通知", help: "AI 或微信 claw 记录下载开始时写入通知中心。" },
    download_completed: { title: "下载完成通知", help: "AI 或微信 claw 记录下载完成时写入通知中心。" },
    resource_search: { title: "资源查询提醒", help: "开启后，AI/微信 claw 的资源查询结果会写入通知中心。" },
    status_query: { title: "状态查询提醒", help: "开启后，各板块状态查询结果会写入通知中心。" },
    wechat_claw_push: { title: "推送到 WeChat claw", help: "通知中心写入后，如果配置了 webhook，同步推送到手机端。" },
  };

  async function toggle(key: NotificationPreferenceKey) {
    setSaving(true);
    setError("");
    try {
      await api("/api/notification-preferences", {
        method: "PUT",
        body: JSON.stringify({ preferences: { ...preferences, [key]: !preferences[key] } }),
      });
      await reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Panel title="通知偏好">
      <div className="preference-list">
        {(Object.keys(labels) as NotificationPreferenceKey[]).map((key) => (
          <label className="preference-row" key={key}>
            <input type="checkbox" checked={Boolean(preferences[key])} disabled={saving || !data} onChange={() => toggle(key)} />
            <span>
              <strong>{labels[key].title}</strong>
              <small>{labels[key].help}</small>
            </span>
          </label>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
    </Panel>
  );
}

const MEMBER_AVATARS: Record<string, { emoji: string; label: string }> = {
  mint: { emoji: "🌿", label: "薄荷" },
  violet: { emoji: "🪻", label: "紫罗兰" },
  coral: { emoji: "🪸", label: "珊瑚" },
  sky: { emoji: "☁️", label: "天空" },
  amber: { emoji: "🍯", label: "琥珀" },
  rose: { emoji: "🌹", label: "玫瑰" },
  indigo: { emoji: "🌌", label: "靛蓝" },
  lime: { emoji: "🍋", label: "青柠" },
};

function MemberAvatar({ member, size = "normal" }: { member: any; size?: "normal" | "small" }) {
  const avatar = MEMBER_AVATARS[String(member?.avatar_key)] ?? MEMBER_AVATARS.mint;
  return <span className={`member-avatar-face ${size} ${String(member?.avatar_key || "mint")}`} title={`${avatar.label}头像`}>{avatar.emoji}</span>;
}

function MemberManagementCard({ onNavigate }: { onNavigate?: (key: NavKey) => void }) {
  const bindings = useLoad<any>(() => api("/api/admin/wechat-claw/bindings"), []);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draftPreferences, setDraftPreferences] = useState<Record<string, any>>({});
  const [deleteConfirm, setDeleteConfirm] = useState<{ member: any; stage: 1 | 2 } | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const items = Array.isArray(bindings.data?.items) ? bindings.data.items : [];
  const selected = items.find((item: any) => item.id === selectedId) ?? null;
  const selectedInteractions: WechatClawInteraction[] = Array.isArray(selected?.recent_interactions) ? selected.recent_interactions.slice(0, 5) : [];

  useEffect(() => {
    if (!items.length) {
      setSelectedId(null);
      return;
    }
    if (!items.some((item: any) => item.id === selectedId)) setSelectedId(items[0].id);
  }, [bindings.data, selectedId]);

  useEffect(() => {
    setDraftPreferences(selected?.notification_preferences ?? {});
  }, [selectedId, selected?.updated_at]);

  async function update(member: any, changes: Record<string, any>): Promise<boolean> {
    setBusy(String(member.id));
    setError("");
    try {
      await api(`/api/admin/wechat-claw/bindings/${member.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: changes.display_name ?? member.display_name,
          role_name: member.role_name,
          enabled: changes.enabled ?? member.enabled,
          notification_preferences: changes.notification_preferences ?? member.notification_preferences,
        }),
      });
      await bindings.reload();
      return true;
    } catch (err) {
      setError((err as Error).message);
      return false;
    } finally {
      setBusy("");
    }
  }

  async function addMember() {
    setBusy("add");
    setError("");
    try {
      const member = await api<any>("/api/admin/wechat-claw/bindings", { method: "POST", body: JSON.stringify({}) });
      await bindings.reload();
      setSelectedId(member.id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function editMember(member: any) {
    const name = window.prompt("成员名称", member.display_name)?.trim();
    if (!name || name === member.display_name) return;
    await update(member, { display_name: name });
  }

  async function deleteMember(member: any) {
    setBusy(`delete-${member.id}`);
    setError("");
    try {
      await api(`/api/admin/wechat-claw/bindings/${member.id}`, { method: "DELETE" });
      await bindings.reload();
      setSelectedId(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  function openConfiguration(member: any) {
    window.sessionStorage.setItem("ptmh.wechat.selectedMember", String(member.id));
    window.sessionStorage.setItem("ptmh.settings.activeStep", "wechat_claw");
    onNavigate?.("settings");
  }

  function requestDelete(member: any) {
    setDeleteConfirm({ member, stage: 1 });
  }

  const exceptionOptions = [
    ["mteam_exception", "站点模块异常"],
    ["qb_exception", "qB 下载器异常"],
    ["ai_exception", "AI 模块异常"],
    ["tmdb_exception", "TMDB 模块异常"],
    ["wechat_claw_exception", "WeChat claw 模块异常"],
  ] as const;
  const hasExceptionPreference = exceptionOptions.some(([key]) => Boolean(draftPreferences[key]));

  async function applyPreferences() {
    if (!selected) return;
    const saved = await update(selected, { notification_preferences: draftPreferences });
    if (saved) setSelectedId(null);
  }

  return (
    <Panel title="成员管理">
      <div className="member-management">
        <div className="member-management-head">
          <p className="muted">点击成员卡片即可展开设置；通知偏好按成员独立保存。</p>
          <button type="button" className="primary" onClick={addMember} disabled={busy !== ""}><Plus size={16} /> 新增成员</button>
        </div>
        <div className="member-card-grid">
          {items.map((member: any) => {
            const configured = Boolean(member.connected);
            return (
              <article
                className={selectedId === member.id ? "member-card selected" : "member-card"}
                key={member.id}
                onClick={() => setSelectedId(member.id)}
                onContextMenu={(event) => {
                  event.preventDefault();
                  setSelectedId(member.id);
                }}
              >
                <span className="member-avatar-button" aria-hidden="true">
                  <MemberAvatar member={member} />
                </span>
                <div className="member-card-copy">
                  <strong>{member.display_name}</strong>
                  <span className={configured ? "member-status configured" : "member-status pending"}>{configured ? "已配置" : "尚未配置"}</span>
                  <small>{configured ? "微信已连接" : "扫码绑定后即可接收主动通知"}</small>
                </div>
                {!configured && <button type="button" onClick={(event) => { event.stopPropagation(); openConfiguration(member); }}>前去配置</button>}
              </article>
            );
          })}
          {!items.length && <div className="member-empty">还没有成员。点击“新增成员”即可创建成员1并分配一个默认头像。</div>}
        </div>
        {selected && (
          <div className="member-notification-settings">
            <div className="member-settings-heading"><div><h3>{selected.display_name}的成员设置</h3><p className="muted">模块异常按小时采集结果计算：连续多个小时没有拉取到数据，才会推送。</p></div></div>
            <div className="member-notification-options">
              {([
                ["download_started", "下载开始"],
                ["download_completed", "下载完成"],
              ] as const).map(([key, label]) => (
                <label className="member-notification-option" key={key}>
                  <input type="checkbox" checked={Boolean(draftPreferences[key])} disabled={busy === String(selected.id)} onChange={() => setDraftPreferences((current) => ({ ...current, [key]: !current[key] }))} />
                  <span>{label}</span>
                </label>
              ))}
              {exceptionOptions.map(([key, label]) => <label className="member-notification-option" key={key}>
                <input type="checkbox" checked={Boolean(draftPreferences[key])} disabled={busy === String(selected.id)} onChange={() => setDraftPreferences((current) => ({ ...current, [key]: !current[key] }))} />
                <span>{label}</span>
              </label>)}
              {hasExceptionPreference && <label className="exception-duration">异常持续
                <select value={String(draftPreferences.exception_after_hours ?? 5)} disabled={busy === String(selected.id)} onChange={(event) => setDraftPreferences((current) => ({ ...current, exception_after_hours: Number(event.target.value) }))}>
                  <option value="3">3小时</option><option value="5">5小时</option><option value="24">24小时</option>
                </select>
                <span>后通知</span>
              </label>}
            </div>
            <div className="wechat-interactions member-interaction-diagnostics">
              <div className="wechat-interactions-head">
                <strong>最近手机端交互诊断</strong>
                <small>{selectedInteractions.length ? `${selectedInteractions.length} 条` : "暂无记录"}</small>
              </div>
              {selectedInteractions.map((item, index) => (
                <div className={`wechat-interaction-item ${item.status === "completed" ? "success" : "failed"}`} key={item.trace_id || `${item.created_at || index}-${index}`}>
                  <strong>
                    <span>{item.status === "completed" ? "已完成" : item.status === "failed" ? "查询失败（已通知）" : "未完成"}</span>
                    <span>{item.duration_ms ?? 0} ms</span>
                    {item.trace_id && <span>{item.trace_id}</span>}
                  </strong>
                  <p>{item.message || "（已脱敏消息）"}</p>
                  <small>{(item.stages ?? []).map((stage, stageIndex) => `${wechatClawStageLabel(stage.stage)} ${stage.duration_ms ?? 0}ms${stage.status === "failed" ? `（失败：${stage.error || "请求失败"}）` : ""}`).join(" · ") || "尚未采集阶段数据"}</small>
                  {item.error && <small className="error">失败原因：{item.error}</small>}
                  <small>{item.created_at ? new Date(item.created_at).toLocaleString() : ""}</small>
                </div>
              ))}
              {!selectedInteractions.length && <p className="muted">手机端发起交互后，这里会保留最近 5 条的耗时与失败阶段。</p>}
            </div>
            <div className="member-settings-actions"><button type="button" onClick={() => void editMember(selected)} disabled={busy !== ""}>修改成员名称</button><button type="button" className="danger" onClick={() => requestDelete(selected)} disabled={busy !== ""}>删除成员</button><button type="button" className="primary" onClick={() => void applyPreferences()} disabled={busy !== ""}>{busy === String(selected.id) ? "应用中..." : "应用"}</button></div>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {deleteConfirm && <div className="app-dialog-backdrop" role="presentation" onMouseDown={() => setDeleteConfirm(null)}>
          <div className="app-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-member-title" onMouseDown={(event) => event.stopPropagation()}>
            <span className="app-dialog-icon"><Trash2 size={20} /></span>
            <h3 id="delete-member-title">{deleteConfirm.stage === 1 ? `删除“${deleteConfirm.member.display_name}”？` : "请再次确认删除"}</h3>
            <p>{deleteConfirm.stage === 1 ? "该成员将不再接收通知。下一步会清除其扫码登录状态和互动记录。" : "此操作不可恢复，成员的 WeChat claw 登录状态、最近轮询与互动记录都会被永久删除。"}</p>
            <div className="app-dialog-actions">
              <button type="button" onClick={() => setDeleteConfirm(null)}>取消</button>
              {deleteConfirm.stage === 1 ? <button type="button" className="primary" onClick={() => setDeleteConfirm((current) => current ? { ...current, stage: 2 } : null)}>继续</button> : <button type="button" className="danger primary" onClick={() => { const member = deleteConfirm.member; setDeleteConfirm(null); void deleteMember(member); }} disabled={busy !== ""}>确认删除</button>}
            </div>
          </div>
        </div>}
      </div>
    </Panel>
  );
}

function WechatClawNotificationBindingsCard() {
  const bindings = useLoad<any>(() => api("/api/admin/wechat-claw/bindings"), []);
  const [savingId, setSavingId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const labels: Record<string, string> = {
    download_started: "下载开始",
    download_completed: "下载完成",
    resource_search: "资源查询结果",
    status_query: "状态查询结果",
  };

  async function save(binding: any, changes: Record<string, any>) {
    setSavingId(binding.id);
    setError("");
    try {
      await api(`/api/admin/wechat-claw/bindings/${binding.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          display_name: changes.display_name ?? binding.display_name,
          role_name: changes.role_name ?? binding.role_name,
          enabled: changes.enabled ?? binding.enabled,
          notification_preferences: changes.notification_preferences ?? binding.notification_preferences,
        }),
      });
      await bindings.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSavingId(null);
    }
  }

  const items = Array.isArray(bindings.data?.items) ? bindings.data.items : [];
  return (
    <Panel title="微信成员通知">
      <div className="integration">
        <p className="muted">角色名会显示在对应微信成员的推送标题中；通知内容按成员独立生效。</p>
        {items.length === 0 && <p className="muted">请先在设置中添加一个 WeChat claw 实例。</p>}
        {items.map((binding: any) => {
          const preferences = binding.notification_preferences ?? {};
          const busy = savingId === binding.id;
          return (
            <div className="wechat-binding-notification" key={binding.id}>
              <div className="settings-grid">
                <label>成员名称<input defaultValue={binding.display_name} onBlur={(event) => event.target.value.trim() !== binding.display_name && save(binding, { display_name: event.target.value.trim() })} /></label>
                <label>角色名<input defaultValue={binding.role_name} onBlur={(event) => event.target.value.trim() !== binding.role_name && save(binding, { role_name: event.target.value.trim() })} placeholder="例如：家庭影院管家" /></label>
              </div>
              <div className="preference-list compact-preferences">
                {Object.entries(labels).map(([key, label]) => (
                  <label className="preference-row" key={key}>
                    <input type="checkbox" checked={Boolean(preferences[key])} disabled={busy} onChange={() => save(binding, { notification_preferences: { ...preferences, [key]: !preferences[key] } })} />
                    <span><strong>{label}</strong></span>
                  </label>
                ))}
                <label className="preference-row">
                  <input type="checkbox" checked={Boolean(binding.enabled)} disabled={busy} onChange={() => save(binding, { enabled: !binding.enabled })} />
                  <span><strong>启用该微信成员</strong></span>
                </label>
              </div>
            </div>
          );
        })}
        {error && <p className="error">{error}</p>}
      </div>
    </Panel>
  );
}

function PersonalWechatClawCard() {
  const setup = useLoad<any>(() => api("/api/me/wechat-claw/setup"), []);
  const [busy, setBusy] = useState("");
  const [result, setResult] = useState<any | null>(null);
  const [error, setError] = useState("");
  const qrcode = setup.data?.qrcode ?? {};
  const lastPoll = result ?? setup.data?.last_poll ?? {};

  async function call(action: "refresh" | "logout" | "poll") {
    setBusy(action);
    setError("");
    try {
      const response = await api<any>(`/api/me/wechat-claw/${action}`, { method: "POST" });
      if (action === "poll") setResult(response);
      await setup.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="我的微信 claw">
      <div className="integration wechat-config">
        <div className={`notice ${setup.data?.connected ? "success" : "info"}`}>
          <strong>{setup.data?.connected ? "已连接" : "等待绑定"}</strong>
          <span>此二维码只绑定当前登录账号。你的微信消息、会话 token 和自动重试队列不会与其他成员共享。</span>
        </div>
        <div className="wechat-setup-card">
          <div className="wechat-setup-main">
            {qrcode.qrcode_url ? <WechatClawQrCode value={String(qrcode.qrcode_url)} /> : <div className="wechat-qr-placeholder"><Lock size={24} /><span>点击刷新二维码后扫码绑定</span></div>}
            <div className="actions compact-actions">
              <button type="button" onClick={() => call("refresh")} disabled={busy !== "" || !setup.data?.enabled}>{busy === "refresh" ? "刷新中..." : "刷新二维码"}</button>
              <button type="button" onClick={() => call("poll")} disabled={busy !== "" || !setup.data?.connected}>{busy === "poll" ? "检查中..." : "立即检查"}</button>
              <button type="button" onClick={() => call("logout")} disabled={busy !== "" || !setup.data?.connected}>退出登录</button>
            </div>
          </div>
          <div className="wechat-setup-side">
            <div className="field-help compact-help"><strong>绑定状态</strong><span>账号：<code>{setup.data?.account_id || "-"}</code></span><span>二维码：<code>{qrcode.status || "waiting"}</code></span></div>
            <div className={lastPoll.success ? "field-help compact-help success" : "field-help compact-help"}>
              <strong>最近处理</strong><span>原始 {lastPoll.raw_count ?? 0} / 解析 {lastPoll.parsed_count ?? 0} / 回发 {lastPoll.reply_sent_count ?? 0} / 待重试 {lastPoll.pending_count ?? 0}</span>{lastPoll.message && <span>{lastPoll.message}</span>}
            </div>
          </div>
        </div>
        {error && <p className="error">{error}</p>}
      </div>
    </Panel>
  );
}

function AdminUserManagementCard() {
  const members = useLoad<any>(() => api("/api/admin/users"), []);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function createMember() {
    setSaving(true);
    setError("");
    try {
      await api("/api/admin/users", { method: "POST", body: JSON.stringify({ username, password }) });
      setUsername("");
      setPassword("");
      await members.reload();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return <Panel title="成员账号"><div className="integration"><div className="settings-grid"><label>用户名<input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="成员用户名" /></label><label>初始密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 8 位" /></label></div><div className="actions"><button type="button" className="primary" disabled={saving || username.trim().length < 3 || password.length < 8} onClick={createMember}>{saving ? "创建中..." : "创建成员"}</button></div><div className="field-help"><strong>已有成员</strong><span>{Array.isArray(members.data?.items) ? members.data.items.map((item: any) => `${item.username}（${item.role}）`).join(" / ") || "暂无" : "加载中..."}</span></div>{error && <p className="error">{error}</p>}</div></Panel>;
}

function SettingsPage({ user }: { user: User }) {
  const { data, reload } = useLoad<any>(() => user.role === "admin" ? api("/api/admin/integrations") : Promise.resolve(null), [user.role]);
  const storage = useLoad<any>(() => user.role === "admin" ? api("/api/admin/storage/status") : Promise.resolve(null), [user.role]);
  const [activeStep, setActiveStep] = useState(() => {
    const saved = window.sessionStorage.getItem("ptmh.settings.activeStep") || "storage";
    return ["qb1", "qb2", "qb3"].includes(saved) ? "downloaders" : saved;
  });
  if (user.role !== "admin") return <div className="grid-page"><PersonalWechatClawCard /></div>;
  if (!data) return <Panel title="设置"><p>正在加载...</p></Panel>;

  const providers = data.providers.filter((provider: any) => ["mteam", "qb1", "qb2", "qb3", "tmdb", "ai", "wechat_claw"].includes(provider.provider));
  const providerById = Object.fromEntries(providers.map((provider: any) => [provider.provider, provider]));
  const steps = [
    { id: "storage", label: "NAS 存储", ready: Boolean(storage.data?.nas_storage_readable) },
    { id: "mteam", label: "M-Team", ready: providerStepReady(providerById.mteam) },
    { id: "downloaders", label: "下载器", ready: ["qb1", "qb2", "qb3"].every((id) => providerStepReady(providerById[id])) },
    { id: "tmdb", label: "TMDB", ready: providerStepReady(providerById.tmdb) },
    { id: "ai", label: "AI", ready: providerStepReady(providerById.ai) },
    { id: "wechat_claw", label: "WeChat claw", ready: providerStepReady(providerById.wechat_claw) },
  ];
  const activeProvider = providerById[activeStep];

  function selectStep(id: string) {
    setActiveStep(id);
    window.sessionStorage.setItem("ptmh.settings.activeStep", id);
    window.requestAnimationFrame(() => document.getElementById("settings-active-card")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  return (
    <div className="grid-page settings-flow">
      <Panel title="配置向导">
        <div className="settings-stepper" aria-label="配置步骤">
          {steps.map((step, index) => <button type="button" key={step.id} className={activeStep === step.id ? "settings-step active" : "settings-step"} onClick={() => selectStep(step.id)}>
            <span className={step.ready ? "settings-step-dot ready" : "settings-step-dot pending"}>{index + 1}</span>
            <span>{step.label}</span>
          </button>)}
        </div>
        <p className="muted">按顺序完成连接测试；绿点表示配置已验证，红点表示尚未配置或测试未通过。点击任一步即可切换到对应配置。</p>
      </Panel>
      <div className="settings-active-card" id="settings-active-card" key={activeStep}>
        {activeStep === "storage" ? <StorageSettingsCard status={storage.data} loading={storage.loading} error={storage.error} /> : activeStep === "downloaders" ? <DownloadersSettingsCard providers={[providerById.qb1, providerById.qb2, providerById.qb3]} onChanged={reload} /> : activeProvider ? <IntegrationEditor provider={activeProvider} onChanged={reload} /> : <Panel title="配置"><p className="muted">该配置项不可用。</p></Panel>}
      </div>
    </div>
  );
}

function providerStepReady(provider: any): boolean {
  const result = provider?.last_test_result;
  return Boolean(provider?.enabled && result?.success === true && result?.can_enable !== false);
}

function DownloadersSettingsCard({ providers, onChanged }: { providers: any[]; onChanged: () => void }) {
  return (
    <div className="downloaders-settings-grid">
      {providers.filter(Boolean).map((provider) => <IntegrationEditor key={provider.provider} provider={provider} onChanged={onChanged} />)}
    </div>
  );
}

function StorageSettingsCard({ status, loading, error }: { status: any; loading: boolean; error: string }) {
  const detectedPaths = Array.isArray(status?.nas_storage_detected_paths) ? status.nas_storage_detected_paths : [];
  const errors = Array.isArray(status?.nas_storage_errors) ? status.nas_storage_errors : [];
  return (
    <Panel title="存储空间">
      <div className="integration">
        <div className={`notice ${status?.nas_storage_readable ? "success" : "info"}`}>
          <strong>{loading ? "正在检测存储挂载..." : status?.nas_storage_summary_label || "未检测到存储挂载"}</strong>
          <span>填写 NAS 文件夹路径后，即可在仪表盘查看空间使用情况。</span>
        </div>
        {error && <p className="error">{error}</p>}
        {detectedPaths.length > 0 ? (
          <div className="field-help">
            <strong>已检测路径：</strong>
            <span className="storage-path-list">{detectedPaths.map((path: string) => <code key={path}>{path}</code>)}</span>
          </div>
        ) : (
          <div className="field-help">
            <strong>未检测到挂载时：</strong>
            <span>请重新部署容器，并把 docker-compose.yml 里 storage volumes 的左侧替换成 NAS 文件夹路径，例如 /volume1/qb1-downloads:/mnt/storage1:ro。</span>
          </div>
        )}
        {errors.length > 0 && (
          <div className="field-help">
            <strong>检测提示：</strong>
            <span>{errors.slice(0, 3).map((item: any) => `${item.path}: ${item.message}`).join("；")}</span>
          </div>
        )}
      </div>
    </Panel>
  );
}

function IntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  if (provider.provider === "mteam") return <MTeamIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (provider.provider === "tmdb") return <TmdbIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (provider.provider === "ai") return <AiIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (provider.provider === "wechat_claw") return <WechatClawIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (["qb1", "qb2", "qb3"].includes(provider.provider)) return <QbIntegrationEditor provider={provider} onChanged={onChanged} />;

  const providerNames: Record<string, string> = { ai: "AI" };
  const [text, setText] = useState(String(provider.saved_payload?.endpoint ?? ""));
  const payload = useMemo(() => ({ endpoint: text || "mock://service", timeout: 10 }), [text]);

  async function save(path = "") {
    const body = path === "/enable" || path === "/disable" ? undefined : JSON.stringify({ payload });
    await api(`/api/admin/integrations/${provider.provider}${path}`, { method: "POST", body });
    onChanged();
  }

  async function draft() {
    await api(`/api/admin/integrations/${provider.provider}`, { method: "PUT", body: JSON.stringify({ payload }) });
    onChanged();
  }

  return (
    <Panel title={`${providerNames[provider.provider] ?? provider.provider} 配置`}>
      <div className="integration">
        <CopyableTextarea value={text} onChange={setText} placeholder="粘贴接口地址、请求头、密钥或 webhook 配置" />
        <div className="actions">
          <button onClick={draft}>保存草稿</button>
          <button onClick={() => save("/test")}>保存并测试</button>
          <button onClick={() => save(provider.enabled ? "/disable" : "/enable")}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
      </div>
    </Panel>
  );
}

function AiIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const [form, setForm] = useState<AiForm>({
    base_url: String(saved.base_url ?? "https://api.deepseek.com"),
    api_key: String(saved.api_key ?? ""),
    model: String(saved.model ?? "deepseek-v4-flash"),
    timeout: String(saved.timeout ?? "90"),
    max_tokens: String(saved.max_tokens ?? "1200"),
    temperature: String(saved.temperature ?? "0.1"),
    thinking: saved.thinking === "enabled" ? "enabled" : "disabled",
    reasoning_effort: saved.reasoning_effort === "max" ? "max" : "high",
  });
  const [busy, setBusy] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [localError, setLocalError] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "ai" && result?.mode === "real" && result?.success === true;

  function updateField<K extends keyof AiForm>(key: K, value: AiForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    return {
      base_url: form.base_url.trim() || "https://api.deepseek.com",
      api_key: form.api_key.trim(),
      model: form.model.trim() || "deepseek-v4-flash",
      timeout: Math.max(90, Number(form.timeout) || 90),
      max_tokens: Number(form.max_tokens) || 1200,
      temperature: Number(form.temperature) || 0.1,
      thinking: form.thinking,
      reasoning_effort: form.reasoning_effort,
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/ai`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: "ai",
        mode: "real",
        message: "AI 助手设置已保存。",
        explanation: "请完成连接测试后再启用。",
        next_step: "点击“保存并测试”。",
      });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>(`/api/admin/integrations/ai/test`, { method: "POST", body: JSON.stringify({ payload: payload() }) });
      setLocalResult(updated.last_test_result);
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/ai${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="AI 助手">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>影视中枢助手</strong>
          <span>配置后，可在通知页和微信中用自然语言查询影视、资源与下载状态。</span>
        </div>
        <div className="settings-grid">
          <label>API Key
            <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="从 platform.deepseek.com 创建并复制" autoComplete="off" />
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
        </button>
        {showAdvanced && <div className="settings-grid advanced-settings">
          <label>服务地址
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="https://api.deepseek.com" inputMode="url" />
          </label>
          <label>模型名称
            <CopyableInput value={form.model} onChange={(value) => updateField("model", value)} placeholder="deepseek-v4-flash" />
          </label>
          <label>响应等待时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="90" />
          </label>
          <label>最大回复长度
            <CopyableInput value={form.max_tokens} onChange={(value) => updateField("max_tokens", value)} inputMode="numeric" placeholder="1200" />
          </label>
          <label>回复随机度
            <CopyableInput value={form.temperature} onChange={(value) => updateField("temperature", value)} inputMode="decimal" placeholder="0.1" />
          </label>
          <label>深度思考
            <select value={form.thinking} onChange={(event) => updateField("thinking", event.target.value as AiForm["thinking"])}>
              <option value="disabled">关闭</option>
              <option value="enabled">开启</option>
            </select>
          </label>
          <label>思考强度
            <select value={form.reasoning_effort} onChange={(event) => updateField("reasoning_effort", event.target.value as AiForm["reasoning_effort"])}>
              <option value="high">标准</option>
              <option value="max">更高</option>
            </select>
          </label>
        </div>}
        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先保存并测试，测试成功后再启用 AI 模块。</p>}
        {localError && <p className="error">{localError}</p>}
        <TestResultCard result={result} emptyProvider="AI 助手" />
      </div>
    </Panel>
  );
}

function WechatClawQrCode({ value }: { value: string }) {
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    if (!value.trim()) {
      setSrc("");
      setError("");
      return () => {
        alive = false;
      };
    }
    QRCode.toDataURL(value, { margin: 1, width: 220, errorCorrectionLevel: "M" })
      .then((dataUrl) => {
        if (!alive) return;
        setSrc(dataUrl);
        setError("");
      })
      .catch((err) => {
        if (!alive) return;
        setSrc("");
        setError((err as Error).message);
      });
    return () => {
      alive = false;
    };
  }, [value]);

  if (value.trim().toLowerCase().startsWith("data:image/")) {
    return <img className="wechat-qr-image" src={value} alt="微信登录二维码" />;
  }
  if (src) return <img className="wechat-qr-image" src={src} alt="微信绑定二维码" />;
  return (
    <div className="wechat-qr-placeholder">
      <RefreshCw size={24} />
      <span>{error || "正在生成二维码..."}</span>
    </div>
  );
}

function WechatClawIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const [form, setForm] = useState<WechatClawForm>({
    mode: saved.mode === "direct" ? "direct" : "ilink",
    name: String(saved.name ?? "通知1"),
    base_url: String(saved.base_url ?? "https://ilinkai.weixin.qq.com"),
    default_target: String(saved.default_target ?? ""),
    admin_user_ids: String(saved.admin_user_ids ?? ""),
    poll_timeout: String(saved.poll_timeout ?? saved.timeout ?? "25"),
    public_base_url: String(saved.public_base_url ?? ""),
    inbound_token: String(saved.inbound_token ?? ""),
    webhook_url: String(saved.webhook_url ?? ""),
    webhook_secret: String(saved.webhook_secret ?? ""),
    default_downloader_id: ["qb1", "qb2", "qb3", "all"].includes(saved.default_downloader_id) ? saved.default_downloader_id : "all",
    timeout: String(saved.timeout ?? "10"),
  });
  const bindings = useLoad<any>(() => api("/api/admin/wechat-claw/bindings"), [provider.config_version], null, false);
  const [selectedBindingId, setSelectedBindingId] = useState<number | null>(() => {
    const savedBinding = Number(window.sessionStorage.getItem("ptmh.wechat.selectedMember"));
    return Number.isFinite(savedBinding) && savedBinding > 0 ? savedBinding : null;
  });
  const setup = useLoad<any>(
    () => selectedBindingId ? api(`/api/admin/wechat-claw/bindings/${selectedBindingId}/setup`) : Promise.resolve(null),
    [provider.config_version, provider.enabled, selectedBindingId],
    null,
    false,
  );
  const [busy, setBusy] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [localError, setLocalError] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "wechat_claw" && result?.success === true && result?.can_enable === true;

  useEffect(() => {
    const current = provider.saved_payload ?? {};
    setForm({
      mode: current.mode === "direct" ? "direct" : "ilink",
      name: String(current.name ?? "通知1"),
      base_url: String(current.base_url ?? "https://ilinkai.weixin.qq.com"),
      default_target: String(current.default_target ?? ""),
      admin_user_ids: String(current.admin_user_ids ?? ""),
      poll_timeout: String(current.poll_timeout ?? current.timeout ?? "25"),
      public_base_url: String(current.public_base_url ?? ""),
      inbound_token: String(current.inbound_token ?? ""),
      webhook_url: String(current.webhook_url ?? ""),
      webhook_secret: String(current.webhook_secret ?? ""),
      default_downloader_id: ["qb1", "qb2", "qb3", "all"].includes(current.default_downloader_id) ? current.default_downloader_id : "all",
      timeout: String(current.timeout ?? "10"),
    });
    setLocalResult(provider.last_test_result);
  }, [provider.config_version, provider.last_test_result]);

  useEffect(() => {
    const items = Array.isArray(bindings.data?.items) ? bindings.data.items : [];
    if (!items.length) {
      setSelectedBindingId(null);
      return;
    }
    if (!items.some((item: any) => item.id === selectedBindingId)) setSelectedBindingId(items[0].id);
  }, [bindings.data, selectedBindingId]);

  function updateField<K extends keyof WechatClawForm>(key: K, value: WechatClawForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    return {
      mode: "ilink",
      name: form.name.trim() || "通知1",
      base_url: form.base_url.trim() || "https://ilinkai.weixin.qq.com",
      default_target: form.default_target.trim(),
      admin_user_ids: form.admin_user_ids.trim(),
      poll_timeout: Number(form.poll_timeout) || 25,
      public_base_url: form.public_base_url.trim(),
      inbound_token: form.inbound_token.trim(),
      webhook_url: form.webhook_url.trim(),
      webhook_secret: form.webhook_secret.trim(),
      default_downloader_id: form.default_downloader_id,
      timeout: Number(form.timeout) || 10,
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api("/api/admin/integrations/wechat_claw", { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: "wechat_claw",
        mode: "real",
        message: "微信连接设置已保存。",
        explanation: "请完成连接测试后再启用。",
        next_step: "测试并启用后，刷新二维码并用微信扫码绑定。",
      });
      onChanged();
      await setup.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>("/api/admin/integrations/wechat_claw/test", { method: "POST", body: JSON.stringify({ payload: payload() }) });
      setLocalResult(updated.last_test_result);
      onChanged();
      await setup.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/wechat_claw${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
      await setup.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  function generateToken() {
    const bytes = new Uint8Array(24);
    window.crypto?.getRandomValues(bytes);
    const token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    updateField("inbound_token", token || Math.random().toString(36).slice(2));
  }

  const setupPayload = setup.data?.qr_payload ?? {};
  const qrText = String(setup.data?.qr_text || "");
  const qrcode = setup.data?.qrcode ?? {};
  const qrReady = Boolean(qrcode.qrcode_url || setupPayload.qrcode_url);
  const loginStatus = setup.data?.connected ? "已登录" : provider.enabled ? "等待扫码" : "未启用";
  const selectedBinding = (bindings.data?.items ?? []).find((item: any) => item.id === selectedBindingId);

  async function refreshQrcode() {
    setBusy("refresh");
    setLocalError("");
    try {
      if (!selectedBindingId) return;
      await api(`/api/admin/wechat-claw/bindings/${selectedBindingId}/refresh`, { method: "POST" });
      await setup.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function logoutWechat() {
    setBusy("logout");
    setLocalError("");
    try {
      if (!selectedBindingId) return;
      await api(`/api/admin/wechat-claw/bindings/${selectedBindingId}/logout`, { method: "POST" });
      await setup.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function addBinding() {
    setBusy("add");
    setLocalError("");
    try {
      const binding = await api<any>("/api/admin/wechat-claw/bindings", {
        method: "POST",
        body: JSON.stringify({}),
      });
      await bindings.reload();
      setSelectedBindingId(binding.id);
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function removeBinding() {
    if (!selectedBindingId || !window.confirm("删除这个微信绑定吗？它的登录态和互动记录也会删除。")) return;
    setBusy("remove");
    setLocalError("");
    try {
      await api(`/api/admin/wechat-claw/bindings/${selectedBindingId}`, { method: "DELETE" });
      setSelectedBindingId(null);
      await bindings.reload();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="微信连接">
      <div className="integration wechat-config">
        <div className={`notice ${setup.data?.connected ? "success" : "info"}`}>
          <strong>{setup.data?.connected ? "配置可用" : "扫码绑定微信"}</strong>
          <span>{setup.data?.connected ? `${selectedBinding?.display_name || "当前成员"} 已完成扫码绑定，可以接收主动通知。` : "绑定后，可在手机上查询影视、搜索资源、发起下载并接收通知。"}</span>
        </div>

        <div className="wechat-binding-tabs" aria-label="WeChat claw 成员">
          <label>当前成员
            <select value={selectedBindingId ?? ""} onChange={(event) => setSelectedBindingId(Number(event.target.value) || null)} disabled={!bindings.data?.items?.length}>
              {(bindings.data?.items ?? []).map((binding: any) => <option key={binding.id} value={binding.id}>{binding.display_name}</option>)}
            </select>
          </label>
          <button type="button" className="primary" onClick={addBinding} disabled={busy !== ""}><Plus size={16} /> 添加成员</button>
        </div>
        {!selectedBindingId && <p className="muted">先添加一个微信成员，再生成二维码完成绑定。</p>}

        <div className="settings-grid">
          <label>默认下载器
            <select value={form.default_downloader_id} onChange={(event) => updateField("default_downloader_id", event.target.value as WechatClawForm["default_downloader_id"])}>
              <option value="all">自动选择</option>
              <option value="qb1">qB1</option>
              <option value="qb2">qB2</option>
              <option value="qb3">qB3</option>
            </select>
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
        </button>
        {showAdvanced && <div className="settings-grid advanced-settings">
          <label>微信服务地址
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="https://ilinkai.weixin.qq.com" inputMode="url" />
          </label>
          <label>默认通知对象
            <CopyableInput value={form.default_target} onChange={(value) => updateField("default_target", value)} placeholder="留空时发送给最近互动的成员" />
          </label>
          <label>轮询超时（秒）
            <CopyableInput value={form.poll_timeout} onChange={(value) => updateField("poll_timeout", value)} inputMode="numeric" placeholder="25" />
          </label>
        </div>}

        {selectedBindingId && <div className="wechat-setup-card">
          <div className="wechat-setup-main">
            <div className="wechat-status-line">
              <span>配置状态</span>
              <strong>{loginStatus}</strong>
            </div>
            {qrReady ? <WechatClawQrCode value={qrText} /> : (
              <div className="wechat-qr-placeholder">
                <Lock size={24} />
                <span>保存并测试后点击刷新二维码</span>
              </div>
            )}
            <div className="actions compact-actions">
              <button type="button" onClick={refreshQrcode} disabled={busy !== "" || !provider.enabled}>{busy === "refresh" ? "刷新中..." : "刷新二维码"}</button>
              <button type="button" onClick={logoutWechat} disabled={busy !== "" || !setup.data?.connected}>{busy === "logout" ? "退出中..." : "退出登录"}</button>
            </div>
          </div>

          <div className="wechat-setup-side">
            <div className="field-help compact-help">
              <strong>微信连接状态</strong>
              <span>账号：{setup.data?.account_id || "等待扫码"}</span>
              <span>二维码：{qrcode.status === "confirmed" ? "已确认" : qrcode.status === "expired" ? "已过期" : "等待扫码"}</span>
            </div>
          </div>
        </div>}

        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先保存并测试，测试成功后再启用微信连接。</p>}
        {setup.error && <p className="error">{setup.error}</p>}
        {localError && <p className="error">{localError}</p>}
      </div>
    </Panel>
  );
}

function LegacyWechatClawIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const [form, setForm] = useState<WechatClawForm>({
    mode: saved.mode === "direct" ? "direct" : "ilink",
    name: String(saved.name ?? "通知1"),
    base_url: String(saved.base_url ?? "https://ilinkai.weixin.qq.com"),
    default_target: String(saved.default_target ?? ""),
    admin_user_ids: String(saved.admin_user_ids ?? ""),
    poll_timeout: String(saved.poll_timeout ?? saved.timeout ?? "25"),
    public_base_url: String(saved.public_base_url ?? ""),
    inbound_token: String(saved.inbound_token ?? ""),
    webhook_url: String(saved.webhook_url ?? ""),
    webhook_secret: String(saved.webhook_secret ?? ""),
    default_downloader_id: ["qb1", "qb2", "qb3", "all"].includes(saved.default_downloader_id) ? saved.default_downloader_id : "all",
    timeout: String(saved.timeout ?? "10"),
  });
  const [busy, setBusy] = useState("");
  const [localError, setLocalError] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "wechat_claw" && result?.success === true && result?.can_enable === true;

  function updateField<K extends keyof WechatClawForm>(key: K, value: WechatClawForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    return {
      public_base_url: form.public_base_url.trim(),
      inbound_token: form.inbound_token.trim(),
      webhook_url: form.webhook_url.trim(),
      webhook_secret: form.webhook_secret.trim(),
      default_downloader_id: form.default_downloader_id,
      timeout: Number(form.timeout) || 10,
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/wechat_claw`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: "wechat_claw",
        mode: "real",
        message: "WeChat claw 草稿已保存。",
        explanation: "入口密钥和 webhook secret 已加密保存。启用前请先测试。",
        next_step: "确认公网或局域网手机可访问地址后，点击保存并测试。",
      });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>(`/api/admin/integrations/wechat_claw/test`, { method: "POST", body: JSON.stringify({ payload: payload() }) });
      setLocalResult(updated.last_test_result);
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/wechat_claw${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  const publicBaseUrl = form.public_base_url.trim().replace(/\/+$/, "");
  const endpoint = publicBaseUrl ? `${publicBaseUrl}/api/wechat-claw/message` : "/api/wechat-claw/message";
  const capabilitiesEndpoint = publicBaseUrl ? `${publicBaseUrl}/api/wechat-claw/capabilities` : "/api/wechat-claw/capabilities";

  return (
    <Panel title="WeChat claw 配置">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>手机端 AI 对话与通知入口</strong>
          <span>WeChat claw 调用后端入口后，会复用 DeepSeek 配置完成自然语言资源查询、下载通知记录和各板块状态查询。公开访问地址可以是内网穿透、反向代理或手机能访问的局域网地址。</span>
        </div>
        <div className="settings-grid">
          <label>APP 公开访问地址
            <CopyableInput value={form.public_base_url} onChange={(value) => updateField("public_base_url", value)} placeholder="例如 https://media.example.com 或 http://192.168.1.20:8000" inputMode="url" />
          </label>
          <label>Inbound token
            <SecretInput value={form.inbound_token} onChange={(value) => updateField("inbound_token", value)} placeholder="手机端调用 /api/wechat-claw/message 时携带" autoComplete="off" />
          </label>
          <label>Webhook URL（可选）
            <CopyableInput value={form.webhook_url} onChange={(value) => updateField("webhook_url", value)} placeholder="WeChat claw 接收主动通知的 webhook" inputMode="url" />
          </label>
          <label>Webhook secret（可选）
            <SecretInput value={form.webhook_secret} onChange={(value) => updateField("webhook_secret", value)} placeholder="作为 X-Wechat-Claw-Secret 发送" autoComplete="off" />
          </label>
          <label>默认下载器
            <select value={form.default_downloader_id} onChange={(event) => updateField("default_downloader_id", event.target.value as WechatClawForm["default_downloader_id"])}>
              <option value="all">自动 / 全部</option>
              <option value="qb1">qB1</option>
              <option value="qb2">qB2</option>
              <option value="qb3">qB3</option>
            </select>
          </label>
          <label>超时秒数
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="10" />
          </label>
        </div>
        <div className="field-help">
          <strong>手机端请求地址</strong>
          <span><code>{endpoint}</code></span>
          <span>Capabilities: <code>{capabilitiesEndpoint}</code></span>
          <span>请求头携带 <code>X-Wechat-Claw-Token</code>，请求体使用 <code>{`{"message":"帮我查一下 qB 状态"}`}</code>。</span>
        </div>
        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先保存并测试，测试成功后再启用 WeChat claw。</p>}
        {localError && <p className="error">{localError}</p>}
        <TestResultCard result={result} emptyProvider="WeChat claw" />
      </div>
    </Panel>
  );
}

function QbIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const label = downloaderShortLabel(provider.provider);
  const saved = provider.saved_payload ?? {};
  const [form, setForm] = useState<QbForm>({
    base_url: String(saved.base_url ?? ""),
    username: String(saved.username ?? ""),
    password: String(saved.password ?? ""),
    timeout: String(saved.timeout ?? "10"),
  });
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState("");
  const [localError, setLocalError] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.success === true;

  function updateField(key: keyof QbForm, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    return {
      base_url: form.base_url.trim(),
      username: form.username.trim(),
      password: form.password,
      timeout: Number(form.timeout) || 10,
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/${provider.provider}`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: provider.provider,
        mode: "real",
        message: "草稿已保存。",
        explanation: "连接信息已保存。",
        next_step: "点击“保存并测试”确认连接。"
      });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>(`/api/admin/integrations/${provider.provider}/test`, { method: "POST", body: JSON.stringify({ payload: payload() }) });
      setLocalResult(updated.last_test_result);
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/${provider.provider}${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title={`${label} 配置`}>
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>连接下载器</strong>
          <span>连接后可查看下载任务与状态，并作为默认下载器使用。</span>
        </div>
        <div className="settings-grid">
          <label>qB WebUI 地址（必填）
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="例如 http://192.168.1.20:8080" />
          </label>
          <label>用户名（必填）
            <CopyableInput value={form.username} onChange={(value) => updateField("username", value)} placeholder="qB WebUI 用户名" autoComplete="off" />
          </label>
          <label>密码（必填）
            <SecretInput value={form.password} onChange={(value) => updateField("password", value)} placeholder="qB WebUI 密码" autoComplete="new-password" />
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
        </button>
        {showAdvanced && <div className="settings-grid advanced-settings">
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="10" />
          </label>
        </div>}
        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先“保存并测试”，测试成功后再启用这个 qB 下载器。</p>}
        {localError && <p className="error">{localError}</p>}
        <TestResultCard result={result} emptyProvider={label} />
      </div>
    </Panel>
  );
}

function MTeamIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const savedHeaders = saved.headers ?? {};
  const [form, setForm] = useState<MTeamForm>({
    base_url: String(saved.base_url ?? "https://kp.m-team.cc"),
    api_key: String(saved.api_key ?? ""),
    cookie: String(savedHeaders.Cookie ?? ""),
    user_agent: String(savedHeaders["User-Agent"] ?? "PT-Media-Hub"),
    authorization: String(savedHeaders.Authorization ?? ""),
    passkey: String(saved.passkey ?? ""),
    timeout: String(saved.timeout ?? "10")
  });
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const [localError, setLocalError] = useState("");
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "mteam" && result?.success === true;

  function updateField(key: keyof MTeamForm, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    const headers: Record<string, string> = {};
    if (form.cookie.trim()) headers.Cookie = form.cookie.trim();
    if (form.user_agent.trim()) headers["User-Agent"] = form.user_agent.trim();
    if (form.authorization.trim()) headers.Authorization = form.authorization.trim();
    return {
      base_url: form.base_url.trim() || "https://kp.m-team.cc",
      api_key: form.api_key.trim(),
      headers,
      passkey: form.passkey.trim(),
      timeout: Number(form.timeout) || 10
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/mteam`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: "mteam",
        mode: "mock",
        message: "草稿已保存。",
        explanation: "连接信息已保存。",
        next_step: "点击“保存并测试”确认连接。"
      });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>(`/api/admin/integrations/mteam/test`, { method: "POST", body: JSON.stringify({ payload: payload() }) });
      setLocalResult(updated.last_test_result);
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/mteam${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="M-Team 配置">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>连接 M-Team</strong>
          <span>配置后可查询站内资源、查看账号状态并从手机端发起下载。</span>
        </div>
        <div className="settings-grid">
          <label>API Key
            <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="从 M-Team 个人资料复制 API Key" autoComplete="off" />
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
        </button>
        {showAdvanced && <div className="settings-grid advanced-settings">
          <label>站点地址
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="https://kp.m-team.cc" />
          </label>
          <label>User-Agent
            <CopyableInput value={form.user_agent} onChange={(value) => updateField("user_agent", value)} placeholder="浏览器或 PT-Media-Hub" />
          </label>
          <label>Cookie
            <SecretInput value={form.cookie} onChange={(value) => updateField("cookie", value)} placeholder="从浏览器复制完整 Cookie" autoComplete="off" />
          </label>
          <label>Passkey
            <SecretInput value={form.passkey} onChange={(value) => updateField("passkey", value)} placeholder="可选，下载链接使用；不是 API Key" autoComplete="off" />
          </label>
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="10" />
          </label>
          <label>Authorization
            <SecretInput value={form.authorization} onChange={(value) => updateField("authorization", value)} placeholder="可选，例如 Bearer token" autoComplete="off" />
          </label>
        </div>}
        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先“保存并测试”，测试成功后再启用 M-Team。</p>}
        {localError && <p className="error">{localError}</p>}
        <TestResultCard result={result} emptyProvider="M-Team" />
      </div>
    </Panel>
  );
}

function TmdbIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const initialMode = ["direct", "proxy"].includes(String(saved.mode ?? "")) ? String(saved.mode) : "direct";
  const [form, setForm] = useState<TmdbForm>({
    mode: initialMode,
    api_key: String(saved.api_key ?? ""),
    bearer_token: String(saved.bearer_token ?? ""),
    proxy_url: String(saved.proxy_url ?? "http://mihomo:7890"),
    language: String(saved.language ?? "zh-CN"),
    region: String(saved.region ?? "CN"),
    timeout: String(saved.timeout ?? "12"),
    endpoint: String(saved.endpoint ?? "")
  });
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [busy, setBusy] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const [localError, setLocalError] = useState("");
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "tmdb" && result?.mode === "real" && result?.can_enable === true;

  function updateField(key: keyof TmdbForm, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function payload() {
    return {
      mode: form.mode,
      api_key: form.api_key.trim(),
      bearer_token: form.bearer_token.trim(),
      proxy_url: form.proxy_url.trim() || "http://mihomo:7890",
      language: form.language.trim() || "zh-CN",
      region: form.region.trim() || "CN",
      timeout: Number(form.timeout) || 12,
      endpoint: form.endpoint.trim()
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/tmdb`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        provider: "tmdb",
        mode: "real",
        can_enable: false,
        message: "草稿已保存。",
        explanation: "密钥已保存。",
        next_step: "点击“保存并测试”，确认连接后再启用 TMDB。",
      });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setLocalError("");
    try {
      const updated = await api<any>(`/api/admin/integrations/tmdb/test`, { method: "POST", body: JSON.stringify({ payload: payload() }) });
      const testResult = updated.last_test_result as IntegrationTestResult;
      setLocalResult(testResult);
      onChanged();
    } catch (err) {
      const message = (err as Error).message;
      setLocalError(message);
    } finally {
      setBusy("");
    }
  }

  async function toggleEnabled() {
    setBusy("enable");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/tmdb${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  const isProxyMode = form.mode === "proxy";

  return (
    <Panel title="TMDB 配置">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>影视资料库</strong>
          <span>配置后，可在发现页和 AI 助手中查询电影、剧集和演员信息。</span>
        </div>
        <div className="settings-grid">
          <label>TMDB Bearer Token
            <SecretInput value={form.bearer_token} onChange={(value) => updateField("bearer_token", value)} placeholder="从 TMDB 账号设置中复制" autoComplete="off" />
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
        </button>
        {showAdvanced && <div className="integration advanced-settings">
        <div className="tmdb-mode-row">
          <span>连接方式</span>
          <div className="segmented compact">
            <button type="button" className={!isProxyMode ? "active" : ""} onClick={() => updateField("mode", "direct")}>
              <Radar size={16} /> 默认
            </button>
            <button type="button" className={isProxyMode ? "active" : ""} onClick={() => updateField("mode", "proxy")}>
              <ShieldCheck size={16} /> 使用代理
            </button>
          </div>
        </div>
        <div className="settings-grid">
          {isProxyMode && <label>代理地址
            <CopyableInput value={form.proxy_url} onChange={(value) => updateField("proxy_url", value)} placeholder="http://mihomo:7890" inputMode="url" />
          </label>}
          <label>语言
            <CopyableInput value={form.language} onChange={(value) => updateField("language", value)} placeholder="zh-CN" />
          </label>
          <label>地区
            <CopyableInput value={form.region} onChange={(value) => updateField("region", value)} placeholder="CN" />
          </label>
          <label>API Key
            <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="仅在无法使用 Bearer Token 时填写" autoComplete="off" />
          </label>
          <label>服务地址
            <CopyableInput value={form.endpoint} onChange={(value) => updateField("endpoint", value)} placeholder="通常无需修改" />
          </label>
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="12" />
          </label>
        </div>
        </div>}
        <div className="actions">
          <button onClick={saveDraft} disabled={busy !== ""}>{busy === "draft" ? "正在保存..." : "保存草稿"}</button>
          <button className="primary" onClick={saveAndTest} disabled={busy !== ""}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
          <button onClick={toggleEnabled} disabled={busy !== "" || (!provider.enabled && !canEnable)}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        {!provider.enabled && !canEnable && <p className="muted">请先“保存并测试”，测试成功后才能启用 TMDB。</p>}
        {localError && <p className="error">{localError}</p>}
        <TestResultCard result={result} emptyProvider="TMDB" />
      </div>
    </Panel>
  );
}

function TestResultCard({ result, emptyProvider }: { result?: IntegrationTestResult | null; emptyProvider: string }) {
  if (!result) {
    return (
      <div className="result-card neutral">
        <strong>还没有测试结果</strong>
        <span>填写 {emptyProvider} 配置后，点击“保存并测试”。</span>
      </div>
    );
  }

  return (
    <div className={result.success ? "result-card success" : "result-card failed"}>
      <strong>{result.message ?? (result.success ? "测试成功" : "测试失败")}</strong>
      {result.explanation && <span>{result.explanation}</span>}
      {result.next_step && <span>下一步：{result.next_step}</span>}
      {result.trace_id && <small>诊断编号：{result.trace_id}</small>}
    </div>
  );
}

function CopyableInput(props: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  inputMode?: "text" | "numeric" | "decimal" | "tel" | "search" | "email" | "url";
  autoComplete?: string;
}) {
  return (
    <div className="input-with-tools">
      <input value={props.value} onChange={(event) => props.onChange(event.target.value)} placeholder={props.placeholder} inputMode={props.inputMode} autoComplete={props.autoComplete} />
      <div className="input-tools">
        <CopyButton text={props.value} label="复制" iconOnly />
      </div>
    </div>
  );
}

function SecretInput(props: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
}) {
  const [visible, setVisible] = useState(false);
  const displayed = visible || !props.value ? props.value : "*".repeat(Math.min(Math.max(props.value.length, 8), 48));
  return (
    <div className="input-with-tools sensitive">
      <input
        value={displayed}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder={props.placeholder}
        autoComplete={props.autoComplete}
        readOnly={!visible && Boolean(props.value)}
      />
      <div className="input-tools">
        <button className="icon-tool" type="button" onClick={() => setVisible((current) => !current)} title={visible ? "隐藏明文" : "显示明文"} aria-label={visible ? "隐藏明文" : "显示明文"}>
          {visible ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
        <CopyButton text={props.value} label="复制" iconOnly />
      </div>
    </div>
  );
}

function CopyableTextarea({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder?: string }) {
  return (
    <div className="input-with-tools textarea-tools">
      <textarea value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
      <div className="input-tools">
        <CopyButton text={value} label="复制" iconOnly />
      </div>
    </div>
  );
}

function CopyButton({ text, label, compact = false, iconOnly = false }: { text: string; label: string; compact?: boolean; iconOnly?: boolean }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await copyToClipboard(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  return (
    <button className={iconOnly ? "copy-button icon-only" : compact ? "copy-button compact" : "copy-button"} type="button" onClick={copy} title={copied ? "已复制" : label} aria-label={copied ? "已复制" : label}>
      {copied ? <Check size={15} /> : <Copy size={15} />}
      {!iconOnly && <span>{copied ? "已复制" : label}</span>}
    </button>
  );
}

async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

function diagnosticModuleName(module: string): string {
  const names: Record<string, string> = {
    mteam: "M-Team",
    qb1: "qB下载器1",
    qb2: "qB下载器2",
    qb3: "qB下载器3",
    tmdb: "TMDB",
    ai: "AI 模块",
    nas_disk: "NAS 存储",
    stats_engine: "数据统计",
  };
  return names[module] ?? module;
}

function diagnosticStatusMeta(item: any): { label: string; detail: string; tone: "success" | "failed" | "neutral" } {
  if (!item.enabled) {
    return { label: "未启用", detail: "当前模块尚未启用", tone: "neutral" };
  }
  if (item.status === "success") {
    return { label: "运行正常", detail: "最近一次检测通过", tone: "success" };
  }
  if (["failed", "failure", "error", "unhealthy"].includes(String(item.status || "").toLowerCase())) {
    return { label: "需要处理", detail: item.last_error || "检测未通过，请检查配置或网络", tone: "failed" };
  }
  return { label: "待检测", detail: "保存并测试后会更新状态", tone: "neutral" };
}

function DiagnosticsPage() {
  const health = useLoad<any>(() => api("/api/diagnostics/health"), []);
  const [exportPayload, setExportPayload] = useState<any | null>(null);
  const modules = health.data?.modules ?? [];
  const members = health.data?.wechat_members ?? [];

  return (
    <div className="grid-page">
      <Panel title="健康概览">
        <div className="diagnostics-health-list">
          {modules.map((item: any) => {
            const meta = diagnosticStatusMeta(item);
            return (
              <article className={`diagnostic-card ${meta.tone}`} key={item.module}>
                <div>
                  <strong>{diagnosticModuleName(item.module)}</strong>
                  <span className="diagnostic-status-pill">{meta.label}</span>
                </div>
                <small>{meta.detail}</small>
                {item.last_success_at && <small>最近检测：{formatDateLabel(item.last_success_at)}</small>}
              </article>
            );
          })}
          {!modules.length && <p className="muted">暂无健康检测数据。</p>}
        </div>
      </Panel>
      <Panel title="WeChat claw 成员状态">
        <div className="diagnostics-member-list">
          {members.map((member: any) => {
            const connected = Boolean(member.connected);
            const tone = connected ? "success" : member.configured ? "failed" : "neutral";
            const lastPoll = member.last_poll ?? {};
            return (
              <article className={`diagnostic-card diagnostic-member-card ${tone}`} key={member.id}>
                <div>
                  <span className="diagnostic-member-title"><MemberAvatar member={member} size="small" /><strong>{member.display_name}</strong></span>
                  <span className="diagnostic-status-pill">{connected ? "已连接" : member.configured ? "等待扫码" : "未配置"}</span>
                </div>
                <small>微信：{member.account_id || member.qrcode_status || "尚未绑定"}</small>
                <small>最近同步：{lastPoll.updated_at ? new Date(lastPoll.updated_at).toLocaleString() : "尚未同步"}{lastPoll.message ? ` · ${lastPoll.message}` : ""}</small>
              </article>
            );
          })}
          {!members.length && <p className="muted">尚未创建 WeChat claw 成员。</p>}
        </div>
      </Panel>
      <Panel title="诊断导出">
        <p className="muted">导出的内容会自动脱敏，便于排查配置、网络和模块状态。</p>
        <button onClick={() => api<any>("/api/diagnostics/export", { method: "POST" }).then(setExportPayload)}>导出脱敏 JSON</button>
        {exportPayload && <pre>{JSON.stringify(exportPayload, null, 2)}</pre>}
      </Panel>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return <section className="panel"><h2>{title}</h2>{children}</section>;
}

function Metric({ icon: Icon, title, value, source }: { icon?: typeof Film; title: string; value: ReactNode; source: string }) {
  return (
    <div className="metric">
      <small>{Icon && <span className="metric-icon"><Icon size={14} /></span>}{title}</small>
      <strong>{value}</strong>
      {source && <span>{source}</span>}
    </div>
  );
}

function StorageMetric({ overview }: { overview: any }) {
  const storage = storageDisplay(overview);
  return (
    <div className="metric storage-metric">
      <div className="storage-metric-head">
        <small><span className="metric-icon"><Database size={14} /></span> 存储空间</small>
        <strong className="storage-metric-percent">{storage.value}</strong>
      </div>
      <div className="storage-line"><span style={{ width: `${storage.percent || 0}%` }} /></div>
      <span className="storage-metric-capacity">{storage.helper}</span>
    </div>
  );
}

function downloaderDisplayName(qb: any): string {
  const id = String(qb?.id ?? "");
  const match = id.match(/^qb(\d+)$/i);
  if (match) return `qB${match[1]}`;
  const name = String(qb?.name ?? "");
  const nameMatch = name.match(/^q?b\s*(\d+)$/i);
  return nameMatch ? `qB${nameMatch[1]}` : name || "qB";
}

function downloaderDashboardTitle(qb: any): string {
  const id = String(qb?.id ?? "");
  const match = id.match(/^qb(\d+)$/i);
  return match ? `qB下载器${match[1]}` : `${downloaderDisplayName(qb)}下载器`;
}

function downloaderShortLabel(value: string): string {
  const match = String(value || "").match(/^qb(\d+)$/i);
  return match ? `qB${match[1]}` : String(value || "qB").replace(/^QB/i, "qB");
}

function DownloaderCard({
  qb,
  onOpen,
  onTestConnection,
  testingConnection = false,
}: {
  qb: any;
  onOpen?: (downloaderId: string) => void;
  onTestConnection?: (downloaderId: string) => void;
  testingConnection?: boolean;
}) {
  const displayName = downloaderDashboardTitle(qb);
  const online = Boolean(qb.online);
  const configured = qb.configured !== false;
  const enabled = qb.enabled !== false;
  const inactive = configured && !enabled;
  const connectionError = configured && enabled && !online;
  const statusLabel = !configured ? "未配置" : connectionError ? "连接异常" : online ? "连接正常" : "未启用";
  const statusTitle = qb.message || statusLabel;
  const updatedAt = qb.updated_at;

  function openDownloader() {
    if (qb.id) onOpen?.(qb.id);
  }

  function handleStatusTest(event: MouseEvent<HTMLButtonElement>) {
    event.stopPropagation();
    if (qb.id && configured) onTestConnection?.(qb.id);
  }

  function handleCardKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDownloader();
    }
  }

  return (
    <article className={configured ? "downloader-node" : "downloader-node empty"} role="button" tabIndex={0} onClick={openDownloader} onKeyDown={handleCardKeyDown}>
      <div className="downloader-node-top">
        <div className="downloader-node-title" title={statusTitle}>
          <h3>{displayName}</h3>
          <span className={connectionError ? "downloader-status-label error" : "downloader-status-label"}>
            <button className={testingConnection ? "downloader-status-button spinning" : "downloader-status-button"} type="button" onClick={handleStatusTest} disabled={testingConnection || !configured} title={`测试 ${displayName} 连通性`} aria-label={`测试 ${displayName} 连通性`}>
              <span className={online ? "status-dot online" : "status-dot offline"} />
            </button>
            {statusLabel}
          </span>
        </div>
        <small className="downloader-updated-at">数据更新于：{formatDateLabel(updatedAt)}</small>
      </div>
      {!configured ? (
        <div className="downloader-empty-body">请去设置里配置这个下载器</div>
      ) : inactive ? (
        <div className="downloader-empty-body">{qb.message || "请在设置里测试并启用这个下载器"}</div>
      ) : connectionError ? (
        <div className="downloader-empty-body">连接异常</div>
      ) : (
        <>
      <div className="downloader-count-row">
        <ActiveTransferCounts upload={qb.active_uploads ?? 0} download={qb.active_downloads ?? 0} />
      </div>
      <div className="downloader-speed-list">
        <span className="download"><b>↓</b><em>下载</em><strong>{formatSpeed(qb.download_speed)}</strong></span>
        <span className="upload"><b>↑</b><em>上传</em><strong>{formatSpeed(qb.upload_speed)}</strong></span>
      </div>
        </>
      )}
    </article>
  );
}

function LockedCard({ title, message, onOpen }: { title: string; message: string; onOpen?: () => void }) {
  return <button className="downloader-node locked" type="button" onClick={onOpen}><div className="downloader-node-head"><div className="downloader-node-title"><h3>{title}</h3></div><Lock size={16} /></div><p>{message}</p><small className="downloader-node-helper">需要管理员验证</small></button>;
}

function MediaSearchResults({ items = [], onSelect, loading }: { items: any[]; onSelect: (item: any) => void; loading?: boolean }) {
  return (
    <Panel title="TMDB 媒体结果">
      <div className="media-result-grid">
        {loading && <SearchLoadingState title="正在搜索 TMDB 媒体" detail="正在匹配影视条目、评分、海报和演职员信息" />}
        {items.map((item) => <MediaResultCard item={item} onSelect={onSelect} key={item.id} />)}
        {!loading && !items.length && <p className="muted">没有搜索到 TMDB 媒体，或 TMDB 尚未启用。</p>}
      </div>
    </Panel>
  );
}

function MediaResultCard({ item, onSelect }: { item: any; onSelect: (item: any) => void }) {
  const genres = (item.genres ?? []).slice(0, 4);
  const seasonEpisode = tvSeasonEpisodeLabel(item);
  const latestSeason = tvLatestSeasonLabel(item);
  const latestEpisode = tvEpisodeAirLabel(item.last_episode_to_air, "已播至");
  const meta = [
    item.media_type === "tv" ? "剧集" : "电影",
    item.year,
    item.runtime ? `${item.runtime} 分钟` : "",
    seasonEpisode,
    mediaCountryLabel(item)
  ].filter(Boolean).join(" / ");

  return (
    <article
      className="media-result-card media-result-card-clickable"
      role="button"
      tabIndex={0}
      onClick={() => onSelect(item)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onSelect(item);
      }}
    >
      {item.backdrop && <img className="media-backdrop" src={item.backdrop} alt="" loading="lazy" decoding="async" onError={handleImageError} />}
      <div className="media-card-shade" />
      <img className="media-poster" src={item.poster} alt="" loading="lazy" decoding="async" onError={handleImageError} />
      <div className="media-card-body">
        <div className="media-card-heading">
          <div>
            <h3>{item.title}</h3>
            <span>{item.original_title && item.original_title !== item.title ? item.original_title : meta}</span>
          </div>
          <span className="rating-badge"><Star size={15} /> {numberLabel(item.rating ?? 0, 1)}</span>
        </div>
        <div className="media-meta-row">
          <InfoPill icon={Film} text={meta} />
          {latestSeason ? <InfoPill icon={CalendarDays} text={latestSeason} /> : null}
          {latestEpisode ? <InfoPill icon={Clock3} text={latestEpisode} /> : null}
          <InfoPill icon={Users} text={`${item.vote_count ?? 0} 票`} />
          {item.popularity ? <InfoPill icon={Activity} text={`热度 ${item.popularity}`} /> : null}
        </div>
        {genres.length > 0 && <div className="chip-row">{genres.map((genre: string) => <span className="soft-chip" key={genre}>{genre}</span>)}</div>}
      </div>
    </article>
  );
}

function MTeamResourceResults({
  items = [],
  loading,
  sortBy,
  sortDirection,
  onSortBy,
  onSortDirection,
  onBack
}: {
  items: any[];
  loading?: boolean;
  sortBy: string;
  sortDirection: "asc" | "desc";
  onSortBy: (value: string) => void;
  onSortDirection: (value: "asc" | "desc") => void;
  onBack: () => void;
}) {
  return (
    <section className="panel">
      <div className="resource-panel-header">
        <div className="resource-panel-title">
          <button type="button" onClick={onBack}>返回发现</button>
          <h2>M-Team 资源结果</h2>
        </div>
        <div className="resource-sort-tools">
          <label>
            <span>排序</span>
            <select value={sortBy} onChange={(event) => onSortBy(event.target.value)}>
              <option value="seeders">做种</option>
              <option value="downloads">下载</option>
              <option value="size_bytes">体积</option>
            </select>
          </label>
          <div className="segmented compact">
            <button className={sortDirection === "desc" ? "active" : ""} type="button" onClick={() => onSortDirection("desc")}>降序</button>
            <button className={sortDirection === "asc" ? "active" : ""} type="button" onClick={() => onSortDirection("asc")}>升序</button>
          </div>
        </div>
      </div>
      <div className="mteam-resource-list">
        {loading && <SearchLoadingState title="正在搜索 M-Team 资源" detail="正在读取资源、体积、做种、促销和评分字段" />}
        {items.map((item) => <MTeamResourceCard item={item} key={item.id} />)}
        {!loading && !items.length && <p className="muted">没有搜索到 M-Team 资源，或 M-Team 尚未启用。</p>}
      </div>
    </section>
  );
}

function SearchLoadingState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="search-loading-state">
      <span className="search-loader-ring" />
      <div>
        <strong>{title}</strong>
        <small>{detail}</small>
      </div>
    </div>
  );
}

function MTeamResourceCard({ item }: { item: any }) {
  const [downloading, setDownloading] = useState(false);
  const [pushNotice, setPushNotice] = useState<PushNoticeState | null>(null);
  const chips = resourceChips(item);
  const detailUrl = mteamResourceDetailUrl(item);

  async function download() {
    if (downloading) return;
    setDownloading(true);
    setPushNotice({ status: "running", title: "正在推送下载", step: "正在向 M-Team 请求种子", detail: item.title });
    const timers = [
      window.setTimeout(() => {
        setPushNotice({ status: "running", title: "正在推送下载", step: "正在下载种子文件", detail: item.title });
      }, 700),
      window.setTimeout(() => {
        setPushNotice({ status: "running", title: "正在推送下载", step: "等待后端完成推送闭环", detail: item.title });
      }, 1700),
    ];
    try {
      await api<any>(`/api/mteam/torrents/${encodeURIComponent(item.id)}/download-to/qb1`, { method: "POST", body: JSON.stringify({ payload: item }) });
      playDoneSound();
      setPushNotice({ status: "success", title: "推送完成", step: "qB1 已接收下载任务", detail: item.title });
      window.setTimeout(() => setPushNotice(null), 5200);
    } catch (err) {
      const detail = apiErrorDetail(err);
      setPushNotice({ status: "error", title: "推送失败", step: "未能完成 M-Team 到 qB1 的闭环", detail });
    } finally {
      timers.forEach((timer) => window.clearTimeout(timer));
      setDownloading(false);
    }
  }

  return (
    <article className="mteam-resource-card">
      <div className="resource-main">
        <div className="resource-title-line">
          {detailUrl ? (
            <a className="resource-title-link" href={detailUrl} target="_blank" rel="noreferrer">{item.title}</a>
          ) : (
            <strong>{item.title}</strong>
          )}
          {item.promotion_label && <span className="free-chip" title={promotionTitle(item)}>{item.promotion_label}</span>}
        </div>
        <div className="chip-row">
          {chips.map((label: string) => <span className="resource-chip" key={label}>{label}</span>)}
        </div>
        {item.subtitle && <p className="resource-subtitle">{item.subtitle}</p>}
        <div className="resource-meta-grid">
          <InfoPill icon={HardDrive} text={item.size || "-"} tone="size" />
          <InfoPill icon={Upload} text={`做种 ${item.seeders ?? 0}`} tone="seed" />
          <InfoPill icon={Download} text={`下载 ${item.downloads ?? 0}`} tone="down" />
          <InfoPill icon={Users} text={`完成 ${item.completed ?? 0}`} />
          <InfoPill icon={MessageSquare} text={`评论 ${item.comments ?? 0}`} />
          <InfoPill icon={Clock3} text={formatDateLabel(item.published_at)} />
        </div>
      </div>
      <aside className="resource-side">
        <div className="score-stack">
          {item.douban_rating && <span className="score-badge douban">豆 {item.douban_rating}</span>}
          {item.imdb_rating && <span className="score-badge imdb">IMDb {item.imdb_rating}</span>}
        </div>
        {item.group && <span className="resource-group">{item.group}</span>}
        <button className="resource-download-button" onClick={download} disabled={downloading}>
          <Download size={17} />
          {downloading ? "推送中" : "下载"}
        </button>
      </aside>
      {pushNotice && <PushDownloadNotice notice={pushNotice} onClose={() => setPushNotice(null)} />}
    </article>
  );
}

function PushDownloadNotice({ notice, onClose }: { notice: PushNoticeState; onClose: () => void }) {
  return (
    <div className={`push-download-notice ${notice.status}`} role="status" aria-live="polite">
      <div className="push-ring">
        {notice.status === "success" ? <Check size={20} /> : notice.status === "error" ? <Bell size={18} /> : <RefreshCw size={19} />}
      </div>
      <div>
        <strong>{notice.title}</strong>
        <span>{notice.step}</span>
        {notice.detail && <small>{notice.detail}</small>}
      </div>
      {notice.status !== "running" && <button type="button" onClick={onClose}>关闭</button>}
    </div>
  );
}

function InfoPill({ icon: Icon, text, tone }: { icon: typeof Film; text: string; tone?: "up" | "down" | "size" | "seed" }) {
  return <span className={tone ? `info-pill ${tone}` : "info-pill"}><Icon size={14} />{text}</span>;
}

function PosterRail({ title, items = [], eagerLimit = 0, onMore, onSelect }: { title: string; items: any[]; eagerLimit?: number; onMore: () => void; onSelect: (item: any) => void }) {
  return (
    <section className="discover-section">
      <div className="discover-section-header">
        <h2><span />{title}</h2>
        <button className="discover-more" type="button" onClick={onMore}>更多 <span>›</span></button>
      </div>
      <div className="poster-rail" aria-label={title}>
        {items.map((item, index) => <DiscoverPosterCard item={item} eager={index < eagerLimit} onSelect={onSelect} key={item.id} />)}
      </div>
    </section>
  );
}

function DiscoverCollectionPage({ title, items = [], onBack, onSelect }: { title: string; items: any[]; onBack: () => void; onSelect: (item: any) => void }) {
  return (
    <section className="discover-collection">
      <div className="discover-collection-header">
        <button type="button" onClick={onBack}>返回发现</button>
        <div>
          <h2>{title}</h2>
          <span>{items.length} 个资源</span>
        </div>
      </div>
      <div className="poster-grid">
        {items.map((item, index) => <DiscoverPosterCard item={item} eager={index < eagerPosterLimit()} onSelect={onSelect} key={item.id} />)}
        {!items.length && <p className="muted">暂无资源。</p>}
      </div>
    </section>
  );
}

function DiscoverPosterCard({ item, eager = false, onSelect }: { item: any; eager?: boolean; onSelect: (item: any) => void }) {
  const [infoOpen, setInfoOpen] = useState(false);
  const rating = Number(item.rating ?? 0);
  const yearLabel = item.year && item.year !== "未知" ? String(item.year) : "";
  const languageLabel = mediaLanguageLabel(item);
  const genreLabel = mediaGenreLabel(item);
  const details = [
    yearLabel,
    languageLabel,
    genreLabel,
  ].filter(Boolean);

  useEffect(() => {
    setInfoOpen(false);
  }, [item.id, item.tmdb_id]);

  function handlePosterClick() {
    if (details.length && !infoOpen && isMobilePosterInteraction()) {
      setInfoOpen(true);
      return;
    }
    onSelect(item);
  }

  return (
    <article
      className={infoOpen ? "poster poster-clickable poster-info-open" : "poster poster-clickable"}
      role="button"
      tabIndex={0}
      onClick={handlePosterClick}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onSelect(item);
      }}
    >
      <div className="poster-art">
        <img src={item.poster} alt="" loading={eager ? "eager" : "lazy"} decoding="async" fetchPriority={eager ? "high" : "auto"} onError={handleImageError} />
        <span className="poster-type">{item.media_type === "tv" ? "电视剧" : "电影"}</span>
        {rating > 0 && <span className="poster-rating">{numberLabel(rating, 1)}</span>}
        <div className="poster-hover">
          {details.map((detail) => <span key={detail}>{detail}</span>)}
        </div>
      </div>
      <strong title={item.title}>{item.title}</strong>
    </article>
  );
}

function MediaDetailPage({
  item,
  loading,
  error,
  onBack,
  onPersonSelect,
  onMediaSelect,
  onMTeamSearch,
  mteamSearching
}: {
  item: any;
  loading: boolean;
  error: string;
  onBack: () => void;
  onPersonSelect: (person: any) => void;
  onMediaSelect: (item: any) => void;
  onMTeamSearch: (item: any) => void;
  mteamSearching: boolean;
}) {
  const castMembers = Array.isArray(item.cast_members) ? item.cast_members : [];
  const recommendations = Array.isArray(item.recommendations) ? item.recommendations : [];
  const seasonEpisode = tvSeasonEpisodeLabel(item);
  const latestSeason = tvLatestSeasonLabel(item, false);
  const latestEpisode = tvEpisodeAirLabel(item.last_episode_to_air, "已播至");
  const nextEpisode = tvEpisodeAirLabel(item.next_episode_to_air, "下一集");
  const facts = [
    ["TMDB ID", item.tmdb_id || mediaTmdbId(item) || "-"],
    ["原始标题", item.original_title || "-"],
    [item.media_type === "tv" ? "首播日期" : "上映日期", item.release_date || "-"],
    ...(item.media_type === "tv" ? [
      ["季集信息", seasonEpisode || "-"],
      ["最新季", latestSeason || "-"],
      ["最近播出", latestEpisode || "-"],
      ["下一集", nextEpisode || "-"],
    ] : []),
    ["出品国家", mediaCountryLabel(item) || "-"],
  ];
  return (
    <section className="tmdb-detail">
      {item.backdrop && <img className="tmdb-detail-backdrop" src={item.backdrop} alt="" loading="lazy" decoding="async" onError={handleImageError} />}
      <div className="tmdb-detail-haze" />
      <div className="tmdb-detail-glass">
        <button className="tmdb-back" type="button" onClick={onBack}>返回发现</button>
        <button className="tmdb-mteam-search" type="button" onClick={() => onMTeamSearch(item)} disabled={mteamSearching} title="用影片名称在 M-Team 搜索资源">
          <Radar size={17} />
          {mteamSearching ? "搜索中" : "M-Team 搜索"}
        </button>
        {loading && <span className="tmdb-loading">正在读取 TMDB 详情...</span>}
        {error && <p className="error">{error}</p>}
        <div className="tmdb-detail-main">
          <img className="tmdb-detail-poster" src={item.poster} alt="" loading="eager" decoding="async" fetchPriority="high" onError={handleImageError} />
          <div className="tmdb-detail-copy">
            <span className="tmdb-type-pill">{item.media_type === "tv" ? "电视剧" : "电影"}</span>
            <h2>{item.title} <small>{item.year && item.year !== "未知" ? `(${item.year})` : ""}</small></h2>
            <p className="tmdb-subtitle">{[item.runtime ? `${item.runtime} 分钟` : "", seasonEpisode, mediaGenreLabel(item), item.director ? `导演 ${item.director}` : ""].filter(Boolean).join(" / ")}</p>
            <div className="tmdb-score-line">
              <span className="tmdb-score"><Star size={16} /> {numberLabel(Number(item.rating ?? 0), 1)}</span>
              <span>{numberLabel(Number(item.vote_count ?? 0))} 票</span>
              {latestEpisode ? <span>{latestEpisode}</span> : null}
              {item.popularity ? <span>热度 {numberLabel(Number(item.popularity), 1)}</span> : null}
            </div>
          </div>
        </div>
      </div>

      <section className="tmdb-synopsis-row">
        <div className="tmdb-synopsis">
            <h3>简介</h3>
            <p className="tmdb-overview">{item.overview || "暂无简介。"}</p>
            <div className="tmdb-credit-line">
              <span><strong>导演</strong>{item.director || "-"}</span>
              <span><strong>主演</strong>{Array.isArray(item.cast) ? item.cast.slice(0, 4).join(" / ") : "-"}</span>
            </div>
        </div>
        <aside className="tmdb-facts">
          <div className="tmdb-star-strip" aria-label={`评分 ${numberLabel(Number(item.rating ?? 0), 1)}`}>
            {ratingStars(Number(item.rating ?? 0)).map((filled, index) => <Star size={18} key={index} fill={filled ? "currentColor" : "none"} />)}
          </div>
          {facts.map(([label, value]) => <span key={label}><small>{label}</small><strong>{value}</strong></span>)}
        </aside>
      </section>

      <section className="tmdb-detail-section">
        <div className="discover-section-header"><h2><span />演员阵容</h2></div>
        <div className="tmdb-cast-rail">
          {castMembers.map((person: any) => (
            <button className="tmdb-person-card" type="button" onClick={() => onPersonSelect(person)} key={person.id}>
              <img src={person.profile} alt="" loading="lazy" decoding="async" onError={handleImageError} />
              <strong>{person.name}</strong>
              <span>{person.character ? `饰 ${person.character}` : "演员"}</span>
            </button>
          ))}
          {!castMembers.length && <p className="muted">暂无演员阵容。</p>}
        </div>
      </section>

      <section className="tmdb-detail-section">
        <div className="discover-section-header"><h2><span />猜你喜欢</h2></div>
        <div className="poster-rail">
          {recommendations.map((next: any) => <DiscoverPosterCard item={next} onSelect={onMediaSelect} key={next.id} />)}
          {!recommendations.length && <p className="muted">暂无同类型推荐。</p>}
        </div>
      </section>
    </section>
  );
}

function PersonDetailPage({
  person,
  loading,
  error,
  onBack,
  onMediaSelect
}: {
  person: any;
  loading: boolean;
  error: string;
  onBack: () => void;
  onMediaSelect: (item: any) => void;
}) {
  const works = Array.isArray(person.known_for) ? person.known_for : [];
  return (
    <section className="tmdb-detail tmdb-person-detail">
      <div className="tmdb-detail-haze" />
      <div className="tmdb-detail-glass">
        <div className="tmdb-person-actions">
          <button className="tmdb-back tmdb-back-inline" type="button" onClick={onBack}>返回影片</button>
        </div>
        {loading && <span className="tmdb-loading">正在读取演员作品...</span>}
        {error && <p className="error">{error}</p>}
        <div className="tmdb-person-hero">
          <img src={person.profile} alt="" loading="eager" decoding="async" fetchPriority="high" onError={handleImageError} />
          <div>
            <span className="tmdb-type-pill">{person.known_for_department || "Acting"}</span>
            <h2>{person.name}</h2>
            <p className="tmdb-subtitle">{[person.birthday, person.place_of_birth].filter(Boolean).join(" / ")}</p>
            <p className="tmdb-overview">{person.biography || "暂无演员简介。"}</p>
          </div>
        </div>
      </div>
      <section className="tmdb-detail-section">
        <div className="discover-section-header"><h2><span />相关作品</h2></div>
        <div className="poster-rail">
          {works.map((work: any) => <DiscoverPosterCard item={work} onSelect={onMediaSelect} key={work.id} />)}
          {!works.length && <p className="muted">暂无相关作品。</p>}
        </div>
      </section>
    </section>
  );
}

function ResourceRow({ item }: { item: any }) {
  async function download() {
    await api(`/api/mteam/torrents/${encodeURIComponent(item.id)}/download-to/qb1`, { method: "POST", body: JSON.stringify({ payload: item }) });
  }

  return <div className="row"><strong>{item.title}</strong><span>{item.resolution} / {item.codec} / {item.size} / 做种 {item.seeders}</span><button className="resource-download-button" onClick={download}><Download size={16} />下载</button></div>;
}

function QbTorrentTableRow({
  item,
  selected,
  onSelect,
  onContextMenu,
  onLongPress
}: {
  item: any;
  selected: boolean;
  onSelect: () => void;
  onContextMenu: (event: MouseEvent<HTMLElement>) => void;
  onLongPress: (point: { x: number; y: number }) => void;
}) {
  const progress = Math.round((item.progress ?? 0) * 1000) / 10;
  const longPressTimer = useRef<number | null>(null);
  const longPressStart = useRef<{ x: number; y: number } | null>(null);
  const longPressFired = useRef(false);

  function clearLongPress() {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
    longPressStart.current = null;
  }

  function handlePointerDown(event: PointerEvent<HTMLElement>) {
    if (event.pointerType === "mouse") return;
    longPressFired.current = false;
    const point = { x: event.clientX, y: event.clientY };
    longPressStart.current = point;
    longPressTimer.current = window.setTimeout(() => {
      longPressFired.current = true;
      onLongPress(point);
    }, 520);
  }

  function handlePointerMove(event: PointerEvent<HTMLElement>) {
    const point = longPressStart.current;
    if (!point) return;
    if (Math.abs(event.clientX - point.x) > 10 || Math.abs(event.clientY - point.y) > 10) {
      clearLongPress();
    }
  }

  function handleClick() {
    if (longPressFired.current) {
      longPressFired.current = false;
      return;
    }
    onSelect();
  }

  return (
    <article
      className={selected ? "qb-task-grid qb-task-row selected" : "qb-task-grid qb-task-row"}
      onClick={handleClick}
      onContextMenu={onContextMenu}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={clearLongPress}
      onPointerCancel={clearLongPress}
      onPointerLeave={clearLongPress}
    >
      <div className="qb-name-cell">
        <strong title={item.name}>{item.name}</strong>
        <small>{item.tracker || item.content_path || item.hash}</small>
      </div>
      <span className="qb-num" data-label="大小">{formatBytes(item.size ?? item.total_size ?? 0)}</span>
      <div className="qb-progress-cell" data-label="进度">
        <div className="qb-progress"><span style={{ width: `${Math.max(2, Math.min(100, progress))}%` }} /></div>
        <b>{progress.toFixed(1)}%</b>
      </div>
      <span className="qb-state-pill" data-label="状态">{stateLabel(item.state)}</span>
      <span className="qb-num" data-label="种子">{numberPair(item.num_seeds, item.num_complete)}</span>
      <span className="qb-num" data-label="用户">{numberPair(item.num_leechs, item.num_incomplete)}</span>
      <span className="qb-num" data-label="下载">{formatSpeed(item.download_speed ?? 0)}</span>
      <span className="qb-num" data-label="上传">{formatSpeed(item.upload_speed ?? 0)}</span>
      <span className="qb-num" data-label="剩余">{formatEta(item.eta)}</span>
      <span className="qb-num" data-label="比率">{numberLabel(item.ratio ?? 0, 2)}</span>
      <span className="qb-num" data-label="流行度">{numberLabel(item.availability ?? 0, 2)}</span>
      <span className="qb-ellipsis" data-label="分类">{item.category || "-"}</span>
      <span className="qb-tags-cell" data-label="标签">{(item.tags ?? []).length ? item.tags.join(", ") : "-"}</span>
      <span className="qb-num" data-label="添加于">{formatDateLabel(item.added_at)}</span>
      <span className="qb-path-cell" data-label="路径" title={item.save_path}>{item.save_path || "-"}</span>
    </article>
  );
}

function QbDetailLoading({ item }: { item: any }) {
  return (
    <section className="qb-detail-loading">
      <RefreshCw size={24} />
      <strong>数据读取中</strong>
      <span>{item.name}</span>
    </section>
  );
}

function QbTorrentDetailPanel({
  item,
  detail,
  error,
  onPriorityChange,
}: {
  item: any | null;
  detail: any | null;
  error: string;
  onPriorityChange: (fileId: number, priority: number) => Promise<void>;
}) {
  const props = detail?.properties ?? {};
  const files = detail?.files ?? [];
  const trackers = detail?.trackers ?? [];
  const detailCards = [
    { label: "保存路径", value: props.save_path || item?.save_path || "-", wide: true },
    { label: "总大小", value: formatBytes(props.total_size || item?.size || 0) },
    { label: "已下载", value: formatBytes(props.total_downloaded || item?.downloaded || 0) },
    { label: "已上传", value: formatBytes(props.total_uploaded || item?.uploaded || 0) },
    { label: "本次下载", value: formatBytes(props.total_downloaded_session || item?.downloaded_session || 0) },
    { label: "本次上传", value: formatBytes(props.total_uploaded_session || item?.uploaded_session || 0) },
    { label: "平均下载", value: formatSpeed(props.download_speed_avg || 0) },
    { label: "平均上传", value: formatSpeed(props.upload_speed_avg || 0) },
    { label: "连接", value: `${props.connections ?? 0} / ${props.connections_limit ?? "-"}` },
    { label: "种子/用户", value: `${props.seeds ?? item?.num_seeds ?? 0} / ${props.peers ?? item?.num_leechs ?? 0}` },
    { label: "分享率", value: numberLabel(props.share_ratio ?? item?.ratio ?? 0, 2) },
    { label: "活跃时间", value: formatDuration(props.time_elapsed || item?.time_active || 0) },
    { label: "做种时间", value: formatDuration(props.seeding_time || item?.seeding_time || 0) },
    { label: "创建工具", value: props.created_by || "-" },
    { label: "完成时间", value: formatDateLabel(props.completion_date || item?.completed_at) },
    { label: "最后活动", value: formatDateLabel(item?.last_activity_at || props.last_seen) },
    ...(props.comment ? [{ label: "备注", value: props.comment, wide: true }] : []),
  ];
  return (
    <section className="qb-detail-panel">
      <div className="qb-detail-title">
        <div>
          <h2>{item?.name ?? "任务详情"}</h2>
          <span>{item?.hash ?? detail?.hash ?? ""}</span>
        </div>
        {error && <strong className="qb-detail-error">{error}</strong>}
      </div>
      <div className="qb-detail-grid">
        {detailCards.map((card) => <DetailItem label={card.label} value={card.value} wide={card.wide} key={card.label} />)}
      </div>
      <div className="qb-detail-tabs">
        <span className="active">内容</span>
        <span>Tracker</span>
      </div>
      <div className="qb-detail-content">
        <div className="qb-files-list">
          <div className="qb-file-row qb-file-head">
            <span>名称</span>
            <span>总大小</span>
            <span>进度</span>
            <span>下载优先级</span>
            <span>剩余</span>
            <span>可用性</span>
          </div>
          {files.map((file: any) => (
            <div className="qb-file-row" key={file.id}>
              <span className="qb-file-name" title={file.name}>{file.name}</span>
              <span>{formatBytes(file.size)}</span>
              <span className="qb-progress-cell">
                <div className="qb-progress"><span style={{ width: `${Math.max(2, Math.min(100, Math.round(file.progress * 1000) / 10))}%` }} /></div>
                <b>{(Math.round(file.progress * 1000) / 10).toFixed(1)}%</b>
              </span>
              <span>
                <select value={file.priority} onChange={(event) => onPriorityChange(file.id, Number(event.target.value))}>
                  <option value={0}>不下载</option>
                  <option value={1}>正常</option>
                  <option value={6}>高</option>
                  <option value={7}>最高</option>
                </select>
              </span>
              <span>{formatBytes(file.size * (1 - file.progress))}</span>
              <span>{numberLabel(file.availability ?? 0, 1)}%</span>
            </div>
          ))}
          {!files.length && <div className="qb-empty">单击任务后会加载文件内容</div>}
        </div>
        <div className="qb-trackers">
          {trackers.slice(0, 6).map((tracker: any) => (
            <div key={`${tracker.url}-${tracker.tier}`}>
              <strong>{tracker.url || "DHT / PeX / LSD"}</strong>
              <span>种子 {tracker.num_seeds} / 用户 {tracker.num_leeches} / 已完成 {tracker.num_downloaded}</span>
              {tracker.message && <small>{tracker.message}</small>}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function DetailItem({ label, value, wide }: { label: string; value: ReactNode; wide?: boolean }) {
  return (
    <div className={wide ? "qb-detail-item wide" : "qb-detail-item"}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function numberLabel(value: number, digits = 0): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function apiErrorDetail(error: unknown): string {
  return normalizeApiErrorMessage(error);
}

function playDoneSound() {
  try {
    const AudioContextClass = window.AudioContext || (window as any).webkitAudioContext;
    const context = new AudioContextClass();
    const gain = context.createGain();
    gain.gain.setValueAtTime(0.0001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.08, context.currentTime + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.22);
    gain.connect(context.destination);
    [660, 880].forEach((frequency, index) => {
      const oscillator = context.createOscillator();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(frequency, context.currentTime + index * 0.08);
      oscillator.connect(gain);
      oscillator.start(context.currentTime + index * 0.08);
      oscillator.stop(context.currentTime + 0.24 + index * 0.02);
    });
    window.setTimeout(() => context.close().catch(() => undefined), 420);
  } catch {
    // Completion sound is best-effort; browsers may block audio in some contexts.
  }
}

function formatBytesFixed(value: number, digits: number): string {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let current = Number(value || 0);
  for (const unit of units) {
    if (current < 1024 || unit === units[units.length - 1]) return `${current.toFixed(digits)} ${unit}`;
    current /= 1024;
  }
  return `${current.toFixed(digits)} PB`;
}

function formatDateLabel(value: string): string {
  if (!value) return "-";
  const normalized = /T/.test(value) && !/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? `${value}Z` : value;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function mediaCountryLabel(item: any): string {
  const countries = Array.isArray(item.production_countries) ? item.production_countries.filter(Boolean) : [];
  if (countries.length) return countries.slice(0, 2).map(localizedCountryName).join(" / ");
  const language = String(item.original_language ?? "").trim();
  return language ? localizedLanguageName(language) : "";
}

function mediaLanguageLabel(item: any): string {
  const language = String(item.original_language ?? "").trim();
  return language ? localizedLanguageName(language) : "";
}

const COUNTRY_NAME_ZH: Record<string, string> = {
  US: "美国",
  USA: "美国",
  "UNITED STATES": "美国",
  "UNITED STATES OF AMERICA": "美国",
  CN: "中国大陆",
  CHINA: "中国大陆",
  HK: "中国香港",
  "HONG KONG": "中国香港",
  TW: "中国台湾",
  TAIWAN: "中国台湾",
  JP: "日本",
  JAPAN: "日本",
  KR: "韩国",
  "SOUTH KOREA": "韩国",
  GB: "英国",
  UK: "英国",
  "UNITED KINGDOM": "英国",
  FR: "法国",
  FRANCE: "法国",
  DE: "德国",
  GERMANY: "德国",
  ES: "西班牙",
  SPAIN: "西班牙",
  IT: "意大利",
  ITALY: "意大利",
  CA: "加拿大",
  CANADA: "加拿大",
  AU: "澳大利亚",
  AUSTRALIA: "澳大利亚",
  RU: "俄罗斯",
  RUSSIA: "俄罗斯",
  IN: "印度",
  INDIA: "印度",
  TH: "泰国",
  THAILAND: "泰国",
  MX: "墨西哥",
  MEXICO: "墨西哥",
  BR: "巴西",
  BRAZIL: "巴西"
};

const LANGUAGE_NAME_ZH: Record<string, string> = {
  EN: "英语",
  ES: "西班牙语",
  RU: "俄语",
  ZH: "中文",
  JA: "日语",
  KO: "韩语",
  FR: "法语",
  DE: "德语",
  IT: "意大利语",
  PT: "葡萄牙语",
  HI: "印地语",
  TH: "泰语"
};

function localizedCountryName(value: string): string {
  const key = String(value).trim().toUpperCase();
  return COUNTRY_NAME_ZH[key] || String(value);
}

function localizedLanguageName(value: string): string {
  const key = String(value).trim().toUpperCase();
  return LANGUAGE_NAME_ZH[key] || key;
}

function mediaGenreLabel(item: any): string {
  const genres = Array.isArray(item.genres) ? item.genres.filter(Boolean) : [];
  return genres.slice(0, 3).join(" / ");
}

function mediaCreditLabel(item: any): string {
  const director = String(item.director ?? "").trim();
  if (director) return `导演 ${director}`;
  const cast = Array.isArray(item.cast) ? item.cast.filter(Boolean).slice(0, 2).join(" / ") : "";
  return cast ? `主演 ${cast}` : "";
}

function tvSeasonEpisodeLabel(item: any): string {
  if (item?.media_type !== "tv") return "";
  const seasons = Number(item.number_of_seasons ?? 0);
  const episodes = Number(item.number_of_episodes ?? 0);
  if (seasons > 0 && episodes > 0) return `共 ${seasons} 季 ${episodes} 集`;
  if (seasons > 0) return `共 ${seasons} 季`;
  if (episodes > 0) return `共 ${episodes} 集`;
  return "";
}

function tvLatestSeasonLabel(item: any, withPrefix = true): string {
  if (item?.media_type !== "tv" || !item.latest_season) return "";
  const seasonNumber = Number(item.latest_season.season_number ?? 0);
  if (seasonNumber <= 0) return "";
  const episodeCount = Number(item.latest_season.episode_count ?? 0);
  const parts = [`第 ${seasonNumber} 季`];
  if (episodeCount > 0) parts.push(`共 ${episodeCount} 集`);
  if (item.latest_season.air_date) parts.push(`${String(item.latest_season.air_date)} 首播`);
  const label = parts.join("，");
  return withPrefix ? `最新季：${label}` : label;
}

function tvEpisodeAirLabel(episode: any, prefix: string): string {
  if (!episode) return "";
  const seasonNumber = Number(episode.season_number ?? 0);
  const episodeNumber = Number(episode.episode_number ?? 0);
  if (seasonNumber <= 0 || episodeNumber <= 0) return "";
  const detail = [
    `第 ${seasonNumber} 季第 ${episodeNumber} 集`,
    episode.air_date ? String(episode.air_date) : "",
    episode.name ? String(episode.name) : "",
  ].filter(Boolean).join("，");
  return prefix ? `${prefix}：${detail}` : detail;
}

function mediaTmdbId(item: any): string {
  if (item?.tmdb_id) return String(item.tmdb_id);
  const id = String(item?.id ?? "");
  const parts = id.split("-");
  return parts.length > 1 ? parts[parts.length - 1] : id;
}

function ratingStars(value: number): boolean[] {
  const filled = Math.max(0, Math.min(10, Math.round(Number(value || 0))));
  return Array.from({ length: 10 }, (_, index) => index < filled);
}

function numberPair(value: number, total: number): string {
  return `${Number(value ?? 0)} (${Number(total ?? 0)})`;
}

function formatEta(value: number): string {
  const seconds = Number(value ?? 0);
  if (!Number.isFinite(seconds) || seconds <= 0) return "∞";
  if (seconds >= 8_640_000) return "∞";
  return formatDuration(seconds);
}

function formatDuration(value: number): string {
  const seconds = Math.max(0, Math.floor(value));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}天 ${hours}时`;
  if (hours) return `${hours}时 ${minutes}分`;
  return `${minutes}分`;
}

function stateLabel(value: string): string {
  const states: Record<string, string> = {
    uploading: "做种",
    stalledup: "做种",
    forcedup: "强制做种",
    queuedup: "排队做种",
    downloading: "下载中",
    stalleddl: "等待下载",
    forceddl: "强制下载",
    metadl: "获取元数据",
    pauseddl: "暂停",
    pausedup: "暂停",
    checkingup: "校验",
    checkingdl: "校验",
    error: "错误",
  };
  return states[String(value || "").toLowerCase()] ?? value ?? "-";
}

function sortResources(items: any[], sortBy: string, direction: "asc" | "desc"): any[] {
  const multiplier = direction === "asc" ? 1 : -1;
  return [...items].sort((left, right) => {
    const a = Number(left?.[sortBy] ?? 0);
    const b = Number(right?.[sortBy] ?? 0);
    if (a === b) return String(left.title ?? "").localeCompare(String(right.title ?? ""));
    return (a - b) * multiplier;
  });
}

function resourceChips(item: any): string[] {
  const values = [
    ...(Array.isArray(item.labels) ? item.labels : []),
    item.resolution,
    item.codec,
    item.audio_codec,
    item.hdr,
  ];
  const result: string[] = [];
  for (const value of values) {
    const label = String(value ?? "").trim();
    if (!label || label === "-" || /^\d+$/.test(label)) continue;
    if (!result.some((item) => item.toLowerCase() === label.toLowerCase())) result.push(label);
  }
  return result.slice(0, 10);
}

function promotionTitle(item: any): string {
  const parts = [];
  if (item.promotion_until) parts.push(`截止：${formatDateLabel(item.promotion_until)}`);
  if (item.discount) parts.push(`原始枚举：${item.discount}`);
  return parts.join(" / ") || "促销";
}

function mteamResourceDetailUrl(item: any): string {
  return String(item?.detail_url || item?.detailUrl || item?.page_url || item?.pageUrl || "").trim();
}

const pages: Record<NavKey, (props: { user: User; resetToken?: number; selectedDownloader?: string; onNavigate?: (key: NavKey) => void; onOpenDownloader?: (downloaderId: string) => void }) => ReactNode> = {
  discover: DiscoverPage,
  dashboard: DashboardPage,
  downloads: DownloadsPage,
  notifications: NotificationsAssistantPage,
  settings: SettingsPage,
  diagnostics: DiagnosticsPage
};
