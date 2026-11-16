"""
Clash 订阅管理 + 节点切换后端
- 代理 Clash 控制器 API（配置、代理列表、选节点）
- 订阅管理：增删改、从订阅链接拉取并转为 Clash 配置、写盘并重载
- 维持 Clash 内核运行：启动时若未检测到则拉起，后台定时检测并自动重启
"""
import asyncio
import copy
import ipaddress
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config

SUBS_FILE = Path(__file__).resolve().parent / "subscriptions.json"
CURRENT_SUB_FILE = Path(__file__).resolve().parent / "current_subscription_id.json"
PROFILE_CACHE_DIR = Path(__file__).resolve().parent / "profile_cache"
CLASH_LOG = Path(__file__).resolve().parent / "logs" / "clash.log"
TRAFFIC_LOG_DIR = Path(__file__).resolve().parent / "logs"
DEFAULT_CLASH_CONFIG_YAML = Path(__file__).resolve().parent / "default_clash_config.yaml"
RUNTIME_STATE_FILE = Path(__file__).resolve().parent / "runtime_state.json"
MANAGED_POSTPROCESS_FILE = Path(__file__).resolve().parent / "managed_postprocess.json"
DEPLOYMENT_BACKUP_DIR = Path(__file__).resolve().parent / "deployment_backups"

# These names are deliberately internal/reserved.  They are stripped before a
# cached subscription is reapplied, so an upstream refresh never accumulates a
# second copy of the managed chain.
MANAGED_PREFIX = "__manager_claude_"
MANAGED_RESIDENTIAL_PROXY = "__manager_claude_residential_socks5"
MANAGED_RELAY_GROUP = "__manager_claude_residential_chain"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

# Exact host matches only.  Do not turn these into DOMAIN-SUFFIX rules: several
# of these hosts deliberately need a different route from their subdomains.
DEFAULT_CLAUDE_DOMAINS = (
    "claude.ai",
    "claude.com",
    "platform.claude.com",
    "api.anthropic.com",
    "www.anthropic.com",
    "console.anthropic.com",
    "clau.de",
    "claude.dev",
    "claudeusercontent.com",
    "check.claudemcpcontent.com",
    "anthropic.auth0.com",
    "hcaptcha.com",
    "challenges.cloudflare.com",
    "claude-check.caixukun.de",
    "anthropic-check.caixukun.de",
)
_DOMAIN_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


def _restrict_file(path: Path, mode: int = PRIVATE_FILE_MODE) -> None:
    """Best-effort restrictive permissions; a permission error must not leak contents."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, PRIVATE_DIR_MODE)
    except OSError:
        pass


def _write_private_text(path: Path, text: str) -> None:
    """Atomically write sensitive runtime data with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_name = ""
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
        temp_name = ""
        _restrict_file(path)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _write_private_json(path: Path, value: object) -> None:
    _write_private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _secure_existing_runtime_files() -> None:
    """Tighten existing manager-owned state without touching unrelated services."""
    for state_file in (SUBS_FILE, CURRENT_SUB_FILE, RUNTIME_STATE_FILE, MANAGED_POSTPROCESS_FILE):
        if state_file.exists():
            _restrict_file(state_file)

    _ensure_private_dir(PROFILE_CACHE_DIR)
    for cache_file in PROFILE_CACHE_DIR.glob("*.yaml"):
        _restrict_file(cache_file)

    config_path = Path(config.CLASH_CONFIG_PATH)
    if config_path.exists():
        _restrict_file(config_path)
        try:
            os.chmod(config_path.parent, PRIVATE_DIR_MODE)
        except OSError:
            pass


def _get_current_sub_id() -> int | None:
    if not CURRENT_SUB_FILE.exists():
        return None
    try:
        with open(CURRENT_SUB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("current_id")
    except Exception:
        return None


def _set_current_sub_id(sub_id: int | None) -> None:
    if sub_id is None:
        CURRENT_SUB_FILE.unlink(missing_ok=True)
        return
    _write_private_json(CURRENT_SUB_FILE, {"current_id": sub_id})


def _load_runtime_state() -> dict:
    if not RUNTIME_STATE_FILE.exists():
        return {"selected_proxies": {}}
    try:
        with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"selected_proxies": {}}
    if not isinstance(data, dict):
        return {"selected_proxies": {}}
    selected = data.get("selected_proxies")
    if not isinstance(selected, dict):
        data["selected_proxies"] = {}
    return data


def _save_runtime_state(data: dict) -> None:
    payload = dict(data)
    selected = payload.get("selected_proxies")
    if not isinstance(selected, dict):
        payload["selected_proxies"] = {}
    _write_private_json(RUNTIME_STATE_FILE, payload)


def _clash_alive() -> bool:
    try:
        base = config.CLASH_API_BASE.rstrip("/")
        # The manager talks to a loopback controller.  Inheriting HTTP_PROXY
        # here can route that control request back into Clash (or a corporate
        # proxy) and turn a healthy controller into a misleading 502.
        r = httpx.get(f"{base}/version", timeout=2.0, trust_env=False)
        return r.is_success
    except Exception:
        return False


def _controller_address() -> str:
    parsed = urlparse(config.CLASH_API_BASE)
    if parsed.netloc:
        return parsed.netloc
    return config.CLASH_API_BASE.replace("http://", "").replace("https://", "").strip("/")


def _default_clash_yaml() -> dict:
    """加载 default_clash_config.yaml，并注入固定的 external-controller。"""
    if not DEFAULT_CLASH_CONFIG_YAML.is_file():
        raise RuntimeError(f"Missing {DEFAULT_CLASH_CONFIG_YAML}")
    with open(DEFAULT_CLASH_CONFIG_YAML, encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{DEFAULT_CLASH_CONFIG_YAML} must be a YAML mapping")
    out = dict(loaded)
    out["external-controller"] = _controller_address()
    return out


def _load_clash_config_yaml(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) if raw.strip() else None
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _effective_clash_yaml() -> dict:
    path = Path(config.CLASH_CONFIG_PATH)
    data = _load_clash_config_yaml(path) if path.exists() else None
    effective = _default_clash_yaml()
    if isinstance(data, dict):
        effective.update(data)
    return effective


def _write_clash_yaml(data: dict) -> None:
    path = Path(config.CLASH_CONFIG_PATH)
    _ensure_private_dir(path.parent)
    _write_private_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def _current_mixed_port() -> int:
    value = _effective_clash_yaml().get("mixed-port", 7890)
    return int(value)


def _runtime_mixed_port_from_clash() -> int | None:
    """从运行中的 Clash /configs 读取 mixed-port，避免磁盘配置与进程不一致时测速走错端口。"""
    try:
        r = _clash_request("GET", "/configs")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        raw = data.get("mixed-port")
        if raw is None:
            raw = data.get("mixed_port")
        if raw is None:
            return None
        return int(raw)
    except Exception:
        return None


def _proxy_url() -> str:
    live = _runtime_mixed_port_from_clash()
    port = live if live is not None else _current_mixed_port()
    return f"http://127.0.0.1:{port}"


def _subscription_via_clash_proxy_enabled() -> bool:
    return bool(_load_runtime_state().get("subscription_fetch_via_clash_proxy", False))


def _ssl_context_tls12_only() -> ssl.SSLContext:
    """部分中转/劣质节点在 TLS 1.3 或与 OpenSSL 组合下会半截断链，强制 TLS 1.2 偶尔可恢复。"""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _tls_eof_like_error(msg: str) -> bool:
    if not msg:
        return False
    u = msg.upper()
    return "SSL" in u and ("EOF" in u or "UNEXPECTED_EOF" in u or "VIOLATION OF PROTOCOL" in u)


def _httpx_client_kwargs_for_subscription_fetch() -> dict:
    """订阅从网络刷新时的出站 HTTP：可选经本机 Clash mixed-port。"""
    # A subscription must be direct unless the existing UI preference explicitly
    # enables the local Clash proxy.  Do not silently inherit the manager
    # process's HTTP_PROXY/HTTPS_PROXY environment.
    opts: dict = dict(timeout=60.0, follow_redirects=True, trust_env=False)
    if _subscription_via_clash_proxy_enabled():
        opts["proxy"] = _proxy_url()
        # 与站点连通性测试一致：经本地代理时关掉 keep-alive，减轻 TLS 半截关闭 / 坏连接复用导致的
        # [SSL: UNEXPECTED_EOF_WHILE_READING]。
        opts["limits"] = httpx.Limits(max_connections=10, max_keepalive_connections=0)
    return opts


def _subscription_http_get(client_kw: dict, url: str, *, headers: dict[str, str]) -> httpx.Response:
    """订阅与转换 URL 拉取：短暂重试；经代理且为 TLS EOF 类错误时再试仅 TLS 1.2。"""
    attempts = 3
    uses_proxy = bool(client_kw.get("proxy"))
    last_err: BaseException | None = None

    def run_phase(kw: dict) -> httpx.Response | None:
        nonlocal last_err
        for attempt in range(attempts):
            try:
                with httpx.Client(**kw) as client:
                    return client.get(url, headers=headers)
            except httpx.RequestError as e:
                last_err = e
                if attempt < attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
        return None

    r = run_phase(client_kw)
    if r is not None:
        return r
    if uses_proxy and last_err is not None and _tls_eof_like_error(str(last_err)):
        kw12 = {**client_kw, "verify": _ssl_context_tls12_only()}
        r = run_phase(kw12)
        if r is not None:
            return r
    assert last_err is not None
    raise last_err


def _persist_mode_to_yaml(mode: str) -> None:
    data = _effective_clash_yaml()
    data["mode"] = mode
    _write_clash_yaml(data)


def _persist_selected_proxy(group_name: str, proxy_name: str) -> None:
    state = _load_runtime_state()
    selected = dict(state.get("selected_proxies", {}))
    selected[group_name] = proxy_name
    state["selected_proxies"] = selected
    _save_runtime_state(state)


def _restore_persisted_proxy_selections() -> None:
    state = _load_runtime_state()
    selected = state.get("selected_proxies", {})
    if not isinstance(selected, dict) or not selected:
        return
    try:
        proxies_resp = _clash_request("GET", "/proxies")
        proxies_resp.raise_for_status()
        proxies = (proxies_resp.json() or {}).get("proxies", {})
    except Exception:
        return
    for group_name, proxy_name in selected.items():
        group = proxies.get(group_name)
        if not isinstance(group, dict):
            continue
        all_nodes = group.get("all") or []
        if proxy_name not in all_nodes:
            continue
        try:
            resp = _clash_request("PUT", f"/proxies/{quote(group_name)}", json={"name": proxy_name})
            resp.raise_for_status()
        except Exception:
            continue


def _ensure_clash_config() -> None:
    """新建或修正 clash 配置文件：缺文件则写默认 YAML；已存在则只对齐静态项并补全残缺结构。"""
    path = Path(config.CLASH_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    minimal = _default_clash_yaml()

    if not path.exists():
        _write_clash_yaml(minimal)
        return

    data = _load_clash_config_yaml(path)
    if not isinstance(data, dict):
        _write_clash_yaml(minimal)
        return

    changed = False
    if data.get("external-controller") != minimal["external-controller"]:
        data["external-controller"] = minimal["external-controller"]
        changed = True
    if "mixed-port" not in data:
        data["mixed-port"] = minimal.get("mixed-port", 7890)
        changed = True
    if "allow-lan" not in data:
        data["allow-lan"] = minimal.get("allow-lan", False)
        changed = True
    if "bind-address" not in data and "bind-address" in minimal:
        data["bind-address"] = minimal["bind-address"]
        changed = True
    if not data.get("proxy-groups") and not data.get("proxy_groups"):
        data.setdefault("proxies", [])
        data["proxy-groups"] = minimal["proxy-groups"]
        data.setdefault("rules", ["MATCH,DIRECT"])
        data.setdefault("mode", "rule")
        data.setdefault("log-level", "info")
        changed = True

    if changed:
        _write_clash_yaml(data)


def _start_clash() -> None:
    """启动 Clash 进程（后台、与当前进程脱离），日志写入 logs/clash.log"""
    _ensure_clash_config()
    CLASH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CLASH_LOG, "a", encoding="utf-8") as logf:
        subprocess.Popen(
            [config.CLASH_BIN, "-f", config.CLASH_CONFIG_PATH, "-d", config.CLASH_CONFIG_DIR],
            cwd=config.CLASH_CONFIG_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


async def _clash_keepalive_task():
    """每 60 秒检查一次，若 Clash 不可达则拉起"""
    while True:
        await asyncio.sleep(60)
        if not _clash_alive():
            _start_clash()
            await asyncio.sleep(3)
            _restore_persisted_proxy_selections()


def _write_traffic_snapshot():
    """拉取 Clash /connections 并追加一行到 logs/traffic_YYYY-MM-DD.log"""
    try:
        r = _clash_request("GET", "/connections")
        if not r.is_success:
            return
        data = r.json()
        upload_total = data.get("uploadTotal") or 0
        download_total = data.get("downloadTotal") or 0
        conns = data.get("connections") or []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        date_part = time.strftime("%Y-%m-%d")
        TRAFFIC_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = TRAFFIC_LOG_DIR / f"traffic_{date_part}.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{now}\t{upload_total}\t{download_total}\t{len(conns)}\n")
    except Exception:
        pass


async def _traffic_log_task():
    """每 60 秒写一条流量快照到 logs/traffic_YYYY-MM-DD.log"""
    while True:
        await asyncio.sleep(60)
        await asyncio.get_event_loop().run_in_executor(None, _write_traffic_snapshot)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _secure_existing_runtime_files()
    # 启动时若 Clash 未运行则启动
    if not _clash_alive():
        _start_clash()
        await asyncio.sleep(2)
    _restore_persisted_proxy_selections()
    # 后台保活 + 流量日志
    keepalive = asyncio.create_task(_clash_keepalive_task())
    traffic_log = asyncio.create_task(_traffic_log_task())
    yield
    keepalive.cancel()
    traffic_log.cancel()
    try:
        await keepalive
    except asyncio.CancelledError:
        pass
    try:
        await traffic_log
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Clash Manager", description="订阅管理与节点切换", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 订阅数据结构 ----------
class SubscriptionCreate(BaseModel):
    url: str  # 订阅链接（可能返回 base64）
    name: str = ""  # 可选备注


class SubscriptionUpdate(BaseModel):
    url: str | None = None
    name: str | None = None


class ManagerPreferencesUpdate(BaseModel):
    subscription_fetch_via_clash_proxy: bool


class RuntimeConfigUpdate(BaseModel):
    mixed_port: int
    allow_lan: bool
    bind_address: str


class ResidentialProxyUpdate(BaseModel):
    server: str = ""
    port: int | None = None
    username: str = ""
    # Omitted means "keep the stored password".  It is never returned by GET.
    password: str | None = None


class ClaudeResidentialPostprocessUpdate(BaseModel):
    enabled: bool = True
    domains: list[str]
    upstream_group: str = ""
    # global_passthrough means: turn on Rule mode for the exact domains, then
    # send every other request through Clash's virtual GLOBAL selector.  This
    # preserves a pre-existing Global-mode decision for non-target traffic.
    non_target_behavior: str | None = None
    residential: ResidentialProxyUpdate
    # Saving independently from applying is useful while a user is editing a
    # live profile with active connections.
    apply_now: bool = True


def _default_postprocess_settings() -> dict:
    return {
        "version": 1,
        "enabled": False,
        "domains": list(DEFAULT_CLAUDE_DOMAINS),
        "upstream_group": "",
        "non_target_behavior": "subscription_rules",
        "residential": {
            "server": "",
            "port": None,
            "username": "",
            "password": "",
        },
    }


def _normalize_exact_domains(values: object) -> list[str]:
    if not isinstance(values, list):
        raise HTTPException(status_code=422, detail="精确域名必须是列表")
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail="精确域名必须是文本")
        domain = raw.strip().lower().rstrip(".")
        if not _DOMAIN_RE.fullmatch(domain):
            raise HTTPException(status_code=422, detail="域名必须是精确主机名，不能包含通配符、协议或路径")
        if domain not in seen:
            seen.add(domain)
            out.append(domain)
    if not out:
        raise HTTPException(status_code=422, detail="至少保留一个精确域名")
    if len(out) > 64:
        raise HTTPException(status_code=422, detail="精确域名数量不能超过 64 个")
    return out


def _normalize_proxy_server(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=422, detail="住宅 SOCKS5 地址必须是文本")
    server = raw.strip().lower()
    if not server:
        raise HTTPException(status_code=422, detail="启用后处理时必须填写住宅 SOCKS5 地址")
    try:
        ipaddress.ip_address(server)
        return server
    except ValueError:
        pass
    if not _DOMAIN_RE.fullmatch(server):
        raise HTTPException(status_code=422, detail="住宅 SOCKS5 地址必须是 IP 或主机名")
    return server


def _load_postprocess_settings() -> dict:
    defaults = _default_postprocess_settings()
    if not MANAGED_POSTPROCESS_FILE.exists():
        return defaults
    try:
        raw = json.loads(MANAGED_POSTPROCESS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail="托管后处理状态无法读取；未改动当前 Clash 配置") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="托管后处理状态格式无效；未改动当前 Clash 配置")

    result = _default_postprocess_settings()
    result["enabled"] = bool(raw.get("enabled", False))
    domains = raw.get("domains", result["domains"])
    try:
        result["domains"] = _normalize_exact_domains(domains)
    except HTTPException as exc:
        raise HTTPException(status_code=500, detail="托管后处理中的域名格式无效；未改动当前 Clash 配置") from exc

    group = raw.get("upstream_group", "")
    if not isinstance(group, str) or len(group.strip()) > 256 or "\n" in group or "\r" in group:
        raise HTTPException(status_code=500, detail="托管后处理中的上游组名称无效；未改动当前 Clash 配置")
    result["upstream_group"] = group.strip()

    behavior = raw.get("non_target_behavior")
    if behavior is None:
        # State written before this field existed is upgraded according to the
        # behavior users were actually receiving at the time: Global mode.
        active_mode = str(_effective_clash_yaml().get("mode", "rule"))
        behavior = "global_passthrough" if result["enabled"] and active_mode == "global" else "subscription_rules"
    if behavior not in {"subscription_rules", "global_passthrough"}:
        raise HTTPException(status_code=500, detail="托管后处理中的非目标流量策略无效；未改动当前 Clash 配置")
    result["non_target_behavior"] = behavior

    residential = raw.get("residential")
    if isinstance(residential, dict):
        server = residential.get("server", "")
        username = residential.get("username", "")
        password = residential.get("password", "")
        port = residential.get("port")
        result["residential"] = {
            "server": server.strip() if isinstance(server, str) else "",
            "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
            "username": username.strip() if isinstance(username, str) else "",
            "password": password if isinstance(password, str) else "",
        }
    return result


def _save_postprocess_settings(settings: dict) -> None:
    _write_private_json(MANAGED_POSTPROCESS_FILE, settings)


def _settings_from_postprocess_update(body: ClaudeResidentialPostprocessUpdate) -> dict:
    previous = _load_postprocess_settings()
    domains = _normalize_exact_domains(body.domains)
    upstream_group = body.upstream_group.strip()
    if len(upstream_group) > 256 or "\n" in upstream_group or "\r" in upstream_group:
        raise HTTPException(status_code=422, detail="上游订阅组名称无效")
    if upstream_group.startswith(MANAGED_PREFIX):
        raise HTTPException(status_code=422, detail="上游订阅组不能引用 Manager 托管组")
    behavior = body.non_target_behavior or previous.get("non_target_behavior")
    if behavior not in {"subscription_rules", "global_passthrough"}:
        raise HTTPException(status_code=422, detail="非目标流量策略无效")

    incoming = body.residential
    password = previous["residential"]["password"] if incoming.password is None else incoming.password
    if not isinstance(password, str) or "\n" in password or "\r" in password:
        raise HTTPException(status_code=422, detail="住宅 SOCKS5 密码格式无效")

    server = incoming.server.strip()
    username = incoming.username.strip()
    port = incoming.port
    if body.enabled:
        server = _normalize_proxy_server(server)
        if port is None or isinstance(port, bool) or port < 1 or port > 65535:
            raise HTTPException(status_code=422, detail="住宅 SOCKS5 端口必须在 1-65535 之间")
        if not username:
            raise HTTPException(status_code=422, detail="启用后处理时必须填写住宅 SOCKS5 用户名")
        if not password:
            raise HTTPException(status_code=422, detail="请填写住宅 SOCKS5 密码（留空只会保留已保存的密码）")

    return {
        "version": 1,
        "enabled": bool(body.enabled),
        "domains": domains,
        "upstream_group": upstream_group,
        "non_target_behavior": behavior,
        "residential": {
            "server": server,
            "port": port,
            "username": username,
            "password": password,
        },
    }


def _groups_key_and_value(clash_data: dict) -> tuple[str, list[dict]]:
    for key in ("proxy-groups", "proxy_groups"):
        value = clash_data.get(key)
        if isinstance(value, list):
            groups = [item for item in value if isinstance(item, dict)]
            if len(groups) != len(value):
                raise HTTPException(status_code=422, detail="订阅中的代理组结构无效")
            return key, groups
    raise HTTPException(status_code=422, detail="订阅中没有可用的代理组，无法构建托管链路")


def _is_managed_rule(rule: object) -> bool:
    if not isinstance(rule, str):
        return False
    parts = [part.strip() for part in rule.split(",")]
    return MANAGED_RELAY_GROUP in parts[2:]


def _strip_managed_postprocess(clash_data: dict) -> dict:
    """Return a source-shaped config without only this manager's generated artifacts."""
    clean = copy.deepcopy(clash_data)
    proxies = clean.get("proxies")
    if isinstance(proxies, list):
        clean["proxies"] = [
            item
            for item in proxies
            if not (isinstance(item, dict) and str(item.get("name", "")).startswith(MANAGED_PREFIX))
        ]
    for key in ("proxy-groups", "proxy_groups"):
        groups = clean.get(key)
        if isinstance(groups, list):
            clean[key] = [
                item
                for item in groups
                if not (isinstance(item, dict) and str(item.get("name", "")).startswith(MANAGED_PREFIX))
            ]
    rules = clean.get("rules")
    if isinstance(rules, list):
        clean["rules"] = [rule for rule in rules if not _is_managed_rule(rule)]
    return clean


def _available_upstream_groups(clash_data: dict) -> list[str]:
    _, groups = _groups_key_and_value(clash_data)
    names: list[str] = []
    for group in groups:
        name = group.get("name")
        if isinstance(name, str) and name and not name.startswith(MANAGED_PREFIX):
            names.append(name)
    return names


def _resolve_upstream_group(clash_data: dict, configured_name: str) -> str:
    """Find the subscription selector that represents the user's chosen node."""
    _, groups = _groups_key_and_value(clash_data)
    by_name = {
        str(group["name"]): group
        for group in groups
        if isinstance(group.get("name"), str) and group.get("name")
    }
    group_names = set(by_name)

    def is_selectable(group: dict) -> bool:
        return str(group.get("type", "")).lower() == "select"

    def has_concrete_member(group: dict) -> bool:
        members = group.get("proxies")
        if not isinstance(members, list):
            return bool(group.get("use"))
        return any(
            isinstance(member, str)
            and member not in group_names
            and member.upper() not in {"DIRECT", "REJECT", "REJECT-DROP"}
            for member in members
        ) or bool(group.get("use"))

    if configured_name:
        group = by_name.get(configured_name)
        if group is None:
            raise HTTPException(status_code=422, detail="所选上游订阅组不在当前订阅中")
        if not is_selectable(group):
            raise HTTPException(status_code=422, detail="上游订阅组必须是 select 类型，才能跟随已选节点")
        return configured_name

    for group in groups:
        if is_selectable(group) and has_concrete_member(group):
            return str(group["name"])
    for group in groups:
        if is_selectable(group):
            return str(group["name"])
    raise HTTPException(status_code=422, detail="订阅中没有可跟随已选节点的 select 代理组")


def _assert_upstream_has_no_manager_cycle(clash_data: dict, upstream_group: str) -> None:
    _, groups = _groups_key_and_value(clash_data)
    by_name = {
        str(group["name"]): group
        for group in groups
        if isinstance(group.get("name"), str) and group.get("name")
    }
    pending = [upstream_group]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if name.startswith(MANAGED_PREFIX):
            raise HTTPException(status_code=422, detail="上游订阅组不能经由 Manager 托管组，避免代理循环")
        group = by_name.get(name)
        if not group:
            continue
        members = group.get("proxies")
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, str):
                    continue
                if member.startswith(MANAGED_PREFIX):
                    raise HTTPException(status_code=422, detail="上游订阅组不能经由 Manager 托管组，避免代理循环")
                if member in by_name:
                    pending.append(member)


def _build_deployed_clash_dict(raw_clash_data: dict, settings: dict | None = None) -> tuple[dict, str | None]:
    """Overlay the managed relay on a raw subscription without modifying that raw source."""
    if not isinstance(raw_clash_data, dict):
        raise HTTPException(status_code=422, detail="订阅配置结构无效")
    managed_settings = settings if settings is not None else _load_postprocess_settings()
    deployed = _strip_managed_postprocess(raw_clash_data)
    _merge_runtime_into_clash_dict(deployed)
    if not managed_settings.get("enabled"):
        return deployed, None

    proxies = deployed.get("proxies")
    if proxies is None:
        proxies = []
        deployed["proxies"] = proxies
    if not isinstance(proxies, list) or not all(isinstance(item, dict) for item in proxies):
        raise HTTPException(status_code=422, detail="订阅中的代理节点结构无效")

    groups_key, groups = _groups_key_and_value(deployed)
    upstream_group = _resolve_upstream_group(deployed, str(managed_settings.get("upstream_group") or ""))
    _assert_upstream_has_no_manager_cycle(deployed, upstream_group)
    residential = managed_settings.get("residential")
    if not isinstance(residential, dict):
        raise HTTPException(status_code=500, detail="住宅 SOCKS5 状态无效；未改动当前 Clash 配置")

    deployed["proxies"] = [
        *proxies,
        {
            "name": MANAGED_RESIDENTIAL_PROXY,
            "type": "socks5",
            "server": residential["server"],
            "port": residential["port"],
            "username": residential["username"],
            "password": residential["password"],
            "udp": False,
        },
    ]
    deployed[groups_key] = [
        *groups,
        {
            "name": MANAGED_RELAY_GROUP,
            "type": "relay",
            # The first hop is the subscription's select group, so a node
            # choice in that group remains the first hop of this chain.
            "proxies": [upstream_group, MANAGED_RESIDENTIAL_PROXY],
        },
    ]
    rules = deployed.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        raise HTTPException(status_code=422, detail="订阅中的规则结构无效")
    managed_rules = [
        *(f"DOMAIN,{domain},{MANAGED_RELAY_GROUP}" for domain in managed_settings["domains"]),
    ]
    if managed_settings.get("non_target_behavior") == "global_passthrough":
        # Original Clash exposes GLOBAL as a virtual selector.  Rule mode is
        # needed for the exact-domain override, while this immediate fallback
        # preserves the current Global-mode choice for every other request.
        deployed["mode"] = "rule"
        managed_rules.append("MATCH,GLOBAL")
    deployed["rules"] = [*managed_rules, *rules]
    return deployed, upstream_group


def _validate_generated_clash_config(clash_data: dict) -> None:
    """Use the installed original Clash binary to reject bad generated YAML before writing it live."""
    try:
        with tempfile.TemporaryDirectory(prefix="clash-manager-validate-") as temp_dir:
            validation_path = Path(temp_dir) / "config.yaml"
            _write_private_text(
                validation_path,
                yaml.safe_dump(clash_data, allow_unicode=True, sort_keys=False),
            )
            result = subprocess.run(
                [config.CLASH_BIN, "-t", "-f", str(validation_path), "-d", config.CLASH_CONFIG_DIR],
                capture_output=True,
                text=True,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=503, detail="无法完成本地 Clash 配置校验；未写入当前配置") from exc
    if result.returncode != 0:
        # Validator output can include upstream node details, so return only a
        # generic diagnostic to the API/UI and never log it.
        raise HTTPException(status_code=422, detail="生成的 Clash 配置未通过本地原版 Clash 校验；未写入当前配置")


def _backup_current_clash_config() -> bool:
    source = Path(config.CLASH_CONFIG_PATH)
    if not source.is_file():
        return False
    _ensure_private_dir(DEPLOYMENT_BACKUP_DIR)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = DEPLOYMENT_BACKUP_DIR / f"config-before-managed-deploy-{stamp}-{time.time_ns()}.yaml"
    try:
        shutil.copyfile(source, destination)
        _restrict_file(destination)
        return True
    except OSError:
        raise HTTPException(status_code=503, detail="无法创建当前 Clash 配置备份；未写入新配置")


def _hot_reload_clash() -> bool:
    try:
        # Original Clash's PUT /configs requires a JSON object.  An empty PUT
        # is a 400 and leaves the on-disk file out of sync with the live core.
        response = _clash_request(
            "PUT",
            "/configs",
            json={"path": str(Path(config.CLASH_CONFIG_PATH).resolve())},
        )
        response.raise_for_status()
        _restore_persisted_proxy_selections()
        return True
    except Exception:
        return False


def _capture_runtime_proxy_selections() -> None:
    """Preserve live selector choices (including virtual GLOBAL) across a hot reload."""
    try:
        response = _clash_request("GET", "/proxies")
        response.raise_for_status()
        proxies = (response.json() or {}).get("proxies", {})
    except Exception:
        return
    if not isinstance(proxies, dict):
        return
    selected: dict[str, str] = {}
    for group_name, group in proxies.items():
        if not isinstance(group_name, str) or not isinstance(group, dict):
            continue
        if str(group.get("type", "")).lower() != "selector":
            continue
        current = group.get("now")
        members = group.get("all")
        if isinstance(current, str) and isinstance(members, list) and current in members:
            selected[group_name] = current
    if not selected:
        return
    state = _load_runtime_state()
    remembered = dict(state.get("selected_proxies", {}))
    remembered.update(selected)
    state["selected_proxies"] = remembered
    _save_runtime_state(state)


def _deploy_raw_clash_config(
    raw_clash_data: dict,
    *,
    current_sub_id: int | None,
    ok_message: str,
) -> dict:
    settings = _load_postprocess_settings()
    # Persist one-time schema upgrades (notably the Global compatibility
    # choice) before the active config's mode is changed by this deployment.
    if MANAGED_POSTPROCESS_FILE.exists():
        _save_postprocess_settings(settings)
    deployed, upstream_group = _build_deployed_clash_dict(raw_clash_data, settings)
    _validate_generated_clash_config(deployed)

    config_path = Path(config.CLASH_CONFIG_PATH)
    previous = _load_clash_config_yaml(config_path) if config_path.exists() else None
    changed = previous != deployed
    backup_created = _backup_current_clash_config() if changed else False
    if changed:
        _capture_runtime_proxy_selections()
        _write_clash_yaml(deployed)
    elif config_path.exists():
        _restrict_file(config_path)

    # An explicit deploy also repairs a possible disk/runtime mismatch left by
    # an older Manager version, while remaining a non-force Clash hot reload.
    hot_reloaded = _hot_reload_clash()
    if current_sub_id is not None:
        _set_current_sub_id(current_sub_id)
    return {
        "ok": True,
        "message": ok_message,
        "deployment_changed": changed,
        "backup_created": backup_created,
        "hot_reloaded": hot_reloaded,
        "upstream_group": upstream_group,
    }


def _current_raw_config_for_postprocess() -> tuple[dict, int | None, str]:
    current_id = _get_current_sub_id()
    if current_id is not None:
        cached = _subscription_cache_file(current_id)
        if cached.is_file():
            raw = _load_clash_config_yaml(cached)
            if isinstance(raw, dict):
                return raw, current_id, "subscription_cache"
    active = _load_clash_config_yaml(Path(config.CLASH_CONFIG_PATH))
    if not isinstance(active, dict):
        raise HTTPException(status_code=503, detail="当前 Clash 配置无法读取；未写入任何变更")
    # The active file may already include the generated overlay; stripping it
    # makes this fallback idempotent without altering any stored raw cache.
    return _strip_managed_postprocess(active), current_id, "active_config"


def _managed_postprocess_status(settings: dict, active_config: dict | None) -> dict:
    mode = str((active_config or {}).get("mode", "rule"))
    proxy_names = {
        str(item.get("name"))
        for item in ((active_config or {}).get("proxies") or [])
        if isinstance(item, dict)
    }
    group_names: set[str] = set()
    if isinstance(active_config, dict):
        try:
            _, groups = _groups_key_and_value(active_config)
            group_names = {str(group.get("name")) for group in groups}
        except HTTPException:
            group_names = set()
    managed_rule_count = sum(
        1 for rule in ((active_config or {}).get("rules") or []) if _is_managed_rule(rule)
    )
    installed = (
        MANAGED_RESIDENTIAL_PROXY in proxy_names
        and MANAGED_RELAY_GROUP in group_names
        and managed_rule_count == len(settings["domains"])
    )
    return {
        "current_mode": mode,
        "installed_in_active_config": installed,
        "active_in_current_mode": bool(settings.get("enabled") and installed and mode == "rule"),
        "managed_rule_count": managed_rule_count,
    }


def _load_subs() -> list[dict]:
    if not SUBS_FILE.exists():
        return []
    with open(SUBS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_subs(subs: list[dict]) -> None:
    _write_private_json(SUBS_FILE, subs)


# ---------- Clash API 代理 ----------
def _clash_request(method: str, path: str, **kwargs) -> httpx.Response:
    base = config.CLASH_API_BASE.rstrip("/")
    url = f"{base}{path}"
    # Control-plane requests must never recurse through this process's
    # HTTP(S)_PROXY environment.  The controller is a local manager resource.
    with httpx.Client(timeout=30.0, trust_env=False) as c:
        return c.request(method, url, **kwargs)


def _clash_unavailable_payload(**extra):
    payload = {
        "ok": False,
        "clash_available": False,
        "message": "clash_not_ready",
    }
    payload.update(extra)
    return payload


@app.get("/api/clash/proxy-info")
def clash_proxy_info():
    """返回本地 Clash 代理地址，供其他程序/开发工具配置使用（如环境变量、IDE 代理）。"""
    return {
        "proxy_url": _proxy_url(),
        "proxy_host": "127.0.0.1",
        "proxy_port": _current_mixed_port(),
        "controller": config.CLASH_API_BASE.rstrip("/"),
    }


@app.get("/api/clash/runtime-config")
def clash_runtime_config():
    data = _effective_clash_yaml()
    return {
        "mixed_port": int(data.get("mixed-port", 7890)),
        "allow_lan": bool(data.get("allow-lan", False)),
        "bind_address": str(data.get("bind-address", "127.0.0.1")),
    }


@app.put("/api/clash/runtime-config")
def update_clash_runtime_config(body: RuntimeConfigUpdate):
    if body.mixed_port <= 0 or body.mixed_port > 65535:
        raise HTTPException(status_code=400, detail="端口必须在 1-65535 之间")
    bind_address = body.bind_address.strip()
    if not bind_address:
        raise HTTPException(status_code=400, detail="bind_address 不能为空")

    data = _effective_clash_yaml()
    changed = (
        int(data.get("mixed-port", 7890)) != body.mixed_port
        or bool(data.get("allow-lan", False)) != body.allow_lan
        or str(data.get("bind-address", "127.0.0.1")) != bind_address
    )
    data["external-controller"] = _controller_address()
    data["mixed-port"] = body.mixed_port
    data["allow-lan"] = body.allow_lan
    data["bind-address"] = bind_address
    _write_clash_yaml(data)
    return {
        "ok": True,
        "runtime_config": {
            "mixed_port": body.mixed_port,
            "allow_lan": body.allow_lan,
            "bind_address": bind_address,
        },
        "requires_restart": changed,
    }


@app.get("/api/clash/version")
def clash_version():
    try:
        r = _clash_request("GET", "/version")
        r.raise_for_status()
        data = r.json() if r.content else {"version": r.text.strip()}
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(version=None)


@app.get("/api/clash/configs")
def clash_configs():
    try:
        r = _clash_request("GET", "/configs")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(mode=None)


@app.get("/api/clash/global-group-name")
def get_global_group_name():
    """
    从当前配置文件读取 proxy-groups 顺序，返回第一个代理组名称。
    CfW 的 Global 标签只显示这个组，Rule 显示全部。
    """
    path = Path(config.CLASH_CONFIG_PATH)
    if not path.exists():
        return {"global_group_name": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        groups = data.get("proxy-groups") or data.get("proxy_groups")
        if not groups or not isinstance(groups, list):
            return {"global_group_name": None}
        first = groups[0]
        name = first.get("name") if isinstance(first, dict) else None
        return {"global_group_name": name}
    except Exception:
        return {"global_group_name": None}


@app.put("/api/clash/configs")
def clash_reload_configs():
    """重载当前配置文件（路径由 Clash 进程启动参数决定）"""
    r = _clash_request(
        "PUT",
        "/configs",
        json={"path": str(Path(config.CLASH_CONFIG_PATH).resolve())},
    )
    r.raise_for_status()
    return r.json() if r.content else {}


@app.patch("/api/clash/configs")
def clash_update_configs(body: dict):
    """更新运行中配置（如 mode: global/rule/direct/script），与 CfW 顶部四个标签一致"""
    mode = body.get("mode")
    if isinstance(mode, str) and mode in {"global", "rule", "direct", "script"}:
        _persist_mode_to_yaml(mode)
    r = _clash_request("PATCH", "/configs", json=body)
    r.raise_for_status()
    return r.json() if r.content else {}


@app.get("/api/clash/proxies")
def clash_proxies():
    try:
        r = _clash_request("GET", "/proxies")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(proxies={})


@app.get("/api/clash/proxies/{name}")
def clash_proxy_detail(name: str):
    try:
        r = _clash_request("GET", f"/proxies/{quote(name)}")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(name=name)


@app.get("/api/clash/proxies/{name}/delay")
def clash_proxy_delay(name: str, url: str = "http://www.gstatic.com/generate_204", timeout: int = 5000):
    """测速：返回该节点的延迟 ms"""
    try:
        path = f"/proxies/{quote(name)}/delay?url={quote(url)}&timeout={timeout}"
        r = _clash_request("GET", path)
        r.raise_for_status()
        data = r.json() if r.content else {}
        delay = data.get("delay") if isinstance(data, dict) else data
        return {
            "delay": delay if isinstance(delay, (int, float)) else None,
            "clash_available": True,
        }
    except Exception:
        return _clash_unavailable_payload(delay=None)


@app.put("/api/clash/proxies/{group_name}")
def clash_select_proxy(group_name: str, body: dict):
    """选择某个代理组使用的节点。body: {"name": "节点名称"}"""
    proxy_name = body.get("name")
    if not isinstance(proxy_name, str) or not proxy_name:
        raise HTTPException(status_code=400, detail="缺少代理节点名称")
    _persist_selected_proxy(group_name, proxy_name)
    r = _clash_request("PUT", f"/proxies/{quote(group_name)}", json=body)
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


@app.get("/api/clash/rules")
def clash_rules():
    try:
        r = _clash_request("GET", "/rules")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(rules=[])


@app.get("/api/clash/connections")
def clash_connections():
    """Clash 当前连接列表：含每条连接的 host/dest、进程路径、上下行字节，用于流量统计与展示。"""
    try:
        r = _clash_request("GET", "/connections")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data["clash_available"] = True
        return data
    except Exception:
        return _clash_unavailable_payload(uploadTotal=0, downloadTotal=0, connections=[])


def _get_proxy_clients() -> list[dict]:
    """查询本机连到 Clash 代理端口(7890)的进程：谁在用 Clash（仅本地连接）。"""
    import config as cfg

    port = _current_mixed_port()
    result: list[dict] = []
    try:
        proc = subprocess.run(
            ["ss", "-tnp", "state", "established", f"( dport = :{port} )"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return result
        import re

        pat = re.compile(r'users:\(\("([^"]*)"[^)]*pid=(\d+)')
        for line in proc.stdout.splitlines():
            # 只统计本机连接（127.0.0.1 或 ::1 连到 7890）
            if "127.0.0.1" not in line and "::1" not in line:
                continue
            m = pat.search(line)
            if m:
                name, pid_s = m.group(1), m.group(2)
                pid = int(pid_s)
                try:
                    with open(f"/proc/{pid}/cmdline", "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read().replace("\x00", " ")
                    cmdline = raw.strip()[:120] or name
                except Exception:
                    cmdline = name
                if not any(r["pid"] == pid for r in result):
                    result.append({"pid": pid, "name": name, "cmdline": cmdline})
    except Exception:
        pass
    return result


@app.get("/api/clash/proxy-clients")
def clash_proxy_clients():
    """当前正在使用 Clash 代理的本机进程（连接 127.0.0.1:7890 的客户端）。若无本地连接则返回空。"""
    return {"clients": _get_proxy_clients()}


# ---------- Manager 托管后处理：Claude 精确域名 -> 订阅节点 -> 住宅 SOCKS5 ----------
def _postprocess_public_payload() -> dict:
    settings = _load_postprocess_settings()
    active = _load_clash_config_yaml(Path(config.CLASH_CONFIG_PATH))
    source: dict | None = None
    try:
        source, _, _ = _current_raw_config_for_postprocess()
    except HTTPException:
        if isinstance(active, dict):
            source = _strip_managed_postprocess(active)

    available_groups: list[str] = []
    resolved_group: str | None = None
    upstream_error: str | None = None
    if isinstance(source, dict):
        try:
            available_groups = _available_upstream_groups(source)
            if settings["enabled"]:
                resolved_group = _resolve_upstream_group(source, settings["upstream_group"])
        except HTTPException as exc:
            # This is a state hint for the editor, not a secret-bearing validator
            # report.  A save/apply still returns its own safe validation error.
            upstream_error = str(exc.detail)

    residential = settings["residential"]
    return {
        "enabled": settings["enabled"],
        "domains": settings["domains"],
        "upstream_group": settings["upstream_group"],
        "non_target_behavior": settings["non_target_behavior"],
        "resolved_upstream_group": resolved_group,
        "available_upstream_groups": available_groups,
        "upstream_error": upstream_error,
        "residential": {
            "server": residential["server"],
            "port": residential["port"],
            "username": residential["username"],
            # Password intentionally never crosses the API boundary.
            "password_configured": bool(residential["password"]),
        },
        "mode_note": {
            "rule": "Rule 模式会将这 15 条精确 DOMAIN 规则置于最前，因此该托管链路可生效。",
            "global": "Global 模式忽略 rules：链路会保存并随订阅部署，但不会接管这 15 个域名。",
            "direct": "Direct 模式忽略 rules：链路会保存并随订阅部署，但不会接管这 15 个域名。",
            "script": "Script 模式由脚本决定路由；不能保证这组 Rule 规则生效。",
        },
        "non_target_behavior_note": (
            "为保持此前 Global 实际行为，Manager 会在 15 条精确规则后加入 MATCH,GLOBAL，并将运行模式设为 Rule；其余流量仍走当前 GLOBAL 选择。"
            if settings["non_target_behavior"] == "global_passthrough"
            else "非目标流量继续按订阅原有规则及顺序处理；要让精确规则生效，请使用 Rule 模式。"
        ),
        **_managed_postprocess_status(settings, active),
    }


@app.get("/api/postprocess/claude-residential")
def get_claude_residential_postprocess():
    return _postprocess_public_payload()


@app.put("/api/postprocess/claude-residential")
def update_claude_residential_postprocess(body: ClaudeResidentialPostprocessUpdate):
    settings = _settings_from_postprocess_update(body)
    _save_postprocess_settings(settings)
    deployment: dict | None = None
    source = None
    if body.apply_now:
        raw, sub_id, source = _current_raw_config_for_postprocess()
        deployment = _deploy_raw_clash_config(
            raw,
            current_sub_id=sub_id,
            ok_message="托管后处理已保存并已尝试热重载当前配置",
        )
    return {
        "ok": True,
        "deployment": deployment,
        "deployment_source": source,
        **_postprocess_public_payload(),
    }


@app.post("/api/postprocess/claude-residential/apply")
def apply_claude_residential_postprocess():
    raw, sub_id, source = _current_raw_config_for_postprocess()
    deployment = _deploy_raw_clash_config(
        raw,
        current_sub_id=sub_id,
        ok_message="托管后处理已从当前原始订阅缓存部署并已尝试热重载",
    )
    return {
        "ok": True,
        "deployment": deployment,
        "deployment_source": source,
        **_postprocess_public_payload(),
    }


# ---------- 订阅管理 ----------
def _subscription_cache_file(sub_id: int) -> Path:
    return PROFILE_CACHE_DIR / f"{sub_id}.yaml"


def _subscription_has_cache(sub_id: int) -> bool:
    return _subscription_cache_file(sub_id).is_file()


def _remove_subscription_cache(sub_id: int) -> None:
    _subscription_cache_file(sub_id).unlink(missing_ok=True)


def _save_subscription_cache_snapshot(sub_id: int, clash_data: dict) -> None:
    _ensure_private_dir(PROFILE_CACHE_DIR)
    _write_private_text(
        _subscription_cache_file(sub_id),
        yaml.safe_dump(dict(clash_data), allow_unicode=True, sort_keys=False),
    )


@app.get("/api/subscriptions")
def list_subscriptions():
    subs = _load_subs()
    out: list[dict] = []
    for s in subs:
        row = dict(s)
        sid = row.get("id")
        row["has_cached_profile"] = _subscription_has_cache(int(sid)) if isinstance(sid, int) else False
        out.append(row)
    return out


@app.get("/api/subscriptions/current")
def get_current_subscription():
    """返回当前已切换到的订阅 id，用于前端高亮"""
    return {"current_id": _get_current_sub_id()}


@app.post("/api/subscriptions")
def add_subscription(sub: SubscriptionCreate):
    subs = _load_subs()
    id_ = max((s.get("id", 0) for s in subs), default=0) + 1
    subs.append({"id": id_, "url": sub.url, "name": sub.name or f"订阅 {id_}"})
    _save_subs(subs)
    return {"id": id_, "url": sub.url, "name": subs[-1]["name"]}


@app.patch("/api/subscriptions/{sub_id}")
def update_subscription(sub_id: int, sub: SubscriptionUpdate):
    subs = _load_subs()
    for s in subs:
        if s.get("id") == sub_id:
            if sub.url is not None:
                prev = str(s.get("url") or "").strip()
                if str(sub.url).strip() != prev:
                    _remove_subscription_cache(sub_id)
                s["url"] = sub.url
            if sub.name is not None:
                s["name"] = sub.name
            _save_subs(subs)
            return s
    raise HTTPException(status_code=404, detail="订阅不存在")


@app.get("/api/preferences")
def get_preferences():
    return {"subscription_fetch_via_clash_proxy": _subscription_via_clash_proxy_enabled()}


@app.patch("/api/preferences")
def patch_preferences(body: ManagerPreferencesUpdate):
    state = _load_runtime_state()
    state["subscription_fetch_via_clash_proxy"] = bool(body.subscription_fetch_via_clash_proxy)
    _save_runtime_state(state)
    return {"ok": True, "subscription_fetch_via_clash_proxy": state["subscription_fetch_via_clash_proxy"]}


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int):
    subs = _load_subs()
    new_subs = [s for s in subs if s.get("id") != sub_id]
    if len(new_subs) == len(subs):
        raise HTTPException(status_code=404, detail="订阅不存在")
    _save_subs(new_subs)
    _remove_subscription_cache(sub_id)
    if _get_current_sub_id() == sub_id:
        _set_current_sub_id(None)
    return {"ok": True}


def _upstream_http_headers() -> dict[str, str]:
    """访问订阅转换/CDN（如 api.dler.io）时用浏览器 UA，降低被拒概率。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }


def _subscription_source_headers() -> dict[str, str]:
    """
    访问机场/提供商的订阅原始链接。
    不少面板按 User-Agent 分流：浏览器 UA 给 base64 节点列表，Clash UA 才给完整 YAML。
    """
    return {"User-Agent": "Clash/1.18.3", "Accept": "*/*"}


def _try_clash_dict_from_subscription_payload(raw_text: str) -> dict | None:
    """若订阅链接本体即返回 Clash YAML，则跳过远程转换接口。"""
    body = (raw_text or "").strip()
    if body.startswith("\ufeff"):
        body = body[1:].strip()
    if not body:
        return None
    try:
        data = yaml.safe_load(body)
    except Exception:
        return None
    if isinstance(data, dict) and "proxies" in data:
        return data
    return None


def _merge_runtime_into_clash_dict(clash_data: dict) -> None:
    runtime_values = _effective_clash_yaml()
    clash_data["external-controller"] = _controller_address()
    for key in ("mixed-port", "allow-lan", "bind-address", "mode"):
        if key in runtime_values:
            clash_data[key] = runtime_values[key]


def _finalize_subscription_deploy(
    sub_id: int,
    raw_clash_data: dict,
    *,
    update_snapshot: bool,
    ok_message: str,
) -> dict:
    if not isinstance(raw_clash_data, dict):
        raise HTTPException(status_code=502, detail="无效 Clash 配置结构")
    # profile_cache stays as the provider/converter returned it.  Runtime
    # fields and the manager overlay exist only in the deployed config.
    if update_snapshot:
        _save_subscription_cache_snapshot(sub_id, raw_clash_data)
    return _deploy_raw_clash_config(
        raw_clash_data,
        current_sub_id=sub_id,
        ok_message=ok_message,
    )


def _pull_remote_subscription_to_clash_dict(sub_url: str) -> dict:
    """从订阅 URL / 转换服务拉取并解析为 Clash dict。"""
    clash_data = None
    direct_fetch_note = ""

    hdrs_provider = _subscription_source_headers()
    hdrs_converter = _upstream_http_headers()
    sub_client_kw = _httpx_client_kwargs_for_subscription_fetch()
    try:
        sub_resp = _subscription_http_get(sub_client_kw, sub_url, headers=hdrs_provider)
        sub_resp.raise_for_status()
        clash_data = _try_clash_dict_from_subscription_payload(sub_resp.text)
    except httpx.HTTPStatusError as e:
        status = getattr(e.response, "status_code", None)
        reason = getattr(e.response, "reason_phrase", "") or ""
        direct_fetch_note = f"直接访问订阅链接得到 HTTP {status} {reason.strip()}。"
    except httpx.RequestError as e:
        direct_fetch_note = f"直接访问订阅链接失败：{e!s}。"

    if clash_data is None:
        converter = config.SUB_CONVERTER_API.rstrip("/")
        if "?" in converter:
            fetch_url = f"{converter}&target=clash&url={quote(sub_url)}"
        else:
            fetch_url = f"{converter}?target=clash&url={quote(sub_url)}"

        try:
            r = _subscription_http_get(sub_client_kw, fetch_url, headers=hdrs_converter)
            r.raise_for_status()
            clash_yaml = r.text
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response is not None else 0
            tip = ""
            if code == 403:
                tip = (
                    "公共服务常拒绝从自身服务器替你拉取某些订阅地址。"
                    "可自建 subconverter（或换其它可用的 sub_converter_api），"
                    "或改用直接返回 Clash YAML 格式的订阅。"
                )
            detail = (
                f"订阅转换服务返回 HTTP {code}。"
                + (tip if tip else "")
                + (f" [{direct_fetch_note}]" if direct_fetch_note else "")
            )
            raise HTTPException(status_code=502, detail=detail.strip())
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=502,
                detail=f"无法连接到订阅转换服务：{e!s}"
                + (f" [{direct_fetch_note}]" if direct_fetch_note else ""),
            )

        clash_data = yaml.safe_load(clash_yaml)

    if not isinstance(clash_data, dict):
        raise HTTPException(status_code=502, detail="订阅转换结果不是有效的 Clash YAML")
    return clash_data


def _subscription_refresh_cache_only_impl(sub_id: int) -> dict:
    """仅从网络刷新 profile_cache/{id}.yaml，不写入当前 Clash 配置、不改变当前选中订阅。"""
    subs = _load_subs()
    sub = next((s for s in subs if s.get("id") == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    clash_data = _pull_remote_subscription_to_clash_dict(sub["url"])
    # Do not merge runtime values or managed rules into the raw cache.  A later
    # switch/apply deterministically overlays the then-current manager state.
    _save_subscription_cache_snapshot(sub_id, clash_data)
    return {
        "ok": True,
        "message": "已更新本地缓存，当前运行配置未改动；点此订阅卡片可从缓存切换。",
        "cache_path": str(_subscription_cache_file(sub_id)),
    }


def _subscription_refresh_and_activate_impl(sub_id: int) -> dict:
    """拉取远端并写入当前 Clash 配置、设为当前选中（兼容 /apply）。"""
    subs = _load_subs()
    sub = next((s for s in subs if s.get("id") == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    clash_data = _pull_remote_subscription_to_clash_dict(sub["url"])
    return _finalize_subscription_deploy(
        sub_id,
        clash_data,
        update_snapshot=True,
        ok_message="已从订阅拉取并切换为当前配置",
    )


def _subscription_switch_impl(sub_id: int) -> dict:
    subs = _load_subs()
    sub = next((s for s in subs if s.get("id") == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    cf = _subscription_cache_file(sub_id)
    if not cf.is_file():
        raise HTTPException(
            status_code=404,
            detail="该订阅暂无本地缓存，请先点击「刷新」从远端拉取一次。",
        )
    clash_data = yaml.safe_load(cf.read_text(encoding="utf-8"))
    if not isinstance(clash_data, dict):
        raise HTTPException(status_code=502, detail="本地缓存损坏，请重新刷新该订阅")
    return _finalize_subscription_deploy(
        sub_id,
        clash_data,
        update_snapshot=False,
        ok_message="已切换为该订阅的本地缓存（未重新下载）",
    )


@app.post("/api/subscriptions/{sub_id}/refresh")
def subscription_refresh(sub_id: int):
    """从订阅链接重新拉取，仅更新本地 profile 缓存。"""
    return _subscription_refresh_cache_only_impl(sub_id)


@app.post("/api/subscriptions/{sub_id}/switch")
def subscription_switch(sub_id: int):
    """仅使用上次刷新时保存的本地缓存切换 profile，不访问网络。"""
    return _subscription_switch_impl(sub_id)


@app.post("/api/subscriptions/{sub_id}/apply")
def apply_subscription(sub_id: int):
    """拉取并切换为当前配置（兼容脚本/旧客户端；网页请用「刷新」+「点卡片切换」）。"""
    return _subscription_refresh_and_activate_impl(sub_id)


@app.get("/api/health")
def health():
    try:
        _clash_request("GET", "/version")
        clash_ok = True
    except Exception:
        clash_ok = False
    return {"clash_reachable": clash_ok}


def _stop_clash() -> bool:
    """终止 Clash 进程，返回是否找到并杀掉了进程。"""
    try:
        # 用配置路径匹配，避免误杀
        proc = subprocess.run(
            ["pkill", "-f", config.CLASH_CONFIG_PATH],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _pids_listening_on_tcp_port(port: int) -> list[int]:
    """返回监听指定 TCP 端口的 PID 列表（依赖 lsof，无则返回空）。"""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    out: list[int] = []
    for line in proc.stdout.strip().splitlines():
        try:
            out.append(int(line.strip()))
        except ValueError:
            continue
    return out


def _kill_listeners_on_tcp_port(port: int) -> None:
    """结束所有占用该 TCP 监听端口的进程（用于释放 mixed-port，避免 address already in use）。"""
    p = int(port)
    try:
        subprocess.run(
            ["fuser", "-k", f"{p}/tcp"],
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    for pid in _pids_listening_on_tcp_port(p):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    time.sleep(0.4)
    for pid in _pids_listening_on_tcp_port(p):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


@app.post("/api/clash/start-or-restart")
def clash_start_or_restart():
    """
    若 Clash 未运行则启动，若已运行则先终止再启动。
    启动前会释放当前 config.yaml 中 mixed-port 对应的监听，避免端口占用。
    返回 action: "started" | "restarted"
    """
    was_alive = _clash_alive()
    if was_alive:
        _stop_clash()
        time.sleep(2)
    _kill_listeners_on_tcp_port(_current_mixed_port())
    time.sleep(0.5)
    _start_clash()
    time.sleep(2)
    return {"ok": True, "action": "restarted" if was_alive else "started"}




# ---------- 代理测试（经 Clash 出口访问站点 + 查看当前 IP/地址）-----------
TEST_SITES = [
    {"name": "Google", "url": "https://www.google.com"},
    {"name": "Facebook", "url": "https://www.facebook.com"},
    {"name": "YouTube", "url": "https://www.youtube.com"},
    {"name": "Bilibili", "url": "https://www.bilibili.com"},
]


# 站点测试时带上常见浏览器请求头，避免被 Bilibili/Facebook 等当爬虫返回 412/400
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _run_one_site(proxy: str | None, site: dict, timeout: float = 15.0) -> dict:
    """单站点测试，返回该站点结果。"""
    ok = False
    status_code = None
    time_ms = None
    err = None
    headers = DEFAULT_HEADERS
    if proxy:
        headers = {**DEFAULT_HEADERS, "Accept-Encoding": "gzip, deflate"}
    client_kw: dict = dict(
        proxy=proxy,
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
        trust_env=False,
    )
    if proxy:
        client_kw["limits"] = httpx.Limits(max_connections=10, max_keepalive_connections=0)
    attempts = 3 if proxy else 1

    def try_verify(verify: ssl.SSLContext | bool, n_attempts: int) -> bool:
        nonlocal ok, status_code, time_ms, err
        for attempt in range(n_attempts):
            try:
                t0 = time.perf_counter()
                kw = {**client_kw, "verify": verify}
                with httpx.Client(**kw) as client:
                    r = client.get(site["url"])
                time_ms = round((time.perf_counter() - t0) * 1000)
                status_code = r.status_code
                ok = 200 <= r.status_code < 400
                err = None
                return True
            except Exception as e:
                err = str(e)
                if attempt < n_attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
        return False

    if not try_verify(True, attempts) and proxy and err and _tls_eof_like_error(err):
        try_verify(_ssl_context_tls12_only(), 2)

    return {
        "name": site["name"],
        "url": site["url"],
        "ok": ok,
        "status_code": status_code,
        "time_ms": time_ms,
        "error": err,
    }


def _run_site_tests(proxy: str | None, timeout: float = 15.0) -> list[dict]:
    """跑站点可达性测试，proxy 为 None 表示直连。"""
    return [_run_one_site(proxy, site, timeout) for site in TEST_SITES]


def _run_ip_check(proxy: str | None, timeout: float = 10.0) -> dict:
    """查出口 IP/地址，proxy 为 None 表示直连。"""
    ip_result: dict = {"ip": None, "city": None, "region": None, "country": None, "org": None, "error": None}
    client_kw: dict = dict(proxy=proxy, timeout=timeout, trust_env=False)
    if proxy:
        client_kw["limits"] = httpx.Limits(max_connections=10, max_keepalive_connections=0)
    attempts = 3 if proxy else 1

    def try_once(url: str, verify: ssl.SSLContext | bool, n_attempts: int) -> bool:
        last_err: str | None = None
        for attempt in range(n_attempts):
            try:
                kw = {**client_kw, "verify": verify}
                with httpx.Client(**kw) as client:
                    r = client.get(url)
                    r.raise_for_status()
                    data = r.json()
                if isinstance(data, dict):
                    ip_result["ip"] = data.get("ip")
                    ip_result["city"] = data.get("city")
                    ip_result["region"] = data.get("region")
                    ip_result["country"] = data.get("country")
                    ip_result["org"] = data.get("org")
                ip_result["error"] = None
                return True
            except Exception as e:
                last_err = str(e)
                if attempt < n_attempts - 1:
                    time.sleep(0.25 * (attempt + 1))
        ip_result["error"] = last_err
        return False

    primary = "https://ipinfo.io/json"
    fallback = "https://api.ipify.org?format=json"

    if try_once(primary, True, attempts):
        return ip_result
    if proxy and _tls_eof_like_error(ip_result["error"] or ""):
        if try_once(primary, _ssl_context_tls12_only(), 2):
            return ip_result
        if try_once(fallback, True, 2):
            return ip_result
        try_once(fallback, _ssl_context_tls12_only(), 2)
    return ip_result


@app.get("/api/test/proxy")
def test_proxy():
    """
    左右对比：左边经 Clash 代理、右边直连（不使用代理），
    各测站点可达性 + 出口 IP/地址。
    """
    proxy_url = _proxy_url()
    return {
        "proxy_url_used": proxy_url,
        "with_proxy": {
            "sites": _run_site_tests(proxy_url),
            "ip": _run_ip_check(proxy_url),
        },
        "without_proxy": {
            "sites": _run_site_tests(None),
            "ip": _run_ip_check(None),
        },
    }


@app.get("/api/test/proxy/stream")
async def test_proxy_stream():
    """
    SSE 流式返回：并行跑 10 项测试（4 站点×2 + IP×2），
    每完成一项就推送一条 event，前端可实时更新卡片。
    """
    async def generate():
        loop = asyncio.get_event_loop()
        proxy_url = _proxy_url()

        async def run_site(side: str, proxy: str | None, site: dict):
            r = await loop.run_in_executor(None, _run_one_site, proxy, site)
            return {"side": side, "type": "site", "name": site["name"], "result": r}

        async def run_ip(side: str, proxy: str | None):
            r = await loop.run_in_executor(None, _run_ip_check, proxy)
            return {"side": side, "type": "ip", "result": r}

        yield f"data: {json.dumps({'type': 'meta', 'proxy_url_used': proxy_url}, ensure_ascii=False)}\n\n"

        tasks = [
            asyncio.create_task(run_site("with_proxy", proxy_url, s)) for s in TEST_SITES
        ] + [
            asyncio.create_task(run_site("without_proxy", None, s)) for s in TEST_SITES
        ] + [
            asyncio.create_task(run_ip("with_proxy", proxy_url)),
            asyncio.create_task(run_ip("without_proxy", None)),
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                event = await coro
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                err_event = {"type": "error", "error": str(e)}
                yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
        yield "data: {\"type\":\"done\"}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
