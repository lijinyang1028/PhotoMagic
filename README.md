# RAW 照片 AI 处理助手

利用 LLM 视觉模型分析 RAW 照片内容，自动生成后期参数，并通过 [RawTherapee](https://www.rawtherapee.com/) 批量输出处理后的高质量 JPEG。

## 功能

* 支持几乎所有主流 RAW 格式（CR2, NEF, ARW, DNG, ORF, RAF, RW2, PEF 等）
* 图形界面基于 PyQt6，操作直观
* 兼容任何提供 OpenAI 标准 API 的 LLM 服务（支持视觉输入，如 GPT-4o）
* 自动将 LLM 返回的 JSON 参数转换为 RawTherapee 配置并批量处理
* 一键完成从分析到输出的完整工作流

## 安装依赖

1. 安装 [RawTherapee 5.9+](https://www.rawtherapee.com/downloads)，确保 `rawtherapee-cli` 可在命令行直接调用。
2. Python 3.8 及以上版本。
3. 克隆项目并安装 Python 依赖：

```bash
pip install -r requirements.txt
```

使用方法

1. 运行 python main.py
2. 在“选择 RAW 照片”区域添加需要处理的文件
3. 填写 LLM 配置：
· API Base URL：兼容 OpenAI 的端点（默认为 OpenAI 官方）
· API Key：你的密钥
· Model：支持视觉能力的模型名称（如 gpt-4o）
4. 提示词可按需调整（System Prompt 已预设 JSON 格式要求）
5. 设置输出目录
6. 点击“开始处理”，日志区域将显示处理进度

工作流程

1. 对每张 RAW 文件，程序提取内嵌 JPEG 缩略图（若无则快速解码低分辨率图像）
2. 将缩略图以 Base64 编码通过视觉 API 发送给 LLM
3. LLM 返回包含后期参数的 JSON
4. 程序将 JSON 转换为 RawTherapee 的 .pp3 配置文件
5. 调用 rawtherapee-cli 对原始 RAW 文件应用配置文件，输出高质量 JPEG

许可

本项目基于 MIT 许可证开源，详见 LICENSE 文件。

# RAW 照片 AI 处理助手

利用 LLM 视觉模型分析 RAW 照片内容，自动生成后期参数，并通过 [RawTherapee](https://www.rawtherapee.com/) 批量输出处理后的高质量 JPEG。

## 功能

* 支持几乎所有主流 RAW 格式（CR2, NEF, ARW, DNG, ORF, RAF, RW2, PEF 等）
* 图形界面基于 PyQt6，操作直观
* 兼容任何提供 OpenAI 标准 API 的 LLM 服务（支持视觉输入，如 GPT-4o）
* 自动将 LLM 返回的 JSON 参数转换为 RawTherapee 配置并批量处理
* 一键完成从分析到输出的完整工作流

## 安装依赖

1. 安装 [RawTherapee 5.9+](https://www.rawtherapee.com/downloads)，确保 `rawtherapee-cli` 可在命令行直接调用。
2. Python 3.8 及以上版本。
3. 克隆项目并安装 Python 依赖：

```bash
pip install -r requirements.txt
```

使用方法

1. 运行 python main.py
2. 在“选择 RAW 照片”区域添加需要处理的文件
3. 填写 LLM 配置：
· API Base URL：兼容 OpenAI 的端点（默认为 OpenAI 官方）
· API Key：你的密钥
· Model：支持视觉能力的模型名称（如 gpt-4o）
4. 提示词可按需调整（System Prompt 已预设 JSON 格式要求）
5. 设置输出目录
6. 点击“开始处理”，日志区域将显示处理进度

工作流程

1. 对每张 RAW 文件，程序提取内嵌 JPEG 缩略图（若无则快速解码低分辨率图像）
2. 将缩略图以 Base64 编码通过视觉 API 发送给 LLM
3. LLM 返回包含后期参数的 JSON
4. 程序将 JSON 转换为 RawTherapee 的 .pp3 配置文件
5. 调用 rawtherapee-cli 对原始 RAW 文件应用配置文件，输出高质量 JPEG

许可

本项目基于 MIT 许可证开源，详见 LICENSE 文件。

