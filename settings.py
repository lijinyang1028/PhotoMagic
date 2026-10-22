"""
PhotoMagic 配置管理

- 非敏感配置：~/.photomagic/settings.json
  包含多套 LLM 配置方案（名称 / API Base URL / Model）、当前选中的方案、
  输出格式、并发数、AI 强度等
- API Key：系统钥匙串（keyring），按配置方案分别保存
  Windows: Credential Manager
  macOS: Keychain
  Linux: SecretService (GNOME Keyring / KWallet)

  钥匙串条目：service="photomagic"，user="api_key:<方案 id>"。
  旧版本只有单套配置时使用的 "api_key" 条目会在启动时自动迁移到
  「默认配置」方案。

如果 keyring 不可用，API Key 只在内存中保留，不落盘。

日志：~/.photomagic/logs/，每次运行一个文件，只保留最近 5 次（见 log_setup.py）。
"""
import json
import logging
import os
import shutil
import uuid
from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------- 路径 ----------------
CONFIG_DIR = Path.home() / ".photomagic"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CUSTOM_PARAMS_FILE = CONFIG_DIR / "custom_params.json"
LOG_DIR = CONFIG_DIR / "logs"

KEYRING_SERVICE = "photomagic"
KEYRING_USER = "api_key"                # 旧版单配置时代的钥匙串条目
KEYRING_PROFILE_PREFIX = "api_key:"     # 多套配置方案的钥匙串条目前缀

# 旧版单套配置迁移后使用的方案 id / 名称
DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "默认配置"

_LEGACY_CUSTOM_PARAMS = Path.home() / ".photomagic_custom_params.json"


# ---------------- 默认值 ----------------
DEFAULT_SETTINGS = {
    "api_profiles": [],
    "active_api_profile": "",
    "api_base": "https://api.openai.com/v1",
    "model": "gpt-4o",
    "output_dir": "",
    "output_format": 0,
    "bit_depth": 1,
    "jpeg_quality": 92,
    "color_space": 0,
    "parallel": 3,
    "strength": 100,
    "unify_style": False,
}


# ---------------- keyring 探测 ----------------
try:
    import keyring
    _KEYRING_IMPORTED = True
except ImportError:
    keyring = None
    _KEYRING_IMPORTED = False

_KEYRING_AVAILABLE = None  # None=未探测 / True / False
# keyring 不可用时的降级存储：{方案 id: API Key}
_MEMORY_API_KEYS = {}


def keyring_available() -> bool:
    """检测系统钥匙串是否可用（结果缓存）。"""
    global _KEYRING_AVAILABLE
    if _KEYRING_AVAILABLE is not None:
        return _KEYRING_AVAILABLE
    if not _KEYRING_IMPORTED:
        _KEYRING_AVAILABLE = False
        logger.info("系统钥匙串不可用：未安装 keyring，API Key 仅保存在内存中")
        return False
    try:
        backend = keyring.get_keyring()
        name = type(backend).__name__.lower()
        # FailKeyring / fail.Keyring 表示系统没有可用后端
        _KEYRING_AVAILABLE = "fail" not in name
        logger.info("系统钥匙串探测结果: %s（后端 %s）",
                    "可用" if _KEYRING_AVAILABLE else "不可用",
                    type(backend).__name__)
    except Exception as e:
        _KEYRING_AVAILABLE = False
        logger.warning("系统钥匙串探测失败，API Key 仅保存在内存中: %s", e)
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
            logger.info("已迁移旧参数文件 %s -> %s",
                        _LEGACY_CUSTOM_PARAMS, CUSTOM_PARAMS_FILE)
        except OSError as e:
            logger.warning("迁移旧参数文件失败 %s: %s",
                           _LEGACY_CUSTOM_PARAMS, e)
    else:
        logger.info("无需迁移旧配置（旧文件存在=%s，新文件存在=%s）",
                    _LEGACY_CUSTOM_PARAMS.exists(), CUSTOM_PARAMS_FILE.exists())

    # 旧版只有一套 API 配置：升级为「配置方案」，并迁移钥匙串中的旧 Key
    try:
        ensure_api_profiles()
    except Exception as e:
        logger.warning("迁移旧版 API 配置方案失败: %s", e)


# ---------------- 配置方案（非敏感部分） ----------------
def _as_text(value) -> str:
    """把配置里的值安全地转成去空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _new_profile_id() -> str:
    return uuid.uuid4().hex[:12]


def _find_profile(profiles, profile_id: str):
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    return None


def _profile_name_taken(profiles, name: str, exclude_id: str = "") -> bool:
    folded = name.casefold()
    return any(
        p.get("id") != exclude_id and _as_text(p.get("name")).casefold() == folded
        for p in profiles
    )


def normalize_api_profiles(data: dict):
    """
    整理 api_profiles / active_api_profile，并让顶层 api_base / model
    与当前方案保持同步（兼容旧代码）。

    返回 (整理后的 data, 是否有改动)。不写盘、不访问钥匙串。
    """
    changed = False
    profiles = data.get("api_profiles")
    if not isinstance(profiles, list):
        profiles = []
        changed = True

    cleaned = []
    seen_ids = set()
    seen_names = set()
    for item in profiles:
        if not isinstance(item, dict):
            changed = True
            continue
        profile_id = _as_text(item.get("id"))
        if not profile_id or profile_id in seen_ids:
            profile_id = _new_profile_id()
            changed = True
        name = _as_text(item.get("name"))
        if not name:
            name = f"配置 {len(cleaned) + 1}"
            changed = True
        # 同名方案会导致下拉框无法区分，自动加序号
        base_name = name
        serial = 2
        while name.casefold() in seen_names:
            name = f"{base_name} ({serial})"
            serial += 1
            changed = True
        profile = {
            "id": profile_id,
            "name": name,
            "api_base": _as_text(item.get("api_base")),
            "model": _as_text(item.get("model")),
        }
        if profile != item:
            changed = True
        seen_ids.add(profile_id)
        seen_names.add(name.casefold())
        cleaned.append(profile)

    if not cleaned:
        # 没有方案（首次运行 / 旧版只有顶层 api_base + model）：
        # 用顶层字段生成一套「默认配置」
        cleaned = [{
            "id": DEFAULT_PROFILE_ID,
            "name": DEFAULT_PROFILE_NAME,
            "api_base": _as_text(data.get("api_base")) or DEFAULT_SETTINGS["api_base"],
            "model": _as_text(data.get("model")) or DEFAULT_SETTINGS["model"],
        }]
        changed = True

    active = _as_text(data.get("active_api_profile"))
    if active not in {p["id"] for p in cleaned}:
        active = cleaned[0]["id"]
        changed = True

    data["api_profiles"] = cleaned
    data["active_api_profile"] = active

    active_profile = _find_profile(cleaned, active)
    if data.get("api_base") != active_profile["api_base"]:
        data["api_base"] = active_profile["api_base"]
        changed = True
    if data.get("model") != active_profile["model"]:
        data["model"] = active_profile["model"]
        changed = True
    return data, changed


def _read_settings_file():
    """读取 settings.json；文件不存在或损坏时返回 None。"""
    if not SETTINGS_FILE.exists():
        return None
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("配置文件内容不是对象，已忽略: %s", SETTINGS_FILE)
            return None
        return data
    except Exception as e:
        logger.error("读取设置失败: %s", e)
        return None


# ---------------- 非敏感配置 ----------------
def load_settings() -> dict:
    ensure_config_dir()
    raw = _read_settings_file()
    if raw is None:
        logger.info("配置文件不存在或不可读，使用默认设置: %s", SETTINGS_FILE)
        merged = dict(DEFAULT_SETTINGS)
    else:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(raw)
    merged, _ = normalize_api_profiles(merged)
    logger.info("已读取设置: %s（%d 项，当前方案=%s，api_base=%s，model=%s）",
                SETTINGS_FILE, len(merged), merged.get("active_api_profile"),
                merged.get("api_base"), merged.get("model"))
    return merged


def save_settings(settings: dict) -> None:
    """原子写入（先写 tmp 再 replace），并同步当前方案的顶层字段。"""
    ensure_config_dir()
    data = dict(settings)
    data, _ = normalize_api_profiles(data)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_FILE)
    logger.info("已保存设置: %s（%d 项，%d 套配置方案，当前方案=%s）",
                SETTINGS_FILE, len(data), len(data.get("api_profiles", [])),
                data.get("active_api_profile"))


def ensure_api_profiles() -> bool:
    """
    把旧版「单套 API 配置」升级为「配置方案」并持久化（幂等）。

    仅在确实有变化时写盘；写盘成功后把旧版钥匙串里的 API Key 迁移到
    「默认配置」方案。返回是否发生了迁移 / 写盘。
    """
    raw = _read_settings_file()
    if raw is None:
        # 还没有 settings.json，但旧版可能已经在钥匙串里存过 Key：
        # 把 Key 提前迁移到未来的「默认配置」方案，避免之后新增第二套
        # 方案时旧 Key 因「单一方案回退」失效而看起来丢失。
        _migrate_legacy_keyring_key([{"id": DEFAULT_PROFILE_ID}])
        return False
    data = dict(DEFAULT_SETTINGS)
    data.update(raw)
    data, changed = normalize_api_profiles(data)
    if not changed:
        return False
    try:
        save_settings(data)
    except Exception as e:
        logger.error("迁移旧版 API 配置方案失败（写入 %s）: %s",
                     SETTINGS_FILE, e)
        return False
    _migrate_legacy_keyring_key(data["api_profiles"])
    logger.info("已把旧版 API 配置迁移为配置方案（当前方案=%s）",
                data["active_api_profile"])
    return True


def _migrate_legacy_keyring_key(profiles) -> None:
    """把旧版单条钥匙串 API Key 迁移到默认方案（已有方案 Key 时跳过）。"""
    if not keyring_available():
        return
    if not any(p["id"] == DEFAULT_PROFILE_ID for p in profiles):
        return
    try:
        existing = keyring.get_password(
            KEYRING_SERVICE, _profile_keyring_user(DEFAULT_PROFILE_ID))
        if existing:
            return
        legacy_key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if not legacy_key:
            return
        keyring.set_password(
            KEYRING_SERVICE, _profile_keyring_user(DEFAULT_PROFILE_ID),
            legacy_key)
        logger.info("已把旧版 API Key 迁移到配置方案「%s」（长度 %d）",
                    DEFAULT_PROFILE_NAME, len(legacy_key))
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            logger.info("已删除旧版单配置 API Key 条目")
        except Exception as e:
            logger.info("旧版单配置 API Key 条目无需删除: %s", e)
    except Exception as e:
        logger.warning("迁移旧版 API Key 失败: %s", e)


# ---------------- 配置方案查询 / 修改 ----------------
def list_api_profiles() -> list:
    """返回所有配置方案（dict 副本，按保存顺序）。"""
    return [dict(p) for p in load_settings()["api_profiles"]]


def get_active_profile_id() -> str:
    return _as_text(load_settings().get("active_api_profile"))


def get_api_profile(profile_id: str):
    """按 id 取配置方案；不存在返回 None。"""
    profile_id = _as_text(profile_id)
    for profile in load_settings()["api_profiles"]:
        if profile["id"] == profile_id:
            return dict(profile)
    return None


def get_active_profile() -> dict:
    """返回当前选中的配置方案（load_settings 保证至少有一套）。"""
    data = load_settings()
    return dict(_find_profile(data["api_profiles"],
                              data["active_api_profile"]))


def profile_name_taken(name: str, exclude_id: str = "") -> bool:
    """配置方案名称是否已被其它方案占用（忽略大小写）。"""
    name = _as_text(name)
    if not name:
        return False
    return _profile_name_taken(load_settings()["api_profiles"], name,
                               exclude_id)


def save_api_profile(name: str, api_base: str, model: str,
                     profile_id: str = "") -> dict:
    """
    新增或更新一套配置方案，并把它设为当前方案。

    profile_id 为空时新建方案；名称不能为空、不能与其它方案重复。
    这里只保存非敏感字段，API Key 请用 set_api_key(..., profile_id)。
    返回保存后的方案 dict；校验失败抛 ValueError。
    """
    name = _as_text(name)
    if not name:
        raise ValueError("配置名称不能为空。")
    data = load_settings()
    profiles = data["api_profiles"]
    # 升级后首次新增第二套方案前，先把旧版单配置 Key 落到默认方案，
    # 否则「单一方案回退」失效会让旧 Key 看起来消失了。
    _migrate_legacy_keyring_key(profiles)
    profile_id = _as_text(profile_id)
    if _profile_name_taken(profiles, name, exclude_id=profile_id):
        raise ValueError(f"配置名称「{name}」已存在，请换一个名称。")

    if profile_id:
        target = _find_profile(profiles, profile_id)
        if target is None:
            raise ValueError("要保存的配置方案不存在（可能已被删除）。")
        target.update({
            "name": name,
            "api_base": _as_text(api_base),
            "model": _as_text(model),
        })
    else:
        profile_id = _new_profile_id()
        target = {
            "id": profile_id,
            "name": name,
            "api_base": _as_text(api_base),
            "model": _as_text(model),
        }
        profiles.append(target)

    data["active_api_profile"] = profile_id
    save_settings(data)
    logger.info("已保存配置方案「%s」（id=%s，model=%s，api_base=%s）并设为当前方案",
                name, profile_id, target["model"], target["api_base"])
    return dict(target)


def rename_api_profile(profile_id: str, name: str) -> dict:
    """重命名配置方案；校验失败抛 ValueError。"""
    name = _as_text(name)
    if not name:
        raise ValueError("配置名称不能为空。")
    data = load_settings()
    profiles = data["api_profiles"]
    target = _find_profile(profiles, _as_text(profile_id))
    if target is None:
        raise ValueError("配置方案不存在（可能已被删除）。")
    if _profile_name_taken(profiles, name, exclude_id=target["id"]):
        raise ValueError(f"配置名称「{name}」已存在，请换一个名称。")
    old_name = target["name"]
    target["name"] = name
    save_settings(data)
    logger.info("配置方案「%s」已重命名为「%s」（id=%s）",
                old_name, name, target["id"])
    return dict(target)


def delete_api_profile(profile_id: str) -> bool:
    """删除一套配置方案及其钥匙串中的 API Key；至少保留一套。"""
    data = load_settings()
    profiles = data["api_profiles"]
    profile_id = _as_text(profile_id)
    target = _find_profile(profiles, profile_id)
    if target is None:
        logger.warning("删除配置方案失败：不存在（id=%s）", profile_id)
        return False
    if len(profiles) <= 1:
        logger.warning("拒绝删除最后一个配置方案（至少保留一套）")
        return False

    data["api_profiles"] = [p for p in profiles if p["id"] != profile_id]
    if data["active_api_profile"] == profile_id:
        data["active_api_profile"] = data["api_profiles"][0]["id"]
    save_settings(data)
    delete_api_key(profile_id)
    logger.info("已删除配置方案「%s」（id=%s）及其 API Key，当前方案=%s",
                target["name"], profile_id, data["active_api_profile"])
    return True


def set_active_api_profile(profile_id: str) -> bool:
    """切换当前配置方案并持久化（顶层 api_base / model 会同步）。"""
    profile_id = _as_text(profile_id)
    data = load_settings()
    target = _find_profile(data["api_profiles"], profile_id)
    if target is None:
        logger.warning("切换配置方案失败：不存在（id=%s）", profile_id)
        return False
    if data["active_api_profile"] == profile_id:
        return True
    data["active_api_profile"] = profile_id
    save_settings(data)
    logger.info("已切换当前配置方案为「%s」（id=%s，model=%s，api_base=%s）",
                target["name"], profile_id, target["model"],
                target["api_base"])
    return True


# ---------------- API Key（钥匙串） ----------------
def _profile_keyring_user(profile_id: str) -> str:
    return f"{KEYRING_PROFILE_PREFIX}{profile_id}"


def _delete_keyring_password(user: str, label: str) -> None:
    """删除钥匙串条目；条目不存在不算错误。"""
    try:
        keyring.delete_password(KEYRING_SERVICE, user)
        logger.info("已从系统钥匙串删除 %s API Key", label)
    except Exception as e:
        logger.info("系统钥匙串中没有可删除的 %s API Key: %s", label, e)


def _single_default_profile(data: dict, profile_id: str) -> bool:
    """是否仍处于「只有一套默认方案」的旧版兼容状态。"""
    profiles = data.get("api_profiles") or []
    return (profile_id == DEFAULT_PROFILE_ID
            and len(profiles) == 1
            and profiles[0].get("id") == DEFAULT_PROFILE_ID)


def get_api_key(profile_id: str = "") -> str:
    """读取指定配置方案的 API Key；不传则读当前选中的方案。"""
    data = load_settings()
    key_profile_id = _as_text(profile_id) or _as_text(
        data.get("active_api_profile"))
    if not key_profile_id:
        return ""

    if keyring_available():
        try:
            key = keyring.get_password(
                KEYRING_SERVICE, _profile_keyring_user(key_profile_id)) or ""
        except Exception as e:
            logger.warning("读取钥匙串失败，改用内存中的 API Key: %s", e)
            key = ""
        if key:
            logger.info("已从系统钥匙串读取配置方案（%s）的 API Key：已配置",
                        key_profile_id)
            return key
        # 钥匙串读失败 / 写入失败时，本次会话可能只在内存里有 Key
        memory_key = _MEMORY_API_KEYS.get(key_profile_id, "")
        if memory_key:
            logger.info("改用本次会话内存中的 API Key（配置方案 %s），"
                        "未写入系统钥匙串", key_profile_id)
            return memory_key
        # 旧版兼容：升级后只有一套默认方案、且尚未迁移时回退到旧条目
        if _single_default_profile(data, key_profile_id):
            try:
                legacy_key = keyring.get_password(
                    KEYRING_SERVICE, KEYRING_USER) or ""
            except Exception as e:
                logger.warning("读取旧版钥匙串条目失败: %s", e)
                legacy_key = ""
            if legacy_key:
                logger.info("从旧版单配置钥匙串条目读取 API Key（下次启动自动迁移）")
                return legacy_key
        logger.info("配置方案（%s）未配置 API Key", key_profile_id)
        return ""

    return _MEMORY_API_KEYS.get(key_profile_id, "")


def set_api_key(api_key: str, profile_id: str = "") -> bool:
    """
    保存指定配置方案的 API Key；不传 profile_id 则保存到当前方案。

    返回 True：已成功写入系统钥匙串
    返回 False：keyring 不可用 / 写入失败（此时仅保存在内存）
    """
    data = load_settings()
    key_profile_id = _as_text(profile_id) or _as_text(
        data.get("active_api_profile"))
    if not key_profile_id:
        logger.warning("保存 API Key 失败：没有可用的配置方案")
        return False

    _MEMORY_API_KEYS[key_profile_id] = api_key

    if not keyring_available():
        logger.info("钥匙串不可用，配置方案（%s）的 API Key 仅保存在内存中（长度 %d）",
                    key_profile_id, len(api_key))
        return False

    try:
        if api_key:
            keyring.set_password(
                KEYRING_SERVICE, _profile_keyring_user(key_profile_id),
                api_key)
            logger.info("API Key 已写入系统钥匙串（配置方案 %s，长度 %d）",
                        key_profile_id, len(api_key))
            if key_profile_id == DEFAULT_PROFILE_ID:
                # 兼容旧版：默认方案的 Key 同步写一份旧版条目
                try:
                    keyring.set_password(KEYRING_SERVICE, KEYRING_USER,
                                         api_key)
                except Exception as e:
                    logger.warning("同步旧版 API Key 条目失败: %s", e)
        else:
            _delete_keyring_password(_profile_keyring_user(key_profile_id),
                                     f"配置方案 {key_profile_id} 的")
            if key_profile_id == DEFAULT_PROFILE_ID:
                _delete_keyring_password(KEYRING_USER, "旧版单配置的")
        return True
    except Exception as e:
        logger.error("写入钥匙串失败，API Key 仅本次会话有效: %s", e)
        return False


def delete_api_key(profile_id: str = "") -> bool:
    """删除指定配置方案的 API Key；不传 profile_id 则删除当前方案的。"""
    data = load_settings()
    key_profile_id = _as_text(profile_id) or _as_text(
        data.get("active_api_profile"))
    if not key_profile_id:
        return False

    _MEMORY_API_KEYS.pop(key_profile_id, None)
    if not keyring_available():
        logger.info("钥匙串不可用，已清空内存中配置方案（%s）的 API Key",
                    key_profile_id)
        return False
    _delete_keyring_password(_profile_keyring_user(key_profile_id),
                             f"配置方案 {key_profile_id} 的")
    if key_profile_id == DEFAULT_PROFILE_ID:
        _delete_keyring_password(KEYRING_USER, "旧版单配置的")
    return True


# ---------------- 自定义参数路径（供 rt_processor 使用） ----------------
def get_custom_params_path() -> Path:
    ensure_config_dir()
    return CUSTOM_PARAMS_FILE
