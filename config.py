# Clash 与订阅管理配置
import os
from pathlib import Path

# Clash 控制器 API（与 config.yaml 中 external-controller 一致）
CLASH_API_BASE = os.environ.get("CLASH_API_BASE", "http://127.0.0.1:9090")

# Clash 混合代理端口（mixed-port，测试请求经此出口）
CLASH_MIXED_PORT = int(os.environ.get("CLASH_MIXED_PORT", "7890"))

# 当前使用的 Clash 配置文件路径（写入/更新订阅后写到这里）
CLASH_CONFIG_PATH = os.environ.get("CLASH_CONFIG_PATH", "/opt/clash-config/config.yaml")
CLASH_CONFIG_DIR = str(Path(CLASH_CONFIG_PATH).resolve().parent)

# Clash 可执行文件路径（后端负责启动/维持运行时使用）
CLASH_BIN = os.environ.get("CLASH_BIN", "/opt/clash/clash")

# 订阅转换 API：将「订阅链接(base64)」转为 Clash 格式的 config
# 格式: SUB_CONVERTER_URL + "?target=clash&url=" + url_encoded_subscription_url
SUB_CONVERTER_API = os.environ.get(
    "SUB_CONVERTER_API",
    "https://api.dler.io/sub",
)

