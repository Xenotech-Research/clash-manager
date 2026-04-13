# Clash Manager

FastAPI + Vite(React) 实现的 Clash 订阅管理与节点切换。

## 获取代码

```bash
git clone git@github.com:Xenotech-Research/clash-manager.git
cd clash-manager
```

项目根目录自带一个 `config.json`，用于配置 Clash 二进制路径、Clash 配置文件路径、控制器地址等本地部署项。

## 功能

- **订阅管理**：添加订阅链接（支持返回 base64 的订阅），一键「使用此订阅」拉取并转为 Clash 配置，写入 `/opt/clash-config/config.yaml` 并触发 Clash 重载。
- **节点选择**：展示当前配置中的代理组，下拉切换节点（调用 Clash 控制器 API）。

## Clash 内核安装（前置）

本项目本身不负责安装 Clash 内核；它依赖一份本地可执行文件，并由后端直接启动。后端默认从项目根目录的 `config.json` 读取配置，当前默认值是：

- `CLASH_BIN=/opt/clash/clash`
- `CLASH_CONFIG_PATH=/opt/clash-config/config.yaml`
- `CLASH_API_BASE=http://127.0.0.1:9090`

这台机器上实际采用的方式不是 `apt install`，而是“源码编译 + 后端按路径启动”；其中软链接只是当前机器的一种接入方式，不是必须要求：

1. 先自行安装 Go 和 `make`：
   该 Clash 仓库的 `go.mod` 当前要求 `go 1.20` 及以上，安装完成后可先用 `go version` 确认版本。
   此处不固定给出安装命令，是因为不同 Linux 发行版、软件源和机器环境下可获得的 Go 版本差异较大，直接写死命令容易装到过旧版本。
2. 克隆 Clash 源码：
   `git clone git@github.com:Xenotech-Research/clash.git /home/ubuntu/apps/clash`
3. 编译 Linux amd64 可执行文件：
   `cd /home/ubuntu/apps/clash && make linux-amd64`
4. 修改项目根目录的 `config.json`，至少确认这些字段符合你的机器实际情况：
   `clash_bin`
   `clash_config_path`
   `clash_api_base`
   `clash_mixed_port`
5. 选择一种接入方式：
   方式 A：保持默认值，准备 `/opt/clash/clash`，可以是真实文件，也可以是软链接。
   方式 B：不使用 `/opt/clash/clash`，直接把 `config.json` 里的 `clash_bin` 改成你的实际路径。
6. 若你选择方式 A，可像当前机器一样创建软链接：
   `sudo mkdir -p /opt/clash`
   `sudo ln -sf /home/ubuntu/apps/clash/bin/clash-linux-amd64 /opt/clash/clash`
7. 准备 Clash 配置文件所在目录：
   例如当 `config.json` 中的 `clash_config_path` 为 `/opt/clash-config/config.yaml` 时，可先执行：
   `sudo mkdir -p /opt/clash-config`
8. 放置 Clash 配置文件：
   例如：
   `sudo cp config.yaml /opt/clash-config/config.yaml`
9. 下载 GeoIP 数据库：
   `sudo wget -O /opt/clash-config/Country.mmdb https://github.com/Dreamacro/maxmind-geoip/releases/latest/download/Country.mmdb`

可选检查：

- 查看版本：`$CLASH_BIN -v` 或直接执行你的 Clash 二进制路径
- 当前机器上的版本示例：`Clash v1.17.0-1-gc7e34bd linux amd64`
- 查看 Go 版本：`go version`
- 查看当前后端配置：直接打开项目根目录的 `config.json`

说明：

- `/opt/clash/clash` 可以是真实文件，也可以像当前机器一样是一个软链接。
- 只要 `config.json` 中的 `clash_bin` 指向一个可执行的 Clash 内核即可，不要求必须安装在 `/opt/clash/`。
- 若你仍然希望通过环境变量注入配置，后端也仍然支持；环境变量会覆盖 `config.json` 中的同名配置。
- `config.yaml` 需要与本项目配置一致，至少应保证 `external-controller: 127.0.0.1:9090`，并让代理端口与 `clash_mixed_port` 对应。
- 如需跟随原始上游，也可以改为从 `https://github.com/BackupTime/clash.git` 克隆后再按同样步骤编译。

## 运行

- 后端：`uvicorn server:app --host 0.0.0.0 --port 9988`
- 前端：仓库在 `web/`，**`cd web && yarn build`** 得到 `web/dist`，nginx 直接指向该目录。
- 前端开发：`cd web && yarn dev`（会代理 `/api` 到 9988）
- 生产：`./start.sh` 用 screen 启动后端；nginx 提供静态 `web/dist` 并转发 `/api` 到 9988。

## 配置

推荐直接编辑项目根目录的 `config.json`：

- `clash_api_base`：Clash 控制器地址，默认 `http://127.0.0.1:9090`
- `clash_mixed_port`：Clash 混合代理端口（HTTP/SOCKS 同一端口），默认 `7890`
- `clash_config_path`：写入的配置路径，默认 `/opt/clash-config/config.yaml`
- `clash_bin`：Clash 可执行文件路径，默认 `/opt/clash/clash`
- `sub_converter_api`：订阅转 Clash 的 API，默认 `https://api.dler.io/sub`

如果你已有现成的部署方式，也可以继续使用环境变量 `CLASH_API_BASE`、`CLASH_MIXED_PORT`、`CLASH_CONFIG_PATH`、`CLASH_BIN`、`SUB_CONVERTER_API` 覆盖这些配置。

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
