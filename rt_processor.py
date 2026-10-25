"""
RawTherapee 处理模块
将 JSON 参数转换为 .pp3 文件，并通过命令行调用 rawtherapee-cli
"""
import os
import re
import json
import logging
import subprocess
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("params_schema.json")
CUSTOM_PARAMS_PATH = Path.home() / ".photomagic_custom_params.json"
_PARAMS_CACHE = None

# rawtherapee-cli 在不同系统上的可执行文件名
RT_CLI_NAMES = ("rawtherapee-cli", "rawtherapee-cli.exe")


# ----------------------------- 参数表加载 -----------------------------
def _load_params():
    """加载参数表，缓存避免重复读盘"""
    global _PARAMS_CACHE
    if _PARAMS_CACHE is None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            _PARAMS_CACHE = json.load(f)
        logger.info("已加载参数表 %s（%d 个参数）",
                    _SCHEMA_PATH, len(_PARAMS_CACHE))
    return _PARAMS_CACHE


def get_params():
    """对外暴露的参数表（友好名 -> 定义）"""
    return _load_params()


def get_builtin_param_names() -> set:
    """返回内置 schema 中定义的所有参数名。"""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        names = set(json.load(f).keys())
    logger.info("读取内置参数名 %d 个", len(names))
    return names


def reload_params():
    """清缓存，下次调用重新读 JSON。"""
    global _PARAMS_CACHE
    _PARAMS_CACHE = None
    logger.info("已清空参数表缓存，下次访问将重新读取 %s", _SCHEMA_PATH)


def merge_params(extra: dict):
    """合并外部 JSON 参数定义，用于用户自定义扩展。"""
    base = dict(_load_params())
    base.update(extra)
    _PARAMS_CACHE = base
    logger.info("已合并 %d 个参数定义，当前共 %d 个（新增/覆盖: %s）",
                len(extra), len(base), ", ".join(list(extra)[:20]) or "无")


def describe_params_for_prompt() -> str:
    """
    读取参数表，生成供 LLM 阅读的参数说明段落。
    格式：- "友好名": 描述，最小值 到 最大值
    """
    schema = _load_params()
    lines = []
    for name, spec in schema.items():
        desc = spec.get("desc", name)
        typ = spec.get("type", "int")
        if typ == "string":
            lines.append(f'- "{name}": {desc}（字符串，见描述中的可选值）')
        elif typ == "bool":
            lines.append(f'- "{name}": {desc}（布尔：true 或 false）')
        else:
            lo = spec["min"]
            hi = spec["max"]
            if typ == "float":
                lo_s, hi_s = f"{lo:g}", f"{hi:g}"
            else:
                lo_s, hi_s = str(int(lo)), str(int(hi))
            lines.append(f'- "{name}": {desc}，{lo_s} 到 {hi_s}')
    return "\n".join(lines)


# ----------------------------- 自定义参数 -----------------------------
def validate_custom_params(data: dict) -> tuple:
    """
    校验外部导入的参数定义。
    返回 (合法参数表, 错误信息列表)。错误不影响合法部分的导入。
    """
    required = {"section", "key", "type"}
    valid = {}
    errors = []

    if not isinstance(data, dict):
        logger.error("自定义参数校验失败：顶层必须是 JSON 对象（实际 %s）",
                     type(data).__name__)
        return {}, ["顶层必须是 JSON 对象"]

    for name, raw in data.items():
        if not isinstance(raw, dict):
            errors.append(f"{name}: 不是对象")
            continue
        missing = required - set(raw.keys())
        if missing:
            errors.append(f"{name}: 缺少字段 {', '.join(sorted(missing))}")
            continue
        typ = raw["type"]
        if typ not in ("int", "float", "string", "bool"):
            errors.append(f"{name}: type 只能是 int / float / string / bool")
            continue

        spec = dict(raw)
        if typ in ("int", "float"):
            if "min" not in spec or "max" not in spec:
                errors.append(f"{name}: 数值类型必须提供 min/max")
                continue
            try:
                float(spec["min"])
                float(spec["max"])
            except (TypeError, ValueError):
                errors.append(f"{name}: min/max 必须是数值")
                continue
        else:
            spec.setdefault("min", 0)
            spec.setdefault("max", 0)

        spec.setdefault("enable", False)
        spec.setdefault("desc", name)
        valid[name] = spec

    logger.info("自定义参数校验完成：合法 %d 个，跳过 %d 个",
                len(valid), len(errors))
    for err in errors:
        logger.warning("自定义参数被跳过: %s", err)
    return valid, errors


def load_custom_params(path) -> tuple:
    """
    从指定 JSON 文件加载并合并到当前参数表。
    返回 (成功加载数量, 错误信息列表)。
    """
    logger.info("开始导入自定义参数: %s", path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("读取自定义参数文件失败 %s: %s", path, e)
        return 0, [f"读取失败: {e}"]

    valid, errors = validate_custom_params(data)
    if valid:
        merge_params(valid)
    logger.info("自定义参数导入结束 %s：成功 %d 个，错误 %d 个",
                path, len(valid), len(errors))
    return len(valid), errors


def save_custom_params(path=None) -> None:
    """把当前自定义参数（相对内置 schema 多出来的部分）写盘。"""
    if path is None:
        path = CUSTOM_PARAMS_PATH
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        builtin_names = set(json.load(f).keys())
    current = _load_params()
    custom = {k: v for k, v in current.items() if k not in builtin_names}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(custom, f, ensure_ascii=False, indent=2)
    logger.info("已保存自定义参数 %d 个到 %s", len(custom), path)


def autoload_custom_params() -> tuple:
    """启动时自动加载上次保存的自定义参数（若存在）。"""
    try:
        if CUSTOM_PARAMS_PATH.exists():
            count, errors = load_custom_params(CUSTOM_PARAMS_PATH)
            logger.info("自动加载自定义参数 %s：%d 个（错误 %d 个）",
                        CUSTOM_PARAMS_PATH, count, len(errors))
            return count, errors
    except Exception as e:
        logger.error("自动加载自定义参数失败 %s: %s", CUSTOM_PARAMS_PATH, e)
        return 0, [f"自动加载失败: {e}"]
    logger.info("没有已保存的自定义参数文件: %s", CUSTOM_PARAMS_PATH)
    return 0, []


def clear_custom_params() -> None:
    """清空所有自定义参数，回到内置 schema。"""
    reload_params()
    if CUSTOM_PARAMS_PATH.exists():
        try:
            CUSTOM_PARAMS_PATH.unlink()
            logger.info("已删除自定义参数文件 %s", CUSTOM_PARAMS_PATH)
        except OSError as e:
            logger.warning("删除自定义参数文件失败 %s: %s", CUSTOM_PARAMS_PATH, e)
    else:
        logger.info("自定义参数文件不存在，无需删除: %s", CUSTOM_PARAMS_PATH)


# ----------------------------- 参数护栏 / 强度缩放 -----------------------------
# 这些参数开太猛，画面必然发灰（RawTherapee 会压平动态范围）
_GUARD_COMPR_LIMITS = {
    "shadow_compr": 55,      # Exposure 分区：阴影压缩
    "highlight_compr": 55,   # Exposure 分区：高光压缩
    "shadows": 55,           # Shadows & Highlights：阴影提亮
    "highlights": 55,        # Shadows & Highlights：高光恢复
    "sh_shcompr": 50,        # Shadows & Highlights：SHCompr
    "sh_hlcompr": 50,        # Shadows & Highlights：HLCompr
}

_GUARD_TRIGGER = 30          # 超过此值即视为"压缩较猛"
_GUARD_MAX_COMP = 25         # 自动补偿时 Contrast 的补偿上限


def _as_float(d: dict, k: str) -> float:
    v = d.get(k)
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def apply_guardrails(params: dict) -> dict:
    """
    对 LLM 返回的参数做保守化处理，防止常见的"发灰 / 发闷 / 发蒙"。

    规则：
    1) 钳位：把阴影/高光压缩类参数限制在合理上限内；
    2) 联动：若压缩较猛、又没给出正向对比度，自动补一点 Contrast。
       如果模型明确给了负对比度（< -5），尊重其意图，不覆盖。
    """
    result = dict(params)

    # ---- 1. 上限钳位 ----
    for key, cap in _GUARD_COMPR_LIMITS.items():
        if key not in result:
            continue
        if _as_float(result, key) > cap:
            logger.info("护栏钳位: %s %s -> %s（上限 %s）",
                        key, result[key], cap, cap)
            result[key] = cap

    # ---- 2. 联动补偿 ----
    shadow_aggr = max(
        _as_float(result, "shadow_compr"),
        _as_float(result, "shadows"),
        _as_float(result, "sh_shcompr"),
    )
    highlight_aggr = max(
        _as_float(result, "highlight_compr"),
        _as_float(result, "highlights"),
        _as_float(result, "sh_hlcompr"),
    )
    worst = max(shadow_aggr, highlight_aggr)
    cur_contrast = _as_float(result, "contrast")

    if worst > _GUARD_TRIGGER and -5 <= cur_contrast <= 5:
        # 压缩越多补得越多，上限 25
        comp = min(_GUARD_MAX_COMP, int((worst - _GUARD_TRIGGER) * 0.5) + 5)
        result["contrast"] = comp
        logger.info("护栏联动补偿: 压缩强度 %.1f -> 自动添加 Contrast=%d",
                    worst, comp)
    else:
        logger.info("护栏检查通过（压缩强度 %.1f，Contrast %.1f）",
                    worst, cur_contrast)

    return result


def scale_strength(params: dict, strength: float) -> dict:
    """
    按强度 0.0~1.0 把数值参数向"中性值"插值。
    用于 UI 的"AI 强度"滑块：strength=0 等同原图，strength=1 保持 LLM 原值。

    - int / float 类型：插值到 neutral（默认 0；tint 的中性值是 1.0）
    - string / bool 类型：原样保留（它们通常表示模式切换，没有可插值的中间态）
    """
    if strength >= 1.0:
        logger.info("AI 强度 %.2f：保持 LLM 原始参数（%d 个键）",
                    strength, len(params))
        return dict(params)
    if strength <= 0.0:
        logger.info("AI 强度 %.2f：忽略全部 LLM 参数，等同原图", strength)
        return {}

    schema = _load_params()
    result = {}
    for k, v in params.items():
        spec = schema.get(k, {})
        typ = spec.get("type", "int")
        if typ not in ("int", "float"):
            result[k] = v
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            result[k] = v
            continue
        neutral = 1.0 if k == "tint" else 0.0
        scaled = neutral + (fv - neutral) * strength
        result[k] = int(round(scaled)) if typ == "int" else round(scaled, 4)
    logger.info("AI 强度 %.2f：已把 %d 个数值参数向中性值缩放",
                strength, len(params))
    return result


# ----------------------------- pp3 生成 -----------------------------
SECTION_DEFAULTS = {
    "White Balance": [("Setting", "Custom")],
    "Sharpening":    [("Method", "rld"), ("DeconvIterations", 40)],
}


def _coerce_bool_to_rt(value) -> str:
    """把各种输入转成 RawTherapee 期望的 'true' / 'false' 字符串。"""
    if isinstance(value, str):
        v = value.strip().lower()
        is_true = v in ("true", "1", "yes", "on")
    else:
        is_true = bool(value)
    return "true" if is_true else "false"


def generate_pp3(params: dict, output_path: str,
                 output_profile: str = "RT_sRGB",
                 strength: float = 1.0) -> dict:
    """
    根据参数字典生成 RawTherapee 的 .pp3 配置文件。

    内部流程：
      1) 按 strength 向中性值插值（"AI 强度"滑块）
      2) 应用 apply_guardrails 防发灰
      3) 只识别参数表中定义的键，数值钳位到合法范围

    返回实际写入的参数（可能因缩放/护栏而与入参不同），便于日志展示。
    """
    # 1) 强度缩放 + 2) 防发灰护栏
    params = scale_strength(params, strength)
    params = apply_guardrails(params)

    schema = _load_params()
    sections = {}
    enabled = set()
    ignored = []

    for name, value in params.items():
        spec = schema.get(name)
        if not spec:
            ignored.append(name)
            continue
        section = spec["section"]
        key = spec["key"]
        typ = spec.get("type", "int")

        if typ == "string":
            value = str(value)
        elif typ == "bool":
            value = _coerce_bool_to_rt(value)
        else:
            lo = spec["min"]
            hi = spec["max"]
            conv = float if typ == "float" else int
            try:
                clamped = conv(max(lo, min(hi, value)))
            except (TypeError, ValueError):
                logger.warning("参数 %s 的值无法转换，已忽略: %r", name, value)
                continue
            if clamped != value and isinstance(value, (int, float)):
                logger.info("参数 %s 越界，已钳位: %s -> %s（%s~%s）",
                            name, value, clamped, lo, hi)
            value = clamped

        sections.setdefault(section, []).append((key, value))
        if spec.get("enable", False):
            enabled.add(section)

    if ignored:
        logger.info("忽略 %d 个参数表中未定义的键: %s",
                    len(ignored), ", ".join(map(str, ignored[:20])))

    sections.setdefault("Output", []).append(("OutputProfile", output_profile))

    lines = ["[Version]", "AppVersion=5.9", "Version=347", ""]
    for section, kvs in sections.items():
        lines.append(f"[{section}]")
        for key, val in SECTION_DEFAULTS.get(section, []):
            lines.append(f"{key}={val}")
        if section in enabled:
            lines.append("Enabled=true")
        for key, val in kvs:
            lines.append(f"{key}={val}")
        lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("已生成 .pp3: %s（%d 个段落，启用段落 %s，输出色彩空间 %s）",
                output_path, len(sections),
                "、".join(sorted(enabled)) or "无", output_profile)
    return params


def _manual_rt_cli_path() -> str:
    """读取用户在设置页手动指定的 rawtherapee-cli 路径（可能为空）。"""
    try:
        import settings
        return settings.get_rawtherapee_cli_path()
    except Exception as e:  # 配置读取失败不应影响 PATH 查找
        logger.warning("读取手动指定的 rawtherapee-cli 路径失败: %s", e)
        return ""


def _strip_quotes(value: str) -> str:
    """去掉首尾空白和成对引号（从 shell 复制路径时常常带引号）。"""
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def _msys_to_windows_path(path: str) -> str:
    """在 Windows 上把 Git Bash / MSYS / Cygwin 风格路径转成 Windows 路径。"""
    if os.name != "nt" or not path:
        return path
    match = re.match(r"^/(?:cygdrive/)?([A-Za-z])(?:/(.*))?$", path)
    if not match:
        return path
    drive = match.group(1).upper()
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


def normalize_rt_cli_path(raw: str) -> str:
    """
    规范化用户输入的 rawtherapee-cli 路径。

    兼容：
    - 首尾空白、成对引号（从 bash / PowerShell 复制路径时常见）
    - ~ 与环境变量（$HOME、${HOME}、%ProgramFiles% 等）
    - Windows 上 Git Bash / MSYS / Cygwin 的 /c/... 路径
    """
    path = _strip_quotes(raw)
    if not path:
        return ""
    path = _msys_to_windows_path(path)
    path = os.path.expanduser(path)
    path = os.path.expandvars(path)
    if os.name == "nt":
        # 统一成 Windows 分隔符，避免 ~ 展开后出现 C:\Users\me/bin/... 这种混合路径
        path = path.replace("/", "\\")
    return path


def rt_cli_file_candidates(path: str):
    """把用户指定的文件或目录展开为可能的 rawtherapee-cli 可执行文件。"""
    path = normalize_rt_cli_path(path)
    if not path:
        return []
    if not os.path.isdir(path):
        return [path]
    candidates = []
    # macOS 的 .app 包：可执行文件在 Contents/MacOS 下
    if path.rstrip("/\\").endswith(".app"):
        candidates.append(os.path.join(
            path, "Contents", "MacOS", "rawtherapee-cli"))
    for name in RT_CLI_NAMES:
        candidates.append(os.path.join(path, name))
    return candidates


def is_rt_cli_executable(path: str) -> bool:
    """路径是否指向可用的 rawtherapee-cli（POSIX 上检查可执行权限）。"""
    if not path or not os.path.isfile(path):
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


def _resolve_rt_cli_candidate(path: str):
    """把手动路径 / 命令名解析成实际可执行文件；找不到返回 None。"""
    manual = normalize_rt_cli_path(path)
    if not manual:
        return None
    for candidate in rt_cli_file_candidates(manual):
        if is_rt_cli_executable(candidate):
            return candidate
    # 兼容 bash 习惯：只输入命令名（如 rawtherapee-cli）时按 PATH 查找
    if os.sep not in manual and "/" not in manual:
        return shutil.which(manual)
    return None


def get_rt_cli_version(path: str = "") -> str:
    """
    读取 rawtherapee-cli 的版本号，供设置页区分 / 命名多个 RT 版本。

    path 为空时使用设置页当前选中的版本（找不到再用系统 PATH）。
    返回 "5.9" 这类版本号；读取失败返回空串，不影响正常处理流程。
    """
    executable = (_resolve_rt_cli_candidate(path) if path
                  else find_rt_cli())
    if not executable:
        logger.warning("读取 rawtherapee-cli 版本失败：未找到可执行文件")
        return ""
    try:
        proc = subprocess.run([executable, "--version"],
                              capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("读取 rawtherapee-cli（%s）版本失败: %s",
                       executable, e)
        return ""
    output = "\n".join(part for part in (proc.stdout, proc.stderr)
                       if part).strip()
    if not output:
        logger.warning("rawtherapee-cli（%s）未输出版本信息", executable)
        return ""
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", output)
    version = match.group(1) if match else output.splitlines()[0].strip()[:40]
    logger.info("rawtherapee-cli（%s）版本: %s", executable, version)
    return version


def find_rt_cli():
    """
    查找 rawtherapee-cli，返回可执行文件路径或 None。

    优先使用设置页手动指定的路径；该路径无效时回退到系统 PATH 查找。
    手动路径会先展开 ~ / 环境变量，并兼容 Windows 下的 bash 风格路径。
    """
    manual = normalize_rt_cli_path(_manual_rt_cli_path())
    if manual:
        for candidate in rt_cli_file_candidates(manual):
            if is_rt_cli_executable(candidate):
                logger.info("使用手动指定的 rawtherapee-cli: %s", candidate)
                return candidate
        logger.warning("手动指定的 rawtherapee-cli 路径不可用: %s，"
                       "回退到 PATH 查找", manual)
    found = shutil.which("rawtherapee-cli")
    logger.info("在 PATH 中查找 rawtherapee-cli: %s", found or "未找到")
    return found


def resolve_rt_cli() -> str:
    """返回实际要调用的可执行文件名 / 路径（找不到时回退为裸命令名）。"""
    return find_rt_cli() or "rawtherapee-cli"


def run_rawtherapee(input_raw: str, pp3_file: str, output_dir: str,
                    output_format: str = "jpg",
                    jpeg_quality: int = 92,
                    bit_depth: str = "16",
                    rt_cli: str = ""):
    """
    调用 rawtherapee-cli 处理单个 RAW 文件。
    output_format: "jpg" / "tiff" / "png"
    bit_depth: "8" / "16" / "16f" / "32"，仅 tiff/png 有效
    rt_cli: 可选的 rawtherapee-cli 路径；为空时按设置 / PATH 自动查找
    """
    os.makedirs(output_dir, exist_ok=True)

    cmd = [rt_cli or resolve_rt_cli(), "-o", output_dir,
           "-p", pp3_file, "-c"]

    if output_format == "jpg":
        cmd.append(f"-j{jpeg_quality}")
    elif output_format == "tiff":
        cmd.append("-tz")
        cmd.append(f"-b{bit_depth}")
    elif output_format == "png":
        cmd.append("-n")
        cmd.append(f"-b{bit_depth}")

    cmd.append(input_raw)

    logger.info("调用 rawtherapee-cli: %s（格式 %s，位深 %s，质量 %s）",
                " ".join(cmd), output_format, bit_depth, jpeg_quality)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        # FileNotFoundError / PermissionError（Linux/macOS 缺少执行权限）等
        logger.error("无法执行 rawtherapee-cli（%s），无法处理 %s: %s",
                     cmd[0], input_raw, e)
        raise RuntimeError(
            f"无法执行 rawtherapee-cli（{cmd[0]}）：{e}") from e
    logger.info("rawtherapee-cli 返回码 %d，stdout %d 字符，stderr %d 字符",
                proc.returncode, len(proc.stdout or ""), len(proc.stderr or ""))
    if proc.returncode != 0:
        logger.error("rawtherapee-cli 执行失败（%s）: %s",
                     input_raw, (proc.stderr or "").strip())
        raise RuntimeError(f"rawtherapee-cli 执行失败:\n{proc.stderr.strip()}")
    if proc.stderr and proc.stderr.strip():
        logger.info("rawtherapee-cli 警告信息: %s", proc.stderr.strip()[:500])


def check_rt_cli():
    """检查 rawtherapee-cli 是否可用（手动指定优先，其次 PATH）"""
    found = find_rt_cli()
    logger.info("检查 rawtherapee-cli: %s", found or "未找到")
    return found is not None
