import { useState, useEffect, useRef } from 'react'
import './App.css'

const API = '/api'

type Subscription = { id: number; url: string; name: string; has_cached_profile?: boolean }

type ProxyItem = { name: string; type: string; now?: string; all?: string[] }
type ProxiesRes = { proxies: Record<string, ProxyItem>; clash_available?: boolean }

type Tab = 'subs' | 'nodes' | 'routing' | 'test' | 'traffic' | 'settings'
const TAB_STORAGE_KEY = 'clash-manager-tab'

function getInitialTab(): Tab {
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY)
    if (saved === 'subs' || saved === 'nodes' || saved === 'routing' || saved === 'test' || saved === 'traffic' || saved === 'settings') return saved
  } catch {
    // Storage may be disabled; default to the subscriptions page.
  }
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
  clash_available?: boolean
}

type RuntimeConfig = {
  mixed_port: number
  allow_lan: boolean
  bind_address: string
}

type AccessScope = 'local_only' | 'lan'

type ManagedResidential = {
  server: string
  port: number | null
  username: string
  password_configured: boolean
}

type ManagedPostprocess = {
  enabled: boolean
  domains: string[]
  upstream_group: string
  non_target_behavior: 'subscription_rules' | 'global_passthrough'
  non_target_behavior_note: string
  resolved_upstream_group: string | null
  available_upstream_groups: string[]
  upstream_error: string | null
  residential: ManagedResidential
  current_mode: ClashMode
  installed_in_active_config: boolean
  active_in_current_mode: boolean
  managed_rule_count: number
  mode_note: Record<ClashMode, string>
}

type ManagedPostprocessDraft = {
  enabled: boolean
  domainsText: string
  upstream_group: string
  non_target_behavior: 'subscription_rules' | 'global_passthrough'
  server: string
  port: number | null
  username: string
  password: string
}

type ManagedDeployment = {
  deployment_changed?: boolean
  backup_created?: boolean
  hot_reloaded?: boolean
  upstream_group?: string | null
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

async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return true
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', 'true')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(textarea)
  }
}

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
  const [delayTestingNodes, setDelayTestingNodes] = useState<Record<string, boolean>>({})
  const [delayResult, setDelayResult] = useState<Record<string, number>>({})
  const [bulkDelayTesting, setBulkDelayTesting] = useState(false)
  const [testLoading, setTestLoading] = useState(false)
  const [testState, setTestState] = useState<TestState>(getInitialTestState)
  const [proxyInfo, setProxyInfo] = useState<{ proxy_url: string; proxy_port: number } | null>(null)
  const [connections, setConnections] = useState<ConnectionsRes | null>(null)
  const [trafficSpeed, setTrafficSpeed] = useState<{ upload: number; download: number }>({ upload: 0, download: 0 })
  const [proxyClients, setProxyClients] = useState<{ pid: number; name: string; cmdline: string }[]>([])
  const [restarting, setRestarting] = useState(false)
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null>(null)
  const [runtimeConfigDraft, setRuntimeConfigDraft] = useState<RuntimeConfig | null>(null)
  const [settingsLoading, setSettingsLoading] = useState(false)
  const [saveAndRestart, setSaveAndRestart] = useState(true)
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const trafficPrevRef = useRef<{ uploadTotal: number; downloadTotal: number; ts: number } | null>(null)
  const [subscriptionFetchViaClashProxy, setSubscriptionFetchViaClashProxy] = useState(false)
  const [prefsLoading, setPrefsLoading] = useState(false)
  const [postprocess, setPostprocess] = useState<ManagedPostprocess | null>(null)
  const [postprocessDraft, setPostprocessDraft] = useState<ManagedPostprocessDraft | null>(null)
  const [postprocessLoading, setPostprocessLoading] = useState(false)
  const [postprocessMessage, setPostprocessMessage] = useState<string | null>(null)

  const hasRuntimeConfigChanges = !!(
    runtimeConfig &&
    runtimeConfigDraft &&
    (
      runtimeConfig.mixed_port !== runtimeConfigDraft.mixed_port ||
      runtimeConfig.allow_lan !== runtimeConfigDraft.allow_lan ||
      runtimeConfig.bind_address !== runtimeConfigDraft.bind_address
    )
  )

  const hasPostprocessChanges = !!(
    postprocess &&
    postprocessDraft &&
    (
      postprocess.enabled !== postprocessDraft.enabled ||
      postprocess.domains.join('\n') !== postprocessDraft.domainsText ||
      postprocess.upstream_group !== postprocessDraft.upstream_group ||
      postprocess.non_target_behavior !== postprocessDraft.non_target_behavior ||
      postprocess.residential.server !== postprocessDraft.server ||
      postprocess.residential.port !== postprocessDraft.port ||
      postprocess.residential.username !== postprocessDraft.username ||
      postprocessDraft.password.length > 0
    )
  )

  const fetchPreferences = async () => {
    const r = await fetch(`${API}/preferences`)
    if (!r.ok) return
    const data = (await r.json()) as { subscription_fetch_via_clash_proxy?: boolean }
    setSubscriptionFetchViaClashProxy(!!data.subscription_fetch_via_clash_proxy)
  }

  const setPostprocessFromApi = (data: ManagedPostprocess) => {
    setPostprocess(data)
    setPostprocessDraft({
      enabled: data.enabled,
      domainsText: data.domains.join('\n'),
      upstream_group: data.upstream_group,
      non_target_behavior: data.non_target_behavior,
      server: data.residential.server,
      port: data.residential.port,
      username: data.residential.username,
      // The API deliberately never returns this value.  A blank field means
      // "retain the saved password" when the user changes another setting.
      password: '',
    })
  }

  const fetchPostprocess = async () => {
    const r = await fetch(`${API}/postprocess/claude-residential`)
    if (!r.ok) throw new Error(r.statusText)
    setPostprocessFromApi((await r.json()) as ManagedPostprocess)
  }

  const setSubscriptionViaClashProxy = async (value: boolean) => {
    setPrefsLoading(true)
    setSubscriptionFetchViaClashProxy(value)
    try {
      const r = await fetch(`${API}/preferences`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ subscription_fetch_via_clash_proxy: value }),
      })
      if (!r.ok) {
        const err = await r.json().catch(() => ({}))
        throw new Error(
          typeof (err as { detail?: unknown }).detail === 'string'
            ? (err as { detail: string }).detail
            : r.statusText,
        )
      }
    } catch (e) {
      setSubscriptionFetchViaClashProxy(!value)
      setError(e instanceof Error ? e.message : '偏好保存失败')
    } finally {
      setPrefsLoading(false)
    }
  }

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
    const data: ProxiesRes = await r.json()
    if (data.clash_available === false) {
      setProxies(null)
      return false
    }
    setProxies(data)
    return true
  }

  const fetchConfigs = async () => {
    const r = await fetch(`${API}/clash/configs`)
    if (!r.ok) return
    const data = await r.json()
    if (data.clash_available === false) return false
    const mode = data.mode
    if (mode === 'global' || mode === 'rule' || mode === 'direct' || mode === 'script') {
      setClashMode(mode)
    }
    return true
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
    return !!data.clash_reachable
  }

  const fetchRuntimeConfig = async () => {
    const r = await fetch(`${API}/clash/runtime-config`)
    if (!r.ok) throw new Error(r.statusText)
    const data: RuntimeConfig = await r.json()
    setRuntimeConfig(data)
    setRuntimeConfigDraft(data)
  }

  const waitForClashReady = async (timeoutMs = 60000, intervalMs = 1000) => {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const ok = await checkHealth().catch(() => false)
      if (ok) {
        await Promise.all([
          fetchProxies().catch(() => false),
          fetchConfigs().catch(() => false),
          fetchGlobalGroupName().catch(() => null),
          fetchRuntimeConfig().catch(() => null),
        ])
        return true
      }
      await new Promise((resolve) => window.setTimeout(resolve, intervalMs))
    }
    return false
  }

  const getAccessScope = (cfg: RuntimeConfig | null): AccessScope => {
    if (!cfg) return 'local_only'
    if (!cfg.allow_lan && cfg.bind_address === '127.0.0.1') return 'local_only'
    if (cfg.allow_lan && cfg.bind_address === '0.0.0.0') return 'lan'
    return 'lan'
  }

  const applyAccessScope = (scope: AccessScope) => {
    setRuntimeConfigDraft((prev) => {
      if (!prev) return prev
      if (scope === 'local_only') {
        return {
          ...prev,
          allow_lan: false,
          bind_address: '127.0.0.1',
        }
      }
      return {
        ...prev,
        allow_lan: true,
        bind_address: '0.0.0.0',
      }
    })
  }

  useEffect(() => {
    fetchSubs().catch(console.error)
    fetchCurrentSub().catch(console.error)
    checkHealth().catch(console.error)
    fetchRuntimeConfig().catch(console.error)
    fetchPreferences().catch(console.error)
    fetchPostprocess().catch(console.error)
  // Bootstrap only once; these helpers intentionally populate initial state.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try {
      localStorage.setItem(TAB_STORAGE_KEY, tab)
    } catch {
      // Local storage is optional.
    }
  }, [tab])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 1800)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    if (clashOk && !restarting) {
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
  }, [clashOk, restarting])

  // 每 2 秒拉取连接列表、代理客户端，并计算实时速率
  useEffect(() => {
    if (!clashOk || restarting) return
    const tick = async () => {
      try {
        const [connR, clientsR] = await Promise.all([
          fetch(`${API}/clash/connections`),
          fetch(`${API}/clash/proxy-clients`),
        ])
        if (connR.ok) {
          const data: ConnectionsRes = await connR.json()
          if (data.clash_available === false) {
            setConnections(null)
            setProxyClients([])
            setTrafficSpeed({ upload: 0, download: 0 })
            trafficPrevRef.current = null
            setClashOk(false)
            return
          }
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
      } catch {
        // A transient polling failure should not clear already-rendered data.
      }
    }
    tick()
    const interval = setInterval(tick, 2000)
    return () => clearInterval(interval)
  }, [clashOk, restarting])

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

  const refreshSubProfile = async (id: number) => {
    setError(null)
    setLoading(`refresh-${id}`)
    try {
      const r = await fetch(`${API}/subscriptions/${id}/refresh`, { method: 'POST' })
      const data = (await r.json()) as { detail?: string; message?: string }
      if (!r.ok) throw new Error(data.detail ?? r.statusText)
      await fetchSubs()
      setToast({
        type: 'success',
        message: typeof data.message === 'string' ? data.message : '本地缓存已更新',
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : '刷新订阅失败')
    } finally {
      setLoading(null)
    }
  }

  const switchSubProfile = async (id: number) => {
    setError(null)
    setLoading(`switch-${id}`)
    try {
      const r = await fetch(`${API}/subscriptions/${id}/switch`, { method: 'POST' })
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
      await checkHealth()
      await fetchProxies()
      await fetchCurrentSub()
      await fetchGlobalGroupName()
      await fetchPostprocess()
    } catch (e) {
      setError(e instanceof Error ? e.message : '切换失败')
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
      await fetchPostprocess()
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
        if (data.type === 'meta') {
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
      } catch {
        // Ignore a malformed SSE event and wait for the next one.
      }
    }
    eventSource.onerror = () => {
      eventSource.close()
      setTestLoading(false)
    }
  }

  const startOrRestartClash = async () => {
    setError(null)
    setRestarting(true)
    setClashOk(false)
    setProxies(null)
    setConnections(null)
    setProxyClients([])
    setTrafficSpeed({ upload: 0, download: 0 })
    trafficPrevRef.current = null
    try {
      const r = await fetch(`${API}/clash/start-or-restart`, { method: 'POST' })
      if (!r.ok) throw new Error('操作失败')
      const ready = await waitForClashReady()
      if (!ready) throw new Error('Clash 重启后长时间未恢复')
      setToast({ type: 'success', message: 'Clash 已恢复' })
    } catch (e) {
      setError(e instanceof Error ? e.message : '启动/重启失败')
    } finally {
      setRestarting(false)
    }
  }

  const saveRuntimeConfig = async () => {
    if (!runtimeConfigDraft) return
    setError(null)
    setSettingsMessage(null)
    setSettingsLoading(true)
    try {
      const payload = {
        mixed_port: Number(runtimeConfigDraft.mixed_port),
        allow_lan: runtimeConfigDraft.allow_lan,
        bind_address: runtimeConfigDraft.bind_address.trim(),
      }
      const r = await fetch(`${API}/clash/runtime-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
      const data = await r.json()
      setRuntimeConfig(data.runtime_config)
      setRuntimeConfigDraft(data.runtime_config)
      if (saveAndRestart && data.requires_restart) {
        setRestarting(true)
        setClashOk(false)
        setProxies(null)
        setConnections(null)
        setProxyClients([])
        setTrafficSpeed({ upload: 0, download: 0 })
        trafficPrevRef.current = null
        const restartResp = await fetch(`${API}/clash/start-or-restart`, { method: 'POST' })
        if (!restartResp.ok) throw new Error('配置已保存，但重启 Clash 失败')
        const ready = await waitForClashReady()
        if (!ready) throw new Error('配置已保存，但 Clash 重启后长时间未恢复')
        setSettingsMessage('运行时配置已保存，并已重启 Clash。')
      } else {
        if (data.requires_restart) {
          setSettingsMessage('运行时配置已保存。变更需要重启 Clash 后生效。')
        } else {
          setSettingsMessage('运行时配置已保存。')
        }
        await fetchRuntimeConfig().catch(() => {})
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存运行时配置失败')
    } finally {
      setRestarting(false)
      setSettingsLoading(false)
    }
  }

  const savePostprocess = async (applyNow: boolean) => {
    if (!postprocessDraft) return
    setError(null)
    setPostprocessMessage(null)
    setPostprocessLoading(true)
    try {
      const residential: {
        server: string
        port: number | null
        username: string
        password?: string
      } = {
        server: postprocessDraft.server.trim(),
        port: postprocessDraft.port,
        username: postprocessDraft.username.trim(),
      }
      // Do not send an empty field: empty means retain the password already
      // stored server-side, and the browser never receives that saved value.
      if (postprocessDraft.password.length > 0) residential.password = postprocessDraft.password
      const payload = {
        enabled: postprocessDraft.enabled,
        domains: postprocessDraft.domainsText
          .split(/\r?\n/)
          .map((domain) => domain.trim())
          .filter(Boolean),
        upstream_group: postprocessDraft.upstream_group,
        non_target_behavior: postprocessDraft.non_target_behavior,
        residential,
        apply_now: applyNow,
      }
      const r = await fetch(`${API}/postprocess/claude-residential`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const data = await r.json().catch(() => ({})) as Partial<ManagedPostprocess> & { detail?: string; deployment?: ManagedDeployment | null }
      if (!r.ok) {
        // The settings can have been safely persisted before an isolated
        // syntax check rejects deployment; refresh only redacted state.
        await fetchPostprocess().catch(() => {})
        throw new Error(typeof data.detail === 'string' ? data.detail : r.statusText)
      }
      setPostprocessFromApi(data as ManagedPostprocess)
      const deployment = data.deployment
      if (applyNow) {
        setPostprocessMessage(
          deployment?.hot_reloaded
            ? '托管后处理已保存并已热重载。'
            : '托管后处理已保存；当前配置文件已更新，但 Clash 未确认热重载。'
        )
        await Promise.all([
          fetchProxies().catch(() => false),
          fetchConfigs().catch(() => false),
          fetchCurrentSub().catch(() => null),
        ])
      } else {
        setPostprocessMessage('托管后处理已保存；下次订阅切换/应用会自动叠加，或可手动部署。')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '托管后处理保存失败')
    } finally {
      setPostprocessLoading(false)
    }
  }

  const applyPostprocess = async () => {
    setError(null)
    setPostprocessMessage(null)
    setPostprocessLoading(true)
    try {
      const r = await fetch(`${API}/postprocess/claude-residential/apply`, { method: 'POST' })
      const data = await r.json().catch(() => ({})) as Partial<ManagedPostprocess> & { detail?: string; deployment?: ManagedDeployment | null }
      if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : r.statusText)
      setPostprocessFromApi(data as ManagedPostprocess)
      setPostprocessMessage(
        data.deployment?.hot_reloaded
          ? '已从当前原始订阅缓存重新部署并热重载。'
          : '配置已写入，但 Clash 未确认热重载。'
      )
      await Promise.all([
        fetchProxies().catch(() => false),
        fetchConfigs().catch(() => false),
        fetchCurrentSub().catch(() => null),
      ])
    } catch (e) {
      setError(e instanceof Error ? e.message : '托管后处理部署失败')
    } finally {
      setPostprocessLoading(false)
    }
  }

  const testDelay = async (nodeName: string) => {
    setDelayTestingNodes((prev) => ({ ...prev, [nodeName]: true }))
    try {
      const r = await fetch(
        `${API}/clash/proxies/${encodeURIComponent(nodeName)}/delay?timeout=5000`
      )
      if (!r.ok) throw new Error('测速失败')
      const data = await r.json()
      if (data.clash_available === false) {
        setToast({ type: 'error', message: 'Clash 尚未启动，暂时无法测速' })
        return
      }
      const ms =
        typeof data.delay === 'number' && data.delay >= 0 ? data.delay : -1
      setDelayResult((prev) => ({ ...prev, [nodeName]: ms }))
    } catch {
      setDelayResult((prev) => ({ ...prev, [nodeName]: -1 }))
    } finally {
      setDelayTestingNodes((prev) => {
        const next = { ...prev }
        delete next[nodeName]
        return next
      })
    }
  }

  const testAllDelays = async () => {
    const allNodeNames = Array.from(new Set(
      displayGroups.flatMap(([, p]) => p.all ?? [])
    ))
    if (allNodeNames.length === 0) return
    setError(null)
    setBulkDelayTesting(true)
    const concurrency = 4
    let index = 0
    const workers = Array.from({ length: Math.min(concurrency, allNodeNames.length) }, async () => {
      while (index < allNodeNames.length) {
        const currentIndex = index
        index += 1
        const nodeName = allNodeNames[currentIndex]
        await testDelay(nodeName)
      }
    })
    try {
      await Promise.all(workers)
    } finally {
      setBulkDelayTesting(false)
    }
  }

  const allGroups = proxies
    ? Object.entries(proxies.proxies || {}).filter(
        ([, p]) => p.all && p.all.length > 0 && p.type === 'Selector'
      )
    : []

  // 按模式过滤列表内容（与 CfW 一致：切换 tab 会改变下面显示的内容）
  const displayGroups = (() => {
    if (clashMode === 'global') {
      // Mihomo / Meta：global 模式实际由名为 GLOBAL 的策略组决定出站。
      // 若优先用 yaml 第一个组（常为「节点选择」），界面选的节点不会生效，连通性测试 IP 会与直连相同。
      const metaGlobal = allGroups.find(([name]) => name.toUpperCase() === 'GLOBAL')
      if (metaGlobal) return [metaGlobal]
      if (globalGroupName) {
        const g = allGroups.find(([name]) => name === globalGroupName)
        if (g) return [g]
      }
      const globalGroup = allGroups.find(
        ([name]) =>
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
      ? Object.entries(proxies.proxies).filter(([, p]) => {
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
            className={tab === 'routing' ? 'active' : ''}
            onClick={() => setTab('routing')}
          >
            Claude 后处理
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
          <button
            type="button"
            className={tab === 'settings' ? 'active' : ''}
            onClick={() => setTab('settings')}
          >
            运行设置
          </button>
        </nav>
        <div className="sidebar-status">
          <span className={`status-dot ${restarting ? 'restarting' : clashOk ? 'connected' : ''}`} />
          {restarting ? 'Restarting' : clashOk ? 'Connected' : 'Disconnected'}
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
                onClick={async () => {
                  try {
                    const ok = await copyText(proxyInfo.proxy_url)
                    if (!ok) {
                      setToast({ type: 'error', message: '复制失败，请手动复制' })
                      return
                    }
                    setToast({ type: 'success', message: '代理地址已复制' })
                  } catch {
                    setToast({ type: 'error', message: '复制失败，请手动复制' })
                  }
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
              onClick={async () => {
                const env = `export HTTP_PROXY=${proxyInfo.proxy_url} HTTPS_PROXY=${proxyInfo.proxy_url}`
                try {
                  const ok = await copyText(env)
                  if (!ok) {
                    setToast({ type: 'error', message: '复制失败，请手动复制' })
                    return
                  }
                  setToast({ type: 'success', message: '环境变量已复制' })
                } catch {
                  setToast({ type: 'error', message: '复制失败，请手动复制' })
                }
              }}
            >
              复制环境变量
            </button>
          </div>
        )}
      </aside>

      <main className="main">
        {toast && (
          <div className={`toast ${toast.type}`}>
            {toast.message}
          </div>
        )}
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
            <label className="subs-proxy-option">
              <input
                type="checkbox"
                checked={subscriptionFetchViaClashProxy}
                disabled={prefsLoading}
                onChange={(e) => void setSubscriptionViaClashProxy(e.target.checked)}
              />
              <span>
                点击「刷新」从网络拉取订阅时，HTTP 出站经本机 Clash 的 mixed-port 代理{' '}
                {proxyInfo && <code className="subs-proxy-option-url">{proxyInfo.proxy_url}</code>}
              </span>
            </label>
            <p className="subs-profile-hint">
              「刷新」只更新该订阅的<strong>本地缓存</strong>（不切换当前 Clash 配置）；点卡片则从缓存<strong>切换</strong>到对应配置。
            </p>
            <div className="tab-body">
            <div className="profile-cards">
              {subs.map((s) => (
                <div
                  key={s.id}
                  role="button"
                  tabIndex={0}
                  className={`profile-card ${currentSubId === s.id ? 'active' : ''} ${
                    loading === `switch-${s.id}` || loading === `refresh-${s.id}` ? 'loading' : ''
                  }`}
                  onClick={() => void switchSubProfile(s.id)}
                  onKeyDown={(e) => e.key === 'Enter' && void switchSubProfile(s.id)}
                >
                  <button
                    type="button"
                    className="profile-card-refresh"
                    title="从远端拉取最新内容，仅写入本地缓存（不切换当前生效配置）"
                    disabled={!!loading}
                    onClick={(e) => {
                      e.stopPropagation()
                      void refreshSubProfile(s.id)
                    }}
                  >
                    刷新
                  </button>
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
                    {!s.has_cached_profile && (
                      <span className="profile-card-hint">尚无本地缓存，首次请点「刷新」</span>
                    )}
                    {loading === `switch-${s.id}` && (
                      <span className="profile-card-status">切换中…</span>
                    )}
                    {loading === `refresh-${s.id}` && (
                      <span className="profile-card-status">刷新中…</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
            </div>
          </div>
        )}

        {tab === 'nodes' && (
          <div className="tab-page nodes-page">
            <h1 className="main-title">Proxies</h1>
            <div className="proxy-header">
              <div className="mode-tabs">
                {MODE_LABELS.map(({ value, label }) => (
                  <button
                    key={value}
                    type="button"
                    className={`mode-tab ${clashMode === value ? 'active' : ''}`}
                    onClick={() => setMode(value)}
                    disabled={!clashOk || restarting}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="proxy-toolbar">
                <button
                  type="button"
                  className="proxy-batch-delay"
                  onClick={testAllDelays}
                  disabled={!clashOk || bulkDelayTesting || displayGroups.length === 0}
                >
                  {bulkDelayTesting ? '全部测速中…' : '全部测速'}
                </button>
              </div>
            </div>
            <div className="tab-body nodes-body">
            {displayGroups.length === 0 && directProxies.length === 0 && clashOk && (
              <p className="empty">
                {clashMode === 'direct'
                  ? '当前配置无 Direct/Reject 项'
                  : '暂无代理组，请先「管理订阅」中刷新任一订阅或使用本地缓存切换。'}
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
                        onClick={() => {
                          if (!clashOk || restarting) return
                          selectProxy(groupName, nodeName)
                        }}
                        onKeyDown={(e) =>
                          e.key === 'Enter' && clashOk && !restarting && selectProxy(groupName, nodeName)
                        }
                      >
                        <div className="proxy-card-info">
                          <span className="proxy-card-name">{nodeName}</span>
                          <span className="proxy-card-type">{p.type}</span>
                        </div>
                        <div className="proxy-card-actions">
                          {delay !== undefined && (
                            <span className={`proxy-card-delay ${delay === -1 ? 'timeout' : ''}`}>
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
                            disabled={!clashOk || restarting || !!delayTestingNodes[nodeName]}
                          >
                            {delayTestingNodes[nodeName] ? '…' : '延迟'}
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

        {tab === 'routing' && (
          <div className="tab-page">
            <h1 className="main-title">Claude 后处理</h1>
            <div className="tab-body">
              <section className="settings-panel managed-routing-panel">
                <p className="settings-desc">
                  仅为列出的精确域名添加一条托管链路：当前订阅中已选节点 → 住宅 SOCKS5。订阅原始缓存不写入这层配置；刷新、切换和应用时都会重新叠加。
                </p>
                {postprocessMessage && <p className="settings-message">{postprocessMessage}</p>}
                {postprocess && (
                  <>
                    <div className={`managed-routing-status ${postprocess.active_in_current_mode ? 'active' : 'inactive'}`}>
                      <span className="managed-routing-status-label">
                        {postprocess.active_in_current_mode
                          ? 'Rule 路由已生效'
                          : postprocess.enabled
                            ? `当前 ${postprocess.current_mode}，规则链未接管流量`
                            : '后处理已关闭'}
                      </span>
                      <span>精确规则 {postprocess.managed_rule_count}/{postprocess.domains.length}</span>
                      {postprocess.resolved_upstream_group && (
                        <span title="托管 relay 的第一跳">上游组：{postprocess.resolved_upstream_group}</span>
                      )}
                    </div>
                    <p className="managed-routing-mode-note">
                      {postprocess.mode_note[postprocess.current_mode]}
                    </p>
                    <p className="managed-routing-mode-note">
                      {postprocess.non_target_behavior_note}
                    </p>
                    {postprocess.upstream_error && (
                      <p className="managed-routing-warning">当前订阅无法解析上游组：{postprocess.upstream_error}</p>
                    )}
                  </>
                )}
                {postprocessDraft && postprocess && (
                  <div className="settings-form managed-routing-form">
                    <label className="settings-checkbox">
                      <input
                        type="checkbox"
                        checked={postprocessDraft.enabled}
                        disabled={postprocessLoading}
                        onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, enabled: e.target.checked } : prev)}
                      />
                      <span>启用 Claude 精确域名后处理</span>
                    </label>

                    <label className="settings-field managed-routing-domains">
                      <span>精确域名（每行一个，不匹配子域名）</span>
                      <textarea
                        value={postprocessDraft.domainsText}
                        spellCheck={false}
                        disabled={postprocessLoading}
                        onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, domainsText: e.target.value } : prev)}
                      />
                    </label>

                    <label className="settings-field">
                      <span>订阅上游选择组</span>
                      <input
                        list="managed-upstream-group-options"
                        value={postprocessDraft.upstream_group}
                        disabled={postprocessLoading}
                        placeholder="留空：自动选择订阅中的首个可选节点组"
                        onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, upstream_group: e.target.value } : prev)}
                      />
                      <datalist id="managed-upstream-group-options">
                        {postprocess.available_upstream_groups.map((group) => <option key={group} value={group} />)}
                      </datalist>
                      <small>留空会随不同订阅自动寻找可选节点组；填写后会严格校验该组仍存在。</small>
                    </label>

                    <label className="settings-field">
                      <span>其余流量策略</span>
                      <select
                        value={postprocessDraft.non_target_behavior}
                        disabled={postprocessLoading}
                        onChange={(e) => setPostprocessDraft((prev) => prev ? {
                          ...prev,
                          non_target_behavior: e.target.value === 'global_passthrough'
                            ? 'global_passthrough'
                            : 'subscription_rules',
                        } : prev)}
                      >
                        <option value="global_passthrough">保持原 Global 行为（其余流量走 GLOBAL）</option>
                        <option value="subscription_rules">使用订阅原始 Rule 行为</option>
                      </select>
                      <small>
                        选择“保持原 Global”时，保存会切到 Rule 以让 15 条精确规则生效，并紧接着以 MATCH,GLOBAL 保持所有其余流量的原出口选择。
                      </small>
                    </label>

                    <div className="managed-routing-grid">
                      <label className="settings-field">
                        <span>住宅 SOCKS5 地址</span>
                        <input
                          type="text"
                          autoComplete="off"
                          value={postprocessDraft.server}
                          disabled={postprocessLoading || !postprocessDraft.enabled}
                          onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, server: e.target.value } : prev)}
                        />
                      </label>
                      <label className="settings-field">
                        <span>端口</span>
                        <input
                          type="number"
                          min={1}
                          max={65535}
                          value={postprocessDraft.port ?? ''}
                          disabled={postprocessLoading || !postprocessDraft.enabled}
                          onChange={(e) => setPostprocessDraft((prev) => prev ? {
                            ...prev,
                            port: e.target.value === '' ? null : Number(e.target.value),
                          } : prev)}
                        />
                      </label>
                    </div>
                    <label className="settings-field">
                      <span>用户名</span>
                      <input
                        type="text"
                        autoComplete="off"
                        value={postprocessDraft.username}
                        disabled={postprocessLoading || !postprocessDraft.enabled}
                        onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, username: e.target.value } : prev)}
                      />
                    </label>
                    <label className="settings-field">
                      <span>密码</span>
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={postprocessDraft.password}
                        disabled={postprocessLoading || !postprocessDraft.enabled}
                        placeholder={postprocess.residential.password_configured ? '已保存；留空保持不变' : '首次启用时必填'}
                        onChange={(e) => setPostprocessDraft((prev) => prev ? { ...prev, password: e.target.value } : prev)}
                      />
                      <small>
                        {postprocess.residential.password_configured
                          ? '密码已保存且不会回传或显示在页面中。'
                          : '密码仅在保存时发送；页面不会读取回显值。'}
                      </small>
                    </label>

                    <p className="managed-routing-scope">
                      只有上方精确域名会先进入该 relay；不会匹配子域名。切换到 Global 或 Direct 会绕过这些规则；保存不会重启 Clash。
                    </p>
                    <div className="settings-actions managed-routing-actions">
                      <button
                        type="button"
                        onClick={() => void savePostprocess(true)}
                        disabled={postprocessLoading}
                      >
                        {postprocessLoading ? '保存中…' : '保存并热重载'}
                      </button>
                      <button
                        type="button"
                        className="settings-reset"
                        onClick={() => void savePostprocess(false)}
                        disabled={postprocessLoading}
                      >
                        仅保存
                      </button>
                      <button
                        type="button"
                        className="settings-reset"
                        onClick={() => void applyPostprocess()}
                        disabled={postprocessLoading}
                      >
                        从当前缓存重新部署
                      </button>
                      {hasPostprocessChanges && (
                        <button
                          type="button"
                          className="settings-reset"
                          onClick={() => {
                            setPostprocessDraft({
                              enabled: postprocess.enabled,
                              domainsText: postprocess.domains.join('\n'),
                              upstream_group: postprocess.upstream_group,
                              non_target_behavior: postprocess.non_target_behavior,
                              server: postprocess.residential.server,
                              port: postprocess.residential.port,
                              username: postprocess.residential.username,
                              password: '',
                            })
                            setPostprocessMessage(null)
                          }}
                          disabled={postprocessLoading}
                        >
                          撤销未保存修改
                        </button>
                      )}
                    </div>
                  </div>
                )}
                {!postprocessDraft && <p className="empty">正在读取托管后处理配置…</p>}
              </section>
            </div>
          </div>
        )}

        {tab === 'settings' && (
          <div className="tab-page">
            <h1 className="main-title">Runtime Settings</h1>
            <div className="tab-body">
              <section className="settings-panel">
                <p className="settings-desc">
                  这里修改的是写入 `config.yaml` 的运行时参数，不属于 `config.json` 的部署静态配置。
                </p>
                {settingsMessage && <p className="settings-message">{settingsMessage}</p>}
                {runtimeConfigDraft && (
                  <div className="settings-form">
                    <label className="settings-field">
                      <span>Mixed Port</span>
                      <input
                        type="number"
                        min={1}
                        max={65535}
                        value={runtimeConfigDraft.mixed_port}
                        onChange={(e) =>
                          setRuntimeConfigDraft((prev) => prev ? {
                            ...prev,
                            mixed_port: Number(e.target.value),
                          } : prev)
                        }
                      />
                    </label>
                    <div className="settings-field">
                      <span>访问范围</span>
                      <div className="settings-scope-options">
                        <label className="settings-scope-option">
                          <input
                            type="radio"
                            name="access-scope"
                            checked={getAccessScope(runtimeConfigDraft) === 'local_only'}
                            onChange={() => applyAccessScope('local_only')}
                          />
                          <span title="设置为 allow-lan=false，bind-address=127.0.0.1">仅本机</span>
                        </label>
                        <label className="settings-scope-option">
                          <input
                            type="radio"
                            name="access-scope"
                            checked={getAccessScope(runtimeConfigDraft) === 'lan'}
                            onChange={() => applyAccessScope('lan')}
                          />
                          <span title="设置为 allow-lan=true，bind-address=0.0.0.0">开放局域网</span>
                        </label>
                      </div>
                    </div>
                    <label className="settings-checkbox">
                      <input
                        type="checkbox"
                        checked={saveAndRestart}
                        onChange={(e) => setSaveAndRestart(e.target.checked)}
                      />
                      <span>保存后自动重启 Clash</span>
                    </label>
                    {hasRuntimeConfigChanges && (
                      <p className="settings-dirty">有未保存的运行时配置修改。</p>
                    )}
                    {hasRuntimeConfigChanges && (
                      <div className="settings-actions">
                        <button
                          type="button"
                          onClick={saveRuntimeConfig}
                          disabled={settingsLoading}
                        >
                          {settingsLoading ? '保存中…' : '保存修改'}
                        </button>
                        {runtimeConfig && (
                          <button
                            type="button"
                            className="settings-reset"
                            onClick={() => {
                              setRuntimeConfigDraft(runtimeConfig)
                              setSettingsMessage(null)
                            }}
                            disabled={settingsLoading}
                          >
                            撤销未保存修改
                          </button>
                        )}
                      </div>
                    )}
                    {!hasRuntimeConfigChanges && runtimeConfig && (
                      <div className="settings-clean">
                        当前运行时配置与已保存内容一致。
                      </div>
                    )}
                  </div>
                )}
              </section>
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
