"""
Clash 订阅管理 + 节点切换后端
- 代理 Clash 控制器 API（配置、代理列表、选节点）
- 订阅管理：增删改、从订阅链接拉取并转为 Clash 配置、写盘并重载
- 维持 Clash 内核运行：启动时若未检测到则拉起，后台定时检测并自动重启
"""
import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config

SUBS_FILE = Path(__file__).resolve().parent / "subscriptions.json"
CURRENT_SUB_FILE = Path(__file__).resolve().parent / "current_subscription_id.json"
CLASH_LOG = Path(__file__).resolve().parent / "logs" / "clash.log"
TRAFFIC_LOG_DIR = Path(__file__).resolve().parent / "logs"


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
    with open(CURRENT_SUB_FILE, "w", encoding="utf-8") as f:
        json.dump({"current_id": sub_id}, f)


def _clash_alive() -> bool:
    try:
        base = config.CLASH_API_BASE.rstrip("/")
        r = httpx.get(f"{base}/version", timeout=2.0)
        return r.is_success
    except Exception:
        return False


def _start_clash() -> None:
    """启动 Clash 进程（后台、与当前进程脱离），日志写入 logs/clash.log"""
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
    # 启动时若 Clash 未运行则启动
    if not _clash_alive():
        _start_clash()
        await asyncio.sleep(2)
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


def _load_subs() -> list[dict]:
    if not SUBS_FILE.exists():
        return []
    with open(SUBS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_subs(subs: list[dict]) -> None:
    with open(SUBS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


# ---------- Clash API 代理 ----------
def _clash_request(method: str, path: str, **kwargs) -> httpx.Response:
    base = config.CLASH_API_BASE.rstrip("/")
    url = f"{base}{path}"
    with httpx.Client(timeout=30.0) as c:
        return c.request(method, url, **kwargs)


@app.get("/api/clash/proxy-info")
def clash_proxy_info():
    """返回本地 Clash 代理地址，供其他程序/开发工具配置使用（如环境变量、IDE 代理）。"""
    return {
        "proxy_url": PROXY_URL,
        "proxy_host": "127.0.0.1",
        "proxy_port": config.CLASH_MIXED_PORT,
        "controller": config.CLASH_API_BASE.rstrip("/"),
    }


@app.get("/api/clash/version")
def clash_version():
    r = _clash_request("GET", "/version")
    r.raise_for_status()
    return r.json() if r.content else {"version": r.text.strip()}


@app.get("/api/clash/configs")
def clash_configs():
    r = _clash_request("GET", "/configs")
    r.raise_for_status()
    return r.json()


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
    r = _clash_request("PUT", "/configs")
    r.raise_for_status()
    return r.json() if r.content else {}


@app.patch("/api/clash/configs")
def clash_update_configs(body: dict):
    """更新运行中配置（如 mode: global/rule/direct/script），与 CfW 顶部四个标签一致"""
    r = _clash_request("PATCH", "/configs", json=body)
    r.raise_for_status()
    return r.json() if r.content else {}


@app.get("/api/clash/proxies")
def clash_proxies():
    r = _clash_request("GET", "/proxies")
    r.raise_for_status()
    return r.json()


@app.get("/api/clash/proxies/{name}")
def clash_proxy_detail(name: str):
    r = _clash_request("GET", f"/proxies/{quote(name)}")
    r.raise_for_status()
    return r.json()


@app.get("/api/clash/proxies/{name}/delay")
def clash_proxy_delay(name: str, url: str = "http://www.gstatic.com/generate_204", timeout: int = 5000):
    """测速：返回该节点的延迟 ms"""
    path = f"/proxies/{quote(name)}/delay?url={quote(url)}&timeout={timeout}"
    r = _clash_request("GET", path)
    r.raise_for_status()
    data = r.json() if r.content else {}
    delay = data.get("delay") if isinstance(data, dict) else data
    return {"delay": delay} if isinstance(delay, (int, float)) else {"delay": None}


@app.put("/api/clash/proxies/{group_name}")
def clash_select_proxy(group_name: str, body: dict):
    """选择某个代理组使用的节点。body: {"name": "节点名称"}"""
    r = _clash_request("PUT", f"/proxies/{quote(group_name)}", json=body)
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return {"ok": True}
    return r.json()


@app.get("/api/clash/rules")
def clash_rules():
    r = _clash_request("GET", "/rules")
    r.raise_for_status()
    return r.json()


@app.get("/api/clash/connections")
def clash_connections():
    """Clash 当前连接列表：含每条连接的 host/dest、进程路径、上下行字节，用于流量统计与展示。"""
    r = _clash_request("GET", "/connections")
    r.raise_for_status()
    return r.json()


def _get_proxy_clients() -> list[dict]:
    """查询本机连到 Clash 代理端口(7890)的进程：谁在用 Clash（仅本地连接）。"""
    import config as cfg

    port = cfg.CLASH_MIXED_PORT
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


# ---------- 订阅管理 ----------
@app.get("/api/subscriptions")
def list_subscriptions():
    return _load_subs()


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
                s["url"] = sub.url
            if sub.name is not None:
                s["name"] = sub.name
            _save_subs(subs)
            return s
    raise HTTPException(status_code=404, detail="订阅不存在")


@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int):
    subs = _load_subs()
    new_subs = [s for s in subs if s.get("id") != sub_id]
    if len(new_subs) == len(subs):
        raise HTTPException(status_code=404, detail="订阅不存在")
    _save_subs(new_subs)
    if _get_current_sub_id() == sub_id:
        _set_current_sub_id(None)
    return {"ok": True}


@app.post("/api/subscriptions/{sub_id}/apply")
def apply_subscription(sub_id: int):
    """
    从该订阅链接拉取内容 → 用转换 API 转为 Clash 配置 → 写入 CLASH_CONFIG_PATH → 让 Clash 重载配置
    """
    subs = _load_subs()
    sub = next((s for s in subs if s.get("id") == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    sub_url = sub["url"]

    # 使用订阅转换 API：target=clash&url=订阅链接
    converter = config.SUB_CONVERTER_API.rstrip("/")
    if "?" in converter:
        fetch_url = f"{converter}&target=clash&url={quote(sub_url)}"
    else:
        fetch_url = f"{converter}?target=clash&url={quote(sub_url)}"

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(fetch_url)
        r.raise_for_status()
        clash_yaml = r.text

    config_path = Path(config.CLASH_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # 强制只监听本机，防止订阅配置覆盖后代理被暴露到公网
    import re
    clash_yaml = re.sub(r"allow-lan:\s*\w+", "allow-lan: true", clash_yaml, count=1)
    if "bind-address:" in clash_yaml:
        clash_yaml = re.sub(r"bind-address:\s*[^\n]+", 'bind-address: "127.0.0.1"', clash_yaml, count=1)
    else:
        clash_yaml = re.sub(r"(allow-lan:\s*\w+)", r'\1\nbind-address: "127.0.0.1"', clash_yaml, count=1)
    config_path.write_text(clash_yaml, encoding="utf-8")

    # 让 Clash 重载配置（若 Clash 是用 -f 指定该路径启动的，重载会读新文件）
    try:
        _clash_request("PUT", "/configs")
    except Exception:
        # 文件已更新，重载失败可能因 Clash 未用该路径启动，仅告警
        pass

    _set_current_sub_id(sub_id)
    return {"ok": True, "message": "配置已写入并已触发重载", "path": str(config_path)}


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


@app.post("/api/clash/start-or-restart")
def clash_start_or_restart():
    """
    若 Clash 未运行则启动，若已运行则先终止再启动。
    返回 action: "started" | "restarted"
    """
    was_alive = _clash_alive()
    if was_alive:
        _stop_clash()
        time.sleep(2)
    _start_clash()
    time.sleep(2)
    return {"ok": True, "action": "restarted" if was_alive else "started"}




# ---------- 代理测试（经 Clash 出口访问站点 + 查看当前 IP/地址）-----------
PROXY_URL = f"http://127.0.0.1:{config.CLASH_MIXED_PORT}"

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
    try:
        t0 = time.perf_counter()
        with httpx.Client(proxy=proxy, timeout=timeout, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
            r = client.get(site["url"])
        time_ms = round((time.perf_counter() - t0) * 1000)
        status_code = r.status_code
        ok = 200 <= r.status_code < 400
    except Exception as e:
        err = str(e)
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
    ip_result = {"ip": None, "city": None, "region": None, "country": None, "org": None, "error": None}
    try:
        with httpx.Client(proxy=proxy, timeout=timeout) as client:
            r = client.get("https://ipinfo.io/json")
            r.raise_for_status()
            data = r.json()
            ip_result["ip"] = data.get("ip")
            ip_result["city"] = data.get("city")
            ip_result["region"] = data.get("region")
            ip_result["country"] = data.get("country")
            ip_result["org"] = data.get("org")
    except Exception as e:
        ip_result["error"] = str(e)
    return ip_result


@app.get("/api/test/proxy")
def test_proxy():
    """
    左右对比：左边经 Clash 代理、右边直连（不使用代理），
    各测站点可达性 + 出口 IP/地址。
    """
    return {
        "with_proxy": {
            "sites": _run_site_tests(PROXY_URL),
            "ip": _run_ip_check(PROXY_URL),
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

        async def run_site(side: str, proxy: str | None, site: dict):
            r = await loop.run_in_executor(None, _run_one_site, proxy, site)
            return {"side": side, "type": "site", "name": site["name"], "result": r}

        async def run_ip(side: str, proxy: str | None):
            r = await loop.run_in_executor(None, _run_ip_check, proxy)
            return {"side": side, "type": "ip", "result": r}

        tasks = [
            asyncio.create_task(run_site("with_proxy", PROXY_URL, s)) for s in TEST_SITES
        ] + [
            asyncio.create_task(run_site("without_proxy", None, s)) for s in TEST_SITES
        ] + [
            asyncio.create_task(run_ip("with_proxy", PROXY_URL)),
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
