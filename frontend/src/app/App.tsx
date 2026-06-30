import {
  Activity,
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
import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";
import { api, formatBytes, formatSpeed, getToken, setToken } from "../api/client";

type User = { username: string; role: string };
type NavKey = "discover" | "search" | "dashboard" | "downloads" | "stats" | "notifications" | "settings" | "diagnostics";

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
};

type TmdbForm = {
  api_key: string;
  bearer_token: string;
  language: string;
  region: string;
  timeout: string;
  endpoint: string;
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
  category: string;
  tags: string;
  path_from: string;
  path_to: string;
};

const navItems: { key: NavKey; label: string; icon: typeof Film; admin?: boolean }[] = [
  { key: "discover", label: "发现", icon: Film },
  { key: "search", label: "搜索", icon: Search },
  { key: "dashboard", label: "仪表盘", icon: Gauge },
  { key: "downloads", label: "下载", icon: Download },
  { key: "stats", label: "统计", icon: Activity },
  { key: "notifications", label: "通知", icon: Bell },
  { key: "settings", label: "设置", icon: Settings },
  { key: "diagnostics", label: "诊断", icon: Wrench, admin: true }
];

const pageDescriptions: Record<NavKey, string> = {
  discover: "从 TMDB 获取流行趋势、热门内容和高分片单。",
  search: "搜索媒体信息和站内资源，确认后再提交下载。",
  dashboard: "查看站点、下载器和 NAS 的核心运行指标。",
  downloads: "查看和管理多个 qB 下载器中的任务。",
  stats: "查看上传、下载和做种数据的趋势。",
  notifications: "集中查看系统提醒和任务通知。",
  settings: "管理运行时凭据，敏感信息只在后端加密保存。",
  diagnostics: "查看模块健康、调用轨迹并导出脱敏诊断信息。"
};

function useLoad<T>(loader: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
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
      setError("");
      return loader()
        .then((value) => {
          setData(value);
          return value;
        })
        .catch((err) => {
          setError((err as Error).message);
          throw err;
        });
    }
  };
}

export function App() {
  const [initialized, setInitialized] = useState<boolean | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [active, setActive] = useState<NavKey>("dashboard");

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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">PT</span>
          <div>
            <strong>Media Hub</strong>
            <small>媒体中枢</small>
          </div>
        </div>
        <nav>
          {visibleNav.map((item) => {
            const Icon = item.icon;
            return (
              <button className={active === item.key ? "nav-item active" : "nav-item"} onClick={() => setActive(item.key)} key={item.key}>
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
        <header className="topbar">
          <div>
            <h1>{navItems.find((item) => item.key === active)?.label}</h1>
            <p>{pageDescriptions[active]}</p>
          </div>
          <div className="user-pill">
            <ShieldCheck size={16} />
            {user.username} / {user.role === "admin" ? "管理员" : "用户"}
          </div>
        </header>
        <ActivePage user={user} />
      </main>
      <nav className="bottom-nav">
        {visibleNav.filter((item) => ["discover", "search", "dashboard", "downloads", "settings"].includes(item.key)).map((item) => {
          const Icon = item.icon;
          return (
            <button className={active === item.key ? "active" : ""} onClick={() => setActive(item.key)} key={item.key} aria-label={item.label}>
              <Icon size={20} />
              <span>{item.key === "settings" ? "我的" : item.label}</span>
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
        <div className="brand large"><span className="brand-mark">PT</span><div><strong>Media Hub</strong><small>面向 NAS 的媒体管理应用</small></div></div>
        <h1>{props.title}</h1>
        <label>用户名<input value={props.username} onChange={(event) => props.onUsername(event.target.value)} /></label>
        <label>密码<input type="password" value={props.password} onChange={(event) => props.onPassword(event.target.value)} /></label>
        {props.error && <p className="error">{props.error}</p>}
        <button className="primary">{props.submitLabel}</button>
      </form>
    </div>
  );
}

function DashboardPage() {
  const { data, loading, reload } = useLoad<any>(() => api("/api/dashboard"), []);
  const [refreshingMTeam, setRefreshingMTeam] = useState(false);
  const [testingMTeam, setTestingMTeam] = useState(false);
  const [mteamStatusOverride, setMteamStatusOverride] = useState<{ success: boolean; message: string } | null>(null);
  const [dashboardError, setDashboardError] = useState("");

  useEffect(() => {
    const timer = window.setInterval(() => {
      reload()
        .then(() => setDashboardError(""))
        .catch((err) => setDashboardError((err as Error).message));
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function refreshDashboard() {
    if (refreshingMTeam) return;
    setRefreshingMTeam(true);
    setDashboardError("");
    try {
      await reload();
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

  if (loading || !data) return <Panel title="仪表盘"><p>正在加载运行数据...</p></Panel>;

  return (
    <div className="grid-page">
      <section className="metric-grid">
        <Metric title="总下载速度" value={formatSpeed(data.overview.total_download_speed)} source="qB 原始数据" />
        <Metric title="总上传速度" value={formatSpeed(data.overview.total_upload_speed)} source="qB 原始数据" />
        <Metric title="NAS 剩余空间" value={data.overview.nas_free_space_label} source="NAS 磁盘快照" />
        <Metric title="活跃任务" value={`${data.overview.download_tasks + data.overview.upload_tasks}`} source="qB 原始数据" />
      </section>

      <MTeamSnapshotPanel
        mteam={data.mteam}
        connection={data.mteam_connection}
        onRefresh={refreshDashboard}
        refreshing={refreshingMTeam}
        onTestConnection={testMTeamConnection}
        testingConnection={testingMTeam}
        statusOverride={mteamStatusOverride}
      />
      {dashboardError && <p className="error">{dashboardError}</p>}

      <Panel title="下载器">
        <div className="cards-row">
          {data.qbs.map((qb: any) => qb.locked ? <LockedCard key={qb.id} title={qb.name} message={qb.message} /> : <DownloaderCard key={qb.id} qb={qb} />)}
        </div>
      </Panel>
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
  statusOverride
}: {
  mteam: any;
  connection: any;
  onRefresh: () => void;
  refreshing: boolean;
  onTestConnection: () => void;
  testingConnection: boolean;
  statusOverride: { success: boolean; message: string } | null;
}) {
  const history = mteam.traffic_history ?? [];
  const maxTraffic = Math.max(1, ...history.map((point: any) => Math.max(point.upload_total ?? 0, point.download_total ?? 0)));
  const connected = statusOverride ? statusOverride.success : Boolean(connection?.enabled && connection?.last_test_success);
  const statusLabel = testingConnection ? "正在测试" : refreshing ? "正在刷新" : connected ? "连接正常" : "连接异常";
  const statusTitle = statusOverride?.message ?? connection?.message ?? statusLabel;

  return (
    <section className="panel">
      <div className="mteam-panel-header">
        <h2>站点用户数据 - 馒头</h2>
        <div className="mteam-status-tools" title={statusTitle}>
          <button className="status-dot-button" onClick={onTestConnection} disabled={testingConnection} title="测试 M-Team 连通性" aria-label="测试 M-Team 连通性">
            <span className={connected ? "status-dot online" : "status-dot offline"} />
          </button>
          <span className={connected ? "status-text online" : "status-text offline"}>{statusLabel}</span>
          <button className={refreshing ? "refresh-icon-button spinning" : "refresh-icon-button"} onClick={onRefresh} disabled={refreshing} title="重新抓取站点数据" aria-label="重新抓取站点数据">
            <RefreshCw size={17} />
          </button>
        </div>
      </div>
      <div className="mteam-stat-grid">
        <InfoTile icon={UserRound} label="用户等级" value={mteam.user_level ?? "User"} />
        <InfoTile icon={Coins} label="魔力值" value={numberLabel(mteam.bonus)} delta={mteam.bonus_delta_label} />
        <InfoTile icon={Percent} label="分享率" value={numberLabel(mteam.ratio, 3)} delta={mteam.ratio_delta_label} negative={String(mteam.ratio_delta_label ?? "").startsWith("-")} />
        <InfoTile icon={Upload} label="总上传量" value={formatBytesFixed(mteam.upload_total, 2)} delta={mteam.upload_delta_label} />
        <InfoTile icon={Download} label="总下载量" value={formatBytes(mteam.download_total)} delta={mteam.download_delta_label} />
        <InfoTile icon={Activity} label="当前活跃上传/下载" value={<ActiveTransferCounts upload={mteam.active_uploads ?? 0} download={mteam.active_downloads ?? 0} />} />
        <InfoTile icon={Database} label="总做种体积" value={formatBytes(mteam.seed_size ?? 0)} delta={mteam.seed_size_delta_label} negative={String(mteam.seed_size_delta_label ?? "").startsWith("-")} />
        <InfoTile icon={CalendarDays} label="加入时间" value={mteam.joined_at ?? "-"} />
      </div>
      <div className="traffic-chart">
        <h3>历史流量</h3>
        <div className="traffic-bars">
          {history.map((point: any) => (
            <div className="traffic-day" key={point.date} title={`${point.date} 上传 ${formatBytes(point.upload_total)} / 下载 ${formatBytes(point.download_total)}`}>
              <span className="traffic-upload" style={{ height: `${Math.max(8, (point.upload_total / maxTraffic) * 160)}px` }} />
              <span className="traffic-download" style={{ height: `${Math.max(6, (point.download_total / maxTraffic) * 160)}px` }} />
              <small>{String(point.date).slice(5).replace("-", "/")}</small>
            </div>
          ))}
        </div>
        <div className="legend"><span className="dot upload" />上传量<span className="dot download" />下载量</div>
      </div>
    </section>
  );
}

function ActiveTransferCounts({ upload, download }: { upload: number; download: number }) {
  return (
    <span className="transfer-counts" aria-label={`活跃上传 ${upload}，活跃下载 ${download}`}>
      <span className="transfer-count upload" title="活跃上传">
        <Upload size={16} />
        {upload}
      </span>
      <span className="transfer-count download" title="活跃下载">
        <Download size={16} />
        {download}
      </span>
    </span>
  );
}

function InfoTile({ icon: Icon, label, value, delta, negative }: { icon: typeof Film; label: string; value: ReactNode; delta?: string; negative?: boolean }) {
  return (
    <div className="info-tile">
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
        {delta && <span className={negative ? "delta negative" : "delta"}>{delta}</span>}
      </div>
      <span className="tile-icon"><Icon size={18} /></span>
    </div>
  );
}

function DiscoverPage() {
  const { data, error, loading } = useLoad<any>(() => api("/api/discover/lists"), []);
  const [query, setQuery] = useState("");
  const [media, setMedia] = useState<any[]>([]);
  const [torrents, setTorrents] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [hasSearched, setHasSearched] = useState(false);
  const [resourceSort, setResourceSort] = useState("seeders");
  const [resourceSortDirection, setResourceSortDirection] = useState<"asc" | "desc">("desc");
  const lists = data ? [
    { title: "流行趋势", items: data.trending },
    { title: "热门电影", items: data.popular_movies },
    { title: "热门剧集", items: data.popular_tv },
    { title: "Top Rated 电影", items: data.top_rated_movies },
    { title: "Top Rated 剧集", items: data.top_rated_tv }
  ] : [];
  const sortedTorrents = useMemo(() => sortResources(torrents, resourceSort, resourceSortDirection), [torrents, resourceSort, resourceSortDirection]);

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    const keyword = query.trim();
    if (!keyword) return;
    setSearching(true);
    setSearchError("");
    setHasSearched(true);
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

  return (
    <div className="grid-page">
      <form className="searchbar" onSubmit={runSearch}>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索电影、剧集、年份、制作组或关键词" />
        <button className="primary" disabled={searching}><Search size={18} /> {searching ? "搜索中..." : "搜索"}</button>
      </form>
      {searchError && <p className="error">{searchError}</p>}
      {hasSearched && (
        <div className="discover-search-results">
          <MediaSearchResults items={media} />
          <MTeamResourceResults
            items={sortedTorrents}
            sortBy={resourceSort}
            sortDirection={resourceSortDirection}
            onSortBy={setResourceSort}
            onSortDirection={setResourceSortDirection}
          />
        </div>
      )}
      {loading && <Panel title="发现"><p>正在从 TMDB 加载片单...</p></Panel>}
      {error && <Panel title="TMDB 获取失败"><p className="error">{error}</p></Panel>}
      {data && !data.configured && <Panel title="需要配置 TMDB"><p>{data.message}</p><p className="muted">进入“设置”，在 TMDB 配置里填写 API Key 或 Bearer Token，保存并启用后再回到发现页。</p></Panel>}
      {lists.map((list) => <PosterRail title={list.title} items={list.items} key={list.title} />)}
    </div>
  );
}

function SearchPage() {
  const [query, setQuery] = useState("");
  const [media, setMedia] = useState<any[]>([]);
  const [torrents, setTorrents] = useState<any[]>([]);

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    const [mediaResult, torrentResult] = await Promise.all([
      api<{ items: any[] }>(`/api/search/media?q=${encodeURIComponent(query)}`),
      api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(query)}`)
    ]);
    setMedia(mediaResult.items);
    setTorrents(torrentResult.items);
  }

  return (
    <div className="grid-page">
      <form className="searchbar" onSubmit={runSearch}>
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索电影、剧集、年份或关键词" />
        <button className="primary"><Search size={18} /> 搜索</button>
      </form>
      <PosterRail title="TMDB 媒体" items={media} />
      <Panel title="M-Team 资源">
        <div className="table-list">{torrents.map((item) => <ResourceRow item={item} key={item.id} />)}</div>
      </Panel>
    </div>
  );
}

function DownloadsPage() {
  const [downloader, setDownloader] = useState("qb1");
  const [grantOpen, setGrantOpen] = useState(false);
  const { data, error, reload } = useLoad<any>(() => api(`/api/qb/${downloader}/torrents`), [downloader]);
  const { data: summary, error: summaryError, reload: reloadSummary } = useLoad<any>(() => api(`/api/qb/${downloader}/summary`), [downloader]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      reload().catch(() => undefined);
      reloadSummary().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [downloader]);

  return (
    <div className="grid-page">
      <div className="segmented">
        {["qb1", "qb2", "qb3"].map((id) => <button className={downloader === id ? "active" : ""} onClick={() => setDownloader(id)} key={id}>{id.toUpperCase()}</button>)}
      </div>
      {error && downloader === "qb2" && <Panel title="qB 2 已锁定"><p>私有下载器需要管理员验证。</p><button className="primary" onClick={() => setGrantOpen(true)}><Lock size={16} /> 验证管理员</button></Panel>}
      {error && downloader !== "qb2" && <p className="error">{error}</p>}
      {grantOpen && <AdminGrant onDone={() => { setGrantOpen(false); reload(); }} />}
      {summary && <Panel title={`${downloader.toUpperCase()} 实时状态`}><div className="cards-row"><DownloaderCard qb={summary} /></div></Panel>}
      {summaryError && !error && <p className="error">{summaryError}</p>}
      {data && <Panel title={`${downloader.toUpperCase()} 任务`}><div className="table-list">{data.items.map((item: any) => <TorrentRow item={item} downloader={downloader} key={item.hash} />)}</div></Panel>}
    </div>
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

function StatsPage() {
  const { data } = useLoad<any>(() => api("/api/stats"), []);
  return <Panel title="统计"><p>{data?.explainability.formula ?? "正在加载..."}</p><div className="chart">{(data?.series ?? []).map((point: any, index: number) => <span style={{ height: `${Math.max(8, point.upload_speed / 50000)}px` }} title={`${point.downloader_id} ${point.completeness}`} key={index} />)}</div><p className="muted">来源：{data?.explainability.source}</p></Panel>;
}

function NotificationsPage() {
  const { data } = useLoad<any>(() => api("/api/notifications"), []);
  return <Panel title="通知中心"><div className="table-list">{(data?.items ?? []).map((item: any) => <div className="row" key={`${item.title}-${item.created_at}`}><strong>{item.title}</strong><span>{item.message}</span><small>{item.level} / {item.source}</small></div>)}</div></Panel>;
}

function SettingsPage() {
  const { data, reload } = useLoad<any>(() => api("/api/admin/integrations"), []);
  if (!data) return <Panel title="设置"><p>正在加载...</p></Panel>;

  return (
    <div className="grid-page">
      <Panel title="运行时凭据中心">
        <p className="muted">凭据由后端加密保存；保存过的 API、账号、密码和路径会直接回填在输入框里，可显示或复制。</p>
      </Panel>
      {data.providers.map((provider: any) => <IntegrationEditor provider={provider} onChanged={reload} key={provider.provider} />)}
    </div>
  );
}

function IntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  if (provider.provider === "mteam") return <MTeamIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (provider.provider === "tmdb") return <TmdbIntegrationEditor provider={provider} onChanged={onChanged} />;
  if (["qb1", "qb2", "qb3"].includes(provider.provider)) return <QbIntegrationEditor provider={provider} onChanged={onChanged} />;

  const providerNames: Record<string, string> = {
    ai: "AI",
    wechat_claw: "微信爪爪"
  };
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
  const label = provider.provider.toUpperCase();
  const saved = provider.saved_payload ?? {};
  const savedMapping = Array.isArray(saved.path_mappings) ? saved.path_mappings[0] ?? {} : {};
  const [form, setForm] = useState<QbForm>({
    name: String(saved.name ?? label),
    base_url: String(saved.base_url ?? ""),
    username: String(saved.username ?? ""),
    password: String(saved.password ?? ""),
    timeout: String(saved.timeout ?? "10"),
    default_save_path: String(saved.default_save_path ?? ""),
    category: String(saved.category ?? ""),
    tags: Array.isArray(saved.tags) ? saved.tags.join(",") : String(saved.tags ?? ""),
    path_from: String(savedMapping.from ?? ""),
    path_to: String(savedMapping.to ?? "")
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
    const mapping = form.path_from.trim() && form.path_to.trim()
      ? [{ from: form.path_from.trim(), to: form.path_to.trim() }]
      : [];
    return {
      name: form.name.trim() || label,
      base_url: form.base_url.trim(),
      username: form.username.trim(),
      password: form.password,
      timeout: Number(form.timeout) || 10,
      default_save_path: form.default_save_path.trim(),
      category: form.category.trim(),
      tags: form.tags.split(",").map((item) => item.trim()).filter(Boolean),
      path_mappings: mapping
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
          <span>必填：WebUI 地址、用户名、密码。保存路径、分类、标签和路径映射是添加任务与后续整理媒体文件时使用的可选项。</span>
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
          <label>默认分类（可选）
            <CopyableInput value={form.category} onChange={(value) => updateField("category", value)} placeholder="例如 movie / tv / anime" />
          </label>
          <label>默认标签（可选，逗号分隔）
            <CopyableInput value={form.tags} onChange={(value) => updateField("tags", value)} placeholder="例如 pt-media-hub,mteam" />
          </label>
          <label>下载器路径前缀（可选）
            <CopyableInput value={form.path_from} onChange={(value) => updateField("path_from", value)} placeholder="例如 /downloads" />
          </label>
          <label>本机/NAS 路径前缀（可选）
            <CopyableInput value={form.path_to} onChange={(value) => updateField("path_to", value)} placeholder="例如 Z:\\downloads 或 /volume1/downloads" />
          </label>
        </div>
        <div className="field-help">
          <strong>实际需要你提供：</strong>
          <span>局域网地址就是 qB WebUI 地址；账号密码用于登录 Web API；储存地址用于添加新任务时指定保存位置；映射路径用于以后把 qB 返回的路径对应到 NAS/本机媒体库路径。</span>
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
          <strong>用来读取馒头站点的用户数据与资源查询</strong>
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
  const [form, setForm] = useState<TmdbForm>({
    api_key: String(saved.api_key ?? ""),
    bearer_token: String(saved.bearer_token ?? ""),
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
      api_key: form.api_key.trim(),
      bearer_token: form.bearer_token.trim(),
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
        next_step: "如果你还没有测试，请点击“保存并测试”。测试成功后再启用 TMDB。"
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
      await api(`/api/admin/integrations/tmdb${provider.enabled ? "/disable" : "/enable"}`, { method: "POST" });
      onChanged();
    } catch (err) {
      setLocalError((err as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <Panel title="TMDB 配置">
      <div className="integration tmdb-editor">
        <div className="notice info">
          <strong>用来获取“发现”页的真实影视片单</strong>
          <span>API Key 和 Bearer Token 填其中一个即可；两个都填时会优先使用 Bearer Token。密钥默认用星号遮住，可点眼睛查看。</span>
        </div>
        <div className="settings-grid">
          <label>API Key
            <SecretInput value={form.api_key} onChange={(value) => updateField("api_key", value)} placeholder="例如 32 位左右的 TMDB API Key" autoComplete="off" />
          </label>
          <label>Bearer Token
            <SecretInput value={form.bearer_token} onChange={(value) => updateField("bearer_token", value)} placeholder="以 eyJ 开头的一长串访问令牌" autoComplete="off" />
          </label>
          <label>语言
            <CopyableInput value={form.language} onChange={(value) => updateField("language", value)} placeholder="zh-CN" />
          </label>
          <label>地区
            <CopyableInput value={form.region} onChange={(value) => updateField("region", value)} placeholder="CN" />
          </label>
          <label>超时时间（秒）
            <CopyableInput value={form.timeout} onChange={(value) => updateField("timeout", value)} inputMode="numeric" placeholder="12" />
          </label>
        </div>
        {form.api_key.trim() && form.bearer_token.trim() && <p className="muted">已同时填写 API Key 和 Bearer Token，测试和发现页会优先使用 Bearer Token。</p>}
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "显示高级设置"}
        </button>
        {showAdvanced && (
          <label>TMDB 接口地址
            <CopyableInput value={form.endpoint} onChange={(value) => updateField("endpoint", value)} placeholder="默认 https://api.themoviedb.org/3，通常不用修改" />
          </label>
        )}
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

function DiagnosticsPage() {
  const health = useLoad<any>(() => api("/api/diagnostics/health"), []);
  const traces = useLoad<any>(() => api("/api/diagnostics/traces"), []);
  const [exportPayload, setExportPayload] = useState<any | null>(null);

  return (
    <div className="grid-page">
      <Panel title="健康概览"><div className="table-list">{(health.data?.modules ?? []).map((item: any) => <div className="row" key={item.module}><strong>{item.module}</strong><span>{item.status}</span><small>{item.enabled ? "已启用" : "已停用"}</small></div>)}</div></Panel>
      <Panel title="调用轨迹"><div className="table-list">{(traces.data?.items ?? []).map((item: any) => <div className="row" key={item.trace_id}><strong>{item.trace_id}</strong><span>{item.event_type} / {item.status}</span><small>{item.duration_ms}ms</small></div>)}</div><button onClick={() => api<any>("/api/diagnostics/export", { method: "POST" }).then(setExportPayload)}>导出脱敏 JSON</button>{exportPayload && <pre>{JSON.stringify(exportPayload, null, 2)}</pre>}</Panel>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return <section className="panel"><h2>{title}</h2>{children}</section>;
}

function Metric({ title, value, source }: { title: string; value: string; source: string }) {
  return <div className="metric"><small>{title}</small><strong>{value}</strong><span>{source}</span></div>;
}

function DownloaderCard({ qb }: { qb: any }) {
  return (
    <div className="data-card">
      <h3>{qb.name}</h3>
      <div className="downloader-live-row">
        <span>活跃资源</span>
        <ActiveTransferCounts upload={qb.active_uploads ?? 0} download={qb.active_downloads ?? 0} />
      </div>
      <p>下载 {formatSpeed(qb.download_speed)}</p>
      <p>上传 {formatSpeed(qb.upload_speed)}</p>
      <small>{qb.source}</small>
    </div>
  );
}

function LockedCard({ title, message }: { title: string; message: string }) {
  return <div className="data-card locked"><Lock size={20} /><h3>{title}</h3><p>{message}</p></div>;
}

function MediaSearchResults({ items = [] }: { items: any[] }) {
  return (
    <Panel title="TMDB 媒体结果">
      <div className="media-result-grid">
        {items.map((item) => <MediaResultCard item={item} key={item.id} />)}
        {!items.length && <p className="muted">没有搜索到 TMDB 媒体，或 TMDB 尚未启用。</p>}
      </div>
    </Panel>
  );
}

function MediaResultCard({ item }: { item: any }) {
  const cast = (item.cast ?? []).slice(0, 5).join(" / ");
  const genres = (item.genres ?? []).slice(0, 4);
  const meta = [
    item.media_type === "tv" ? "剧集" : "电影",
    item.year,
    item.runtime ? `${item.runtime} 分钟` : "",
    item.original_language ? String(item.original_language).toUpperCase() : ""
  ].filter(Boolean).join(" / ");

  return (
    <article className="media-result-card">
      {item.backdrop && <img className="media-backdrop" src={item.backdrop} alt="" />}
      <div className="media-card-shade" />
      <img className="media-poster" src={item.poster} alt="" />
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
          <InfoPill icon={Users} text={`${item.vote_count ?? 0} 票`} />
          {item.popularity ? <InfoPill icon={Activity} text={`热度 ${item.popularity}`} /> : null}
        </div>
        {genres.length > 0 && <div className="chip-row">{genres.map((genre: string) => <span className="soft-chip" key={genre}>{genre}</span>)}</div>}
        <p className="media-overview">{item.overview || "暂无简介。"}</p>
        <div className="media-credit-grid">
          <span><strong>导演</strong>{item.director || "-"}</span>
          <span><strong>主演</strong>{cast || "-"}</span>
          {item.release_date && <span><strong>上映</strong>{item.release_date}</span>}
          {item.imdb_id && <span><strong>IMDb</strong>{item.imdb_id}</span>}
        </div>
      </div>
    </article>
  );
}

function MTeamResourceResults({
  items = [],
  sortBy,
  sortDirection,
  onSortBy,
  onSortDirection
}: {
  items: any[];
  sortBy: string;
  sortDirection: "asc" | "desc";
  onSortBy: (value: string) => void;
  onSortDirection: (value: "asc" | "desc") => void;
}) {
  return (
    <section className="panel">
      <div className="resource-panel-header">
        <h2>M-Team 资源结果</h2>
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
        {items.map((item) => <MTeamResourceCard item={item} key={item.id} />)}
        {!items.length && <p className="muted">没有搜索到 M-Team 资源，或 M-Team 尚未启用。</p>}
      </div>
    </section>
  );
}

function MTeamResourceCard({ item }: { item: any }) {
  async function download() {
    await api("/api/qb/qb1/torrents", { method: "POST", body: JSON.stringify({ payload: item }) });
    alert("已提交到 qB 1。");
  }

  return (
    <article className="mteam-resource-card">
      <div className="resource-main">
        <div className="resource-title-line">
          <strong>{item.title}</strong>
          {item.promotion_label && <span className="free-chip" title={promotionTitle(item)}>{item.promotion_label}</span>}
        </div>
        <div className="chip-row">
          {(item.labels ?? []).map((label: string) => <span className="resource-chip" key={label}>{label}</span>)}
          {item.resolution && item.resolution !== "-" && <span className="resource-chip">{item.resolution}</span>}
          {item.codec && item.codec !== "-" && <span className="resource-chip">{item.codec}</span>}
          {item.audio_codec && <span className="resource-chip">{item.audio_codec}</span>}
          {item.hdr && <span className="resource-chip">{item.hdr}</span>}
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
        <button onClick={download}>下载到 qB 1</button>
      </aside>
    </article>
  );
}

function InfoPill({ icon: Icon, text, tone }: { icon: typeof Film; text: string; tone?: "up" | "down" | "size" | "seed" }) {
  return <span className={tone ? `info-pill ${tone}` : "info-pill"}><Icon size={14} />{text}</span>;
}

function PosterRail({ title, items = [] }: { title: string; items: any[] }) {
  return <Panel title={title}><div className="poster-rail">{items.map((item) => <article className="poster" key={item.id}><img src={item.poster} alt="" /><strong>{item.title}</strong><span>{item.media_type === "tv" ? "剧集" : "电影"} / {item.year} / {item.rating}</span></article>)}</div></Panel>;
}

function ResourceRow({ item }: { item: any }) {
  async function download() {
    await api("/api/qb/qb1/torrents", { method: "POST", body: JSON.stringify({ payload: item }) });
    alert("已提交到 qB 1。");
  }

  return <div className="row"><strong>{item.title}</strong><span>{item.resolution} / {item.codec} / {item.size} / 做种 {item.seeders}</span><button onClick={download}>下载到 qB 1</button></div>;
}

function TorrentRow({ item, downloader }: { item: any; downloader: string }) {
  async function mutate(action: string) {
    await api(`/api/qb/${downloader}/torrents/${item.hash}/${action}`, { method: "POST", body: JSON.stringify({ payload: {} }) });
    alert(`${action} 已提交到 qB。`);
  }

  return <div className="row"><strong>{item.name}</strong><span>{Math.round(item.progress * 100)}% / {formatSpeed(item.download_speed)} / {item.state}</span><div className="actions"><button onClick={() => mutate("pause")}>暂停</button><button onClick={() => mutate("resume")}>继续</button><button onClick={() => mutate("tags")}>打标签</button></div></div>;
}

function numberLabel(value: number, digits = 0): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
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
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
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

function promotionTitle(item: any): string {
  const parts = [];
  if (item.promotion_until) parts.push(`截止：${formatDateLabel(item.promotion_until)}`);
  if (item.discount) parts.push(`原始枚举：${item.discount}`);
  return parts.join(" / ") || "促销";
}

const pages: Record<NavKey, (props: { user: User }) => ReactNode> = {
  discover: DiscoverPage,
  search: SearchPage,
  dashboard: DashboardPage,
  downloads: DownloadsPage,
  stats: StatsPage,
  notifications: NotificationsPage,
  settings: SettingsPage,
  diagnostics: DiagnosticsPage
};
