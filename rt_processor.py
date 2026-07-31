"""
RawTherapee 处理模块
将 JSON 参数转换为 .pp3 文件，并通过命令行调用 rawtherapee-cli
"""
import os
import subprocess
import shutil

# 参数表：友好名 -> (RT 段落, RT 键, 类型, 最小值, 最大值, 是否需要 Enabled=true)
# 段落名与键名均依据 RawTherapee 源码 rtengine/procparams.cc
PARAMS = {
    "exposure":        ("Exposure",             "Compensation",   float, -3.0,   3.0, False),
    "contrast":        ("Exposure",             "Contrast",       int,  -100,   100, False),
    "saturation":      ("Exposure",             "Saturation",     int,  -100,   100, False),
    "highlight_compr": ("Exposure",             "HighlightCompr", int,     0,   100, False),
    "shadow_compr":    ("Exposure",             "ShadowCompr",    int,     0,   100, False),
    "highlights":      ("Shadows & Highlights", "Highlights",     int,     0,   100, True),
    "shadows":         ("Shadows & Highlights", "Shadows",        int,     0,   100, True),
    "temperature":     ("White Balance",        "Temperature",    int,  2000, 12000, False),
    "tint":            ("White Balance",        "Green",          float, 0.5,   2.0, False),
    "sharpen_amount":  ("Sharpening",           "DeconvAmount",   int,     0,   200, True),
    "vibrance":        ("Vibrance",             "Saturated",      int,     0,   100, True),
    "distortion":      ("Distortion",           "Amount",         float, -1.0,   1.0, False),
}

# 某些段落除参数键外，还需要固定的伴随键才能按预期生效
SECTION_DEFAULTS = {
    "White Balance": [("Setting", "Custom")],   # 自定义白平衡才会采用 Temperature/Green
    "Sharpening":    [("Method", "rld"), ("DeconvIterations", 40)],  # 反卷积锐化需迭代次数
}


def generate_pp3(params: dict, output_path: str):
    """
    根据参数字典生成 RawTherapee 的 .pp3 配置文件。
    只识别 PARAMS 中定义的键，其余忽略；数值会钳位到合法范围。
    """
    sections = {}   # RT 段落名 -> 该段要写入的 (键, 值) 列表
    enabled = set() # 需要 Enabled=true 的段落

    for name, value in params.items():
        spec = PARAMS.get(name)
        if not spec:
            continue  # 未定义的键直接忽略，避免写出无效配置
        section, key, typ, lo, hi, need_enable = spec
        try:
            value = typ(max(lo, min(hi, value)))  # 钳位到 [lo, hi]
        except (TypeError, ValueError):
            continue  # 非法值跳过，不影响其它参数
        sections.setdefault(section, []).append((key, value))
        if need_enable:
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
    """
    调用 rawtherapee-cli 处理单个 RAW 文件
    """
    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # rawtherapee-cli 命令 (假设已加入环境变量)
    cmd = [
        "rawtherapee-cli",
        "-o", output_dir,
        "-p", pp3_file,
        "-j", str(jpeg_quality),
        "-c",   # 覆盖已有输出
        "-t",   # 忽略定向标签（避免旋转冲突，可选）
        input_raw
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # 把 RT 的报错信息带出来，否则日志只剩一个退出码，只能干瞪眼
        raise RuntimeError(f"rawtherapee-cli 执行失败:\n{proc.stderr.strip()}")

def check_rt_cli():
    """检查 rawtherapee-cli 是否可用"""
    return shutil.which("rawtherapee-cli") is not None