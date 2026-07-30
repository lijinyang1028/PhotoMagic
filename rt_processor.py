"""
RawTherapee 处理模块
将 JSON 参数转换为 .pp3 文件，并通过命令行调用 rawtherapee-cli
"""
import os
import subprocess
import tempfile
import shutil

def generate_pp3(params: dict, output_path: str):
    """
    根据参数字典生成 RawTherapee 的 .pp3 配置文件
    参数键名需与 RawTherapee 内部命名一致（可简化）
    示例: {"Exposure": 1.2, "Contrast": 30, "Saturation": 10,
          "WhiteBalance": {"Temperature": 5500, "Tint": 1.0}}
    """
    lines = ["[Version]", "AppVersion=5.9", "Version=347"]
    lines.append("")  # 空行分隔

    # 曝光
    if "Exposure" in params:
        lines.append(f"[Exposure]")
        lines.append(f"Exposure={params['Exposure']}")
        lines.append("")

    # 对比度 / 饱和度 (属于 Vibrance 或 Lab Adjustments)
    if "Contrast" in params or "Saturation" in params:
        lines.append("[Lab Adjustments]")
        if "Contrast" in params:
            lines.append(f"Contrast={params['Contrast']}")
        if "Saturation" in params:
            lines.append(f"Saturation={params['Saturation']}")
        lines.append("")

    # 白平衡
    if "WhiteBalance" in params:
        wb = params["WhiteBalance"]
        lines.append("[White Balance]")
        lines.append("Setting=Custom")
        if "Temperature" in wb:
            lines.append(f"Temperature={wb['Temperature']}")
        if "Tint" in wb:
            lines.append(f"Green={wb['Tint']}")  # 注意 RawTherapee 使用 Green
        lines.append("")

    # 高光/阴影 (通过 Shadow/Highlight 工具)
    if "Highlights" in params or "Shadows" in params:
        lines.append("[Shadows/Highlights]")
        if "Highlights" in params:
            lines.append(f"Highlights={params['Highlights']}")
        if "Shadows" in params:
            lines.append(f"Shadows={params['Shadows']}")
        lines.append("")

    # 更多参数可按需扩展...

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
    subprocess.run(cmd, check=True, capture_output=True, text=True)

def check_rt_cli():
    """检查 rawtherapee-cli 是否可用"""
    return shutil.which("rawtherapee-cli") is not None