import {
  Activity,
  Bell,
  CalendarDays,
  Coins,
  Database,
  Download,
  Film,
  Gauge,
  Lock,
  LogOut,
  Percent,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Upload,
  UserRound,
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
  const [query, setQuery] = useState("");
  const [torrents, setTorrents] = useState<any[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [refreshingMTeam, setRefreshingMTeam] = useState(false);
  const [testingMTeam, setTestingMTeam] = useState(false);
  const [mteamStatusOverride, setMteamStatusOverride] = useState<{ success: boolean; message: string } | null>(null);
  const [dashboardError, setDashboardError] = useState("");

  async function runMTeamSearch(event: FormEvent) {
    event.preventDefault();
    setSearching(true);
    setSearchError("");
    try {
      const result = await api<{ items: any[] }>(`/api/search/mteam?q=${encodeURIComponent(query)}`);
      setTorrents(result.items);
    } catch (err) {
      setSearchError((err as Error).message);
    } finally {
      setSearching(false);
    }
  }

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

      <Panel title="M-Team 站内查询">
        <form className="searchbar" onSubmit={runMTeamSearch}>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索电影、剧集、年份、制作组或关键词" />
          <button className="primary" disabled={searching}><Search size={18} /> {searching ? "查询中..." : "查询"}</button>
        </form>
        {searchError && <p className="error">{searchError}</p>}
        <div className="table-list">
          {torrents.map((item) => <ResourceRow item={item} key={item.id} />)}
          {!torrents.length && <p className="muted">输入关键词后可直接查询馒头站点资源。</p>}
        </div>
      </Panel>

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
        <InfoTile icon={Coins} label="积分" value={numberLabel(mteam.bonus)} delta={mteam.bonus_delta_label} />
        <InfoTile icon={Percent} label="分享率" value={numberLabel(mteam.ratio, 3)} delta={mteam.ratio_delta_label} negative={String(mteam.ratio_delta_label ?? "").startsWith("-")} />
        <InfoTile icon={Upload} label="总上传量" value={formatBytes(mteam.upload_total)} delta={mteam.upload_delta_label} />
        <InfoTile icon={Download} label="总下载量" value={formatBytes(mteam.download_total)} delta={mteam.download_delta_label} />
        <InfoTile icon={Activity} label="总做种数" value={String(mteam.seed_count ?? mteam.active_uploads ?? 0)} delta={mteam.seed_count_delta_label} />
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

function InfoTile({ icon: Icon, label, value, delta, negative }: { icon: typeof Film; label: string; value: string; delta?: string; negative?: boolean }) {
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
  const lists = data ? [
    { title: "流行趋势", items: data.trending },
    { title: "热门电影", items: data.popular_movies },
    { title: "热门剧集", items: data.popular_tv },
    { title: "Top Rated 电影", items: data.top_rated_movies },
    { title: "Top Rated 剧集", items: data.top_rated_tv }
  ] : [];

  return (
    <div className="grid-page">
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

  return (
    <div className="grid-page">
      <div className="segmented">
        {["qb1", "qb2", "qb3"].map((id) => <button className={downloader === id ? "active" : ""} onClick={() => setDownloader(id)} key={id}>{id.toUpperCase()}</button>)}
      </div>
      {error && downloader === "qb2" && <Panel title="qB 2 已锁定"><p>私有下载器需要管理员验证。</p><button className="primary" onClick={() => setGrantOpen(true)}><Lock size={16} /> 验证管理员</button></Panel>}
      {grantOpen && <AdminGrant onDone={() => { setGrantOpen(false); reload(); }} />}
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
        <p className="muted">凭据由后端加密保存，保存后前端只会看到脱敏摘要。</p>
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
  const [text, setText] = useState("");
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
        <textarea value={text} onChange={(event) => setText(event.target.value)} placeholder="粘贴接口地址、请求头、密钥或 webhook 配置" />
        <div className="actions">
          <button onClick={draft}>保存草稿</button>
          <button onClick={() => save("/test")}>保存并测试</button>
          <button onClick={() => save(provider.enabled ? "/disable" : "/enable")}>{provider.enabled ? "停用" : "启用"}</button>
        </div>
        <RedactedSummary provider={provider} />
      </div>
    </Panel>
  );
}

function QbIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const label = provider.provider.toUpperCase();
  const [form, setForm] = useState<QbForm>({
    name: label,
    base_url: "",
    username: "",
    password: "",
    timeout: "10",
    default_save_path: "",
    category: "",
    tags: "",
    path_from: "",
    path_to: ""
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
            <input value={form.name} onChange={(event) => updateField("name", event.target.value)} placeholder="例如 主下载器 / 动漫下载器" />
          </label>
          <label>qB WebUI 地址（必填）
            <input value={form.base_url} onChange={(event) => updateField("base_url", event.target.value)} placeholder="例如 http://192.168.1.20:8080" />
          </label>
          <label>用户名（必填）
            <input value={form.username} onChange={(event) => updateField("username", event.target.value)} placeholder="qB WebUI 用户名" autoComplete="off" />
          </label>
          <label>密码（必填）
            <input type="password" value={form.password} onChange={(event) => updateField("password", event.target.value)} placeholder="qB WebUI 密码" autoComplete="new-password" />
          </label>
          <label>超时时间（秒）
            <input value={form.timeout} onChange={(event) => updateField("timeout", event.target.value)} inputMode="numeric" placeholder="10" />
          </label>
          <label>默认保存路径（可选）
            <input value={form.default_save_path} onChange={(event) => updateField("default_save_path", event.target.value)} placeholder="例如 /downloads/media 或 D:\\Downloads" />
          </label>
          <label>默认分类（可选）
            <input value={form.category} onChange={(event) => updateField("category", event.target.value)} placeholder="例如 movie / tv / anime" />
          </label>
          <label>默认标签（可选，逗号分隔）
            <input value={form.tags} onChange={(event) => updateField("tags", event.target.value)} placeholder="例如 pt-media-hub,mteam" />
          </label>
          <label>下载器路径前缀（可选）
            <input value={form.path_from} onChange={(event) => updateField("path_from", event.target.value)} placeholder="例如 /downloads" />
          </label>
          <label>本机/NAS 路径前缀（可选）
            <input value={form.path_to} onChange={(event) => updateField("path_to", event.target.value)} placeholder="例如 Z:\\downloads 或 /volume1/downloads" />
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
        <RedactedSummary provider={provider} />
      </div>
    </Panel>
  );
}

function MTeamIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const [form, setForm] = useState<MTeamForm>({
    base_url: "https://kp.m-team.cc",
    api_key: "",
    cookie: "",
    user_agent: "PT-Media-Hub",
    authorization: "",
    passkey: "",
    timeout: "10"
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
        explanation: "M-Team 凭据已经加密保存到后端，页面只显示脱敏摘要。",
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
          <span>真实 API 需要 M-Team API Key；Cookie 和 Passkey 只作为兼容字段保存。敏感字段保存后只显示脱敏摘要。</span>
        </div>
        <div className="settings-grid">
          <label>站点地址
            <input value={form.base_url} onChange={(event) => updateField("base_url", event.target.value)} placeholder="https://kp.m-team.cc" />
          </label>
          <label>API Key
            <input value={form.api_key} onChange={(event) => updateField("api_key", event.target.value)} placeholder="从 M-Team 个人资料复制 API Key" autoComplete="off" />
          </label>
          <label>User-Agent
            <input value={form.user_agent} onChange={(event) => updateField("user_agent", event.target.value)} placeholder="浏览器或 PT-Media-Hub" />
          </label>
          <label>Cookie
            <input value={form.cookie} onChange={(event) => updateField("cookie", event.target.value)} placeholder="从浏览器复制完整 Cookie" autoComplete="off" />
          </label>
          <label>Passkey
            <input value={form.passkey} onChange={(event) => updateField("passkey", event.target.value)} placeholder="可选，下载链接使用；不是 API Key" autoComplete="off" />
          </label>
          <label>超时时间（秒）
            <input value={form.timeout} onChange={(event) => updateField("timeout", event.target.value)} inputMode="numeric" placeholder="10" />
          </label>
        </div>
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "显示高级设置"}
        </button>
        {showAdvanced && (
          <label>Authorization
            <input value={form.authorization} onChange={(event) => updateField("authorization", event.target.value)} placeholder="可选，例如 Bearer token" autoComplete="off" />
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
        <RedactedSummary provider={provider} />
      </div>
    </Panel>
  );
}

function TmdbIntegrationEditor({ provider, onChanged }: { provider: any; onChanged: () => void }) {
  const [form, setForm] = useState<TmdbForm>({
    api_key: "",
    bearer_token: "",
    language: "zh-CN",
    region: "CN",
    timeout: "12",
    endpoint: ""
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
        explanation: "密钥已经加密保存到后端，页面不会再显示完整密钥。",
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
          <span>API Key 和 Bearer Token 填其中一个即可；两个都填时会优先使用 Bearer Token。密钥保存后只显示脱敏摘要。</span>
        </div>
        <div className="settings-grid">
          <label>API Key
            <input value={form.api_key} onChange={(event) => updateField("api_key", event.target.value)} placeholder="例如 32 位左右的 TMDB API Key" autoComplete="off" />
          </label>
          <label>Bearer Token
            <input value={form.bearer_token} onChange={(event) => updateField("bearer_token", event.target.value)} placeholder="以 eyJ 开头的一长串访问令牌" autoComplete="off" />
          </label>
          <label>语言
            <input value={form.language} onChange={(event) => updateField("language", event.target.value)} placeholder="zh-CN" />
          </label>
          <label>地区
            <input value={form.region} onChange={(event) => updateField("region", event.target.value)} placeholder="CN" />
          </label>
          <label>超时时间（秒）
            <input value={form.timeout} onChange={(event) => updateField("timeout", event.target.value)} inputMode="numeric" placeholder="12" />
          </label>
        </div>
        {form.api_key.trim() && form.bearer_token.trim() && <p className="muted">已同时填写 API Key 和 Bearer Token，测试和发现页会优先使用 Bearer Token。</p>}
        <button className="inline-tool" type="button" onClick={() => setShowAdvanced((value) => !value)}>
          <SlidersHorizontal size={16} /> {showAdvanced ? "隐藏高级设置" : "显示高级设置"}
        </button>
        {showAdvanced && (
          <label>TMDB 接口地址
            <input value={form.endpoint} onChange={(event) => updateField("endpoint", event.target.value)} placeholder="默认 https://api.themoviedb.org/3，通常不用修改" />
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
        <RedactedSummary provider={provider} />
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

function RedactedSummary({ provider }: { provider: any }) {
  if (Object.keys(provider.redacted_summary ?? {}).length === 0) return null;
  return (
    <div className="redacted-summary">
      <strong>已保存的信息</strong>
      <pre>{JSON.stringify(provider.redacted_summary, null, 2)}</pre>
    </div>
  );
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
  return <div className="data-card"><h3>{qb.name}</h3><p>下载 {formatSpeed(qb.download_speed)}</p><p>上传 {formatSpeed(qb.upload_speed)}</p><small>{qb.source}</small></div>;
}

function LockedCard({ title, message }: { title: string; message: string }) {
  return <div className="data-card locked"><Lock size={20} /><h3>{title}</h3><p>{message}</p></div>;
}

function PosterRail({ title, items = [] }: { title: string; items: any[] }) {
  return <Panel title={title}><div className="poster-rail">{items.map((item) => <article className="poster" key={item.id}><img src={item.poster} alt="" /><strong>{item.title}</strong><span>{item.media_type === "tv" ? "剧集" : "电影"} / {item.year} / {item.rating}</span></article>)}</div></Panel>;
}

function ResourceRow({ item }: { item: any }) {
  async function download() {
    await api("/api/qb/qb1/torrents", { method: "POST", body: JSON.stringify({ payload: item }) });
    alert("已提交到 qB 1 Mock 适配器。");
  }

  return <div className="row"><strong>{item.title}</strong><span>{item.resolution} / {item.codec} / {item.size} / 做种 {item.seeders}</span><button onClick={download}>下载到 qB 1</button></div>;
}

function TorrentRow({ item, downloader }: { item: any; downloader: string }) {
  async function mutate(action: string) {
    await api(`/api/qb/${downloader}/torrents/${item.hash}/${action}`, { method: "POST", body: JSON.stringify({ payload: {} }) });
    alert(`${action} 已被 Mock 适配器接受。`);
  }

  return <div className="row"><strong>{item.name}</strong><span>{Math.round(item.progress * 100)}% / {formatSpeed(item.download_speed)} / {item.state}</span><div className="actions"><button onClick={() => mutate("pause")}>暂停</button><button onClick={() => mutate("resume")}>继续</button><button onClick={() => mutate("tags")}>打标签</button></div></div>;
}

function numberLabel(value: number, digits = 0): string {
  if (typeof value !== "number" || Number.isNaN(value)) return "-";
  return value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
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
