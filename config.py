# Clash 与订阅管理配置
import json
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULTS = {
    "clash_api_base": "http://127.0.0.1:9090",
    "clash_mixed_port": 7890,
    "clash_config_path": "/opt/clash-config/config.yaml",
    "clash_bin": "/opt/clash/clash",
    "sub_converter_api": "https://api.dler.io/sub",
}


def _load_file_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {CONFIG_FILE}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{CONFIG_FILE} must contain a JSON object")

    return data


FILE_CONFIG = _load_file_config()


def _get_str(key: str, env_key: str) -> str:
    value = os.environ.get(env_key)
    if value:
        return value

    value = FILE_CONFIG.get(key, DEFAULTS[key])
    return str(value)


def _get_int(key: str, env_key: str) -> int:
    value = os.environ.get(env_key)
    if value is not None:
        return int(value)

    value = FILE_CONFIG.get(key, DEFAULTS[key])
    return int(value)


# Clash 控制器 API（与 config.yaml 中 external-controller 一致）
CLASH_API_BASE = _get_str("clash_api_base", "CLASH_API_BASE")

# Clash 混合代理端口（mixed-port，测试请求经此出口）
CLASH_MIXED_PORT = _get_int("clash_mixed_port", "CLASH_MIXED_PORT")

# 当前使用的 Clash 配置文件路径（写入/更新订阅后写到这里）
CLASH_CONFIG_PATH = _get_str("clash_config_path", "CLASH_CONFIG_PATH")
CLASH_CONFIG_DIR = str(Path(CLASH_CONFIG_PATH).resolve().parent)

# Clash 可执行文件路径（后端负责启动/维持运行时使用）
CLASH_BIN = _get_str("clash_bin", "CLASH_BIN")

# 订阅转换 API：将「订阅链接(base64)」转为 Clash 格式的 config
# 格式: SUB_CONVERTER_URL + "?target=clash&url=" + url_encoded_subscription_url
SUB_CONVERTER_API = _get_str("sub_converter_api", "SUB_CONVERTER_API")
