import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
CONFIG_SAMPLE_FILE = BASE_DIR / "config.sample.json"

REQUIRED_KEYS = (
    "clash_api_base",
    "clash_config_path",
    "clash_bin",
    "sub_converter_api",
)


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")

    return data


def _load_file_config() -> dict:
    if not CONFIG_SAMPLE_FILE.exists():
        raise RuntimeError(f"Missing {CONFIG_SAMPLE_FILE}")

    sample_data = _read_json_object(CONFIG_SAMPLE_FILE)
    missing_in_sample = [key for key in REQUIRED_KEYS if key not in sample_data]
    if missing_in_sample:
        raise RuntimeError(
            f"{CONFIG_SAMPLE_FILE} is missing required keys: {', '.join(missing_in_sample)}"
        )

    if not CONFIG_FILE.exists():
        message = f"Missing {CONFIG_FILE}"
        message += f". Copy {CONFIG_SAMPLE_FILE.name} to {CONFIG_FILE.name} and adjust it for this machine."
        raise RuntimeError(message)

    file_data = _read_json_object(CONFIG_FILE)
    merged = dict(sample_data)
    merged.update(file_data)

    missing = [key for key in REQUIRED_KEYS if key not in merged]
    if missing:
        raise RuntimeError(f"Effective config is missing required keys: {', '.join(missing)}")

    return merged


FILE_CONFIG = _load_file_config()


# Clash 控制器 API（与 config.yaml 中 external-controller 一致）
CLASH_API_BASE = str(FILE_CONFIG["clash_api_base"])

# 当前使用的 Clash 配置文件路径（写入/更新订阅后写到这里）
CLASH_CONFIG_PATH = str(FILE_CONFIG["clash_config_path"])
CLASH_CONFIG_DIR = str(Path(CLASH_CONFIG_PATH).resolve().parent)

# Clash 可执行文件路径（后端负责启动/维持运行时使用）
CLASH_BIN = str(FILE_CONFIG["clash_bin"])

# 订阅转换 API：将「订阅链接(base64)」转为 Clash 格式的 config
# 格式: SUB_CONVERTER_URL + "?target=clash&url=" + url_encoded_subscription_url
SUB_CONVERTER_API = str(FILE_CONFIG["sub_converter_api"])
