import { useState, useEffect, useRef } from 'react'
import './App.css'

const API = '/api'

type Subscription = { id: number; url: string; name: string }

type ProxyItem = { name: string; type: string; now?: string; all?: string[] }
type ProxiesRes = { proxies: Record<string, ProxyItem> }

type Tab = 'subs' | 'nodes' | 'test' | 'traffic'
const TAB_STORAGE_KEY = 'clash-manager-tab'

function getInitialTab(): Tab {
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY)
    if (saved === 'subs' || saved === 'nodes' || saved === 'test' || saved === 'traffic') return saved
  } catch (_) {}
  return 'subs'
}

type ConnectionMeta = {
  host?: string
  sourceIP?: string
  destinationIP?: string
  destinationPort?: string
  processPath?: string
  type?: string
}
type Connection = {
  id: string
  metadata: ConnectionMeta
  upload: number
  download: number
  chains?: string[]
  start?: string
}
type ConnectionsRes = {
  uploadTotal: number
  downloadTotal: number
  connections: Connection[]
}

function formatBytes(n: number): string {
  if (n >= 1024 * 1024 * 1024) return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB'
  if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(2) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(2) + ' KB'
  return n + ' B'
}
function formatSpeed(Bps: number): string {
  if (Bps >= 1024 * 1024) return (Bps / (1024 * 1024)).toFixed(2) + ' MB/s'
  if (Bps >= 1024) return (Bps / 1024).toFixed(2) + ' KB/s'
  return Bps + ' B/s'
}

type ClashMode = 'global' | 'rule' | 'direct' | 'script'

type SiteResult = { name: string; url: string; ok: boolean; status_code: number | null; time_ms: number | null; error: string | null }
type IpResult = { ip: string | null; city: string | null; region: string | null; country: string | null; org: string | null; error: string | null }

const TEST_SITES: { name: string; url: string }[] = [
  { name: 'Google', url: 'https://www.google.com' },
  { name: 'Facebook', url: 'https://www.facebook.com' },
  { name: 'YouTube', url: 'https://www.youtube.com' },
  { name: 'Bilibili', url: 'https://www.bilibili.com' },
]

type TestSlot = 'idle' | 'testing' | 'done'
type TestState = {
  with_proxy: { sites: { name: string; status: TestSlot; result: SiteResult | null }[]; ip: { status: TestSlot; result: IpResult | null } }
  without_proxy: { sites: { name: string; status: TestSlot; result: SiteResult | null }[]; ip: { status: TestSlot; result: IpResult | null } }
}

function getInitialTestState(): TestState {
  return {
    with_proxy: {
      sites: TEST_SITES.map((s) => ({ name: s.name, status: 'idle' as TestSlot, result: null as SiteResult | null })),
      ip: { status: 'idle', result: null },
    },
    without_proxy: {
      sites: TEST_SITES.map((s) => ({ name: s.name, status: 'idle' as TestSlot, result: null as SiteResult | null })),
      ip: { status: 'idle', result: null },
    },
  }
}

const MODE_LABELS: { value: ClashMode; label: string }[] = [
  { value: 'global', label: 'Global' },
  { value: 'rule', label: 'Rule' },
  { value: 'direct', label: 'Direct' },
  { value: 'script', label: 'Script' },
]

function App() {
  const [tab, setTab] = useState<Tab>(getInitialTab)
  const [subs, setSubs] = useState<Subscription[]>([])
  const [currentSubId, setCurrentSubId] = useState<number | null>(null)
  const [subUrl, setSubUrl] = useState('')
  const [subName, setSubName] = useState('')
  const [proxies, setProxies] = useState<ProxiesRes | null>(null)
  const [clashMode, setClashMode] = useState<ClashMode>('rule')
  const [globalGroupName, setGlobalGroupName] = useState<string | null>(null)
  const [clashOk, setClashOk] = useState<boolean | null>(null)
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [delayTesting, setDelayTesting] = useState<string | null>(null)
  const [delayResult, setDelayResult] = useState<Record<string, number>>({})
  const [testLoading, setTestLoading] = useState(false)
  const [testState, setTestState] = useState<TestState>(getInitialTestState)
  const [proxyInfo, setProxyInfo] = useState<{ proxy_url: string; proxy_port: number } | null>(null)
  const [connections, setConnections] = useState<ConnectionsRes | null>(null)
  const [trafficSpeed, setTrafficSpeed] = useState<{ upload: number; download: number }>({ upload: 0, download: 0 })
  const [proxyClients, setProxyClients] = useState<{ pid: number; name: string; cmdline: string }[]>([])
  const [restarting, setRestarting] = useState(false)
  const trafficPrevRef = useRef<{ uploadTotal: number; downloadTotal: number; ts: number } | null>(null)

  const fetchSubs = async () => {
    const r = await fetch(`${API}/subscriptions`)
    if (!r.ok) throw new Error(r.statusText)
    setSubs(await r.json())
  }

  const fetchCurrentSub = async () => {
    const r = await fetch(`${API}/subscriptions/current`)
    if (!r.ok) return
    const data = await r.json()
    setCurrentSubId(data.current_id ?? null)
  }

  const fetchProxies = async () => {
    const r = await fetch(`${API}/clash/proxies`)
    if (!r.ok) throw new Error(r.statusText)
    setProxies(await r.json())
  }

  const fetchConfigs = async () => {
    const r = await fetch(`${API}/clash/configs`)
    if (!r.ok) return
    const data = await r.json()
    const mode = data.mode
    if (mode === 'global' || mode === 'rule' || mode === 'direct' || mode === 'script') {
      setClashMode(mode)
    }
  }

  const fetchGlobalGroupName = async () => {
    const r = await fetch(`${API}/clash/global-group-name`)
    if (!r.ok) return
    const data = await r.json()
    setGlobalGroupName(data.global_group_name ?? null)
  }

  const checkHealth = async () => {
    const r = await fetch(`${API}/health`)
    const data = await r.json()
    setClashOk(data.clash_reachable ?? false)
  }

  useEffect(() => {
    fetchSubs().catch(console.error)
    fetchCurrentSub().catch(console.error)
    checkHealth().catch(console.error)
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(TAB_STORAGE_KEY, tab)
    } catch (_) {}
  }, [tab])

  useEffect(() => {
    if (clashOk) {
      fetchProxies().catch(console.error)
      fetchConfigs().catch(console.error)
      fetchGlobalGroupName().catch(console.error)
      fetch(`${API}/clash/proxy-info`)
        .then((r) => r.ok ? r.json() : null)
        .then((d) => d && setProxyInfo({ proxy_url: d.proxy_url, proxy_port: d.proxy_port }))
        .catch(() => setProxyInfo(null))
    } else {
      setProxyInfo(null)
      setConnections(null)
      setProxyClients([])
      setTrafficSpeed({ upload: 0, download: 0 })
      trafficPrevRef.current = null
    }
  }, [clashOk])

  // 每 2 秒拉取连接列表、代理客户端，并计算实时速率
  useEffect(() => {
    if (!clashOk) return
    const tick = async () => {
      try {
        const [connR, clientsR] = await Promise.all([
          fetch(`${API}/clash/connections`),
          fetch(`${API}/clash/proxy-clients`),
        ])
        if (connR.ok) {
          const data: ConnectionsRes = await connR.json()
          setConnections(data)
          const now = Date.now() / 1000
          const prev = trafficPrevRef.current
          if (prev && now > prev.ts) {
            const dt = now - prev.ts
            if (dt > 0) {
              setTrafficSpeed({
                upload: (data.uploadTotal - prev.uploadTotal) / dt,
                download: (data.downloadTotal - prev.downloadTotal) / dt,
              })
            }
          }
          trafficPrevRef.current = {
            uploadTotal: data.uploadTotal,
            downloadTotal: data.downloadTotal,
            ts: now,
          }
        }
        if (clientsR.ok) {
          const { clients } = await clientsR.json()
          setProxyClients(clients || [])
        }
      } catch (_) {}
    }
    tick()
    const interval = setInterval(tick, 2000)
    return () => clearInterval(interval)
  }, [clashOk])

  const addSub = async () => {
    if (!subUrl.trim()) return
    setError(null)
    setLoading('add')
    try {
      const r = await fetch(`${API}/subscriptions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: subUrl.trim(), name: subName.trim() }),
      })
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
      await fetchSubs()
      setSubUrl('')
      setSubName('')
    } catch (e) {
      setError(e instanceof Error ? e.message : '添加失败')
    } finally {
      setLoading(null)
    }
  }

  const applySub = async (id: number) => {
    setError(null)
    setLoading(`apply-${id}`)
    try {
      const r = await fetch(`${API}/subscriptions/${id}/apply`, { method: 'POST' })
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
      await fetchProxies()
      await checkHealth()
      await fetchCurrentSub()
      await fetchGlobalGroupName()
    } catch (e) {
      setError(e instanceof Error ? e.message : '应用订阅失败')
    } finally {
      setLoading(null)
    }
  }

  const deleteSub = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation()
    setError(null)
    try {
      const r = await fetch(`${API}/subscriptions/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
      await fetchSubs()
      await fetchCurrentSub()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  const selectProxy = async (groupName: string, nodeName: string) => {
    setError(null)
    try {
      const r = await fetch(`${API}/clash/proxies/${encodeURIComponent(groupName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nodeName }),
      })
      if (!r.ok) throw new Error(r.statusText)
      await fetchProxies()
    } catch (e) {
      setError(e instanceof Error ? e.message : '切换节点失败')
    }
  }

  const setMode = async (mode: ClashMode) => {
    setError(null)
    try {
      const r = await fetch(`${API}/clash/configs`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      })
      if (!r.ok) throw new Error(r.statusText)
      setClashMode(mode)
    } catch (e) {
      setError(e instanceof Error ? e.message : '切换模式失败')
    }
  }

  const runProxyTest = () => {
    setError(null)
    setTestState((prev) => ({
      with_proxy: {
        ...prev.with_proxy,
        sites: prev.with_proxy.sites.map((s) => ({ ...s, status: 'testing' as TestSlot })),
        ip: prev.with_proxy.ip,
      },
      without_proxy: {
        ...prev.without_proxy,
        sites: prev.without_proxy.sites.map((s) => ({ ...s, status: 'testing' as TestSlot })),
        ip: prev.without_proxy.ip,
      },
    }))
    setTestLoading(true)
    const eventSource = new EventSource(`${window.location.origin}${API}/test/proxy/stream`)
    eventSource.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data)
        if (data.type === 'done') {
          eventSource.close()
          setTestLoading(false)
          return
        }
        if (data.type === 'error') {
          setError(data.error || '测试出错')
          return
        }
        const side = data.side as 'with_proxy' | 'without_proxy'
        setTestState((prev) => {
          const next = { ...prev, [side]: { ...prev[side] } }
          if (data.type === 'site') {
            next[side].sites = prev[side].sites.map((s) =>
              s.name === data.name ? { ...s, status: 'done' as TestSlot, result: data.result } : s
            )
          } else {
            next[side].ip = { status: 'done', result: data.result }
          }
          return next
        })
      } catch (_) {}
    }
    eventSource.onerror = () => {
      eventSource.close()
      setTestLoading(false)
    }
  }

  const startOrRestartClash = async () => {
    setError(null)
    setRestarting(true)
    try {
      const r = await fetch(`${API}/clash/start-or-restart`, { method: 'POST' })
      if (!r.ok) throw new Error('操作失败')
      await checkHealth()
      await fetchProxies().catch(() => {})
      await fetchConfigs().catch(() => {})
      await fetchGlobalGroupName().catch(() => {})
    } catch (e) {
      setError(e instanceof Error ? e.message : '启动/重启失败')
    } finally {
      setRestarting(false)
    }
  }

  const testDelay = async (nodeName: string) => {
    setDelayTesting(nodeName)
    try {
      const r = await fetch(
        `${API}/clash/proxies/${encodeURIComponent(nodeName)}/delay?timeout=5000`
      )
      if (!r.ok) throw new Error('测速失败')
      const data = await r.json()
      const ms =
        typeof data.delay === 'number' && data.delay >= 0 ? data.delay : -1
      setDelayResult((prev) => ({ ...prev, [nodeName]: ms }))
    } catch {
      setDelayResult((prev) => ({ ...prev, [nodeName]: -1 }))
    } finally {
      setDelayTesting(null)
    }
  }

  const allGroups = proxies
    ? Object.entries(proxies.proxies || {}).filter(
        ([_, p]) => p.all && p.all.length > 0 && p.type === 'Selector'
      )
    : []

  // 按模式过滤列表内容（与 CfW 一致：切换 tab 会改变下面显示的内容）
  const displayGroups = (() => {
    if (clashMode === 'global') {
      // 优先用配置文件里第一个 proxy-group 的名称（与 CfW 一致）
      if (globalGroupName) {
        const g = allGroups.find(([name]) => name === globalGroupName)
        if (g) return [g]
      }
      const globalGroup = allGroups.find(
        ([name]) =>
          name.toUpperCase() === 'GLOBAL' ||
          name === 'Proxy' ||
          name === 'Proxies' ||
          name === '节点选择' ||
          name === '全球' ||
          name === '代理'
      )
      return globalGroup ? [globalGroup] : (allGroups.length ? [allGroups[0]] : [])
    }
    if (clashMode === 'direct') {
      return [] // Direct 模式下列表用 directProxies 渲染
    }
    // rule / script：显示所有代理组
    return allGroups
  })()

  const directProxies =
    clashMode === 'direct' && proxies?.proxies
      ? Object.entries(proxies.proxies).filter(([_, p]) => {
          const t = (p.type || '').toLowerCase()
          return t === 'direct' || t === 'reject'
        })
      : []

  return (
    <div className="cfw-layout">
      <aside className="sidebar">
        <div className="sidebar-traffic">
          <span>↑ {formatSpeed(trafficSpeed.upload)}</span>
          <span>↓ {formatSpeed(trafficSpeed.download)}</span>
        </div>
        <nav className="sidebar-nav">
          <button
            type="button"
            className={tab === 'subs' ? 'active' : ''}
            onClick={() => setTab('subs')}
          >
            管理订阅
          </button>
          <button
            type="button"
            className={tab === 'nodes' ? 'active' : ''}
            onClick={() => setTab('nodes')}
          >
            节点选择
          </button>
          <button
            type="button"
            className={tab === 'traffic' ? 'active' : ''}
            onClick={() => setTab('traffic')}
          >
            流量
          </button>
          <button
            type="button"
            className={tab === 'test' ? 'active' : ''}
            onClick={() => setTab('test')}
          >
            测试
          </button>
        </nav>
        <div className="sidebar-status">
          <span className={`status-dot ${clashOk ? 'connected' : ''}`} />
          {clashOk ? 'Connected' : 'Disconnected'}
          <button
            type="button"
            className="sidebar-restart-btn"
            onClick={startOrRestartClash}
            disabled={restarting}
            title={clashOk ? '重启 Clash' : '启动 Clash'}
          >
            {restarting ? '…' : clashOk ? '重启' : '启动'}
          </button>
        </div>
        {proxyInfo && (
          <div className="sidebar-proxy">
            <div className="sidebar-proxy-row">
              <span className="sidebar-proxy-label">本地代理</span>
              <button
                type="button"
                className="sidebar-proxy-copy"
                title="复制代理地址"
                onClick={() => {
                  navigator.clipboard.writeText(proxyInfo.proxy_url)
                }}
              >
                复制
              </button>
            </div>
            <div className="sidebar-proxy-url" title={proxyInfo.proxy_url}>
              {proxyInfo.proxy_url}
            </div>
            <button
              type="button"
              className="sidebar-proxy-env"
              title="复制环境变量（终端 / 开发工具用）"
              onClick={() => {
                const env = `export HTTP_PROXY=${proxyInfo.proxy_url} HTTPS_PROXY=${proxyInfo.proxy_url}`
                navigator.clipboard.writeText(env)
              }}
            >
              复制环境变量
            </button>
          </div>
        )}
      </aside>

      <main className="main">
        {error && <p className="error">{error}</p>}
        <div className="main-content">
        {tab === 'subs' && (
          <div className="tab-page">
            <h1 className="main-title">Profiles</h1>
            <div className="profile-download">
              <input
                type="text"
                placeholder="从 URL 下载（订阅链接）"
                value={subUrl}
                onChange={(e) => setSubUrl(e.target.value)}
              />
              <input
                type="text"
                placeholder="备注名（可选）"
                value={subName}
                onChange={(e) => setSubName(e.target.value)}
                className="profile-download-name"
              />
              <button type="button" onClick={addSub} disabled={!!loading}>
                {loading === 'add' ? '下载中…' : '下载'}
              </button>
            </div>
            <div className="tab-body">
            <div className="profile-cards">
              {subs.map((s) => (
                <div
                  key={s.id}
                  role="button"
                  tabIndex={0}
                  className={`profile-card ${currentSubId === s.id ? 'active' : ''} ${loading === `apply-${s.id}` ? 'loading' : ''}`}
                  onClick={() => applySub(s.id)}
                  onKeyDown={(e) => e.key === 'Enter' && applySub(s.id)}
                >
                  <button
                    type="button"
                    className="profile-card-delete"
                    onClick={(e) => deleteSub(e, s.id)}
                    aria-label="删除"
                  >
                    ×
                  </button>
                  <div className="profile-card-body">
                    <span className="profile-card-title">{s.name || '未命名'}</span>
                    <span className="profile-card-desc">{s.url}</span>
                    {loading === `apply-${s.id}` && (
                      <span className="profile-card-status">切换中…</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
            </div>
          </div>
        )}

        {tab === 'nodes' && (
          <div className="tab-page">
            <h1 className="main-title">Proxies</h1>
            <div className="mode-tabs" style={{ flexShrink: 0 }}>
              {MODE_LABELS.map(({ value, label }) => (
                <button
                  key={value}
                  type="button"
                  className={`mode-tab ${clashMode === value ? 'active' : ''}`}
                  onClick={() => setMode(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="tab-body">
            {displayGroups.length === 0 && directProxies.length === 0 && clashOk && (
              <p className="empty">
                {clashMode === 'direct'
                  ? '当前配置无 Direct/Reject 项'
                  : '暂无代理组，请先在「管理订阅」中下载并应用订阅。'}
              </p>
            )}
            {displayGroups.map(([groupName, p]) => (
              <section key={groupName} className="proxy-section">
                <h2 className="proxy-section-title">
                  {groupName}
                  {p.now && (
                    <span className="proxy-section-now">
                      {' '}→ {p.now}
                    </span>
                  )}
                </h2>
                <div className="proxy-cards">
                  {(p.all ?? []).map((nodeName) => {
                    const isSelected = p.now === nodeName
                    const delay = delayResult[nodeName]
                    return (
                      <div
                        key={nodeName}
                        role="button"
                        tabIndex={0}
                        className={`proxy-card ${isSelected ? 'selected' : ''}`}
                        onClick={() => selectProxy(groupName, nodeName)}
                        onKeyDown={(e) =>
                          e.key === 'Enter' && selectProxy(groupName, nodeName)
                        }
                      >
                        <div className="proxy-card-info">
                          <span className="proxy-card-name">{nodeName}</span>
                          <span className="proxy-card-type">{p.type}</span>
                        </div>
                        <div className="proxy-card-actions">
                          {delay !== undefined && (
                            <span className="proxy-card-delay">
                              {delay === -1 ? '超时' : `${delay} ms`}
                            </span>
                          )}
                          <button
                            type="button"
                            className="proxy-card-check"
                            onClick={(e) => {
                              e.stopPropagation()
                              testDelay(nodeName)
                            }}
                            disabled={delayTesting === nodeName}
                          >
                            {delayTesting === nodeName ? '…' : '延迟'}
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </section>
            ))}
            {clashMode === 'direct' && directProxies.length > 0 && (
              <section className="proxy-section">
                <h2 className="proxy-section-title">Direct / Reject</h2>
                <div className="proxy-cards">
                  {directProxies.map(([name, p]) => (
                    <div key={name} className="proxy-card proxy-card-static">
                      <div className="proxy-card-info">
                        <span className="proxy-card-name">{name}</span>
                        <span className="proxy-card-type">{p.type}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
            </div>
          </div>
        )}

        {tab === 'traffic' && (
          <div className="tab-page">
            <h1 className="main-title">流量</h1>
            {!clashOk && <p className="empty">Clash 未连接，无法获取数据。</p>}
            {clashOk && connections && (
              <>
                <div className="traffic-summary-bar">
                  <span>总上行 <strong>{formatBytes(connections.uploadTotal)}</strong></span>
                  <span>总下行 <strong>{formatBytes(connections.downloadTotal)}</strong></span>
                  <span>连接数 {connections.connections.length}</span>
                </div>

                <section className="traffic-block">
                  <h2 className="traffic-block-title">谁在用 Clash</h2>
                  {proxyClients.length > 0 ? (
                    <div className="traffic-tag-list">
                      {proxyClients.map((c) => (
                        <div key={c.pid} className="traffic-tag" title={c.cmdline}>
                          <span className="traffic-tag-name">{c.name}</span>
                          <span className="traffic-tag-cmd">{c.cmdline}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="traffic-hint">暂无本机进程连接代理（可能全部为外部 IP 连接）</p>
                  )}
                  <h3 className="traffic-subtitle">按来源 IP 汇总（谁在连 Clash）</h3>
                  <table className="traffic-table traffic-table-compact">
                    <thead>
                      <tr>
                        <th>来源 IP</th>
                        <th>上行</th>
                        <th>下行</th>
                        <th>连接数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        const bySrc: Record<string, { upload: number; download: number; count: number }> = {}
                        for (const c of connections.connections) {
                          const src = (c.metadata?.sourceIP || '').trim() || '(未知)'
                          if (!bySrc[src]) bySrc[src] = { upload: 0, download: 0, count: 0 }
                          bySrc[src].upload += c.upload
                          bySrc[src].download += c.download
                          bySrc[src].count += 1
                        }
                        return Object.entries(bySrc)
                          .sort((a, b) => (b[1].upload + b[1].download) - (a[1].upload + a[1].download))
                          .slice(0, 15)
                          .map(([ip, v]) => (
                            <tr key={ip}>
                              <td>{ip}</td>
                              <td><strong>{formatBytes(v.upload)}</strong></td>
                              <td>{formatBytes(v.download)}</td>
                              <td>{v.count}</td>
                            </tr>
                          ))
                      })()}
                    </tbody>
                  </table>
                </section>

                <section className="traffic-block">
                  <h2 className="traffic-block-title">上传最多的目标（IP/域名）</h2>
                  <p className="traffic-hint">排查 400G 等异常时，看这里哪些目标消耗最多上行。</p>
                  <table className="traffic-table traffic-table-compact">
                    <thead>
                      <tr>
                        <th>目标</th>
                        <th>上行</th>
                        <th>下行</th>
                        <th>连接数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(() => {
                        const byDest: Record<string, { upload: number; download: number; count: number }> = {}
                        for (const c of connections.connections) {
                          const key = (c.metadata?.host && c.metadata.host.trim()) || `${c.metadata?.destinationIP || ''}:${c.metadata?.destinationPort || ''}`.replace(/^:/, '') || '(未知)'
                          if (!byDest[key]) byDest[key] = { upload: 0, download: 0, count: 0 }
                          byDest[key].upload += c.upload
                          byDest[key].download += c.download
                          byDest[key].count += 1
                        }
                        return Object.entries(byDest)
                          .sort((a, b) => b[1].upload - a[1].upload)
                          .slice(0, 15)
                          .map(([dest, v]) => (
                            <tr key={dest}>
                              <td className="traffic-cell-dest" title={dest}>{dest}</td>
                              <td><strong>{formatBytes(v.upload)}</strong></td>
                              <td>{formatBytes(v.download)}</td>
                              <td>{v.count}</td>
                            </tr>
                          ))
                      })()}
                    </tbody>
                  </table>
                </section>
              </>
            )}
          </div>
        )}

        {tab === 'test' && (
          <div className="tab-page">
            <h1 className="main-title">测试</h1>
            <p className="hint">经当前 Clash 代理访问以下站点并检测出口 IP，用于验证节点是否可用。</p>
            <button
              type="button"
              className="test-run-btn"
              onClick={runProxyTest}
              disabled={testLoading}
            >
              {testLoading ? '测试中…' : '运行测试'}
            </button>
            {!clashOk && (
              <p className="empty">请先确保 Clash 已连接（侧栏显示 Connected）后再运行测试。</p>
            )}
            <div className="tab-body">
              <div className="test-results test-results-compare">
                <div className="test-column">
                  <h2 className="test-column-title">使用代理</h2>
                  <section className="test-section">
                    <h3 className="test-section-title">站点可达性</h3>
                    <div className="test-cards">
                      {testState.with_proxy.sites.map((s) => {
                        const url = s.status === 'done' && s.result?.url ? s.result.url : TEST_SITES.find((t) => t.name === s.name)?.url
                        return (
                        <div
                          key={s.name}
                          className={`test-card ${s.status === 'idle' || s.status === 'testing' ? 'idle' : s.result!.ok ? 'ok' : 'fail'}`}
                        >
                          <span className="test-card-name">{s.name}</span>
                          {url && (
                            <span className="test-card-url" title={url}>{url}</span>
                          )}
                          <span className="test-card-detail">
                            {s.status === 'idle'
                              ? '未测试'
                              : s.status === 'testing'
                                ? '测试中'
                                : s.result!.ok
                                  ? `HTTP ${s.result!.status_code} · ${s.result!.time_ms ?? '-'} ms`
                                  : s.result!.error || `HTTP ${s.result!.status_code ?? '-'}`}
                          </span>
                        </div>
                        )
                      })}
                    </div>
                  </section>
                  <section className="test-section">
                    <h3 className="test-section-title">出口 IP / 地址</h3>
                    {testState.with_proxy.ip.status === 'idle' ? (
                      <div className="test-ip-box test-ip-idle">
                        <span className="test-ip-value">未测试</span>
                      </div>
                    ) : testState.with_proxy.ip.result!.error ? (
                      <p className="test-ip-error">{testState.with_proxy.ip.result!.error}</p>
                    ) : (
                      <div className="test-ip-box">
                        <div className="test-ip-row">
                          <span className="test-ip-label">IP</span>
                          <span className="test-ip-value">{testState.with_proxy.ip.result!.ip ?? '-'}</span>
                        </div>
                        <div className="test-ip-row">
                          <span className="test-ip-label">位置</span>
                          <span className="test-ip-value">
                            [{(testState.with_proxy.ip.result!.country ?? '').toUpperCase()}] {testState.with_proxy.ip.result!.region ?? ''} {testState.with_proxy.ip.result!.city ?? ''}
                          </span>
                        </div>
                        {testState.with_proxy.ip.result!.org && (
                          <div className="test-ip-row">
                            <span className="test-ip-label">ISP</span>
                            <span className="test-ip-value">{testState.with_proxy.ip.result!.org}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </section>
                </div>
                <div className="test-column">
                  <h2 className="test-column-title">不使用代理</h2>
                  <section className="test-section">
                    <h3 className="test-section-title">站点可达性</h3>
                    <div className="test-cards">
                      {testState.without_proxy.sites.map((s) => {
                        const url = s.status === 'done' && s.result?.url ? s.result.url : TEST_SITES.find((t) => t.name === s.name)?.url
                        return (
                        <div
                          key={s.name}
                          className={`test-card ${s.status === 'idle' || s.status === 'testing' ? 'idle' : s.result!.ok ? 'ok' : 'fail'}`}
                        >
                          <span className="test-card-name">{s.name}</span>
                          {url && (
                            <span className="test-card-url" title={url}>{url}</span>
                          )}
                          <span className="test-card-detail">
                            {s.status === 'idle'
                              ? '未测试'
                              : s.status === 'testing'
                                ? '测试中'
                                : s.result!.ok
                                  ? `HTTP ${s.result!.status_code} · ${s.result!.time_ms ?? '-'} ms`
                                  : s.result!.error || `HTTP ${s.result!.status_code ?? '-'}`}
                          </span>
                        </div>
                        )
                      })}
                    </div>
                  </section>
                  <section className="test-section">
                    <h3 className="test-section-title">出口 IP / 地址</h3>
                    {testState.without_proxy.ip.status === 'idle' ? (
                      <div className="test-ip-box test-ip-idle">
                        <span className="test-ip-value">未测试</span>
                      </div>
                    ) : testState.without_proxy.ip.result!.error ? (
                      <p className="test-ip-error">{testState.without_proxy.ip.result!.error}</p>
                    ) : (
                      <div className="test-ip-box">
                        <div className="test-ip-row">
                          <span className="test-ip-label">IP</span>
                          <span className="test-ip-value">{testState.without_proxy.ip.result!.ip ?? '-'}</span>
                        </div>
                        <div className="test-ip-row">
                          <span className="test-ip-label">位置</span>
                          <span className="test-ip-value">
                            [{(testState.without_proxy.ip.result!.country ?? '').toUpperCase()}] {testState.without_proxy.ip.result!.region ?? ''} {testState.without_proxy.ip.result!.city ?? ''}
                          </span>
                        </div>
                        {testState.without_proxy.ip.result!.org && (
                          <div className="test-ip-row">
                            <span className="test-ip-label">ISP</span>
                            <span className="test-ip-value">{testState.without_proxy.ip.result!.org}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </section>
                </div>
              </div>
            </div>
          </div>
        )}
        </div>
      </main>
    </div>
  )
}

export default App
