# PhotoMagic — RAW 照片 AI 处理助手

利用 LLM 视觉模型分析 RAW 照片，自动生成后期参数，并通过 [RawTherapee](https://www.rawtherapee.com/) 批量输出高质量 JPEG。附带 AI 照片评价、多维度雷达图与前后对比功能。

当前版本：**v0.3.2**

## 功能

### 批量处理
* 支持几乎所有主流 RAW 格式（CR2、NEF、ARW、DNG、ORF、RAF、RW2、PEF 等）
* 兼容任何提供 OpenAI 标准 API 的视觉 LLM 服务（GPT-4o、Claude、Gemini 等）
* 自动将 LLM 返回的 JSON 参数转换为 RawTherapee `.pp3` 配置并批量处理
* 多线程并发处理（默认 3 路），UI 全程不阻塞
* 列表项缩略图懒加载，万张量级也不卡
* 多选勾选、全选 / 全不选，支持只处理部分照片
* 一键重试失败项
* 开始 / 停止控制，可随时中断批处理
* **组图统一风格**：把多张照片设为一组，一次性提交给 LLM，得到统一风格 + 逐张微调

### 组图（统一风格）
* 勾选若干照片后点「设为一组」即可成组，列表项上会显示组名徽标
* 勾选「统一风格：同组照片一次提交给 LLM」后，同组照片合并为**一次**请求
* LLM 返回 `{"style": {...共用参数...}, "photos": [{...逐张微调...}, ...]}`
* 程序把 `style` 与每张的微调合并后，分别生成 `.pp3` 并调用 RawTherapee
* 未分组的照片仍按单张处理；只处理已勾选的组内成员

### 照片评价
* 从技术、构图、表达、完成度四个维度对照片打分
* 雷达图可视化
* 提交时可附加一句文字描述，供 AI 参考
* 历史记录本地保存，可随时回看

### 前后对比
* 处理页右侧预览支持拖动中缝，左半边看原图、右半边看处理后
* 处理完成的瞬间，当前选中项自动刷新对比

### 界面
* 基于 PyQt6，WinUI3 风格侧边栏与滚动条
* 跟随系统深浅色模式自动切换
* 侧边栏可折叠

## 安装依赖

1. 安装 [RawTherapee 5.9+](https://www.rawtherapee.com/downloads)，确保 `rawtherapee-cli` 已加入系统 PATH（命令行输入 `rawtherapee-cli --version` 能输出版本号）。
2. Python **3.10 及以上**（需要 PyQt6 6.5+ 提供的 `colorSchemeChanged` 信号）。
3. 克隆项目并安装 Python 依赖：

```bash
pip install -r requirements.txt
```

## 使用方法

### 批量处理

1. 运行 `python main.py`
2. 在「选择 RAW 照片」区域添加文件
3. 填写 LLM 配置：
   - **API Base URL**：兼容 OpenAI 的端点（默认 OpenAI 官方）
   - **API Key**：你的密钥
   - **Model**：支持视觉能力的模型名称（如 `gpt-4o`）
4. 按需调整 System Prompt（已预设 JSON 格式要求）
5. 设置输出目录
6. 勾选要处理的照片，点击 ⬆ 开始处理
7. 处理页右侧拖动中缝即可对比处理前后
8. 现已支持输出格式/质量自定义
9. 可根据JSON轻松添加修改项目，原生支持70余种修改

### 组图统一风格

1. 勾选要成组的照片（可多选），点「设为一组」并填写组名，列表项上会出现组名徽标
2. 勾选「统一风格：同组照片一次提交给 LLM」
3. 勾选要处理的照片后开始处理：同组照片合成一次请求，其余照片仍按单张处理
4. 组名徽标、组图批次提示会显示在「选择 RAW 照片」区域；超过 8 张会提示建议拆组
5. 想拆组时勾选这些照片后点「取消分组」

组图模式下 LLM 需按下面的结构返回（程序已自动追加到 System Prompt）：

```json
{
  "style":  { "contrast": 18, "saturation": 8 },
  "photos": [ { "exposure": 0.3 }, {} ]
}
```

`style` 是整套照片共用的风格，`photos[i]` 是第 `i+1` 张照片的差异化微调（可为
`{}`）。程序会把两者合并后写入该张照片的 `.pp3`，因此仍受「AI 强度」与防发灰
护栏的约束。解析层对常见变体（`photos` 写成字典、按文件名返回、直接返回数组等）
做了兼容，详见 `grouping.py`。

> 提示：一次请求包含的图片越多，token 成本与超时风险越高。建议每组 8 张以内。

### 照片评价

1. 点击左侧边栏「照片评价」
2. 选择一张图片，可选填一句描述
3. 点击「提交评价」，等待 AI 返回评分与建议
4. 评分会以雷达图呈现，历史记录自动保存

## 工作流程

### 批量处理

1. 对每张 RAW 文件，程序提取内嵌 JPEG 缩略图（若无则快速解码低分辨率图像）
2. 将缩略图以 Base64 编码通过视觉 API 发送给 LLM
3. LLM 返回包含后期参数的 JSON
4. 程序将 JSON 转换为 RawTherapee 的 `.pp3` 配置文件
5. 调用 `rawtherapee-cli` 对原始 RAW 文件应用配置，输出高质量 JPEG

组图模式下第 2~4 步按组进行：整组图片一次编码发送 -> 返回统一风格与逐张微调 ->
逐张合并后各自生成 `.pp3`。

### 参数表

后期参数定义在 `params_schema.json` 中，包含每个参数的 RT 段落、键名、类型与取值范围。可通过 `rt_processor.merge_params()` 导入外部 JSON 扩展，或修改该文件后调用 `reload_params()` 生效。

**注意**：schema 中的 min/max 是「AI 建议的有意义区间」，不等于 RawTherapee 本身能接受的极限范围。放宽范围前请确认 LLM 给出的建议仍在摄影意义上合理。

## 项目结构

```
raw-llm-rt/
├── main.py              # 主窗口与导航
├── processing.py        # 批处理页面（含组图任务）
├── grouping.py          # 组图纯逻辑：分组、批次切分、组图返回解析（无 Qt 依赖）
├── image_review.py      # 照片评价页面（雷达图 + 历史）
├── llm_handler.py       # LLM 调用（单图 / 多图）与 JSON 鲁棒解析
├── rt_processor.py      # .pp3 生成与 rawtherapee-cli 调用
├── params_schema.json   # 后期参数定义表
├── test/
│   └── test_photomagic.py
└── requirements.txt
```

## 测试

```bash
# 安装测试依赖（如果还没装）
pip install pytest

# 在项目根目录运行
pytest
```

## 常见问题

**启动时提示未检测到 `rawtherapee-cli`**
说明 RawTherapee 未安装或未加入 PATH。Windows 下安装完成后需要重启终端；或手动把 RawTherapee 安装目录加入系统环境变量 `Path`。

**处理时 LLM 返回的不是有效 JSON**
程序内置了正则兜底解析（处理 ` ```json ` 围栏、前后夹带说明文字等情况）。如果仍然报错，多为模型本身不支持视觉输入或网络传输被截断，可换 `gpt-4o` 或等价模型测试。

**照片评价与批处理共用 API 配置**
两个页面共享同一份 API Base / Key / Model，在批处理页填好即可，无需在评价页重复填写。

## 许可

本项目基于 MIT 许可证开源，详见 LICENSE 文件。

