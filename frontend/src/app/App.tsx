import {
  Activity,
  ArrowLeft,
  ArrowUp,
  Bell,
  CalendarDays,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  Coins,
  Copy,
  Database,
  Download,
  Eye,
  EyeOff,
  Film,
  Gauge,
  Grid3X3,
  HardDrive,
  Heart,
  Link2,
  Lock,
  LogOut,
  MessageSquare,
  Plus,
  Percent,
  PanelLeftClose,
  PanelLeftOpen,
  Radar,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Star,
  Trash2,
  Upload,
  UploadCloud,
  UserRound,
  Users,
  Wrench
} from "lucide-react";
import QRCode from "qrcode";
import { FormEvent, KeyboardEvent, MouseEvent, PointerEvent, ReactNode, RefObject, SyntheticEvent, createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, formatBytes, formatSpeed, getToken, normalizeApiErrorMessage, setToken } from "../api/client";

type User = { id?: number; username: string; role: string; must_change_credentials?: boolean };
type SetupStatus = { initialized: boolean; default_credentials_pending?: boolean };
type NavKey = "discover" | "favorites" | "dashboard" | "downloads" | "notifications" | "settings" | "diagnostics";
type TrafficDimension = "year" | "month" | "week" | "day" | "hour";
type DiscoverMode = "home" | "dual" | "mteam";
type DiscoverBrowseMode = "casual" | "filter";
type DiscoverDetailHistoryEntry = { kind: "media" | "person"; item: any; scrollTop: number };
type DiscoverFilters = {
  media_type: "movie" | "tv";
  sort_by: string;
  genre: string;
  region: string;
  language: string;
  year: string;
  min_rating: string;
};
type DiscoverFilterSession = {
  key: string;
  filters: DiscoverFilters;
  payload: any;
  items: any[];
  nextPage: number | null;
  hasMore: boolean;
};

type FavoritesContextValue = {
  items: any[];
  loading: boolean;
  error: string;
  isFavorite: (item: any) => boolean;
  isBusy: (item: any) => boolean;
  toggle: (item: any) => Promise<void>;
};

const FavoritesContext = createContext<FavoritesContextValue>({
  items: [],
  loading: false,
  error: "",
  isFavorite: () => false,
  isBusy: () => false,
  toggle: async () => undefined,
});

function useFavorites() {
  return useContext(FavoritesContext);
}

function favoriteMediaKey(item: any): string {
  const mediaType = String(item?.media_type || "").toLowerCase();
  const tmdbId = mediaTmdbId(item);
  return mediaType && tmdbId ? `${mediaType}:${tmdbId}` : "";
}

const DEFAULT_DISCOVER_FILTERS: DiscoverFilters = {
  media_type: "movie",
  sort_by: "popularity.desc",
  genre: "",
  region: "",
  language: "",
  year: "",
  min_rating: "0",
};

const TMDB_LANGUAGE_OPTIONS = [
  { value: "zh-CN", label: "简体中文" },
  { value: "zh-TW", label: "繁体中文" },
  { value: "en-US", label: "English" },
  { value: "ja-JP", label: "日本語" },
  { value: "ko-KR", label: "한국어" },
];

const TMDB_REGION_OPTIONS = [
  { value: "CN", label: "中国大陆" },
  { value: "HK", label: "中国香港" },
  { value: "TW", label: "中国台湾" },
  { value: "US", label: "美国" },
  { value: "GB", label: "英国" },
  { value: "JP", label: "日本" },
  { value: "KR", label: "韩国" },
];

const TMDB_PROXY_DOMAIN_OPTIONS = [
  {
    domain: "api.themoviedb.org",
    label: "TMDB 数据接口",
    description: "用于搜索、发现、影视详情、演职员、趋势和筛选数据。网络受限时使用代理通常可提升返回速度、降低超时概率并提高稳定性。",
  },
  {
    domain: "image.tmdb.org",
    label: "TMDB 图片资源",
    description: "用于海报、背景图、头像和 Logo。网络受限时使用代理通常可减少图片空白，提高加载与缓存成功率。",
  },
] as const;
const TMDB_PROXY_DOMAINS = TMDB_PROXY_DOMAIN_OPTIONS.map((option) => option.domain);

type IntegrationTestResult = {
  success?: boolean;
  state?: "draft";
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
    proxy_domains?: string[];
    domain_routes?: Record<string, "proxy" | "direct">;
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
  api_key: string;
  bearer_token: string;
  proxy_enabled: boolean;
  proxy_url: string;
  proxy_domains: string[];
  language: string;
  region: string;
  timeout: string;
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
    agent_decision: "Agent 自主决策",
    tmdb_media_details: "TMDB 影片详情",
    tmdb_person_details: "TMDB 人物详情",
    mteam_result_details: "M-Team 资源详情",
    qb_list_torrents: "qB 任务查询",
    qb_torrent_details: "qB 任务详情",
    prepare_mteam_download: "准备下载确认",
    confirm_mteam_download: "确认提交下载",
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
  { key: "favorites", label: "收藏", icon: Heart },
  { key: "dashboard", label: "仪表盘", icon: Gauge },
  { key: "downloads", label: "下载", icon: Download },
  { key: "notifications", label: "通知", icon: Bell },
  { key: "settings", label: "设置", icon: Settings },
  { key: "diagnostics", label: "诊断", icon: Wrench, admin: true }
];

const pageDescriptions: Record<NavKey, string> = {
  discover: "从 TMDB 获取流行趋势、热门内容和高分片单。",
  favorites: "集中查看收藏的电影和剧集，并按收藏时间回顾。",
  dashboard: "查看站点、下载器和 NAS 的核心运行指标。",
  downloads: "查看和管理多个 qB 下载器中的任务。",
  notifications: "集中查看系统提醒和任务通知。",
  settings: "管理运行时凭据，敏感信息只在后端加密保存。",
  diagnostics: "查看核心模块是否正常运行。"
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
      setLoading(true);
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
        })
        .finally(() => setLoading(false));
    },
    setData
  };
}

const SEARCH_HISTORY_STORAGE_KEY = "ptmh_search_history";
const SIDEBAR_COLLAPSED_STORAGE_KEY = "ptmh_sidebar_collapsed";
const LOCAL_SNAPSHOT_PREFIX = "ptmh_snapshot:v3:";
const LOCAL_SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024;
const LOCAL_SNAPSHOT_TOTAL_MAX_BYTES = 4 * 1024 * 1024;
const LOCAL_SNAPSHOT_MAX_ENTRIES = 20;
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


function snapshotBytes(value: string) {
  return new Blob([value]).size;
}

function snapshotContentHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function localSnapshotVersion(key: string, payload: any, serializedPayload: string) {
  if (key === "dashboard") {
    const dashboardVersion = payload?._preload?.cached_at ?? payload?.updated_at;
    if (dashboardVersion) return String(dashboardVersion);
  }
  return `content:${snapshotContentHash(serializedPayload)}`;
}

function pruneLocalSnapshots(incomingKey: string, incomingBytes: number) {
  const entries: Array<{ key: string; bytes: number; savedAt: number }> = [];
  let totalBytes = 0;
  for (let index = 0; index < localStorage.length; index += 1) {
    const key = localStorage.key(index);
    if (!key?.startsWith(LOCAL_SNAPSHOT_PREFIX) || key === incomingKey) continue;
    const raw = localStorage.getItem(key);
    if (!raw) continue;
    const bytes = snapshotBytes(raw);
    let savedAt = 0;
    try {
      savedAt = Date.parse(JSON.parse(raw)?.saved_at ?? "") || 0;
    } catch {
      // Invalid cache entries are the first candidates for eviction.
    }
    entries.push({ key, bytes, savedAt });
    totalBytes += bytes;
  }
  entries.sort((left, right) => left.savedAt - right.savedAt);
  while (
    entries.length > 0 &&
    (totalBytes + incomingBytes > LOCAL_SNAPSHOT_TOTAL_MAX_BYTES || entries.length + 1 > LOCAL_SNAPSHOT_MAX_ENTRIES)
  ) {
    const oldest = entries.shift();
    if (!oldest) break;
    localStorage.removeItem(oldest.key);
    totalBytes -= oldest.bytes;
  }
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
    const storageKey = `${LOCAL_SNAPSHOT_PREFIX}${key}`;
    const normalizedPayload = rewriteTmdbImageUrls(payload);
    const serializedPayload = JSON.stringify(normalizedPayload);
    const version = localSnapshotVersion(key, normalizedPayload, serializedPayload);
    const existing = localStorage.getItem(storageKey);
    if (existing) {
      try {
        if (JSON.parse(existing)?.version === version) return;
      } catch {
        localStorage.removeItem(storageKey);
      }
    }
    const value = JSON.stringify({ saved_at: new Date().toISOString(), version, payload: normalizedPayload });
    const bytes = snapshotBytes(value);
    if (bytes > LOCAL_SNAPSHOT_MAX_BYTES) return;
    pruneLocalSnapshots(storageKey, bytes);
    localStorage.setItem(storageKey, value);
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
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [active, setActive] = useState<NavKey>("dashboard");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === "true");
  const [discoverResetToken, setDiscoverResetToken] = useState(0);
  const [selectedDownloader, setSelectedDownloader] = useState("qb1");
  const [accountDialogOpen, setAccountDialogOpen] = useState(false);
  const [favoriteItems, setFavoriteItems] = useState<any[]>([]);
  const [favoritesLoading, setFavoritesLoading] = useState(false);
  const [favoritesError, setFavoritesError] = useState("");
  const [favoriteBusyKeys, setFavoriteBusyKeys] = useState<Set<string>>(() => new Set());

  useEffect(() => {
    api<SetupStatus>("/api/setup/status").then(setSetupStatus);
    if (getToken()) {
      api<User>("/api/auth/me").then(setUser).catch(() => setToken(null));
    }
  }, []);

  useEffect(() => {
    if (user?.must_change_credentials) setAccountDialogOpen(true);
  }, [user?.must_change_credentials]);

  useEffect(() => {
    let alive = true;
    if (!user) {
      setFavoriteItems([]);
      setFavoritesError("");
      return () => { alive = false; };
    }
    setFavoritesLoading(true);
    setFavoritesError("");
    api<any>("/api/favorites")
      .then((value) => {
        if (alive) setFavoriteItems(Array.isArray(value?.items) ? value.items : []);
      })
      .catch((err) => {
        if (alive) setFavoritesError((err as Error).message);
      })
      .finally(() => {
        if (alive) setFavoritesLoading(false);
      });
    return () => { alive = false; };
  }, [user?.id]);

  useEffect(() => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  if (setupStatus === null) return <Splash />;
  if (!setupStatus.initialized) return <SetupPage onDone={(nextUser) => { setSetupStatus({ initialized: true }); setUser(nextUser); }} />;
  if (!user) return <LoginPage onLogin={setUser} />;

  const visibleNav = navItems.filter((item) => !item.admin || user.role === "admin");
  const ActivePage = pages[active];

  function openNav(key: NavKey) {
    if (key === "discover" && active === "discover") {
      setDiscoverResetToken((value) => value + 1);
    }
    setActive(key);
  }

  function isFavorite(item: any) {
    const key = favoriteMediaKey(item);
    return Boolean(key && favoriteItems.some((favorite) => favoriteMediaKey(favorite) === key));
  }

  function isFavoriteBusy(item: any) {
    const key = favoriteMediaKey(item);
    return Boolean(key && favoriteBusyKeys.has(key));
  }

  async function toggleFavorite(item: any) {
    const key = favoriteMediaKey(item);
    if (!key || favoriteBusyKeys.has(key)) return;
    const previous = favoriteItems;
    const existing = previous.find((favorite) => favoriteMediaKey(favorite) === key);
    setFavoriteBusyKeys((current) => new Set(current).add(key));
    setFavoritesError("");
    if (existing) {
      setFavoriteItems((current) => current.filter((favorite) => favoriteMediaKey(favorite) !== key));
    } else {
      setFavoriteItems((current) => [{ ...item, favorited_at: new Date().toISOString() }, ...current]);
    }
    try {
      if (existing) {
        await api(`/api/favorites/${item.media_type}/${mediaTmdbId(item)}`, { method: "DELETE" });
      } else {
        const response = await api<any>("/api/favorites", { method: "POST", body: JSON.stringify({ media: item }) });
        setFavoriteItems((current) => [response.item, ...current.filter((favorite) => favoriteMediaKey(favorite) !== key)]);
      }
    } catch (err) {
      setFavoriteItems(previous);
      setFavoritesError((err as Error).message);
    } finally {
      setFavoriteBusyKeys((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  }

  return (
    <div className={sidebarCollapsed ? "app-shell sidebar-collapsed" : "app-shell"}>
      <aside className="sidebar">
        <BrandLogo subtitle="媒体中枢" />
        <button
          type="button"
          className="sidebar-toggle"
          onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
          aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
          title={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
        <nav>
          {visibleNav.map((item) => {
            const Icon = item.icon;
            return (
              <button className={active === item.key ? "nav-item active" : "nav-item"} onClick={() => openNav(item.key)} key={item.key} title={item.label}>
                <Icon size={20} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <button className="nav-item logout" onClick={() => { setAccountDialogOpen(false); setToken(null); setUser(null); }} title="退出登录">
          <LogOut size={20} />
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
          <button
            type="button"
            className={user.must_change_credentials ? "user-pill needs-credential-update" : "user-pill"}
            onClick={() => setAccountDialogOpen(true)}
            title="修改登录账号和密码"
          >
            <ShieldCheck size={16} />
            {user.username} / {user.role === "admin" ? "管理员" : "用户"}
          </button>
        </header>
        <FavoritesContext.Provider value={{ items: favoriteItems, loading: favoritesLoading, error: favoritesError, isFavorite, isBusy: isFavoriteBusy, toggle: toggleFavorite }}>
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
        </FavoritesContext.Provider>
      </main>
      <nav className="bottom-nav">
        {visibleNav.filter((item) => ["discover", "favorites", "dashboard", "downloads", "notifications", "settings", "diagnostics"].includes(item.key)).map((item) => {
          const Icon = item.icon;
          return (
            <button className={active === item.key ? "active" : ""} onClick={() => openNav(item.key)} key={item.key} aria-label={item.label}>
              <Icon size={20} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>
      {accountDialogOpen && <AccountCredentialsDialog user={user} onClose={() => setAccountDialogOpen(false)} onUpdated={(nextUser) => setUser(nextUser)} />}
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
        <img className="brand-popcorn" src="/popcorn-icon.png" alt="" />
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

function AccountCredentialsDialog({ user, onClose, onUpdated }: { user: User; onClose: () => void; onUpdated: (user: User) => void }) {
  const [username, setUsername] = useState(user.username);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setSaving(true);
    try {
      const result = await api<{ access_token: string; user: User }>("/api/auth/account", {
        method: "PUT",
        body: JSON.stringify({ username, current_password: currentPassword, new_password: newPassword })
      });
      setToken(result.access_token);
      onUpdated(result.user);
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return <div className="app-dialog-backdrop" role="presentation" onMouseDown={onClose}>
    <form className="app-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-dialog-title" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
      <span className="app-dialog-icon"><ShieldCheck size={20} /></span>
      <h3 id="account-dialog-title">修改登录账号</h3>
      {user.must_change_credentials && <p className="account-dialog-note">当前正在使用初始管理员账号，请及时修改用户名和密码。</p>}
      <div className="form-grid">
        <label>登录用户名<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
        <label>当前密码<input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" /></label>
        <label>新密码<input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" minLength={8} /></label>
        <label>确认新密码<input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" minLength={8} /></label>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="app-dialog-actions">
        <button type="button" onClick={onClose} disabled={saving}>取消</button>
        <button className="primary" disabled={saving || username.trim().length < 3 || currentPassword.length === 0 || newPassword.length < 8 || confirmPassword.length < 8}>{saving ? "保存中..." : "保存账号"}</button>
      </div>
    </form>
  </div>;
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
    qbs_updated_at: realtime.updated_at ?? current.qbs_updated_at,
    updated_at: current.updated_at,
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
  const entryRefreshStarted = useRef(false);

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
    if (!data || entryRefreshStarted.current) return;
    const refreshOnEntry = () => {
      if (document.hidden || entryRefreshStarted.current) return;
      entryRefreshStarted.current = true;
      void loadDashboard("/api/dashboard?refresh=true", true);
    };
    const timer = window.setTimeout(refreshOnEntry, 150);
    document.addEventListener("visibilitychange", refreshOnEntry);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", refreshOnEntry);
    };
  }, [data]);

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

  const enabledDownloaders = data.qbs.filter((qb: any) => qb.configured !== false && qb.enabled !== false);
  const onlineDownloaders = enabledDownloaders.filter((qb: any) => qb.online).length;

  return (
    <div className="grid-page dashboard-page">
      <div className="dashboard-composite">
        <section className="dashboard-surface dashboard-summary-surface">
          <div className="dashboard-section-heading">
            <div className="dashboard-section-title">
              <span className="dashboard-section-icon"><Gauge size={18} /></span>
              <div><span className="dashboard-eyebrow">运行概览</span><h2>核心指标</h2></div>
            </div>
            <span className="dashboard-section-note">吞吐、任务与存储</span>
          </div>
          <div className="metric-grid dashboard-overview">
            <Metric tone="download" icon={Download} title="总下载速度" value={formatSpeed(data.overview.total_download_speed)} source="" />
            <Metric tone="upload" icon={Upload} title="总上传速度" value={formatSpeed(data.overview.total_upload_speed)} source="" />
            <Metric tone="activity" icon={Activity} title="活跃上传/下载" value={<ActiveTransferCounts upload={data.overview.upload_tasks ?? 0} download={data.overview.download_tasks ?? 0} />} source="" />
            <StorageMetric overview={data.overview} />
          </div>
        </section>
        <section className="dashboard-surface dashboard-downloaders">
          <div className="dashboard-section-heading">
            <div className="dashboard-section-title">
              <span className="dashboard-section-icon downloader"><HardDrive size={18} /></span>
              <div><span className="dashboard-eyebrow">任务节点</span><h2>下载器</h2></div>
            </div>
            <span className={onlineDownloaders === enabledDownloaders.length && enabledDownloaders.length ? "dashboard-section-note online" : "dashboard-section-note"}>{onlineDownloaders}/{enabledDownloaders.length} 在线</span>
          </div>
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
        updatedAt={data._preload?.cached_at || data.updated_at}
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
    <section className="panel mteam-panel">
      <div className="mteam-panel-header">
        <div className="mteam-panel-title">
          <span className="dashboard-section-icon mteam"><Database size={18} /></span>
          <div><span className="dashboard-eyebrow">站点数据</span><h2><a className="mteam-title-link" href="https://kp.m-team.cc/index" target="_blank" rel="noreferrer">M-Team 用户概览</a></h2></div>
        </div>
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
        <InfoTile className="secondary" icon={UserRound} label="用户等级" value={mteam.user_level ?? "User"} />
        <InfoTile className="emphasis" icon={Coins} label="魔力值" value={numberLabel(mteam.bonus)} delta={deltas.bonus} />
        <InfoTile className="emphasis" icon={Percent} label="分享率" value={numberLabel(mteam.ratio, 3)} delta={deltas.ratio} negative={String(deltas.ratio ?? "").includes("-")} />
        <InfoTile className="positive" icon={Upload} label="总上传量" value={formatBytesFixed(mteam.upload_total, 2)} delta={deltas.upload} />
        <InfoTile className="warm" icon={Download} label="总下载量" value={formatBytes(mteam.download_total)} delta={deltas.download} />
        <InfoTile className="activity" icon={Activity} label="当前活跃上传/下载" value={<ActiveTransferCounts upload={mteam.active_uploads ?? 0} download={mteam.active_downloads ?? 0} />} />
        <InfoTile className="secondary" icon={Database} label="总做种体积" value={formatBytes(mteam.seed_size ?? 0)} delta={deltas.seed_size} negative={String(deltas.seed_size ?? "").includes("-")} />
        <InfoTile className="subtle" icon={CalendarDays} label="加入时间" value={mteam.joined_at ?? "-"} delta={joinedDurationLabel(mteam.joined_at)} />
      </div>
      <div className="traffic-chart">
        <div className="traffic-chart-header">
          <h3>历史流量</h3>
          <div className="traffic-dimension-tools" aria-label="统计维度">
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
  const date = parseSystemDate(raw);
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
  const date = parseSystemDate(value);
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
  const initialFilterSession = useMemo(() => readLocalSnapshot<DiscoverFilterSession>("discover.filter.session"), []);
  const { data, error, loading, setData } = useLoad<any>(() => api("/api/discover/lists?cached=true"), [], initialDiscover, false);
  const [query, setQuery] = useState("");
  const [media, setMedia] = useState<any[]>([]);
  const [torrents, setTorrents] = useState<any[]>([]);
  const [mediaSearching, setMediaSearching] = useState(false);
  const [torrentSearching, setTorrentSearching] = useState(false);
  const [mediaSearchError, setMediaSearchError] = useState("");
  const [torrentSearchError, setTorrentSearchError] = useState("");
  const searching = mediaSearching || torrentSearching;
  const [discoverMode, setDiscoverMode] = useState<DiscoverMode>("home");
  const [browseMode, setBrowseMode] = useState<DiscoverBrowseMode>("casual");
  const [searchHistory, setSearchHistory] = useState<string[]>(() => readSearchHistory());
  const [expandedDiscoverTitle, setExpandedDiscoverTitle] = useState<string | null>(null);
  const [selectedMedia, setSelectedMedia] = useState<any | null>(null);
  const [selectedPerson, setSelectedPerson] = useState<any | null>(null);
  const [detailHistory, setDetailHistory] = useState<DiscoverDetailHistoryEntry[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [resourceSort, setResourceSort] = useState("seeders");
  const [resourceSortDirection, setResourceSortDirection] = useState<"asc" | "desc">("desc");
  const [discoverFilters, setDiscoverFilters] = useState<DiscoverFilters>(() => initialFilterSession?.filters ?? DEFAULT_DISCOVER_FILTERS);
  const [filterPayload, setFilterPayload] = useState<any | null>(() => initialFilterSession?.payload ?? null);
  const [filterItems, setFilterItems] = useState<any[]>(() => Array.isArray(initialFilterSession?.items) ? initialFilterSession.items : []);
  const [filterLoading, setFilterLoading] = useState(false);
  const [filterLoadingMore, setFilterLoadingMore] = useState(false);
  const [filterError, setFilterError] = useState("");
  const [filterNextPage, setFilterNextPage] = useState<number | null>(() => initialFilterSession?.nextPage ?? null);
  const [filterHasMore, setFilterHasMore] = useState(() => Boolean(initialFilterSession?.hasMore));
  const filterRequestId = useRef(0);
  const filterKeyRef = useRef("");
  const filterLoadedKeyRef = useRef(initialFilterSession?.key ?? "");
  const filterSentinelRef = useRef<HTMLDivElement | null>(null);
  const filterLoadingPageRef = useRef<number | null>(null);
  const detailReturnScrollRef = useRef<number | null>(null);
  const detailRequestId = useRef(0);
  const searchRequestId = useRef(0);
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
    const key = discoverFilterKey(discoverFilters);
    if (!filterPayload || filterLoadedKeyRef.current !== key) return;
    writeLocalSnapshot("discover.filter.session", {
      key,
      filters: discoverFilters,
      payload: filterPayload,
      items: filterItems,
      nextPage: filterNextPage,
      hasMore: filterHasMore,
    } satisfies DiscoverFilterSession);
  }, [discoverFilters, filterPayload, filterItems, filterNextPage, filterHasMore]);

  useEffect(() => {
    resetDiscoverHome();
  }, [resetToken]);

  useEffect(() => {
    if (discoverMode !== "home" || browseMode !== "filter") return;
    const filterKey = discoverFilterKey(discoverFilters);
    filterKeyRef.current = filterKey;
    if (filterLoadedKeyRef.current === filterKey && filterPayload) return;
    const timer = window.setTimeout(() => {
      loadDiscoverFilter(discoverFilters, { page: 1, pages: 1, cached: true }).then((result) => {
        if (filterKeyRef.current === filterKey && result?._preload?.preloaded) {
          window.setTimeout(() => {
            if (!document.hidden && filterKeyRef.current === filterKey) {
              loadDiscoverFilter(discoverFilters, { page: 1, pages: 1, refresh: true, silent: true }).catch(() => undefined);
            }
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

  useEffect(() => {
    if (selectedMedia || selectedPerson || expandedDiscoverTitle || detailReturnScrollRef.current === null) return;
    const scrollTop = detailReturnScrollRef.current;
    const firstFrame = window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        window.scrollTo(0, scrollTop);
        detailReturnScrollRef.current = null;
      });
    });
    return () => window.cancelAnimationFrame(firstFrame);
  }, [selectedMedia, selectedPerson, expandedDiscoverTitle]);

  function resetDiscoverHome() {
    detailRequestId.current += 1;
    searchRequestId.current += 1;
    setQuery("");
    setMedia([]);
    setTorrents([]);
    setMediaSearching(false);
    setTorrentSearching(false);
    setMediaSearchError("");
    setTorrentSearchError("");
    setDiscoverMode("home");
    setBrowseMode("casual");
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailHistory([]);
    setDetailLoading(false);
    setDetailError("");
    setFilterError("");
  }

  function switchBrowseMode(nextMode: DiscoverBrowseMode) {
    detailRequestId.current += 1;
    searchRequestId.current += 1;
    setBrowseMode(nextMode);
    setDiscoverMode("home");
    setMedia([]);
    setTorrents([]);
    setMediaSearching(false);
    setTorrentSearching(false);
    setMediaSearchError("");
    setTorrentSearchError("");
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailHistory([]);
    setDetailLoading(false);
    setDetailError("");
  }

  function enterSearchMode() {
    if (discoverMode !== "dual") {
      detailRequestId.current += 1;
      setDiscoverMode("dual");
      setSelectedMedia(null);
      setSelectedPerson(null);
      setDetailHistory([]);
      setDetailLoading(false);
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
    const requestId = searchRequestId.current + 1;
    searchRequestId.current = requestId;
    setQuery(keyword);
    setMediaSearching(true);
    setTorrentSearching(true);
    setMediaSearchError("");
    setTorrentSearchError("");
    setDiscoverMode("dual");
    detailRequestId.current += 1;
    setExpandedDiscoverTitle(null);
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailHistory([]);
    setDetailLoading(false);
    setMedia([]);
    setTorrents([]);
    setSearchHistory(writeSearchHistory(keyword));
    const mediaRequest = api<{ items: any[] }>(`/api/search/media?q=${encodeURIComponent(keyword)}`)
      .then((result) => {
        if (searchRequestId.current === requestId) setMedia(Array.isArray(result.items) ? result.items : []);
      })
      .catch((err) => {
        if (searchRequestId.current === requestId) {
          setMedia([]);
          setMediaSearchError((err as Error).message);
        }
      })
      .finally(() => {
        if (searchRequestId.current === requestId) setMediaSearching(false);
      });
    const torrentRequest = api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(keyword)}`)
      .then((result) => {
        if (searchRequestId.current === requestId) setTorrents(Array.isArray(result.items) ? result.items : []);
      })
      .catch((err) => {
        if (searchRequestId.current === requestId) {
          setTorrents([]);
          setTorrentSearchError((err as Error).message);
        }
      })
      .finally(() => {
        if (searchRequestId.current === requestId) setTorrentSearching(false);
      });
    await Promise.allSettled([mediaRequest, torrentRequest]);
  }

  async function openMediaDetail(item: any) {
    const tmdbId = mediaTmdbId(item);
    if (!tmdbId || !item.media_type) return;
    const currentDetail: DiscoverDetailHistoryEntry | null = selectedPerson
      ? { kind: "person", item: selectedPerson, scrollTop: window.scrollY }
      : selectedMedia
        ? { kind: "media", item: selectedMedia, scrollTop: window.scrollY }
        : null;
    if (currentDetail) {
      setDetailHistory((current) => [...current, currentDetail].slice(-20));
    } else {
      detailReturnScrollRef.current = window.scrollY;
      setDetailHistory([]);
    }
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setSelectedPerson(null);
    setSelectedMedia(item);
    setExpandedDiscoverTitle(null);
    setDetailLoading(true);
    setDetailError("");
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
    try {
      const detail = await api<any>(`/api/tmdb/media/${item.media_type}/${tmdbId}`);
      if (detailRequestId.current === requestId) setSelectedMedia(detail);
    } catch (err) {
      if (detailRequestId.current === requestId) setDetailError((err as Error).message);
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  }

  async function openPersonDetail(person: any) {
    const personId = person?.person_id ?? person?.id;
    if (!personId) return;
    const currentDetail: DiscoverDetailHistoryEntry | null = selectedPerson
      ? { kind: "person", item: selectedPerson, scrollTop: window.scrollY }
      : selectedMedia
        ? { kind: "media", item: selectedMedia, scrollTop: window.scrollY }
        : null;
    if (currentDetail) setDetailHistory((current) => [...current, currentDetail].slice(-20));
    const requestId = detailRequestId.current + 1;
    detailRequestId.current = requestId;
    setSelectedMedia(null);
    setSelectedPerson(person);
    setDetailLoading(true);
    setDetailError("");
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
    try {
      const detail = await api<any>(`/api/tmdb/person/${personId}`);
      if (detailRequestId.current === requestId) setSelectedPerson(detail);
    } catch (err) {
      if (detailRequestId.current === requestId) setDetailError((err as Error).message);
    } finally {
      if (detailRequestId.current === requestId) setDetailLoading(false);
    }
  }

  function returnToPreviousDetail() {
    const previous = detailHistory[detailHistory.length - 1];
    if (!previous) {
      closeMediaDetail();
      return;
    }
    detailRequestId.current += 1;
    setDetailHistory((current) => current.slice(0, -1));
    setSelectedMedia(previous.kind === "media" ? previous.item : null);
    setSelectedPerson(previous.kind === "person" ? previous.item : null);
    setDetailLoading(false);
    setDetailError("");
    window.requestAnimationFrame(() => window.scrollTo(0, previous.scrollTop));
  }

  function closeMediaDetail() {
    detailRequestId.current += 1;
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailHistory([]);
    setDetailLoading(false);
    setDetailError("");
  }

  async function searchMTeamFromMedia(item: any) {
    const keyword = String(item?.title || item?.original_title || "").trim();
    if (!keyword) return;
    const requestId = searchRequestId.current + 1;
    searchRequestId.current = requestId;
    setQuery(keyword);
    detailRequestId.current += 1;
    setSelectedMedia(null);
    setSelectedPerson(null);
    setDetailHistory([]);
    setDetailLoading(false);
    setExpandedDiscoverTitle(null);
    setMedia([]);
    setTorrents([]);
    setBrowseMode("casual");
    setDiscoverMode("mteam");
    setMediaSearching(false);
    setTorrentSearching(true);
    setMediaSearchError("");
    setTorrentSearchError("");
    try {
      setSearchHistory(writeSearchHistory(keyword));
      const result = await api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(keyword)}`);
      if (searchRequestId.current === requestId) setTorrents(Array.isArray(result.items) ? result.items : []);
    } catch (err) {
      if (searchRequestId.current === requestId) setTorrentSearchError((err as Error).message);
    } finally {
      if (searchRequestId.current === requestId) setTorrentSearching(false);
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
      filterLoadedKeyRef.current = discoverFilterKey(filters);
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
        filterLoadedKeyRef.current = discoverFilterKey(filters);
        setFilterPayload((current: any) => append ? { ...(current ?? {}), ...result, options: result?.options ?? current?.options } : { ...result, options: result?.options ?? current?.options });
        setFilterNextPage(result?.next_page ?? null);
        setFilterHasMore(Boolean(result?.next_page));
        setFilterItems((current) => append
          ? mergeMediaItems(current, result?.items ?? [])
          : (result?.items ?? []));
        if (!append && page === 1) writeLocalSnapshot(snapshotKey, result);
      }
      return result;
    } catch (err) {
      if (filterRequestId.current === requestId) {
        if (!append && !options.silent) {
          setFilterPayload(null);
          setFilterItems([]);
          setFilterNextPage(null);
          setFilterHasMore(false);
        }
        if (!options.silent) setFilterError((err as Error).message);
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
      {selectedPerson ? (
        <PersonDetailPage person={selectedPerson} loading={detailLoading} error={detailError} canGoBack={detailHistory.length > 0} onBack={returnToPreviousDetail} onExit={closeMediaDetail} onMediaSelect={openMediaDetail} />
      ) : selectedMedia ? (
        <MediaDetailPage item={selectedMedia} loading={detailLoading} error={detailError} canGoBack={detailHistory.length > 0} onBack={returnToPreviousDetail} onExit={closeMediaDetail} onPersonSelect={openPersonDetail} onMediaSelect={openMediaDetail} onMTeamSearch={searchMTeamFromMedia} mteamSearching={torrentSearching} />
      ) : expandedDiscoverList ? (
        <DiscoverCollectionPage title={expandedDiscoverList.title} items={expandedDiscoverList.items} onBack={() => setExpandedDiscoverTitle(null)} onSelect={openMediaDetail} />
      ) : (
        <>
          {discoverMode === "dual" || discoverMode === "mteam" ? (
            <div className="discover-search-results">
              {discoverMode === "dual" && (
                <div className="search-results-toolbar">
                  <button type="button" onClick={resetDiscoverHome}><ArrowLeft size={17} />返回发现</button>
                  {query && <span>{searching ? `正在搜索“${query}”` : `“${query}”的搜索结果`}</span>}
                </div>
              )}
              {discoverMode === "dual" && <MediaSearchResults items={media} onSelect={openMediaDetail} loading={mediaSearching} error={mediaSearchError} />}
              <MTeamResourceResults
                items={sortedTorrents}
                loading={torrentSearching}
                error={torrentSearchError}
                sortBy={resourceSort}
                sortDirection={resourceSortDirection}
                onSortBy={setResourceSort}
                onSortDirection={setResourceSortDirection}
                onBack={discoverMode === "mteam" ? resetDiscoverHome : undefined}
              />
            </div>
          ) : null}
          {discoverMode === "home" && browseMode === "casual" && (
            <>
              {loading && !data && <Panel title="发现"><p>正在从 TMDB 加载片单...</p></Panel>}
              {error && <Panel title="TMDB 获取失败"><p className="error">{error}</p></Panel>}
              {data && !data.configured && <Panel title="需要配置媒体搜索"><p>{data.message}</p><p className="muted">进入“设置”的“媒体搜索”，填写 TMDB API Key 或 Bearer Token，保存并启用后再回到发现页。</p></Panel>}
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
    { value: "release_date.desc", label: "首播时间优先" },
    { value: "vote_average.desc", label: "高分优先" },
    { value: "vote_count.desc", label: "讨论热度优先" },
  ];
  const regionOptions = options?.regions ?? [
    { value: "", label: "不限地区" },
    { value: "CN", label: "中国大陆" },
    { value: "HK", label: "中国香港" },
    { value: "TW", label: "中国台湾" },
    { value: "US", label: "美国" },
    { value: "GB", label: "英国" },
    { value: "JP", label: "日本" },
    { value: "KR", label: "韩国" },
    { value: "FR", label: "法国" },
    { value: "DE", label: "德国" },
    { value: "IN", label: "印度" },
    { value: "TH", label: "泰国" },
    { value: "OTHER", label: "其他" },
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
    { value: "1990s", label: "1990年代" },
    { value: "1980s", label: "1980年代" },
    { value: "1970s", label: "1970年代" },
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
  const [addDialogMode, setAddDialogMode] = useState<"link" | "file" | null>(null);

  useEffect(() => {
    if (selectedDownloader && selectedDownloader !== downloader) {
      setAddDialogMode(null);
      setDownloader(selectedDownloader);
    }
  }, [selectedDownloader]);

  useEffect(() => {
    const poll = () => {
      if (!document.hidden) refreshDownloadOverview(downloader);
    };
    poll();
    const timer = window.setInterval(poll, 5000);
    const onVisibilityChange = () => {
      if (!document.hidden) poll();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
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
      {summary && !qb2Locked && (
        <QbSummaryCards
          qb={summary}
          count={items.length}
          onRefresh={manualRefreshDownloadOverview}
          refreshing={refreshingDownload}
          onAddLink={() => setAddDialogMode("link")}
          onAddFile={() => setAddDialogMode("file")}
        />
      )}
      {downloader === "qb2" && qb2Authorized && !qb2Locked && <div className="qb-grant-strip"><span>qB2 隐私授权已开启</span><button type="button" onClick={revokeQb2Grant}>退出授权</button></div>}
      {qb2Locked && <Panel title="qB2 已锁定"><p>私有下载器需要管理员验证。</p><button className="primary" onClick={() => setGrantOpen(true)}><Lock size={16} /> 验证管理员</button></Panel>}
      {error && downloader !== "qb2" && <p className="error">{error}</p>}
      {grantOpen && <AdminGrant onDone={() => { setGrantOpen(false); setQb2Authorized(true); setData(null); refreshDownloadOverview("qb2"); }} />}
      {loading && !visibleData && (!error || qb2Authorized) && <Panel title="下载器"><p>正在读取 qB 真实数据...</p></Panel>}
      {visibleData && !qb2Locked && <QbTorrentTable key={downloader} items={items} downloader={downloader} onChanged={() => { refreshDownloadOverview(); }} />}
      {addDialogMode && !qb2Locked && (
        <DownloadTaskDialog
          key={`${downloader}-${addDialogMode}`}
          downloader={downloader}
          mode={addDialogMode}
          onClose={() => setAddDialogMode(null)}
          onAdded={() => { refreshDownloadOverview(); }}
        />
      )}
    </div>
  );
}

function DownloadTaskDialog({ downloader, mode, onClose, onAdded }: { downloader: string; mode: "link" | "file"; onClose: () => void; onAdded: () => void }) {
  const [links, setLinks] = useState("");
  const [torrentFile, setTorrentFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const canSubmit = mode === "link" ? Boolean(links.trim()) : Boolean(torrentFile);

  function chooseTorrentFile(file: File | null) {
    setError("");
    if (!file) {
      setTorrentFile(null);
      return;
    }
    if (!file.name.toLowerCase().endsWith(".torrent")) {
      setTorrentFile(null);
      setError("请选择扩展名为 .torrent 的种子文件。");
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      setTorrentFile(null);
      setError("种子文件不能超过 20 MB。");
      return;
    }
    setTorrentFile(file);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || busy) return;
    setBusy(true);
    setError("");
    try {
      if (mode === "link") {
        await api(`/api/qb/${downloader}/torrents`, {
          method: "POST",
          body: JSON.stringify({ payload: { urls: links.trim() } }),
        });
      } else if (torrentFile) {
        await api(`/api/qb/${downloader}/torrents/file`, {
          method: "POST",
          headers: {
            "Content-Type": torrentFile.type || "application/x-bittorrent",
            "X-Torrent-Filename": encodeURIComponent(torrentFile.name),
          },
          body: torrentFile,
        });
      }
      setSubmitted(true);
      onAdded();
    } catch (error) {
      setError(normalizeApiErrorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  if (submitted) {
    return (
      <div className="app-dialog-backdrop" role="presentation" onMouseDown={onClose}>
        <div className="app-dialog download-task-dialog download-task-success" role="dialog" aria-modal="true" aria-labelledby="download-task-success-title" onMouseDown={(event) => event.stopPropagation()}>
          <span className="download-dialog-success-icon"><Check size={24} /></span>
          <h3 id="download-task-success-title">下载任务已添加</h3>
          <p>任务已成功提交到 {downloaderShortLabel(downloader)}，下载列表会自动更新。</p>
          <button type="button" className="primary" onClick={onClose}>完成</button>
        </div>
      </div>
    );
  }

  return (
    <div className="app-dialog-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <form className="app-dialog download-task-dialog" role="dialog" aria-modal="true" aria-labelledby="download-task-dialog-title" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="download-task-dialog-heading">
          <span className="download-task-dialog-icon">{mode === "link" ? <Link2 size={20} /> : <UploadCloud size={20} />}</span>
          <div>
            <h3 id="download-task-dialog-title">{mode === "link" ? "通过链接下载" : "通过种子文件下载"}</h3>
            <p>添加到 {downloaderShortLabel(downloader)}</p>
          </div>
          <button className="download-dialog-close" type="button" onClick={onClose} disabled={busy} aria-label="关闭">×</button>
        </div>
        {mode === "link" ? (
          <label className="download-link-field">
            <span>下载链接</span>
            <textarea autoFocus value={links} onChange={(event) => { setLinks(event.target.value); setError(""); }} rows={5} placeholder={"粘贴 magnet、HTTP 或 HTTPS 种子链接\n每行一条，最多可同时添加 20 条"} disabled={busy} />
          </label>
        ) : (
          <div
            className={torrentFile ? "download-torrent-dropzone selected" : "download-torrent-dropzone"}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => { event.preventDefault(); chooseTorrentFile(event.dataTransfer.files[0] ?? null); }}
          >
            <span className="download-dropzone-icon"><UploadCloud size={26} /></span>
            <div><strong>{torrentFile?.name || "选择一个 .torrent 文件"}</strong><span>{torrentFile ? `${formatBytes(torrentFile.size)} · 已准备就绪` : "也可以将文件拖放到这里"}</span></div>
            <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}>{torrentFile ? "重新选择" : "浏览文件"}</button>
            <input ref={fileInput} type="file" accept=".torrent,application/x-bittorrent" onChange={(event) => chooseTorrentFile(event.target.files?.[0] ?? null)} hidden />
          </div>
        )}
        {error && <p className="download-dialog-error"><CircleAlert size={15} />{error}</p>}
        <div className="download-task-dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>取消</button>
          <button className="primary" type="submit" disabled={!canSubmit || busy}>{busy ? <RefreshCw className="spinning" size={16} /> : <Download size={16} />}{busy ? "正在添加..." : "开始下载"}</button>
        </div>
      </form>
    </div>
  );
}

function QbSummaryCards({ qb, count, onRefresh, refreshing, onAddLink, onAddFile }: { qb: any; count: number; onRefresh: () => void; refreshing: boolean; onAddLink: () => void; onAddFile: () => void }) {
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
        footerAction={(
          <span className="summary-card-quick-actions" aria-label="添加下载任务">
            <button type="button" onClick={onAddLink} title="通过链接添加" aria-label="通过链接添加下载任务"><Link2 size={15} /></button>
            <button type="button" onClick={onAddFile} title="添加种子文件" aria-label="通过种子文件添加下载任务"><UploadCloud size={15} /></button>
          </span>
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

function SummaryCard({ icon: Icon, label, value, helper, tone, action, footerAction }: { icon: typeof Film; label: string; value: ReactNode; helper: string; tone: "mint" | "teal" | "orange" | "blue"; action?: ReactNode; footerAction?: ReactNode }) {
  return (
    <article className={`summary-card ${tone}`}>
      <div className="summary-card-icon"><Icon size={24} /></div>
      <div>
        <span>{label}{action}</span>
        <strong>{value}</strong>
        <span className="summary-card-helper"><small>{helper}</small>{footerAction}</span>
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
  const [deleteTorrentConfirm, setDeleteTorrentConfirm] = useState<any | null>(null);
  const [deletingTorrent, setDeletingTorrent] = useState(false);
  const [torrentAction, setTorrentAction] = useState<"resume" | "pause" | "">("");
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
    setContextMenu(null);
    setTorrentAction(action);
    setDetailError("");
    try {
      const result = await api<any>(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}/${action}`, { method: "POST", body: JSON.stringify({ payload: {} }) });
      if (!result?.verified) throw new Error(`qB 未能确认任务已${action === "pause" ? "暂停" : "启动"}`);
      onChanged();
      await loadDetail(item.hash);
    } catch (err) {
      setDetailError((err as Error).message);
      onChanged();
    } finally {
      setTorrentAction("");
    }
  }

  function requestDeleteTorrent(item: any) {
    setContextMenu(null);
    setDeleteTorrentConfirm(item);
  }

  async function deleteTorrent() {
    if (!deleteTorrentConfirm) return;
    const item = deleteTorrentConfirm;
    setDeletingTorrent(true);
    setDetailError("");
    try {
      const confirmResult = await api<{ confirm_token: string }>(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}/delete-confirm`, { method: "POST" });
      await api(`/api/qb/${downloader}/torrents/${encodeURIComponent(item.hash)}?confirm_token=${encodeURIComponent(confirmResult.confirm_token)}&delete_files=true`, { method: "DELETE" });
      setDeleteTorrentConfirm(null);
      setDetail(null);
      setSelectedHash("");
      onChanged();
    } catch (err) {
      setDetailError((err as Error).message);
    } finally {
      setDeletingTorrent(false);
    }
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
          <button onClick={() => mutateTorrent(contextMenu.item, "resume")} disabled={Boolean(torrentAction)}>启动</button>
          <button onClick={() => mutateTorrent(contextMenu.item, "pause")} disabled={Boolean(torrentAction)}>暂停</button>
          <button className="danger" onClick={() => requestDeleteTorrent(contextMenu.item)}>删除</button>
        </div>
      )}
      {deleteTorrentConfirm && <div className="app-dialog-backdrop" role="presentation" onMouseDown={() => !deletingTorrent && setDeleteTorrentConfirm(null)}>
        <div className="app-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-torrent-title" onMouseDown={(event) => event.stopPropagation()}>
          <span className="app-dialog-icon"><Trash2 size={20} /></span>
          <h3 id="delete-torrent-title">删除下载任务？</h3>
          <p>将从 {downloaderShortLabel(downloader)} 删除此任务，并永久删除对应的本地文件。此操作不可恢复。</p>
          <div className="field-help compact-help"><strong>{deleteTorrentConfirm.name}</strong></div>
          <div className="app-dialog-actions">
            <button type="button" onClick={() => setDeleteTorrentConfirm(null)} disabled={deletingTorrent}>取消</button>
            <button type="button" className="danger primary" onClick={() => void deleteTorrent()} disabled={deletingTorrent}>{deletingTorrent ? "正在删除..." : "确认删除"}</button>
          </div>
        </div>
      </div>}
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

function NotificationsAssistantPage({ onNavigate }: { onNavigate?: (key: NavKey) => void }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conversationId, setConversationId] = useState(() => {
    const saved = window.sessionStorage.getItem("ptmh.assistant.conversation");
    if (saved) return saved;
    const created = "web-" + Date.now() + "-" + Math.random().toString(36).slice(2, 9);
    window.sessionStorage.setItem("ptmh.assistant.conversation", created);
    return created;
  });
  const [turns, setTurns] = useState<Array<{ role: "user" | "assistant"; content: string; tools?: string[] }>>([]);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const prompt = message.trim();
    if (!prompt || busy) return;
    setBusy(true);
    setError("");
    setTurns((current) => [...current, { role: "user", content: prompt }]);
    setMessage("");
    try {
      const response = await api<any>("/api/assistant/chat", {
        method: "POST",
        body: JSON.stringify({ message: prompt, conversation_id: conversationId }),
      });
      setTurns((current) => [
        ...current,
        {
          role: "assistant",
          content: String(response.reply || "这次没有收到有效回复，请稍后再试。"),
          tools: Array.isArray(response.intent?.tools_used) ? response.intent.tools_used : [],
        },
      ]);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function startNewConversation() {
    const created = "web-" + Date.now() + "-" + Math.random().toString(36).slice(2, 9);
    window.sessionStorage.setItem("ptmh.assistant.conversation", created);
    setConversationId(created);
    setTurns([]);
    setMessage("");
    setError("");
  }

  return (
    <div className="grid-page notifications-page">
      <MemberManagementCard onNavigate={onNavigate} />
      <Panel title="影视中枢 Agent">
        <form className="assistant-box" onSubmit={submit}>
          <div className="notice info">
            <strong>直接告诉我你想做什么</strong>
            <span>可以查询影片和资源、查看 M-Team、qB、NAS 与仪表盘状态，也可以接着追问“第四个结果的完整简介”这类上下文问题。</span>
          </div>
          <div className="assistant-conversation" aria-live="polite">
            {turns.length === 0 && <div className="assistant-empty">还没有消息。试试“帮我找几部高分科幻片”，或者直接聊聊你的观影计划。</div>}
            {turns.map((turn, index) => (
              <div className={"assistant-message " + turn.role} key={turn.role + "-" + index}>
                <strong>{turn.role === "user" ? "你" : "影视中枢"}</strong>
                <AssistantReply reply={turn.content} />
                {turn.role === "assistant" && Boolean(turn.tools?.length) && (
                  <small>本轮调用：{turn.tools!.map((tool) => wechatClawStageLabel(tool)).join(" · ")}</small>
                )}
              </div>
            ))}
            {busy && <div className="assistant-message assistant pending"><strong>影视中枢</strong><span>正在理解并调用需要的功能…</span></div>}
          </div>
          <label>消息
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="例如：查一下沙丘的影视信息；再看看第四个结果的完整简介"
            />
          </label>
          <div className="actions">
            <button className="primary" disabled={busy || !message.trim()}><MessageSquare size={16} />{busy ? "处理中..." : "发送"}</button>
            <button type="button" onClick={startNewConversation} disabled={busy}>新对话</button>
          </div>
          {error && <p className="error">{error}</p>}
        </form>
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
    download_started: { title: "下载开始通知", help: "AI 或微信 claw 记录下载开始，并按成员偏好推送。" },
    download_completed: { title: "下载完成通知", help: "AI 或微信 claw 记录下载完成，并按成员偏好推送。" },
    resource_search: { title: "资源查询提醒", help: "开启后，资源查询完成时会按成员偏好推送。" },
    status_query: { title: "状态查询提醒", help: "开启后，各板块状态查询完成时会按成员偏好推送。" },
    wechat_claw_push: { title: "推送到 WeChat claw", help: "将允许的通知发送给已绑定的微信成员。" },
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

const WECHAT_CLAW_MEMBER_LIMIT = 5;

function isWechatClawMemberLimitError(error: unknown): boolean {
  return error instanceof Error && error.message.includes("最多可添加 5 位微信成员");
}

function MemberLimitDialog({ onClose }: { onClose: () => void }) {
  return (
    <div className="app-dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <div className="app-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="member-limit-title" onMouseDown={(event) => event.stopPropagation()}>
        <span className="app-dialog-icon"><Users size={20} /></span>
        <h3 id="member-limit-title">成员数量已达上限</h3>
        <p>最多可添加 5 位微信成员。如需添加新成员，请先删除一位不再使用的成员。</p>
        <div className="app-dialog-actions">
          <button type="button" className="primary" onClick={onClose}>知道了</button>
        </div>
      </div>
    </div>
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
  const [renameDialog, setRenameDialog] = useState<{ member: any; name: string } | null>(null);
  const [memberLimitDialogOpen, setMemberLimitDialogOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const items = Array.isArray(bindings.data?.items) ? bindings.data.items : [];
  const memberLimit = Number(bindings.data?.limit) || WECHAT_CLAW_MEMBER_LIMIT;
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
    if (items.length >= memberLimit) {
      setMemberLimitDialogOpen(true);
      return;
    }
    setBusy("add");
    setError("");
    try {
      const member = await api<any>("/api/admin/wechat-claw/bindings", { method: "POST", body: JSON.stringify({}) });
      await bindings.reload();
      setSelectedId(member.id);
    } catch (err) {
      if (isWechatClawMemberLimitError(err)) setMemberLimitDialogOpen(true);
      else setError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  function editMember(member: any) {
    setRenameDialog({ member, name: member.display_name });
  }

  async function saveMemberName() {
    if (!renameDialog) return;
    const name = renameDialog.name.trim();
    if (!name || name === renameDialog.member.display_name) {
      setRenameDialog(null);
      return;
    }
    const saved = await update(renameDialog.member, { display_name: name });
    if (saved) setRenameDialog(null);
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
        <div className="member-selection-row">
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
          {selected && <div className="member-selection-actions">
            <button type="button" onClick={() => editMember(selected)} disabled={busy !== ""}>修改成员名称</button>
            <button type="button" className="danger" onClick={() => requestDelete(selected)} disabled={busy !== ""}>删除成员</button>
            <button type="button" className="primary" onClick={() => void applyPreferences()} disabled={busy !== ""}>{busy === String(selected.id) ? "应用中..." : "应用"}</button>
          </div>}
        </div>
        {selected && (
          <div className="member-notification-settings">
            <div className="member-settings-heading"><div><h3>{selected.display_name}的成员设置</h3><p className="muted">模块异常按小时采集结果计算：连续多个小时没有拉取到数据，才会推送。</p></div></div>
            <div className="member-notification-options">
              {([
                ["qb1_download_started", "qB1 下载开始"],
                ["qb1_download_completed", "qB1 下载完成"],
                ["qb2_download_started", "qB2 下载开始"],
                ["qb2_download_completed", "qB2 下载完成"],
                ["qb3_download_started", "qB3 下载开始"],
                ["qb3_download_completed", "qB3 下载完成"],
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
                  <small>{item.created_at ? formatSystemDateTime(item.created_at) : ""}</small>
                </div>
              ))}
              {!selectedInteractions.length && <p className="muted">手机端发起交互后，这里会保留最近 5 条的耗时与失败阶段。</p>}
            </div>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        {renameDialog && <div className="app-dialog-backdrop" role="presentation" onMouseDown={() => setRenameDialog(null)}>
          <form className="app-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="rename-member-title" onMouseDown={(event) => event.stopPropagation()} onSubmit={(event) => { event.preventDefault(); void saveMemberName(); }}>
            <span className="app-dialog-icon"><UserRound size={20} /></span>
            <h3 id="rename-member-title">修改成员名称</h3>
            <label>成员名称
              <input autoFocus value={renameDialog.name} onChange={(event) => setRenameDialog((current) => current ? { ...current, name: event.target.value } : null)} maxLength={80} />
            </label>
            <div className="app-dialog-actions">
              <button type="button" onClick={() => setRenameDialog(null)}>取消</button>
              <button type="submit" className="primary" disabled={busy !== ""}>保存</button>
            </div>
          </form>
        </div>}
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
        {memberLimitDialogOpen && <MemberLimitDialog onClose={() => setMemberLimitDialogOpen(false)} />}
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
  const integrations = useLoad<any>(() => user.role === "admin" ? api("/api/admin/integrations") : Promise.resolve(null), [user.role]);
  const { data, error, loading, reload } = integrations;
  const storage = useLoad<any>(() => user.role === "admin" ? api("/api/admin/storage/status") : Promise.resolve(null), [user.role]);
  const wechatBindings = useLoad<any>(() => user.role === "admin" ? api("/api/admin/wechat-claw/bindings") : Promise.resolve(null), [user.role]);
  const [activeStep, setActiveStep] = useState(() => {
    const saved = window.sessionStorage.getItem("ptmh.settings.activeStep") || "storage";
    if (["qb1", "qb2", "qb3"].includes(saved)) return "downloaders";
    return saved === "tmdb" ? "media_search" : saved;
  });
  if (user.role !== "admin") return <div className="grid-page"><PersonalWechatClawCard /></div>;
  if (!data) return (
    <Panel title="设置">
      {loading ? (
        <p>正在加载设置...</p>
      ) : (
        <div className="integration">
          <p className="error">{error || "设置数据加载失败，请稍后重试。"}</p>
          <div className="actions">
            <button type="button" className="primary" onClick={() => reload().catch(() => undefined)}>重新加载</button>
          </div>
        </div>
      )}
    </Panel>
  );

  const providers = data.providers.filter((provider: any) => ["mteam", "qb1", "qb2", "qb3", "tmdb", "ai", "wechat_claw"].includes(provider.provider));
  const providerById = Object.fromEntries(providers.map((provider: any) => [provider.provider, provider]));
  const unreadableProviders = providers.filter((provider: any) => provider.configuration_unreadable);
  const settingsProviderNames: Record<string, string> = { mteam: "M-Team", qb1: "qB1", qb2: "qB2", qb3: "qB3", tmdb: "TMDB", ai: "AI", wechat_claw: "WeChat claw" };
  const wechatClawConnected = Array.isArray(wechatBindings.data?.items) && wechatBindings.data.items.some((binding: any) => binding.connected);
  const steps = [
    { id: "storage", label: "NAS 存储", ready: Boolean(storage.data?.nas_storage_readable) },
    { id: "mteam", label: "M-Team", ready: providerStepReady(providerById.mteam) },
    { id: "downloaders", label: "下载器", ready: ["qb1", "qb2", "qb3"].every((id) => providerStepReady(providerById[id])) },
    { id: "media_search", label: "媒体搜索", ready: providerStepReady(providerById.tmdb) },
    { id: "ai", label: "AI", ready: providerStepReady(providerById.ai) },
    { id: "wechat_claw", label: "WeChat claw", ready: providerStepReady(providerById.wechat_claw) && wechatClawConnected },
  ];
  const activeProvider = providerById[activeStep];

  function selectStep(id: string) {
    setActiveStep(id);
    window.sessionStorage.setItem("ptmh.settings.activeStep", id);
    window.scrollTo(0, 0);
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
  }

  return (
    <div className="grid-page settings-flow">
      <Panel title="配置向导">
        {unreadableProviders.length > 0 && (
          <p className="error">部分配置由另一套加密密钥保存，当前实例无法读取。请重新填写并保存：{unreadableProviders.map((provider: any) => settingsProviderNames[provider.provider] ?? provider.provider).join("、")}。</p>
        )}
        <div className="settings-stepper" aria-label="配置步骤">
          {steps.map((step, index) => <button type="button" key={step.id} className={activeStep === step.id ? "settings-step active" : "settings-step"} onClick={() => selectStep(step.id)}>
            <span className={step.ready ? "settings-step-dot ready" : "settings-step-dot pending"}>{index + 1}</span>
            <span>{step.label}</span>
          </button>)}
        </div>
        <p className="muted">按顺序完成连接测试；绿点表示配置已验证，红点表示尚未配置或测试未通过。点击任一步即可切换到对应配置。</p>
      </Panel>
      <div className="settings-active-card" id="settings-active-card" key={activeStep}>
        {activeStep === "storage" ? <StorageSettingsCard status={storage.data} loading={storage.loading} error={storage.error} /> : activeStep === "downloaders" ? <DownloadersSettingsCard providers={[providerById.qb1, providerById.qb2, providerById.qb3]} defaultDownloader={data.default_downloader} onChanged={reload} /> : activeStep === "media_search" ? <MediaSearchSettingsCard provider={providerById.tmdb} onChanged={reload} /> : activeProvider ? <IntegrationEditor provider={activeProvider} onChanged={reload} /> : <Panel title="配置"><p className="muted">该配置项不可用。</p></Panel>}
      </div>
    </div>
  );
}

function providerStepReady(provider: any): boolean {
  const result = provider?.last_test_result;
  return Boolean(!provider?.configuration_unreadable && provider?.enabled && result?.success === true && result?.can_enable !== false);
}

function DownloadersSettingsCard({ providers, defaultDownloader, onChanged }: { providers: any[]; defaultDownloader?: any; onChanged: () => void }) {
  const [settingDefault, setSettingDefault] = useState("");
  const [error, setError] = useState("");
  const defaultDownloaderId = String(defaultDownloader?.downloader_id ?? "");

  async function setDefaultDownloader(downloaderId: string) {
    setSettingDefault(downloaderId);
    setError("");
    try {
      await api("/api/admin/downloaders/default", { method: "PUT", body: JSON.stringify({ downloader_id: downloaderId }) });
      onChanged();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSettingDefault("");
    }
  }

  return (
    <div className="downloaders-settings-section">
      <div className="notice info default-downloader-notice">
        <strong>默认下载器</strong>
        <span>M-Team、Media Hub AI 和 WeChat Claw 发起的下载都会发送到这里。请在已测试并启用的下载器中选择。</span>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="downloaders-settings-grid">
        {providers.filter(Boolean).map((provider) => (
          <QbIntegrationEditor
            key={provider.provider}
            provider={provider}
            onChanged={onChanged}
            isDefault={provider.provider === defaultDownloaderId}
            defaultBusy={settingDefault !== ""}
            onSetDefault={() => setDefaultDownloader(provider.provider)}
          />
        ))}
      </div>
    </div>
  );
}

function StorageSettingsCard({ status, loading, error }: { status: any; loading: boolean; error: string }) {
  const detectedPaths = Array.isArray(status?.nas_storage_detected_paths) ? status.nas_storage_detected_paths : [];
  const errors = Array.isArray(status?.nas_storage_errors) ? status.nas_storage_errors : [];
  const readable = Boolean(status?.nas_storage_readable);
  return (
    <Panel title="存储空间">
      <div className="integration">
        <div className="notice info">
          <strong>NAS 存储空间</strong>
          <span>填写 NAS 文件夹路径后，即可在仪表盘查看空间使用情况。</span>
        </div>
        <div className="field-help">
          <strong>配置方式</strong>
          <span>重新部署容器时，将 docker-compose.yml 中 storage volumes 左侧替换为 NAS 文件夹路径，例如 /volume1/qb1-downloads:/mnt/storage1:ro。</span>
        </div>
        {!loading && <div className={`result-card ${error || !readable ? "failed" : "success"}`}>
          <strong>{error || status?.nas_storage_summary_label || (readable ? "NAS 存储路径可访问" : "未检测到可访问的 NAS 挂载")}</strong>
          {readable ? (
            <span>已检测路径：<span className="storage-path-list">{detectedPaths.map((path: string) => <code key={path}>{path}</code>)}</span></span>
          ) : (
            <span>{errors.length ? errors.slice(0, 3).map((item: any) => `容器内路径 ${item.path} ${item.message || "无法访问"}`).join("；") : "请检查 NAS 文件夹路径是否已正确写入 YAML 并挂载到容器。"}</span>
          )}
        </div>}
        {loading && <div className="result-card neutral"><strong>正在检测 NAS 挂载...</strong></div>}
      </div>
    </Panel>
  );
}

function IntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  if (provider.provider === "mteam") return <MTeamIntegrationEditor provider={provider} onChanged={onChanged} />;
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
        state: "draft",
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
  const [localError, setLocalError] = useState("");
  const [memberLimitDialogOpen, setMemberLimitDialogOpen] = useState(false);
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
      poll_timeout: Math.max(5, Number(form.poll_timeout) || 25),
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api("/api/admin/integrations/wechat_claw", { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setLocalResult({
        success: false,
        state: "draft",
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

  const setupPayload = setup.data?.qr_payload ?? {};
  const qrText = String(setup.data?.qr_text || "");
  const qrcode = setup.data?.qrcode ?? {};
  const qrReady = Boolean(qrcode.qrcode_url || setupPayload.qrcode_url);
  const selectedBinding = (bindings.data?.items ?? []).find((item: any) => item.id === selectedBindingId);
  const sessionConnected = Boolean(setup.data?.connected);
  const selectedMemberEnabled = selectedBinding?.enabled !== false;
  const loginStatus = !provider.enabled
    ? "未启用"
    : !selectedMemberEnabled
      ? "成员已停用"
      : sessionConnected
        ? "已登录"
        : "等待扫码";
  const preservedLoginNote = sessionConnected && (!provider.enabled || !selectedMemberEnabled)
    ? "微信登录状态已保留，重新启用后通常无需再次扫码。"
    : "";

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
    const items = Array.isArray(bindings.data?.items) ? bindings.data.items : [];
    const memberLimit = Number(bindings.data?.limit) || WECHAT_CLAW_MEMBER_LIMIT;
    if (items.length >= memberLimit) {
      setMemberLimitDialogOpen(true);
      return;
    }
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
      if (isWechatClawMemberLimitError(err)) setMemberLimitDialogOpen(true);
      else setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="微信连接">
      <div className="integration wechat-config">
        <div className="notice info">
          <strong>微信消息助手</strong>
          <span>完成配置并扫码绑定后，可在微信中查询影视与站内资源、发起下载，并接收任务完成和异常通知。</span>
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
          <label className="compact-field">轮询超时（秒）
            <input type="number" min="5" max="120" value={form.poll_timeout} onChange={(event) => updateField("poll_timeout", event.target.value)} />
          </label>
        </div>

        {selectedBindingId && <div className="wechat-setup-card">
          <div className="wechat-setup-main">
            <div className="wechat-status-line">
              <span>绑定状态</span>
              <strong>{loginStatus}</strong>
            </div>
            {preservedLoginNote && <p className="field-note">{preservedLoginNote}</p>}
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
            <div className="wechat-connection-summary">
              <strong>当前连接</strong>
              <div><span>微信账号</span><b>{setup.data?.account_id || "等待扫码"}</b></div>
              <div><span>二维码</span><b>{qrcode.status === "confirmed" ? "已确认" : qrcode.status === "expired" ? "已过期" : "等待扫码"}</b></div>
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
        <TestResultCard result={result} emptyProvider="WeChat claw" />
        {memberLimitDialogOpen && <MemberLimitDialog onClose={() => setMemberLimitDialogOpen(false)} />}
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
        state: "draft",
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

function QbIntegrationEditor({ provider, onChanged, isDefault, defaultBusy, onSetDefault }: { provider: any; onChanged: () => void; isDefault?: boolean; defaultBusy?: boolean; onSetDefault?: () => void }) {
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
        state: "draft",
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
        <div className="downloader-default-control">
          {isDefault ? (
            <span className={providerStepReady(provider) ? "default-downloader-badge" : "default-downloader-badge unavailable"}><Star size={15} fill="currentColor" /> 默认下载器{providerStepReady(provider) ? "" : "（当前不可用）"}</span>
          ) : (
            <button className="inline-tool set-default-downloader" type="button" onClick={onSetDefault} disabled={defaultBusy || !providerStepReady(provider)} title={providerStepReady(provider) ? `将 ${label} 设为默认下载器` : "请先保存、测试并启用此下载器"}>
              <Star size={15} /> 设为默认下载器
            </button>
          )}
        </div>
        <div className="notice info">
          <strong>连接下载器</strong>
          <span>连接成功并启用后，可查看下载任务和运行状态，也可手动设为 M-Team、AI 与 WeChat Claw 的默认下载目标。</span>
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
  const legacyApiKey = String(saved.api_key ?? saved.passkey ?? savedHeaders.Authorization ?? "").replace(/^Bearer\s+/i, "");
  const [form, setForm] = useState<MTeamForm>({
    base_url: String(saved.base_url ?? "https://kp.m-team.cc"),
    api_key: legacyApiKey,
    cookie: String(savedHeaders.Cookie ?? ""),
    user_agent: String(savedHeaders["User-Agent"] ?? "PT-Media-Hub"),
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
    return {
      base_url: form.base_url.trim() || "https://kp.m-team.cc",
      api_key: form.api_key.trim(),
      headers,
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
        state: "draft",
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
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="10" />
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

function MediaSearchSettingsCard({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const saved = provider.saved_payload ?? {};
  const savedProxyDomains = Array.isArray(saved.proxy_domains)
    ? saved.proxy_domains.filter((domain: string) => TMDB_PROXY_DOMAINS.includes(domain as typeof TMDB_PROXY_DOMAINS[number]))
    : [...TMDB_PROXY_DOMAINS];
  const savedProxyEnabled = typeof saved.proxy_enabled === "boolean" ? saved.proxy_enabled : saved.mode === "proxy";
  const savedProxyUrl = String(saved.proxy_url ?? "http://host.docker.internal:7890");
  const [form, setForm] = useState<TmdbForm>({
    api_key: String(saved.api_key ?? ""),
    bearer_token: String(saved.bearer_token ?? ""),
    proxy_enabled: savedProxyEnabled,
    proxy_url: savedProxyUrl,
    proxy_domains: savedProxyDomains,
    language: String(saved.language ?? "zh-CN"),
    region: String(saved.region ?? "CN"),
    timeout: String(saved.timeout ?? "12"),
  });
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showProxyDetails, setShowProxyDetails] = useState(false);
  const [busy, setBusy] = useState("");
  const [localResult, setLocalResult] = useState<IntegrationTestResult | null>(provider.last_test_result);
  const [localError, setLocalError] = useState("");
  const [enabled, setEnabled] = useState(Boolean(provider.enabled));
  useEffect(() => setEnabled(Boolean(provider.enabled)), [provider.enabled]);
  const result = localResult ?? provider.last_test_result;
  const canEnable = result?.provider === "tmdb" && result?.mode === "real" && result?.can_enable === true;

  function updateField<K extends keyof TmdbForm>(key: K, value: TmdbForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function updateProxyEnabled(value: boolean) {
    updateField("proxy_enabled", value);
    if (value) {
      setShowProxyDetails(true);
    }
  }

  function toggleProxyDomain(domain: string) {
    setForm((current) => ({
      ...current,
      proxy_domains: current.proxy_domains.includes(domain)
        ? current.proxy_domains.filter((item) => item !== domain)
        : [...current.proxy_domains, domain],
    }));
  }

  function payload() {
    return {
      api_key: form.api_key.trim(),
      bearer_token: form.bearer_token.trim(),
      proxy_enabled: form.proxy_enabled,
      proxy_url: form.proxy_url.trim(),
      proxy_domains: form.proxy_domains,
      language: form.language.trim() || "zh-CN",
      region: form.region.trim() || "CN",
      timeout: Number(form.timeout) || 12,
    };
  }

  async function saveDraft() {
    setBusy("draft");
    setLocalError("");
    try {
      await api(`/api/admin/integrations/tmdb`, { method: "PUT", body: JSON.stringify({ payload: payload() }) });
      setEnabled(false);
      setLocalResult({
        success: false,
        state: "draft",
        provider: "tmdb",
        mode: "real",
        can_enable: false,
        message: "草稿已保存。",
        explanation: "媒体源凭据和精细代理策略已保存。",
        next_step: "点击“保存并测试”，确认数据接口和图片资源均可访问。",
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
      setEnabled(false);
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
      await api(`/api/admin/integrations/tmdb${enabled ? "/disable" : "/enable"}`, { method: "POST" });
      setEnabled(!enabled);
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  let proxyValidationMessage = "";
  if (form.proxy_enabled) {
    try {
      const parsed = new URL(form.proxy_url);
      if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname) proxyValidationMessage = "代理地址必须是有效的 http:// 或 https:// 地址。";
    } catch {
      proxyValidationMessage = "代理地址必须是有效的 http:// 或 https:// 地址。";
    }
    if (!form.proxy_domains.length) proxyValidationMessage = "启用代理时至少选择一个需要代理的网站。";
  }
  const routingHasChanges = form.proxy_enabled !== savedProxyEnabled
    || form.proxy_url.trim() !== savedProxyUrl.trim()
    || [...form.proxy_domains].sort().join("|") !== [...savedProxyDomains].sort().join("|");
  const configurationHasChanges = routingHasChanges
    || form.api_key.trim() !== String(saved.api_key ?? "").trim()
    || form.bearer_token.trim() !== String(saved.bearer_token ?? "").trim()
    || form.language.trim() !== String(saved.language ?? "zh-CN").trim()
    || form.region.trim() !== String(saved.region ?? "CN").trim()
    || (Number(form.timeout) || 12) !== (Number(saved.timeout) || 12);

  return (
    <div className="media-search-settings">
      <Panel title="媒体搜索来源">
        <div className="integration media-source-selector">
          <label>当前媒体源
            <select value="tmdb" onChange={() => undefined}>
              <option value="tmdb">TMDB（当前支持）</option>
            </select>
          </label>
          <div className="field-help compact-help">
            <strong>TMDB 影视资料库</strong>
            <span>为发现页和 AI 助手提供电影、剧集、演员、趋势、筛选和图片信息。</span>
          </div>
        </div>
      </Panel>

      <Panel title="TMDB 数据源">
        <div className="integration tmdb-editor">
          <div className="notice info">
            <strong>访问凭据与内容偏好</strong>
            <span>这里仅配置 TMDB 身份凭据、返回语言和默认地区；网络代理在下方“网络连接”板块单独管理。</span>
          </div>
          <div className="settings-grid">
            <label>TMDB Bearer Token
              <SecretInput value={form.bearer_token} onChange={(value) => updateField("bearer_token", value)} placeholder="从 TMDB 账号设置中复制" autoComplete="off" />
            </label>
            <label>语言
              <select value={form.language} onChange={(event) => updateField("language", event.target.value)}>
                {TMDB_LANGUAGE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label>地区
              <select value={form.region} onChange={(event) => updateField("region", event.target.value)}>
                {TMDB_REGION_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </label>
          </div>
          <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
            <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "高级设置"}
          </button>
          {showAdvanced && <div className="settings-grid advanced-settings">
            <label>TMDB API Key（备用凭据）
              <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="可替代 Bearer Token 使用" autoComplete="off" />
            </label>
            <label>超时时间（秒）
              <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="12" />
            </label>
          </div>}
        </div>
      </Panel>

      <Panel title="网络连接">
        <div className="integration network-settings-card">
          <div className="direct-network-summary">
            <span className="direct-network-icon"><Activity size={19} /></span>
            <span>
              <strong>默认连接：DoH 直连</strong>
              <small>推荐保持默认。对大多数用户来说无需额外代理，TMDB 数据和图片会通过 DoH 解析后直接访问。</small>
            </span>
            <b className={`network-mode-pill ${form.proxy_enabled ? "proxy" : "direct"}`}>{form.proxy_enabled ? (routingHasChanges ? "代理待保存" : enabled ? "代理已启用" : "代理待启用") : "DoH 直连"}</b>
          </div>

          <div className={`proxy-collapsible-card ${form.proxy_enabled ? "enabled" : ""}`}>
            <div className="proxy-collapse-header">
              <button className="proxy-collapse-trigger" type="button" aria-expanded={showProxyDetails} onClick={() => setShowProxyDetails((value) => !value)}>
                <span className="proxy-collapse-copy">
                  <span className="proxy-collapse-icon"><ShieldCheck size={19} /></span>
                  <span>
                    <strong>可选：Mihomo / HTTP 代理</strong>
                    <small>{form.proxy_enabled ? "代理开关已打开；展开后可填写地址并选择需要代理的网站。" : "可选网络连接方式，默认收起；需要通过 Mihomo 或 HTTP 代理访问 TMDB 时可启用。"}</small>
                  </span>
                </span>
                <span className="proxy-collapse-action">{showProxyDetails ? "收起" : "配置详情"}<ChevronDown className={showProxyDetails ? "expanded" : ""} size={17} /></span>
              </button>
              <label className="proxy-collapse-switch">
                <span>代理开关</span>
                <input className="proxy-toggle-switch" type="checkbox" role="switch" aria-label="使用已有 Mihomo 或 HTTP 代理" checked={form.proxy_enabled} onChange={(event) => updateProxyEnabled(event.target.checked)} />
              </label>
            </div>

            {showProxyDetails && <div className="proxy-collapse-body">
              <div className="field-help proxy-scope-help">
                <strong>仅代理 TMDB 白名单</strong>
                <span>M-Team、qBittorrent、NAS 存储、本地接口和其他应用请求始终直连，不受这里影响。</span>
              </div>
              <label className="proxy-address-field">Mihomo / HTTP 代理地址
                <CopyableInput value={form.proxy_url} onChange={(value) => updateField("proxy_url", value)} placeholder="例如 http://mihomo:7890" inputMode="url" />
                <span className="field-note">填写 Media Hub 容器能够访问的 HTTP/HTTPS 地址。同一 Docker 网络可使用 http://mihomo:7890；带认证时可填写 http://用户名:密码@mihomo:7890。</span>
                <span className="field-note">该地址只有在打开代理、完成“保存并测试”并再次“启用”后才会生效。</span>
              </label>
              {!form.proxy_enabled && <p className="muted proxy-disabled-note">代理当前关闭。你可以预先填写地址，但测试和运行仍会使用 DoH 直连；打开开关后才可选择代理网站。</p>}
              <div className="proxy-domain-section">
                <div className="proxy-domain-heading">
                  <strong>选择需要代理的网站</strong>
                  <span>未选中的网站继续使用 DoH 直连</span>
                </div>
                <div className="proxy-domain-list" aria-label="需要使用代理的网站">
                  {TMDB_PROXY_DOMAIN_OPTIONS.map((option) => (
                    <label className={form.proxy_enabled ? "proxy-domain-option" : "proxy-domain-option disabled"} key={option.domain}>
                      <input type="checkbox" checked={form.proxy_domains.includes(option.domain)} disabled={!form.proxy_enabled} onChange={() => toggleProxyDomain(option.domain)} />
                      <span><strong>{option.label}</strong><code>{option.domain}</code><small>{option.description}</small></span>
                    </label>
                  ))}
                </div>
              </div>
              <TmdbRouteFeedback form={form} result={result} enabled={enabled} hasUnsavedChanges={routingHasChanges} />
              {proxyValidationMessage && <p className="error">{proxyValidationMessage}</p>}
            </div>}
          </div>
          {proxyValidationMessage && !showProxyDetails && <p className="error">代理配置需要完善，请展开“可选：Mihomo / HTTP 代理”查看。</p>}
        </div>
      </Panel>

      <Panel title="连接测试与启用">
        <div className="integration media-search-activation">
          <div className="connection-workflow" aria-label="TMDB 配置生效步骤">
            <div className={!configurationHasChanges ? "ready" : ""}><b>1</b><span><strong>保存配置</strong><small>记录凭据和网络选择</small></span></div>
            <div className={!configurationHasChanges && result?.success ? "ready" : ""}><b>2</b><span><strong>测试连接</strong><small>验证数据接口和图片资源</small></span></div>
            <div className={enabled ? "ready" : ""}><b>3</b><span><strong>启用生效</strong><small>测试通过后手动启用</small></span></div>
          </div>
          <div className="actions">
            <button onClick={saveDraft} disabled={busy !== "" || Boolean(proxyValidationMessage)}>{busy === "draft" ? "正在保存..." : "仅保存草稿"}</button>
            <button className="primary" onClick={saveAndTest} disabled={busy !== "" || Boolean(proxyValidationMessage)}>{busy === "test" ? "正在测试..." : "保存并测试"}</button>
            <button onClick={toggleEnabled} disabled={busy !== "" || (!enabled && (!canEnable || configurationHasChanges))}>{enabled ? "停用" : "启用"}</button>
          </div>
          {!enabled && (!canEnable || configurationHasChanges) && <p className="muted">{configurationHasChanges ? "当前有尚未保存或测试的改动，请重新点击“保存并测试”。" : "请先点击“保存并测试”；连接成功后才能启用 TMDB。"}</p>}
          {!enabled && canEnable && !configurationHasChanges && <p className="field-note proxy-enable-ready">连接测试已通过，请点击“启用”，当前 TMDB 配置和网络路线才会正式生效。</p>}
          {localError && <p className="error">{localError}</p>}
          <div className="connection-result-section">
            <div className="connection-result-heading">
              <span><Activity size={17} /><strong>TMDB 整体连接测试结果</strong></span>
              <small>反馈数据 API、图片资源和凭据的整体测试结果；代理白名单路线请在“网络连接”的折叠卡中查看。</small>
            </div>
            {configurationHasChanges && result?.success && <p className="stale-test-note">当前表单已有新改动；下方绿色结果属于上一次已保存配置，请重新“保存并测试”。</p>}
            <TestResultCard result={result} emptyProvider="TMDB" />
          </div>
        </div>
      </Panel>
    </div>
  );
}

function TmdbRouteFeedback({ form, result, enabled, hasUnsavedChanges }: {
  form: TmdbForm;
  result?: IntegrationTestResult | null;
  enabled: boolean;
  hasUnsavedChanges: boolean;
}) {
  const previewRoutes = Object.fromEntries(TMDB_PROXY_DOMAIN_OPTIONS.map((option) => [
    option.domain,
    form.proxy_enabled && form.proxy_domains.includes(option.domain) ? "proxy" : "direct",
  ])) as Record<string, "proxy" | "direct">;
  const testedRoutes = result?.detail?.domain_routes ?? {};
  const testMatchesPreview = result?.success === true
    && TMDB_PROXY_DOMAIN_OPTIONS.every((option) => testedRoutes[option.domain] === previewRoutes[option.domain]);
  const status = hasUnsavedChanges
    ? { label: "尚未保存", tone: "pending", help: "开关仅是草稿；请依次点击“保存并测试”和“启用”，完成前不会生效。" }
    : testMatchesPreview && enabled
      ? { label: "已生效", tone: "verified", help: "这套路由已通过连接测试并启用，当前请求会按下方方式访问 TMDB。" }
      : testMatchesPreview
        ? { label: "待启用", tone: "ready", help: "连接测试已经通过；点击“启用”后，这套路由才会正式生效。" }
      : result?.success === false && result?.state !== "draft"
        ? { label: "测试失败", tone: "failed", help: "路由已经保存，但最近一次连接测试未通过。" }
        : { label: "待测试", tone: "saved", help: "路由已经保存；点击“保存并测试”可验证实际连通性。" };
  let proxyEndpoint = "已配置的代理";
  try {
    const parsed = new URL(form.proxy_url);
    proxyEndpoint = parsed.hostname ? `${parsed.hostname}${parsed.port ? `:${parsed.port}` : ""}` : proxyEndpoint;
  } catch {
    proxyEndpoint = "待完善的代理地址";
  }

  return (
    <section className="tmdb-route-feedback" aria-label="TMDB 白名单路由状态">
      <div className="tmdb-route-feedback-head">
        <span><Activity size={17} /><strong>当前白名单路由</strong></span>
        <b className={`tmdb-route-feedback-state ${status.tone}`}>{status.label}</b>
      </div>
      <div className="tmdb-route-feedback-list">
        {TMDB_PROXY_DOMAIN_OPTIONS.map((option) => {
          const usesProxy = previewRoutes[option.domain] === "proxy";
          return (
            <div className={`tmdb-route-feedback-item ${usesProxy ? "proxy" : "direct"}`} key={option.domain}>
              <span>
                <strong>{option.label}</strong>
                <code>{option.domain}</code>
                <small>{usesProxy ? `请求交给 ${proxyEndpoint} 转发` : "绕过代理，通过 DoH 解析后直连"}</small>
              </span>
              <b>{usesProxy ? "Mihomo" : "DoH 直连"}</b>
            </div>
          );
        })}
      </div>
      <small className="tmdb-route-feedback-help">{status.help}</small>
    </section>
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

  const tone = result.state === "draft" ? "neutral" : result.success ? "success" : "failed";
  const domainRoutes = result.detail?.domain_routes ?? {};
  return (
    <div className={`result-card ${tone}`}>
      <strong>{result.message ?? (result.state === "draft" ? "草稿已保存" : result.success ? "测试成功" : "测试失败")}</strong>
      {result.explanation && <span>{result.explanation}</span>}
      {result.next_step && <span>下一步：{result.next_step}</span>}
      {Object.keys(domainRoutes).length > 0 && <div className="proxy-route-summary">
        {TMDB_PROXY_DOMAIN_OPTIONS.map((option) => domainRoutes[option.domain] && <span key={option.domain}>
          <code>{option.domain}</code>
          <b className={domainRoutes[option.domain] === "proxy" ? "proxy" : "direct"}>{domainRoutes[option.domain] === "proxy" ? "Mihomo" : "DoH 直连"}</b>
        </span>)}
      </div>}
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
  return (
    <div className="input-with-tools sensitive">
      <input
        type={visible ? "text" : "password"}
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder={props.placeholder}
        autoComplete={props.autoComplete}
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
    wechat_claw: "WeChat claw",
    nas_disk: "NAS 存储",
  };
  return names[module] ?? module;
}

const DIAGNOSTIC_MODULE_IDS = ["mteam", "qb1", "qb2", "qb3", "tmdb", "ai", "wechat_claw", "nas_disk"] as const;

function checkingDiagnosticModules(): any[] {
  return DIAGNOSTIC_MODULE_IDS.map((module) => ({ module, status: "checking", checking: true }));
}

function diagnosticStatusMeta(item: any): { label: string; detail: string; tone: "success" | "failed" | "neutral" } {
  if (item.checking) {
    return { label: "检测中", detail: "正在连接并读取当前实际状态…", tone: "neutral" };
  }
  if (!item.enabled) {
    return { label: "未启用", detail: item.message || "当前模块尚未启用", tone: "neutral" };
  }
  if (item.status === "success") {
    return { label: "运行正常", detail: item.message || "本次实时检测通过", tone: "success" };
  }
  if (["failed", "failure", "error", "unhealthy"].includes(String(item.status || "").toLowerCase())) {
    return { label: "需要处理", detail: item.last_error || item.message || "检测未通过，请检查配置或网络", tone: "failed" };
  }
  return { label: "待检测", detail: "尚未取得本次实时检测结果", tone: "neutral" };
}

function DiagnosticsPage() {
  const [modules, setModules] = useState<any[]>(checkingDiagnosticModules);
  const [members, setMembers] = useState<any[]>([]);
  const [membersChecking, setMembersChecking] = useState(true);
  const mountedRef = useRef(false);
  const autoStartedRef = useRef(false);
  const runRef = useRef(0);
  const checking = modules.some((item) => item.checking);
  const completedCount = modules.filter((item) => !item.checking).length;

  function runDiagnostics() {
    const runId = ++runRef.current;
    setModules(checkingDiagnosticModules());
    setMembersChecking(true);

    DIAGNOSTIC_MODULE_IDS.forEach((module) => {
      api<any>(`/api/diagnostics/modules/${module}/check`, { method: "POST" })
        .then((payload) => {
          if (!mountedRef.current || runRef.current !== runId) return;
          const result = payload?.module;
          if (!result || result.module !== module) throw new Error("诊断接口返回了无效结果");
          setModules((current) => current.map((item) => item.module === module ? { ...result, checking: false } : item));
          if (module === "wechat_claw" && Array.isArray(payload.wechat_members)) {
            setMembers(payload.wechat_members);
          }
        })
        .catch((error) => {
          if (!mountedRef.current || runRef.current !== runId) return;
          setModules((current) => current.map((item) => item.module === module ? {
            module,
            enabled: true,
            status: "failed",
            checking: false,
            checked_at: new Date().toISOString(),
            last_error: (error as any)?.status === 404 ? "诊断服务尚未加载，请重启应用服务后重新检测。" : apiErrorDetail(error),
            message: "未能完成该模块的实时检测。",
          } : item));
        })
        .finally(() => {
          if (!mountedRef.current || runRef.current !== runId) return;
          if (module === "wechat_claw") setMembersChecking(false);
        });
    });
  }

  useEffect(() => {
    mountedRef.current = true;
    if (!autoStartedRef.current) {
      autoStartedRef.current = true;
      runDiagnostics();
    }
    return () => {
      mountedRef.current = false;
    };
  }, []);

  return (
    <div className="grid-page">
      <Panel title="健康概览">
        <div className="diagnostics-toolbar">
          <div className="diagnostics-toolbar-copy">
            <strong>{checking ? `正在实时检测（${completedCount}/${DIAGNOSTIC_MODULE_IDS.length}）` : "本轮实时检测已完成"}</strong>
            <small>{checking ? "系统正在核验各项服务的当前状态，检测结果会陆续更新。" : "检测结果已更新。如调整了服务配置，可重新检测。"}</small>
          </div>
          <button className={`diagnostics-refresh${checking ? " spinning" : ""}`} type="button" onClick={runDiagnostics} disabled={checking}>
            <RefreshCw size={15} aria-hidden="true" />
            {checking ? "检测中" : "重新检测全部"}
          </button>
        </div>
        <div className="diagnostics-health-list" aria-live="polite">
          {modules.map((item: any) => {
            const meta = diagnosticStatusMeta(item);
            return (
              <article className={`diagnostic-card ${meta.tone}${item.checking ? " checking" : ""}`} key={item.module} aria-busy={item.checking || undefined}>
                <div>
                  <strong>{diagnosticModuleName(item.module)}</strong>
                  <span className="diagnostic-status-pill">{meta.label}</span>
                </div>
                <small>{meta.detail}</small>
                {!item.checking && item.checked_at && (
                  <small>本次检测：{formatSystemDateTime(item.checked_at)}{typeof item.duration_ms === "number" ? ` · ${item.duration_ms} ms` : ""}</small>
                )}
              </article>
            );
          })}
        </div>
      </Panel>
      <Panel title="WeChat claw 成员状态">
        {membersChecking && <div className="diagnostics-inline-loading" role="status"><RefreshCw size={15} aria-hidden="true" /><span>正在实时检测微信成员连接状态…</span></div>}
        <div className="diagnostics-member-list" aria-live="polite">
          {members.map((member: any) => {
            const connected = Boolean(member.connected);
            const moduleEnabled = member.module_enabled !== false;
            const memberEnabled = member.enabled !== false;
            const operational = Boolean(member.operational ?? (moduleEnabled && memberEnabled && connected));
            const liveFailed = member.live_connection_ok === false && Boolean(member.saved_connection);
            const tone = liveFailed ? "failed" : operational ? "success" : "neutral";
            const statusLabel = !moduleEnabled
              ? "未启用"
              : !memberEnabled
                ? "成员已停用"
                : liveFailed
                  ? "连接异常"
                  : connected
                    ? "已连接"
                    : member.configured
                      ? "等待扫码"
                      : "未配置";
            const lastPoll = member.last_poll ?? {};
            return (
              <article className={`diagnostic-card diagnostic-member-card ${tone}`} key={member.id}>
                <div>
                  <span className="diagnostic-member-title"><MemberAvatar member={member} size="small" /><strong>{member.display_name}</strong></span>
                  <span className="diagnostic-status-pill">{statusLabel}</span>
                </div>
                <small>微信：{member.account_id || member.qrcode_status || "尚未绑定"}{member.saved_connection && !connected ? " · 登录信息已保存" : ""}</small>
                {member.live_message && <small>实时检测：{member.live_message}</small>}
                <small>最近同步：{lastPoll.updated_at ? formatSystemDateTime(lastPoll.updated_at) : "尚未同步"}{lastPoll.message ? ` · ${lastPoll.message}` : ""}</small>
              </article>
            );
          })}
          {!membersChecking && !members.length && <p className="muted">尚未创建 WeChat claw 成员。</p>}
        </div>
      </Panel>
    </div>
  );
}
function Panel({ title, children }: { title: string; children: ReactNode }) {
  return <section className="panel"><h2>{title}</h2>{children}</section>;
}

function Metric({ icon: Icon, title, value, source, tone }: { icon?: typeof Film; title: string; value: ReactNode; source: string; tone?: "download" | "upload" | "activity" }) {
  return (
    <div className={tone ? `metric metric-${tone}` : "metric"}>
      <small>{Icon && <span className="metric-icon"><Icon size={14} /></span>}{title}</small>
      <strong>{value}</strong>
      {source && <span>{source}</span>}
    </div>
  );
}

function StorageMetric({ overview }: { overview: any }) {
  const storage = storageDisplay(overview);
  return (
    <div className="metric storage-metric metric-storage">
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
    <article className={`downloader-node ${configured ? "" : "empty"} ${online ? "online" : "offline"}`.trim()} role="button" tabIndex={0} onClick={openDownloader} onKeyDown={handleCardKeyDown}>
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

function MediaSearchResults({ items = [], onSelect, loading, error }: { items: any[]; onSelect: (item: any) => void; loading?: boolean; error?: string }) {
  return (
    <section className="panel search-result-panel">
      <div className="search-result-header">
        <div className="search-result-heading">
          <h2>TMDB 媒体结果</h2>
          <small>{loading ? "正在读取 TMDB" : error ? "读取失败" : `共 ${items.length} 条`}</small>
        </div>
      </div>
      {loading && !items.length ? <SearchLoadingState title="正在搜索 TMDB 媒体" detail="正在匹配影视条目、评分、海报和演职员信息" /> : (
        <div className="media-result-grid">
        {items.map((item) => <MediaResultCard item={item} onSelect={onSelect} key={item.id} />)}
          {!loading && !items.length && !error && <p className="muted">没有搜索到 TMDB 媒体，或 TMDB 尚未启用。</p>}
        </div>
      )}
      {error && <p className="search-result-error">{error}</p>}
    </section>
  );
}

function MediaResultCard({ item, onSelect }: { item: any; onSelect: (item: any) => void }) {
  const genres = (item.genres ?? []).slice(0, 4);
  const seasonEpisode = tvSeasonEpisodeLabel(item);
  const latestSeason = tvLatestSeasonLabel(item, false);
  const latestEpisode = tvEpisodeAirLabel(item.last_episode_to_air, "");
  const mediaTypeLabel = item.media_type === "tv" ? "剧集" : "电影";
  const countryLabel = mediaCountryLabel(item);
  const rating = Number(item.rating ?? 0);
  const voteCount = Number(item.vote_count ?? 0);
  const popularity = Number(item.popularity ?? 0);
  const overview = String(item.overview ?? "").trim();
  const facts = [
    mediaTypeLabel,
    item.year && item.year !== "未知" ? String(item.year) : "年份未知",
    item.runtime ? `${item.runtime} 分钟` : "",
    seasonEpisode,
    countryLabel
  ].filter(Boolean);

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
      <div className="media-poster-shell">
        <img className="media-poster" src={item.poster} alt="" loading="lazy" decoding="async" onError={handleImageError} />
      </div>
      <div className="media-card-body">
        <div className="media-card-heading">
          <div className="media-card-title-copy">
            <h3>{item.title}</h3>
            {item.original_title && item.original_title !== item.title ? <span>{item.original_title}</span> : null}
          </div>
          <span className={rating > 0 ? "rating-badge" : "rating-badge unrated"}>
            <Star size={15} />
            {rating > 0 ? numberLabel(rating, 1) : "暂无评分"}
          </span>
        </div>
        {facts.length > 0 && (
          <div className="media-card-facts">
            {facts.map((fact) => <span key={fact}>{fact}</span>)}
          </div>
        )}
        {(latestSeason || latestEpisode) && (
          <div className="media-card-updates">
            {latestSeason && (
              <div className="media-card-update">
                <span className="media-card-update-icon"><CalendarDays size={15} /></span>
                <div>
                  <small>最新季度</small>
                  <span>{latestSeason}</span>
                </div>
              </div>
            )}
            {latestEpisode && (
              <div className="media-card-update">
                <span className="media-card-update-icon"><Clock3 size={15} /></span>
                <div>
                  <small>最近播出</small>
                  <span>{latestEpisode}</span>
                </div>
              </div>
            )}
          </div>
        )}
        {item.media_type === "movie" && overview && (
          <div className="media-card-overview">
            <small>剧情简介</small>
            <p>{overview}</p>
          </div>
        )}
        <div className="media-card-footer">
          {genres.length > 0 && <div className="chip-row">{genres.map((genre: string) => <span className="soft-chip" key={genre}>{genre}</span>)}</div>}
          <div className="media-card-stats">
            <span><Users size={14} />{numberLabel(voteCount)} 票</span>
            {popularity > 0 ? <span><Activity size={14} />热度 {numberLabel(popularity, 1)}</span> : null}
          </div>
        </div>
      </div>
    </article>
  );
}

function MTeamResourceResults({
  items = [],
  loading,
  error,
  sortBy,
  sortDirection,
  onSortBy,
  onSortDirection,
  onBack,
  backLabel = "返回发现"
}: {
  items: any[];
  loading?: boolean;
  error?: string;
  sortBy: string;
  sortDirection: "asc" | "desc";
  onSortBy: (value: string) => void;
  onSortDirection: (value: "asc" | "desc") => void;
  onBack?: () => void;
  backLabel?: string;
}) {
  return (
    <section className="panel search-result-panel">
      <div className="search-result-header">
        <div className="search-result-title-row">
          {onBack && <button type="button" onClick={onBack}><ArrowLeft size={17} />{backLabel}</button>}
          <div className="search-result-heading">
            <h2>M-Team 资源结果</h2>
            <small>{loading ? "正在读取 M-Team" : error ? "读取失败" : `共 ${items.length} 条`}</small>
          </div>
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
      {loading && !items.length ? <SearchLoadingState title="正在搜索 M-Team 资源" detail="正在读取资源、体积、做种、促销和评分字段" /> : (
        <div className="mteam-resource-list">
          {items.map((item) => <MTeamResourceCard item={item} key={item.id} />)}
          {!loading && !items.length && !error && <p className="muted">没有搜索到 M-Team 资源，或 M-Team 尚未启用。</p>}
        </div>
      )}
      {error && <p className="search-result-error">{error}</p>}
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
      const result = await api<any>(`/api/mteam/torrents/${encodeURIComponent(item.id)}/download`, { method: "POST", body: JSON.stringify({ payload: item }) });
      const targetLabel = downloaderShortLabel(result?.downloader_id || "qB");
      playDoneSound();
      setPushNotice({ status: "success", title: "推送完成", step: `${targetLabel} 已接收下载任务`, detail: item.title });
      window.setTimeout(() => setPushNotice(null), 5200);
    } catch (err) {
      const detail = apiErrorDetail(err);
      setPushNotice({ status: "error", title: "推送失败", step: "未能完成 M-Team 到默认下载器的推送", detail });
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

function FavoriteButton({ item, detail = false }: { item: any; detail?: boolean }) {
  const favorites = useFavorites();
  const active = favorites.isFavorite(item);
  const busy = favorites.isBusy(item);
  const label = active ? "取消收藏" : "加入收藏";
  return (
    <button
      className={`${detail ? "detail-favorite-button" : "poster-favorite-button"}${active ? " active" : ""}`}
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active}
      disabled={busy}
      onClick={(event) => {
        event.stopPropagation();
        void favorites.toggle(item);
      }}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <Heart size={detail ? 19 : 18} fill={active ? "currentColor" : "none"} />
    </button>
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
        <FavoriteButton item={item} />
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
  canGoBack,
  onBack,
  onExit,
  onPersonSelect,
  onMediaSelect,
  onMTeamSearch,
  mteamSearching = false,
  exitLabel = "返回发现"
}: {
  item: any;
  loading: boolean;
  error: string;
  canGoBack: boolean;
  onBack: () => void;
  onExit: () => void;
  onPersonSelect?: (person: any) => void;
  onMediaSelect: (item: any) => void;
  onMTeamSearch?: (item: any) => void;
  mteamSearching?: boolean;
  exitLabel?: string;
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
      {(loading || error) && <div className="tmdb-detail-status-stack" aria-live="polite">
        {loading && <div className="tmdb-loading" role="status"><RefreshCw size={15} aria-hidden="true" /><span>正在读取 TMDB 详情...</span></div>}
        {error && <div className="tmdb-detail-error" role="alert"><CircleAlert size={16} aria-hidden="true" /><span>{error}</span></div>}
      </div>}
      <div className="tmdb-detail-glass">
        <div className="tmdb-detail-nav">
          {canGoBack && <button className="tmdb-back tmdb-back-icon" type="button" onClick={onBack} aria-label="返回上一层详情" title="返回上一层详情"><ArrowLeft size={18} /></button>}
          <button className="tmdb-back" type="button" onClick={onExit}>{exitLabel}</button>
        </div>
        <div className="tmdb-detail-actions">
          <FavoriteButton item={item} detail />
          {onMTeamSearch && (
            <button className="tmdb-mteam-search" type="button" onClick={() => onMTeamSearch(item)} disabled={mteamSearching} title="用影片名称在 M-Team 搜索资源">
              <Radar size={17} />
              {mteamSearching ? "搜索中" : "M-Team 搜索"}
            </button>
          )}
        </div>
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
          {castMembers.map((person: any) => onPersonSelect ? (
            <button className="tmdb-person-card" type="button" onClick={() => onPersonSelect(person)} key={person.id}>
              <img src={person.profile} alt="" loading="lazy" decoding="async" onError={handleImageError} />
              <strong>{person.name}</strong>
              <span>{person.character ? `饰 ${person.character}` : "演员"}</span>
            </button>
          ) : (
            <div className="tmdb-person-card" key={person.id}>
              <img src={person.profile} alt="" loading="lazy" decoding="async" onError={handleImageError} />
              <strong>{person.name}</strong>
              <span>{person.character ? `饰 ${person.character}` : "演员"}</span>
            </div>
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
  canGoBack,
  onBack,
  onExit,
  onMediaSelect
}: {
  person: any;
  loading: boolean;
  error: string;
  canGoBack: boolean;
  onBack: () => void;
  onExit: () => void;
  onMediaSelect: (item: any) => void;
}) {
  const works = Array.isArray(person.known_for) ? person.known_for : [];
  const aliases = Array.isArray(person.also_known_as) ? person.also_known_as.filter(Boolean).slice(0, 4) : [];
  const department = personDepartmentLabel(person.known_for_department);
  return (
    <section className="tmdb-detail tmdb-person-detail">
      <div className="tmdb-detail-haze" />
      {(loading || error) && <div className="tmdb-detail-status-stack" aria-live="polite">
        {loading && <div className="tmdb-loading" role="status"><RefreshCw size={15} aria-hidden="true" /><span>正在读取演员作品...</span></div>}
        {error && <div className="tmdb-detail-error" role="alert"><CircleAlert size={16} aria-hidden="true" /><span>{error}</span></div>}
      </div>}
      <div className="tmdb-person-header">
        <div className="tmdb-person-toolbar">
          <div className="tmdb-detail-nav tmdb-person-actions">
          {canGoBack && <button className="tmdb-back tmdb-back-icon" type="button" onClick={onBack} aria-label="返回上一层详情" title="返回上一层详情"><ArrowLeft size={18} /></button>}
          <button className="tmdb-back" type="button" onClick={onExit}>返回发现</button>
          </div>
        </div>
        <div className="tmdb-person-hero">
          <div className="tmdb-person-portrait-wrap">
            <img src={person.profile} alt={person.name || "人物头像"} loading="eager" decoding="async" fetchPriority="high" onError={handleImageError} />
          </div>
          <div className="tmdb-person-identity">
            <span className="tmdb-person-department"><UserRound size={15} />{department}</span>
            <h2>{person.name}</h2>
            <div className="tmdb-person-meta">
              {person.birthday && <span><small>出生日期</small><strong>{personDateLabel(person.birthday)}</strong></span>}
              {person.deathday && <span><small>逝世日期</small><strong>{personDateLabel(person.deathday)}</strong></span>}
              {person.place_of_birth && <span className="wide"><small>出生地点</small><strong>{person.place_of_birth}</strong></span>}
            </div>
            {aliases.length > 0 && <p className="tmdb-person-aliases"><strong>其他名字</strong><span>{aliases.join(" / ")}</span></p>}
          </div>
        </div>
      </div>
      <section className="tmdb-person-biography">
        <div className="discover-section-header"><h2><span />人物简介</h2></div>
        <p>{person.biography || "暂无人物简介。"}</p>
      </section>
      <section className="tmdb-detail-section tmdb-person-works">
        <div className="discover-section-header"><h2><span />相关作品</h2>{works.length > 0 && <small>共 {works.length} 部</small>}</div>
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
    await api(`/api/mteam/torrents/${encodeURIComponent(item.id)}/download`, { method: "POST", body: JSON.stringify({ payload: item }) });
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

function parseSystemDate(value: string | number | Date): Date {
  if (value instanceof Date) return value;
  const raw = String(value ?? "").trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return new Date(`${raw}T00:00:00`);
  const normalized = /T/.test(raw) && !/(Z|[+-]\d{2}:?\d{2})$/.test(raw) ? `${raw}Z` : raw;
  return new Date(normalized);
}

function formatSystemDateTime(value: string | number | Date): string {
  const date = parseSystemDate(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString();
}

function formatDateLabel(value: string): string {
  if (!value) return "-";
  const date = parseSystemDate(value);
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

function personDepartmentLabel(value: unknown): string {
  const department = String(value || "").trim();
  const labels: Record<string, string> = {
    Acting: "演员",
    Directing: "导演",
    Writing: "编剧",
    Production: "制片",
    Camera: "摄影",
    Editing: "剪辑",
    Sound: "声音",
    Art: "美术",
    "Costume & Make-Up": "服装与造型",
    "Visual Effects": "视觉特效",
    Crew: "剧组",
    Creator: "创作者",
  };
  return labels[department] || department || "影视工作者";
}

function personDateLabel(value: unknown): string {
  const raw = String(value || "").trim();
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (!matched) return raw;
  return `${matched[1]}年${Number(matched[2])}月${Number(matched[3])}日`;
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

function favoriteMonthKey(value: string): string {
  const date = parseSystemDate(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function favoriteMonthLabel(key: string): string {
  if (key === "unknown") return "时间未知";
  const [year, month] = key.split("-");
  return `${year}年${Number(month)}月`;
}

function FavoritesPage() {
  const favorites = useFavorites();
  const [viewMode, setViewMode] = useState<"grid" | "timeline">("grid");
  const [selectedMedia, setSelectedMedia] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [mteamItems, setMteamItems] = useState<any[]>([]);
  const [mteamSearching, setMteamSearching] = useState(false);
  const [mteamError, setMteamError] = useState("");
  const [mteamSearchActive, setMteamSearchActive] = useState(false);
  const [resourceSort, setResourceSort] = useState("seeders");
  const [resourceSortDirection, setResourceSortDirection] = useState<"asc" | "desc">("desc");
  const mteamSearchRequestId = useRef(0);
  const sortedMteamItems = useMemo(() => sortResources(mteamItems, resourceSort, resourceSortDirection), [mteamItems, resourceSort, resourceSortDirection]);

  const timelineGroups = useMemo(() => {
    const groups = new Map<string, any[]>();
    favorites.items.forEach((item) => {
      const key = favoriteMonthKey(String(item.favorited_at || ""));
      groups.set(key, [...(groups.get(key) || []), item]);
    });
    return Array.from(groups.entries()).map(([key, items]) => ({ key, label: favoriteMonthLabel(key), items }));
  }, [favorites.items]);

  async function openFavoriteDetail(item: any) {
    const tmdbId = mediaTmdbId(item);
    if (!tmdbId || !item.media_type) return;
    setSelectedMedia(item);
    setDetailLoading(true);
    setDetailError("");
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
    try {
      setSelectedMedia(await api<any>(`/api/tmdb/media/${item.media_type}/${tmdbId}`));
    } catch (err) {
      setDetailError((err as Error).message);
    } finally {
      setDetailLoading(false);
    }
  }

  async function searchMTeamFromFavorite(item: any) {
    const keyword = String(item?.title || item?.original_title || "").trim();
    if (!keyword) return;
    const requestId = mteamSearchRequestId.current + 1;
    mteamSearchRequestId.current = requestId;
    setMteamItems([]);
    setMteamError("");
    setMteamSearching(true);
    setMteamSearchActive(true);
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
    try {
      const result = await api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(keyword)}`);
      if (mteamSearchRequestId.current === requestId) setMteamItems(Array.isArray(result.items) ? result.items : []);
    } catch (err) {
      if (mteamSearchRequestId.current === requestId) setMteamError((err as Error).message);
    } finally {
      if (mteamSearchRequestId.current === requestId) setMteamSearching(false);
    }
  }

  function returnToFavoriteDetail() {
    mteamSearchRequestId.current += 1;
    setMteamSearchActive(false);
    setMteamSearching(false);
    setMteamError("");
    window.requestAnimationFrame(() => window.scrollTo(0, 0));
  }

  if (mteamSearchActive) {
    return (
      <div className="grid-page discover-search-results favorites-mteam-results">
        <MTeamResourceResults
          items={sortedMteamItems}
          loading={mteamSearching}
          error={mteamError}
          sortBy={resourceSort}
          sortDirection={resourceSortDirection}
          onSortBy={setResourceSort}
          onSortDirection={setResourceSortDirection}
          onBack={returnToFavoriteDetail}
          backLabel="返回影片详情"
        />
      </div>
    );
  }

  if (selectedMedia) {
    return (
      <MediaDetailPage
        item={selectedMedia}
        loading={detailLoading}
        error={detailError}
        canGoBack={false}
        onBack={() => undefined}
        onExit={() => setSelectedMedia(null)}
        onMediaSelect={openFavoriteDetail}
        onMTeamSearch={searchMTeamFromFavorite}
        mteamSearching={mteamSearching}
        exitLabel="返回收藏"
      />
    );
  }

  return (
    <div className="grid-page favorites-page">
      <section className="favorites-toolbar">
        <div>
          <span className="favorites-eyebrow">MY COLLECTION</span>
          <h2>我的收藏</h2>
          <p>{favorites.items.length ? `已收藏 ${favorites.items.length} 部作品` : "喜欢的影片会在这里汇集"}</p>
        </div>
        <button
          className={viewMode === "timeline" ? "favorites-view-button active" : "favorites-view-button"}
          type="button"
          onClick={() => setViewMode((current) => current === "grid" ? "timeline" : "grid")}
          aria-label={viewMode === "grid" ? "切换到收藏时间视图" : "切换到海报视图"}
          title={viewMode === "grid" ? "按收藏时间查看" : "返回海报视图"}
        >
          {viewMode === "grid" ? <CalendarDays size={20} /> : <Grid3X3 size={20} />}
        </button>
      </section>

      {favorites.error && <p className="error">{favorites.error}</p>}
      {favorites.loading && !favorites.items.length && <Panel title="收藏"><p>正在读取收藏...</p></Panel>}
      {!favorites.loading && !favorites.items.length && (
        <section className="favorites-empty">
          <span><Heart size={30} /></span>
          <h3>还没有收藏影片</h3>
          <p>在发现页点击海报右下角的爱心，影片就会出现在这里。</p>
        </section>
      )}

      {favorites.items.length > 0 && viewMode === "grid" && (
        <div className="poster-grid favorites-grid">
          {favorites.items.map((item, index) => <DiscoverPosterCard item={item} eager={index < eagerPosterLimit()} onSelect={openFavoriteDetail} key={favoriteMediaKey(item)} />)}
        </div>
      )}

      {favorites.items.length > 0 && viewMode === "timeline" && (
        <div className="favorites-timeline">
          {timelineGroups.map((group) => (
            <section className="favorites-time-group" key={group.key}>
              <header><h3>{group.label}</h3><span>{group.items.length} 部</span></header>
              <div className="poster-grid favorites-grid">
                {group.items.map((item) => (
                  <div className="favorite-time-item" key={favoriteMediaKey(item)}>
                    <DiscoverPosterCard item={item} onSelect={openFavoriteDetail} />
                    <time dateTime={item.favorited_at}>收藏于 {formatDateLabel(item.favorited_at)}</time>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
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
    stoppeddl: "暂停",
    stoppedup: "暂停",
    stopped: "暂停",
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
  favorites: FavoritesPage,
  dashboard: DashboardPage,
  downloads: DownloadsPage,
  notifications: NotificationsAssistantPage,
  settings: SettingsPage,
  diagnostics: DiagnosticsPage
};
