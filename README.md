# Clash Manager

FastAPI + Vite(React) 实现的 Clash 订阅管理与节点切换。

## 获取代码

```bash
git clone git@github.com:Xenotech-Research/clash-manager.git
cd clash-manager
```

## 功能

- **订阅管理**：添加订阅链接（支持返回 base64 的订阅），一键「使用此订阅」拉取并转为 Clash 配置，写入 `/opt/clash-config/config.yaml` 并触发 Clash 重载。
- **节点选择**：展示当前配置中的代理组，下拉切换节点（调用 Clash 控制器 API）。

## Clash 内核安装（前置）

本项目本身不负责安装 Clash 内核；它依赖一份本地可执行文件，并由后端直接启动。当前代码默认使用：

- `CLASH_BIN=/opt/clash/clash`
- `CLASH_CONFIG_PATH=/opt/clash-config/config.yaml`
- `CLASH_API_BASE=http://127.0.0.1:9090`

这台机器上实际采用的方式不是 `apt install`，而是“源码编译 + 软链接接入”：

1. 安装 Go（该 Clash 仓库的 `go.mod` 当前要求 `go 1.20` 及以上）：
   `sudo apt update`
   `sudo apt install -y golang-go make`
   `go version`
2. 克隆 Clash 源码：
   `git clone git@github.com:Xenotech-Research/clash.git /home/ubuntu/apps/clash`
3. 编译 Linux amd64 可执行文件：
   `cd /home/ubuntu/apps/clash && make linux-amd64`
4. 准备程序入口路径：
   `sudo mkdir -p /opt/clash`
5. 将编译产物软链接到项目默认路径：
   `sudo ln -sf /home/ubuntu/apps/clash/bin/clash-linux-amd64 /opt/clash/clash`
6. 准备配置目录：
   `sudo mkdir -p /opt/clash-config`
7. 放置 Clash 配置文件：
   `sudo cp config.yaml /opt/clash-config/config.yaml`
8. 下载 GeoIP 数据库：
   `sudo wget -O /opt/clash-config/Country.mmdb https://github.com/Dreamacro/maxmind-geoip/releases/latest/download/Country.mmdb`

可选检查：

- 查看版本：`/opt/clash/clash -v`
- 当前机器上的版本示例：`Clash v1.17.0-1-gc7e34bd linux amd64`
- 查看 Go 版本：`go version`

说明：

- `/opt/clash/clash` 可以是真实文件，也可以像当前机器一样是一个软链接。
- 只要 `CLASH_BIN` 指向一个可执行的 Clash 内核即可，不要求必须安装在 `/opt/clash/`。
- 若你的内核路径不同，可通过环境变量覆盖 `CLASH_BIN`。
- `config.yaml` 需要与本项目配置一致，至少应保证 `external-controller: 127.0.0.1:9090`，并让代理端口与 `CLASH_MIXED_PORT` 对应。
- 如需跟随原始上游，也可以改为从 `https://github.com/BackupTime/clash.git` 克隆后再按同样步骤编译。

## 运行

- 后端：`uvicorn server:app --host 0.0.0.0 --port 9988`
- 前端：仓库在 `web/`，**`cd web && yarn build`** 得到 `web/dist`，nginx 直接指向该目录。
- 前端开发：`cd web && yarn dev`（会代理 `/api` 到 9988）
- 生产：`./start.sh` 用 screen 启动后端；nginx 提供静态 `web/dist` 并转发 `/api` 到 9988。

## 配置（可选）

- `CLASH_API_BASE`：Clash 控制器地址，默认 `http://127.0.0.1:9090`
- `CLASH_MIXED_PORT`：Clash 混合代理端口（HTTP/SOCKS 同一端口），默认 `7890`
- `CLASH_CONFIG_PATH`：写入的配置路径，默认 `/opt/clash-config/config.yaml`
- `SUB_CONVERTER_API`：订阅转 Clash 的 API，默认 `https://api.dler.io/sub`

## 其他程序使用本地代理

本服务使用的 Clash 在本地提供代理，默认地址为 **`http://127.0.0.1:7890`**（与 Clash for Windows 常见配置一致）。要让终端、IDE、npm、git 等走该代理，可：

1. **接口查询**：`GET /api/clash/proxy-info` 返回 `proxy_url`、`proxy_port`、`controller`，便于脚本或工具自动读取。
2. **侧栏复制**：前端 Connected 下方会显示「本地代理」和「复制环境变量」按钮，一键复制代理 URL 或 `HTTP_PROXY`/`HTTPS_PROXY` 导出命令。
4. **手动配置示例**：
   - 当前终端：`export HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890`
   - npm：`npm config set proxy http://127.0.0.1:7890` / `npm config set https-proxy http://127.0.0.1:7890`
   - git：`git config --global http.proxy http://127.0.0.1:7890` / `git config --global https.proxy http://127.0.0.1:7890`
   - 取消：将上述 proxy 设为空字符串或 `""` 即可。

## 入口

- 已加入 `~/start_all.sh`，开机/统一启动时会启动本服务。
- 已加入 `~/nginx.conf`，域名 `prox.xenotech.studio`（80/443），需在 nginx 主配置 reload 后生效。
