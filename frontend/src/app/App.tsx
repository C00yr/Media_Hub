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
  Percent,
  Radar,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Star,
  Upload,
  UserRound,
  Users,
  Wrench
} from "lucide-react";
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
  name: string;
  base_url: string;
  username: string;
  password: string;
  timeout: string;
  default_save_path: string;
};

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
          onOpenDownloader={(downloaderId) => {
            setSelectedDownloader(downloaderId);
            setActive("downloads");
          }}
        />
      </main>
      <nav className="bottom-nav">
        {visibleNav.filter((item) => ["discover", "dashboard", "downloads", "settings", "diagnostics"].includes(item.key)).map((item) => {
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
      helper: "请在部署 YAML 中配置 NAS 文件夹挂载以显示总空间",
      value: formatBytesFixed(free, 2),
    };
  }
  return {
    percent: 0,
    helper: "请在部署 YAML 中配置 NAS 文件夹挂载",
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
          <Metric icon={Activity} title="活跃上传/下载" value={<ActiveTransferCounts upload={data.overview.upload_tasks ?? 0} download={data.overview.download_tasks ?? 0} />} source={`共 ${(data.overview.upload_tasks ?? 0) + (data.overview.download_tasks ?? 0)} 个活跃任务`} />
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

function SettingsPage() {
  const { data, reload } = useLoad<any>(() => api("/api/admin/integrations"), []);
  const storage = useLoad<any>(() => api("/api/admin/storage/status"), []);
  if (!data) return <Panel title="设置"><p>正在加载...</p></Panel>;

  return (
    <div className="grid-page">
      <Panel title="运行时凭据中心">
        <p className="muted">凭据由后端加密保存；保存过的 API、账号、密码和路径会直接回填在输入框里，可显示或复制。</p>
      </Panel>
      <StorageSettingsCard status={storage.data} loading={storage.loading} error={storage.error} />
      {data.providers
        .filter((provider: any) => ["mteam", "qb1", "qb2", "qb3", "tmdb"].includes(provider.provider))
        .map((provider: any) => <IntegrationEditor provider={provider} onChanged={reload} key={provider.provider} />)}
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
          <span>只需要在部署 YAML 左侧填写 NAS 真实文件夹路径；右侧 /mnt/storage1、/mnt/storage2、/mnt/storage3 由 APP 固定识别，不需要在设置里填写。</span>
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

function QbIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const label = downloaderShortLabel(provider.provider);
  const saved = provider.saved_payload ?? {};
  const [form, setForm] = useState<QbForm>({
    name: String(saved.name ?? label),
    base_url: String(saved.base_url ?? ""),
    username: String(saved.username ?? ""),
    password: String(saved.password ?? ""),
    timeout: String(saved.timeout ?? "10"),
    default_save_path: String(saved.default_save_path ?? "")
  });
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
      name: form.name.trim() || label,
      base_url: form.base_url.trim(),
      username: form.username.trim(),
      password: form.password,
      timeout: Number(form.timeout) || 10,
      default_save_path: form.default_save_path.trim(),
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
        explanation: "qB 凭据已经加密保存到后端。实时读取任务前，请点击“保存并测试”。",
        next_step: "确认 qB WebUI 地址、账号和密码可用后再启用。"
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
          <strong>用于实时读取 qBittorrent WebUI 中的下载器状态和任务列表</strong>
          <span>必填：WebUI 地址、用户名、密码。存储空间由部署 YAML 的固定挂载自动检测，qB 设置里不需要填写容器路径。</span>
        </div>
        <div className="settings-grid">
          <label>显示名称
            <CopyableInput value={form.name} onChange={(value) => updateField("name", value)} placeholder="例如 主下载器 / 动漫下载器" />
          </label>
          <label>qB WebUI 地址（必填）
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="例如 http://192.168.1.20:8080" />
          </label>
          <label>用户名（必填）
            <CopyableInput value={form.username} onChange={(value) => updateField("username", value)} placeholder="qB WebUI 用户名" autoComplete="off" />
          </label>
          <label>密码（必填）
            <SecretInput value={form.password} onChange={(value) => updateField("password", value)} placeholder="qB WebUI 密码" autoComplete="new-password" />
          </label>
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="10" />
          </label>
          <label>默认保存路径（可选）
            <CopyableInput value={form.default_save_path} onChange={(value) => updateField("default_save_path", value)} placeholder="例如 /downloads/media 或 D:\\Downloads" />
          </label>
        </div>
        <div className="field-help">
          <strong>实际需要你提供：</strong>
          <span>局域网地址就是 qB WebUI 地址；账号密码用于登录 Web API；默认保存路径用于添加新任务时指定位置。NAS 总空间请在部署 YAML 左侧填写真实 NAS 文件夹路径，APP 会自动读取 /mnt/storage1~3 并按存储池去重。</span>
        </div>
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
        explanation: "M-Team 凭据已经加密保存到后端，并会回填到当前输入框。",
        next_step: "点击“保存并测试”确认站点链接状态。"
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
          <strong>用来读取 M-Team 站点的用户数据与资源查询</strong>
          <span>真实 API 需要 M-Team API Key；Cookie 和 Passkey 只作为兼容字段保存。敏感字段默认用星号遮住，可点眼睛查看。</span>
        </div>
        <div className="settings-grid">
          <label>站点地址
            <CopyableInput value={form.base_url} onChange={(value) => updateField("base_url", value)} placeholder="https://kp.m-team.cc" />
          </label>
          <label>API Key
            <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="从 M-Team 个人资料复制 API Key" autoComplete="off" />
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
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "显示高级设置"}
        </button>
        {showAdvanced && (
          <label>Authorization
            <SecretInput value={form.authorization} onChange={(value) => updateField("authorization", value)} placeholder="可选，例如 Bearer token" autoComplete="off" />
          </label>
        )}
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
        explanation: "密钥已经加密保存到后端，并会回填到当前输入框。",
        next_step: "如果你还没有测试，请点击“保存并测试”。测试成功后再启用 TMDB。",
        detail: tmdbNetworkDetail(form)
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
  const networkDetail = result?.detail ?? tmdbNetworkDetail(form);

  return (
    <Panel title="TMDB 配置">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>TMDB 只支持两种网络模式</strong>
          <span>默认使用直连 + DoH。只有选择 mihomo VPN 代理时，TMDB 请求才会交给 mihomo；qB、M-Team 和 NAS 功能始终直连。</span>
        </div>
        <div className="tmdb-mode-row">
          <span>连接方式</span>
          <div className="segmented compact">
            <button type="button" className={!isProxyMode ? "active" : ""} onClick={() => updateField("mode", "direct")}>
              <Radar size={16} /> 直连 TMDB + DoH
            </button>
            <button type="button" className={isProxyMode ? "active" : ""} onClick={() => updateField("mode", "proxy")}>
              <ShieldCheck size={16} /> mihomo VPN 代理
            </button>
          </div>
        </div>
        <div className="field-help compact-help">
          <strong>网络边界</strong>
          <span>{networkDetail.route_label || "TMDB：直连 + DoH"}</span>
          <span>qB 下载器：强制直连</span>
          <span>M-Team：强制直连</span>
          {networkDetail.proxy_enabled && <span>TMDB 请求路径：{networkDetail.proxy_url || form.proxy_url || "mihomo:7890"}</span>}
        </div>
        {isProxyMode ? (
          <>
            <div className="settings-grid">
              <label>mihomo 代理地址
                <CopyableInput value={form.proxy_url} onChange={(value) => updateField("proxy_url", value)} placeholder="http://mihomo:7890" inputMode="url" />
                <small className="field-note">Docker Compose 默认服务名是 mihomo，端口是 7890。这个地址只用于 TMDB。</small>
              </label>
              <label>TMDB Bearer Token
                <SecretInput value={form.bearer_token} onChange={(value) => updateField("bearer_token", value)} placeholder="从 TMDB 账号后台复制，通常以 eyJ 开头" autoComplete="off" />
                <small className="field-note">推荐只填 Bearer Token。代理只改变网络路径，不改变 TMDB 鉴权方式。</small>
              </label>
              <label>语言 / 地区
                <div className="split-inputs">
                  <CopyableInput value={form.language} onChange={(value) => updateField("language", value)} placeholder="zh-CN" />
                  <CopyableInput value={form.region} onChange={(value) => updateField("region", value)} placeholder="CN" />
                </div>
              </label>
            </div>
            <div className="field-help compact-help">
              <strong>mihomo 配置要求</strong>
              <span>请把 Clash Verge 导出的节点放到 nas-mihomo/config.yaml，并保留规则：TMDB 域名走代理，MATCH 走 DIRECT。</span>
            </div>
          </>
        ) : (
          <>
            <div className="settings-grid">
              <label>TMDB v4 Bearer Token
                <SecretInput value={form.bearer_token} onChange={(value) => updateField("bearer_token", value)} placeholder="以 eyJ 开头的一长串访问令牌" autoComplete="off" />
                <small className="field-note">推荐只填这个。APP 会通过 doh.pub 解析 TMDB，并使用 IPv4 访问。</small>
              </label>
              <label>语言
                <CopyableInput value={form.language} onChange={(value) => updateField("language", value)} placeholder="zh-CN" />
              </label>
              <label>地区
                <CopyableInput value={form.region} onChange={(value) => updateField("region", value)} placeholder="CN" />
              </label>
            </div>
            {form.api_key.trim() && form.bearer_token.trim() && <p className="muted">已同时填写 API Key 和 Bearer Token，测试和发现页会优先使用 Bearer Token。</p>}
            <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
              <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏直连高级设置" : "显示直连高级设置"}
            </button>
            {showAdvanced && (
              <div className="settings-grid">
                <label>API Key
                  <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="可选，通常不需要；Bearer Token 优先" autoComplete="off" />
                </label>
                <label>TMDB 接口地址
                  <CopyableInput value={form.endpoint} onChange={(value) => updateField("endpoint", value)} placeholder="默认 https://api.themoviedb.org/3，通常不用修改" />
                </label>
              </div>
            )}
          </>
        )}
        <label className="compact-field">超时时间（秒）
          <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="12" />
        </label>
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

function tmdbNetworkDetail(form: TmdbForm): IntegrationTestResult["detail"] {
  const proxyUrl = form.proxy_url.trim() || "http://mihomo:7890";
  const proxyHost = displayProxyHost(proxyUrl);
  return {
    network_mode: form.mode === "proxy" ? "proxy" : "direct",
    route_label: form.mode === "proxy" ? "TMDB：mihomo VPN 代理" : "TMDB：直连 + DoH",
    proxy_enabled: form.mode === "proxy",
    proxy_url: form.mode === "proxy" ? proxyHost : "",
    non_tmdb_policy: "direct_only",
  };
}

function displayProxyHost(value: string): string {
  try {
    const url = new URL(value.includes("://") ? value : `http://${value}`);
    return url.host || value;
  } catch {
    return value;
  }
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
      {result.detail?.route_label && <small>TMDB 请求路径：{result.detail.route_label}</small>}
      {result.detail?.proxy_enabled && result.detail?.proxy_url && <small>代理地址：{result.detail.proxy_url}</small>}
      {result.detail?.image_probe?.checked && <small>图片域：{result.detail.image_probe.ok ? "image.tmdb.org 可访问" : `image.tmdb.org 失败：${result.detail.image_probe.error || result.detail.image_probe.reason || "未知错误"}`}</small>}
      {result.detail?.non_tmdb_policy === "direct_only" && <small>qB / M-Team / NAS：强制直连</small>}
      {result.http_status && <small>HTTP 状态码：{result.http_status}</small>}
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

const pages: Record<NavKey, (props: { user: User; resetToken?: number; selectedDownloader?: string; onOpenDownloader?: (downloaderId: string) => void }) => ReactNode> = {
  discover: DiscoverPage,
  dashboard: DashboardPage,
  downloads: DownloadsPage,
  notifications: NotificationsPage,
  settings: SettingsPage,
  diagnostics: DiagnosticsPage
};
