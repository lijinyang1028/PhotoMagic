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
_PARAMS_CACHE = None


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


def reload_params():
    """清缓存，下次调用重新读 JSON。改了 schema 后调一下即可生效。"""
    global _PARAMS_CACHE
    _PARAMS_CACHE = None


def merge_params(extra: dict):
    """
    合并外部 JSON 参数定义，用于用户自定义扩展。
    extra 的结构与 params_schema.json 一致。
    """
    base = dict(_load_params())
    base.update(extra)
    _PARAMS_CACHE = base


# 某些段落除参数键外，还需要固定的伴随键才能按预期生效
SECTION_DEFAULTS = {
    "White Balance": [("Setting", "Custom")],
    "Sharpening":    [("Method", "rld"), ("DeconvIterations", 40)],
}


def generate_pp3(params: dict, output_path: str):
    """
    根据参数字典生成 RawTherapee 的 .pp3 配置文件。
    只识别参数表中定义的键，其余忽略；数值会钳位到合法范围。
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
        typ = float if spec["type"] == "float" else int
        lo, hi = spec["min"], spec["max"]
        try:
            value = typ(max(lo, min(hi, value)))
        except (TypeError, ValueError):
            continue
        sections.setdefault(section, []).append((key, value))
        if spec.get("enable", False):
            enabled.add(section)

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


def run_rawtherapee(input_raw: str, pp3_file: str, output_dir: str, jpeg_quality: int = 92):
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        "rawtherapee-cli",
        "-o", output_dir,
        "-p", pp3_file,
        "-j", str(jpeg_quality),
        "-c",
        "-t",
        input_raw
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rawtherapee-cli 执行失败:\n{proc.stderr.strip()}")


def check_rt_cli():
    return shutil.which("rawtherapee-cli") is not None