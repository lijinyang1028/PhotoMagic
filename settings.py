"""
PhotoMagic 配置管理

- 非敏感配置：~/.photomagic/settings.json
  包含 API Base URL、Model、输出格式、并发数、AI 强度等
- API Key：系统钥匙串（keyring）
  Windows: Credential Manager
  macOS: Keychain
  Linux: SecretService (GNOME Keyring / KWallet)

如果 keyring 不可用，API Key 只在内存中保留，不落盘。
"""
import json
import os
import shutil
from pathlib import Path


# ---------------- 路径 ----------------
CONFIG_DIR = Path.home() / ".photomagic"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CUSTOM_PARAMS_FILE = CONFIG_DIR / "custom_params.json"

KEYRING_SERVICE = "photomagic"
KEYRING_USER = "api_key"

_LEGACY_CUSTOM_PARAMS = Path.home() / ".photomagic_custom_params.json"


# ---------------- 默认值 ----------------
DEFAULT_SETTINGS = {
    "api_base": "https://api.openai.com/v1",
    "model": "gpt-4o",
    "output_dir": "",
    "output_format": 0,
    "bit_depth": 1,
    "jpeg_quality": 92,
    "color_space": 0,
    "parallel": 3,
    "strength": 100,
}


# ---------------- keyring 探测 ----------------
try:
    import keyring
    _KEYRING_IMPORTED = True
except ImportError:
    keyring = None
    _KEYRING_IMPORTED = False

_KEYRING_AVAILABLE = None  # None=未探测 / True / False
_MEMORY_API_KEY = ""       # keyring 不可用时的降级存储


def keyring_available() -> bool:
    """检测系统钥匙串是否可用（结果缓存）。"""
    global _KEYRING_AVAILABLE
    if _KEYRING_AVAILABLE is not None:
        return _KEYRING_AVAILABLE
    if not _KEYRING_IMPORTED:
        _KEYRING_AVAILABLE = False
        return False
    try:
        backend = keyring.get_keyring()
        name = type(backend).__name__.lower()
        # FailKeyring / fail.Keyring 表示系统没有可用后端
        _KEYRING_AVAILABLE = "fail" not in name
    except Exception:
        _KEYRING_AVAILABLE = False
    return _KEYRING_AVAILABLE


# ---------------- 目录 ----------------
def ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # POSIX 上把权限收紧到 0700
        os.chmod(CONFIG_DIR, 0o700)
    except (OSError, NotImplementedError):
        pass


def migrate_legacy_files():
    """把旧位置的配置文件迁移到 ~/.photomagic/（幂等）。"""
    ensure_config_dir()
    if _LEGACY_CUSTOM_PARAMS.exists() and not CUSTOM_PARAMS_FILE.exists():
        try:
            shutil.copy2(_LEGACY_CUSTOM_PARAMS, CUSTOM_PARAMS_FILE)
        except OSError:
            pass


# ---------------- 非敏感配置 ----------------
def load_settings() -> dict:
    ensure_config_dir()
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_SETTINGS)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    """原子写入（先写 tmp 再 replace）。"""
    ensure_config_dir()
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_FILE)


# ---------------- API Key（钥匙串） ----------------
def get_api_key() -> str:
    """读取 API Key；keyring 不可用时返回内存中的值。"""
    if keyring_available():
        try:
            return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
        except Exception:
            return _MEMORY_API_KEY
    return _MEMORY_API_KEY


def set_api_key(api_key: str) -> bool:
    """
    保存 API Key。

    返回 True：已成功写入系统钥匙串
    返回 False：keyring 不可用 / 写入失败（此时仅保存在内存）
    """
    global _MEMORY_API_KEY
    _MEMORY_API_KEY = api_key

    if not keyring_available():
        return False

    try:
        if api_key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, api_key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        return True
    except Exception:
        return False


def delete_api_key() -> bool:
    """从钥匙串删除 API Key。"""
    global _MEMORY_API_KEY
    _MEMORY_API_KEY = ""
    if not keyring_available():
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except Exception:
        return False


# ---------------- 自定义参数路径（供 rt_processor 使用） ----------------
def get_custom_params_path() -> Path:
    ensure_config_dir()
    return CUSTOM_PARAMS_FILE