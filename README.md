# Clash Manager

FastAPI + Vite(React) 实现的 Clash 订阅管理与节点切换。

## 获取代码

```bash
git clone git@github.com:Xenotech-Research/clash-manager.git
cd clash-manager
```

仓库提供 `config.sample.json` 作为模板；首次使用时，请先复制为 `config.json`，再按你的机器环境修改其中路径。`config.json` 不需要把每个字段都重新写一遍，只放你想覆盖的项即可。

## 功能

- **订阅管理**：添加订阅链接（支持返回 base64 的订阅），一键「使用此订阅」拉取并转为 Clash 配置，写入 `/opt/clash-config/config.yaml` 并触发 Clash 重载。
- **节点选择**：展示当前配置中的代理组，下拉切换节点（调用 Clash 控制器 API）。

## Clash 内核安装（前置）

本项目本身不负责安装 Clash 内核；它依赖一份本地可执行文件，并由后端直接启动。后端只读取项目根目录的 `config.json`。

开始之前，先执行：

```bash
cp config.sample.json config.json
```

`config.sample.json` 中提供了完整的推荐字段；`config.json` 会在启动时覆盖这些默认值。示例值如下：

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
4. 修改项目根目录的 `config.json`，按需覆盖与你机器相关的字段，例如：
   `clash_bin`
   `clash_config_path`
   `clash_api_base`
   `clash_mixed_port`
5. 选择一种接入方式：
   方式 A：保持默认值，准备 `/opt/clash/clash`，可以是真实文件，也可以是软链接。
   如果你选择方式 A，可像当前机器一样创建软链接：
   `sudo mkdir -p /opt/clash`
   `sudo ln -sf /home/ubuntu/apps/clash/bin/clash-linux-amd64 /opt/clash/clash`
   方式 B：不使用 `/opt/clash/clash`，直接把 `config.json` 里的 `clash_bin` 改成你的实际路径。
6. 准备 Clash 配置文件所在目录：
   例如当 `config.json` 中的 `clash_config_path` 为 `/opt/clash-config/config.yaml` 时，可先执行：
   `sudo mkdir -p /opt/clash-config`
   如果你把 `clash_config_path` 改成了别的路径，例如 `/data/clash/config.yaml`，那就应创建对应父目录：
   `sudo mkdir -p /data/clash`
7. 放置 Clash 配置文件：
   例如默认路径下：
   `sudo cp config.yaml /opt/clash-config/config.yaml`
   如果你使用的是自定义路径，则应复制到 `clash_config_path` 指定的位置，例如：
   `sudo cp config.yaml /data/clash/config.yaml`
   如果该文件不存在，后端在首次启动 Clash 时也会自动创建一份最小可运行配置；但如果你已有自己的配置，仍然建议显式放置你要使用的 `config.yaml`。

可选检查：

- 查看版本：`$CLASH_BIN -v` 或直接执行你的 Clash 二进制路径
- 当前机器上的版本示例：`Clash v1.17.0-1-gc7e34bd linux amd64`
- 查看 Go 版本：`go version`
- 查看当前后端配置：先看 `config.sample.json` 默认值，再看 `config.json` 中你覆盖了哪些字段
- 若缺少 `config.json`：从 `config.sample.json` 复制一份后再启动后端

说明：

- `/opt/clash/clash` 可以是真实文件，也可以像当前机器一样是一个软链接。
- 只要 `config.json` 中的 `clash_bin` 指向一个可执行的 Clash 内核即可，不要求必须安装在 `/opt/clash/`。
- `config.yaml` 需要与本项目配置一致，至少应保证 `external-controller: 127.0.0.1:9090`，并让代理端口与 `clash_mixed_port` 对应。
- 若 `clash_config_path` 指向的文件不存在，后端会自动生成一份最小配置，确保 Clash 至少可以先启动并暴露控制器。
- 如果你的 Clash 配置实际使用了 GeoIP 相关规则或功能，还需要按配置要求准备 `Country.mmdb`。
- 如需跟随原始上游，也可以改为从 `https://github.com/BackupTime/clash.git` 克隆后再按同样步骤编译。

## 运行

- 安装后端依赖：`pip install -r requirements.txt`
- 安装前端依赖：`cd web && yarn install`
- 后端开发启动：`uvicorn server:app --host 0.0.0.0 --port 9988`
- 后端常驻启动：`./start.sh`
- 正式使用：
  前端仓库在 `web/`，先执行 `cd web && yarn build` 生成 `web/dist`，再由 nginx 提供静态页面并转发 `/api` 到 `9988`。
- 临时使用：
  如果暂时不想配 nginx，可以让后端继续通过 `./start.sh` 在后台常驻，然后单独启动 Vite 前端：
  `cd web && yarn dev --host 0.0.0.0 --port 5173`
  `yarn dev` 会自动把 `/api` 代理到 `http://127.0.0.1:9988`，因此它可以临时同时承担“前端页面 + 后端接口转发”的作用。
  这种方式下，后端端口 `9988` 不需要公网开放；只要 `yarn dev` 所在端口能被公网访问即可。

## 配置

推荐通过 `config.sample.json` + `config.json` 的方式管理配置：

- `clash_api_base`：Clash 控制器地址，默认 `http://127.0.0.1:9090`
- `clash_mixed_port`：Clash 混合代理端口（HTTP/SOCKS 同一端口），默认 `7890`
- `clash_config_path`：写入的配置路径，默认 `/opt/clash-config/config.yaml`
- `clash_bin`：Clash 可执行文件路径，默认 `/opt/clash/clash`
- `sub_converter_api`：订阅转 Clash 的 API，默认 `https://api.dler.io/sub`

其中：

- `config.sample.json`：仓库内的完整默认模板
- `config.json`：本机部署覆盖项，不纳入版本控制

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
