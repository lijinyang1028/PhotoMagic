"""
RawTherapee 处理模块
将 JSON 参数转换为 .pp3 文件，并通过命令行调用 rawtherapee-cli
"""
import os
import json
import subprocess
import shutil
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("params_schema.json")
CUSTOM_PARAMS_PATH = Path.home() / ".photomagic_custom_params.json"
_PARAMS_CACHE = None


# ----------------------------- 参数表加载 -----------------------------
def _load_params():
    """加载参数表，缓存避免重复读盘"""
    global _PARAMS_CACHE
    if _PARAMS_CACHE is None:
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
            _PARAMS_CACHE = json.load(f)
    return _PARAMS_CACHE


def get_params():
    """对外暴露的参数表（友好名 -> 定义）"""
    return _load_params()


def get_builtin_param_names() -> set:
    """返回内置 schema 中定义的所有参数名。"""
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f).keys())


def reload_params():
    """清缓存，下次调用重新读 JSON。"""
    global _PARAMS_CACHE
    _PARAMS_CACHE = None


def merge_params(extra: dict):
    """合并外部 JSON 参数定义，用于用户自定义扩展。"""
    base = dict(_load_params())
    base.update(extra)
    _PARAMS_CACHE = base


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

    return valid, errors


def load_custom_params(path) -> tuple:
    """
    从指定 JSON 文件加载并合并到当前参数表。
    返回 (成功加载数量, 错误信息列表)。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return 0, [f"读取失败: {e}"]

    valid, errors = validate_custom_params(data)
    if valid:
        merge_params(valid)
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


def autoload_custom_params() -> tuple:
    """启动时自动加载上次保存的自定义参数（若存在）。"""
    try:
        if CUSTOM_PARAMS_PATH.exists():
            return load_custom_params(CUSTOM_PARAMS_PATH)
    except Exception as e:
        return 0, [f"自动加载失败: {e}"]
    return 0, []


def clear_custom_params() -> None:
    """清空所有自定义参数，回到内置 schema。"""
    reload_params()
    if CUSTOM_PARAMS_PATH.exists():
        try:
            CUSTOM_PARAMS_PATH.unlink()
        except OSError:
            pass


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


def generate_pp3(params: dict, output_path: str, output_profile: str = "RT_sRGB"):
    """
    根据参数字典生成 RawTherapee 的 .pp3 配置文件。
    只识别参数表中定义的键，其余忽略；数值会钳位到合法范围。
    string / bool 类型分别按字符串直写 / true|false 处理。
    """
    schema = _load_params()
    sections = {}
    enabled = set()

    for name, value in params.items():
        spec = schema.get(name)
        if not spec:
            continue
        section = spec["section"]
        key = spec["key"]
        typ = spec.get("type", "int")

        if typ == "string":
            # 字符串参数（如 "Lab" / 曲线控制点 "0;0;1;1;"）直接写入
            value = str(value)
        elif typ == "bool":
            # RawTherapee 的 pp3 使用 true / false
            value = _coerce_bool_to_rt(value)
        else:
            lo = spec["min"]
            hi = spec["max"]
            conv = float if typ == "float" else int
            try:
                value = conv(max(lo, min(hi, value)))
            except (TypeError, ValueError):
                continue

        sections.setdefault(section, []).append((key, value))
        if spec.get("enable", False):
            enabled.add(section)

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


def run_rawtherapee(input_raw: str, pp3_file: str, output_dir: str,
                    output_format: str = "jpg",
                    jpeg_quality: int = 92,
                    bit_depth: str = "16"):
    """
    调用 rawtherapee-cli 处理单个 RAW 文件。
    output_format: "jpg" / "tiff" / "png"
    bit_depth: "8" / "16" / "16f" / "32"，仅 tiff/png 有效
    """
    os.makedirs(output_dir, exist_ok=True)

    cmd = ["rawtherapee-cli", "-o", output_dir, "-p", pp3_file, "-c"]

    if output_format == "jpg":
        cmd.append(f"-j{jpeg_quality}")
    elif output_format == "tiff":
        cmd.append("-tz")
        cmd.append(f"-b{bit_depth}")
    elif output_format == "png":
        cmd.append("-n")
        cmd.append(f"-b{bit_depth}")

    cmd.append(input_raw)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rawtherapee-cli 执行失败:\n{proc.stderr.strip()}")


def check_rt_cli():
    """检查 rawtherapee-cli 是否可用"""
    return shutil.which("rawtherapee-cli") is not None