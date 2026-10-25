"""
LLM 调用模块
支持标准 OpenAI 接口 (视觉模型), 发送图片并获取 JSON 参数
"""
import base64
import json
import logging
import requests
from io import BytesIO
from PIL import Image, ExifTags
import rawpy

try:
    import exifread
    _EXIFREAD_AVAILABLE = True
except ImportError:
    exifread = None
    _EXIFREAD_AVAILABLE = False


logger = logging.getLogger(__name__)

if not _EXIFREAD_AVAILABLE:
    logger.warning(
        "未安装 exifread，RAW 的 EXIF 可能不完整；"
        "如需更完整的 RAW EXIF 支持，可运行 pip install exifread")


# ----------------------------- 原生思考（CoT） -----------------------------
# DeepSeek 系列模型支持用 thinking 字段控制「是否先输出推理过程」：
#   {"thinking": {"type": "enabled"}}  /  {"thinking": {"type": "disabled"}}
# 其他厂商（OpenAI 等）的接口不一定接受这个字段，因此只对识别出的模型发送，
# 避免出现 400 Unrecognized request argument。
THINKING_MODEL_PREFIXES = ("deepseek",)


def normalize_model_name(model: str) -> str:
    """取模型名的最后一段，兼容 openrouter / siliconflow 的 provider/model 写法。"""
    name = (model or "").strip().lower()
    return name.rsplit("/", 1)[-1] if "/" in name else name


def supports_native_thinking(model: str) -> bool:
    """当前模型是否支持原生思考开关（目前为 DeepSeek 系模型）。"""
    name = normalize_model_name(model)
    return any(name.startswith(prefix) for prefix in THINKING_MODEL_PREFIXES)


def build_thinking_fields(model: str, thinking_enabled):
    """
    构造控制原生 CoT 的请求字段。

    thinking_enabled:
      - True  : 开启思考（enabled）
      - False : 关闭思考（disabled）
      - None  : 不干预，沿用接口默认行为
    不支持的模型一律返回 {}，保证不会把非标准参数发给别的接口。
    """
    if thinking_enabled is None or not supports_native_thinking(model):
        return {}
    return {"thinking": {"type": "enabled" if thinking_enabled else "disabled"}}


# Exif SubIFD 的 tag id；Pillow 8.2+ 可以用 ExifTags.IFD.Exif 代替，
# 这里用数字常量以兼容更老的 Pillow
_EXIF_SUB_IFD = 0x8769

# 字段映射：显示标签 -> (exifread 字段名候选, PIL 标准名候选)
# exifread 返回的键形如 "Image Make" / "EXIF FNumber"；
# PIL 的 ExifTags.TAGS 返回的是 "Make" / "FNumber" 等标准名。
_EXIF_FIELD_MAP = [
    ("厂商", ("Image Make",), ("Make",)),
    ("相机", ("Image Model",), ("Model",)),
    ("镜头", ("EXIF LensModel", "Image LensModel"), ("LensModel",)),
    ("光圈", ("EXIF FNumber",), ("FNumber",)),
    ("快门", ("EXIF ExposureTime",), ("ExposureTime",)),
    ("ISO", ("EXIF ISOSpeedRatings", "EXIF PhotographicSensitivity"),
            ("ISOSpeedRatings", "PhotographicSensitivity")),
    ("焦距", ("EXIF FocalLength",), ("FocalLength",)),
    ("等效焦距", ("EXIF FocalLengthIn35mmFilm",), ("FocalLengthIn35mmFilm",)),
    ("曝光补偿", ("EXIF ExposureBiasValue",), ("ExposureBiasValue",)),
    ("白平衡", ("EXIF WhiteBalance",), ("WhiteBalance",)),
    ("闪光灯", ("EXIF Flash",), ("Flash",)),
    ("测光模式", ("EXIF MeteringMode",), ("MeteringMode",)),
    ("曝光模式", ("EXIF ExposureProgram",), ("ExposureProgram",)),
    ("拍摄时间", ("EXIF DateTimeOriginal",), ("DateTimeOriginal",)),
]


# ----------------------------- 图片编码 -----------------------------
def load_image_base64_from_raw(raw_path: str, thumb_size: tuple = (512, 512)) -> str:
    """
    从 RAW 文件提取缩略图/转换后转为 base64 JPEG 字符串
    """
    try:
        with rawpy.imread(raw_path) as raw:
            # 尝试提取内嵌缩略图
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                # 内嵌缩略图是 JPEG，用 Pillow 解码后再统一缩放（不做缩放会
                # 把相机内嵌的大尺寸预览原样发给 LLM，浪费 token 且易超时）
                img = Image.open(BytesIO(thumb.data))
                logger.info("已从 %s 提取内嵌 JPEG 缩略图（原始 %d 字节）",
                            raw_path, len(thumb.data))
            else:
                # 其他格式需转换为 RGB 再编码
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb)
                logger.info("已解码 %s 的 RAW 数据（%s），尺寸 %sx%s",
                            raw_path, thumb.format, img.width, img.height)
    except Exception as e:
        # 如果 rawpy 无法处理，尝试用 Pillow 直接打开（可能是普通图片）
        logger.info("rawpy 无法处理 %s（%s），改用 Pillow 直接打开",
                    raw_path, e)
        img = Image.open(raw_path)

    # 统一转 RGB（P 模式 / 灰度 / CMYK 等都要转，否则 JPEG 保存会报错）
    if img.mode != "RGB":
        src_mode = img.mode
        img = img.convert("RGB")
        logger.info("图片模式 %s -> RGB（%s）", src_mode, raw_path)

    # 统一缩放并转为 JPEG base64
    img.thumbnail(thumb_size, Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    data = buf.getvalue()
    logger.info("图片编码完成 %s：%dx%d，JPEG %d 字节，base64 %d 字符，"
                "请求缩略图上限 %s",
                raw_path, img.width, img.height, len(data),
                len(base64.b64encode(data)), thumb_size)
    return base64.b64encode(data).decode("utf-8")


# ----------------------------- EXIF 读取 -----------------------------
def _format_exif_value(value) -> str:
    """把 EXIF 值转成易读字符串。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    if isinstance(value, tuple):
        parts = [_format_exif_value(v) for v in value if v is not None]
        return "/".join(p for p in parts if p)
    # Pillow 的 IFDRational：尽量保留分数 / 小数值
    try:
        from PIL.TiffImagePlugin import IFDRational
        if isinstance(value, IFDRational):
            if value.denominator == 1:
                return str(int(value))
            try:
                fv = float(value)
                # 快门类的小数值尽量还原成 "1/x"
                if 0 < fv < 1:
                    inv = round(1 / fv)
                    if inv > 0 and abs(1 / inv - fv) / fv < 0.01:
                        return f"1/{inv}"
            except Exception:
                pass
            return f"{value.numerator}/{value.denominator}"
    except ImportError:
        pass
    return str(value).strip()


def _read_exif_with_exifread(raw_path: str):
    """用 exifread 读取 EXIF；失败 / 未安装返回 None。"""
    if not _EXIFREAD_AVAILABLE:
        return None
    try:
        with open(raw_path, "rb") as f:
            # details=False 跳过 MakerNotes，strict=False 让解析更宽容
            tags = exifread.process_file(f, details=False, strict=False)
    except Exception as e:
        logger.info("exifread 读取 EXIF 失败 %s: %s", raw_path, e)
        return None
    return tags or None


def _read_exif_with_pil(raw_path: str):
    """
    用 PIL 兜底读取 EXIF（对 CR3 等 exifread 不擅长的格式）。

    策略：
      1) 直接 Image.open(raw_path).getexif()（PIL 能识别的容器，如 DNG）；
      2) 失败时用 rawpy 提取内嵌 JPEG 缩略图，再读缩略图的 EXIF
         —— CR3 / CR2 等相机都会在 RAW 里内嵌带完整 EXIF 的 JPEG 预览，
            这是对 CR3 最稳妥的兜底路径。

    返回 {标准名: 值} dict；完全读取失败时返回 None。
    """
    exif = None

    # 策略 1：直接打开（若 PIL 认出该格式）
    try:
        with Image.open(raw_path) as img:
            exif = img.getexif()
    except Exception:
        exif = None

    # 策略 2：从 RAW 内嵌 JPEG 缩略图读取
    if not exif:
        try:
            with rawpy.imread(raw_path) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(BytesIO(thumb.data))
                    exif = img.getexif()
        except Exception as e:
            logger.info("PIL 兜底读取 EXIF 失败 %s: %s", raw_path, e)
            return None

    if not exif:
        return None

    result = {}
    # 顶层 IFD（Make / Model）
    try:
        for tag_id, value in exif.items():
            name = ExifTags.TAGS.get(tag_id)
            if name and value is not None:
                result[name] = value
    except Exception:
        pass
    # Exif SubIFD（FNumber / ExposureTime / ISO 等）
    try:
        sub_ifd = exif.get_ifd(_EXIF_SUB_IFD)
        for tag_id, value in sub_ifd.items():
            name = ExifTags.TAGS.get(tag_id)
            if name and value is not None:
                result[name] = value
    except Exception:
        pass

    return result or None


def _read_exif_tags(raw_path: str):
    """
    统一读取 EXIF：优先 exifread，空结果时用 PIL 兜底。

    返回 {字段名: 值}；两个来源都读不到返回 None。
    """
    tags = _read_exif_with_exifread(raw_path)
    if tags:
        return tags
    tags = _read_exif_with_pil(raw_path)
    if tags:
        logger.info("使用 PIL 兜底读取 EXIF：%s（%d 个字段）",
                    raw_path, len(tags))
    return tags


def load_exif_summary(raw_path: str) -> str:
    """读取图片 EXIF，返回一行式拍摄参数摘要；不可用或读取失败返回空串。"""
    tags = _read_exif_tags(raw_path)
    if not tags:
        logger.info("图片无可用 EXIF: %s", raw_path)
        return ""

    parts = []
    for label, exifread_names, pil_names in _EXIF_FIELD_MAP:
        value = None
        for name in exifread_names:
            if name in tags:
                value = tags[name]
                break
        if value is None:
            for name in pil_names:
                if name in tags:
                    value = tags[name]
                    break
        if value is None:
            continue
        text = _format_exif_value(value)
        if not text:
            continue
        parts.append(f"{label}={text}")
    return "，".join(parts)


def build_exif_context(image_paths) -> str:
    """
    按图片顺序构建 EXIF 描述段落，追加到 user prompt 末尾。

    - 单张：`拍摄参数：厂商=...，相机=...`
    - 多张：按"第 N 张"编号，与 prompt 中的照片顺序严格对齐
    - 全部缺失 / 全部读取失败时返回空串（调用方保持原 prompt 不变）
    """
    paths = list(image_paths or [])
    if not paths:
        return ""

    lines = []
    for i, p in enumerate(paths, 1):
        summary = load_exif_summary(p)
        if not summary:
            continue
        if len(paths) == 1:
            lines.append(f"拍摄参数：{summary}")
        else:
            lines.append(f"第 {i} 张：{summary}")
    if not lines:
        return ""
    return "【照片拍摄参数（EXIF）】\n" + "\n".join(lines)


# ----------------------------- JSON 提取 -----------------------------
def _extract_first_json_object(text: str):
    """
    从任意文本中提取第一个完整的 JSON 对象（字符串）。

    使用括号计数而非贪婪正则 r"\\{.*\\}"，能正确处理：
      - LLM 在 JSON 前后或之后输出额外文字 / 第二个 JSON 时，只取第一个对象
      - JSON 字符串内部出现的 '{' 或 '}'
      - 反斜杠转义（\\" 与 \\\\）

    返回匹配到的子串；找不到匹配返回 None。
    """
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


class LLMClient:
    """兼容 OpenAI 的 LLM 客户端"""

    def __init__(self, api_base: str, api_key: str, model: str,
                 thinking=None, reasoning_cb=None):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        # None = 不干预（接口默认）；True/False = 显式开启 / 关闭原生思考
        self.thinking = thinking
        # 可选回调：收到原生思考内容（reasoning_content）时调用，
        # 由 UI 决定显示到哪里；回调异常不能影响主流程。
        self.reasoning_cb = reasoning_cb
        logger.info("创建 LLM 客户端：model=%s，api_base=%s，API Key %s，"
                    "原生思考=%s，思考回调=%s",
                    self.model, self.api_base,
                    "已配置" if self.api_key else "未配置（请求将失败）",
                    {None: "不干预", True: "开启",
                     False: "关闭"}.get(self.thinking, self.thinking),
                    "已设置" if self.reasoning_cb else "未设置")

    def request_json(self, system_prompt: str, user_prompt: str,
                     image_path: str, max_tokens=1024,
                     stop_event=None) -> dict:
        """
        将图片（RAW）编码后发送给视觉 LLM，期望返回一个 JSON 对象。

        stop_event: 可选 threading.Event。
                    设置后会在下一个网络数据块到达时中止本次请求
                    （通常 1 秒内生效），抛出 InterruptedError。
        """
        return self.request_json_multi(
            system_prompt, user_prompt, [image_path],
            max_tokens=max_tokens, stop_event=stop_event)

    def request_json_multi(self, system_prompt: str, user_prompt: str,
                           image_paths, max_tokens=1024,
                           stop_event=None) -> dict:
        """
        一次把多张图片发给视觉 LLM（组图 / 统一风格模式）。

        image_paths 中的图片按顺序出现在 user 消息里，与 user_prompt
        中的"第 N 张"编号一一对应。其余行为与 request_json 相同。

        发送前会自动读取每张图片的 EXIF（拍摄参数）并附加到 user prompt
        末尾；读取失败 / 字段缺失时静默降级，不影响主流程。
        """
        paths = [p for p in (image_paths or []) if p]
        if not paths:
            logger.error("request_json_multi 调用失败：未提供任何图片")
            raise ValueError("request_json_multi 至少需要一张图片")

        # 读取 EXIF 并附加到 user prompt；失败时优雅降级，不阻塞请求
        exif_text = build_exif_context(paths)
        if exif_text:
            user_prompt = f"{user_prompt}\n\n{exif_text}"
            logger.info("已在 user prompt 中附加 EXIF 信息（%d 张照片，"
                        "新增 %d 字符）", len(paths), len(exif_text))
        else:
            logger.info("未读到可用的 EXIF 信息，user prompt 保持原样")

        logger.info("准备请求 LLM：model=%s，图片 %d 张，max_tokens=%d，"
                    "system prompt %d 字符，user prompt %d 字符",
                    self.model, len(paths), max_tokens,
                    len(system_prompt or ""), len(user_prompt or ""))
        content = [{"type": "text", "text": user_prompt}]
        for index, path in enumerate(paths, start=1):
            logger.info("编码第 %d/%d 张图片: %s", index, len(paths), path)
            base64_img = load_image_base64_from_raw(path)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_img}",
                    "detail": "low"
                }
            })

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        return self._post_and_extract(messages, max_tokens, stop_event)

    def _post_and_extract(self, messages, max_tokens, stop_event) -> dict:
        """发送 messages 并取出第一个完整 JSON 对象。"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2
        }
        # 支持的模型带上原生思考开关；不支持的模型不会发送该字段
        thinking_fields = build_thinking_fields(self.model, self.thinking)
        payload.update(thinking_fields)
        thinking_desc = ("不干预"
                         if not thinking_fields else
                         thinking_fields["thinking"]["type"])
        url = f"{self.api_base}/chat/completions"
        # 只统计文本与图片数据量，避免为了算长度把整段 base64 序列化一遍
        text_chars = 0
        image_chars = 0
        image_count = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        image_count += 1
                        image_chars += len(part["image_url"]["url"])
                    else:
                        text_chars += len(part.get("text", ""))
        logger.info("POST %s（model=%s，max_tokens=%d，messages=%d 条，"
                    "文本 %d 字符，图片 %d 张 / base64 %d 字符，"
                    "原生思考=%s，stream=True，timeout=(10, 60)）",
                    url, self.model, max_tokens, len(messages),
                    text_chars, image_count, image_chars, thinking_desc)

        # 用 stream=True 以便逐块读取，并在用户点停止时尽快中断。
        # timeout=(connect, read)：连接 10s，块间读取 60s。
        try:
            resp = requests.post(
                url, headers=headers, json=payload,
                timeout=(10, 60), stream=True,
            )
        except Exception as e:
            logger.error("LLM 请求发送失败 %s: %s", url, e)
            raise

        try:
            chunks = []
            for chunk in resp.iter_content(chunk_size=4096):
                if stop_event is not None and stop_event.is_set():
                    logger.info("用户中止：已读取 %d 块，丢弃本次响应",
                                len(chunks))
                    raise InterruptedError("请求已被用户中止")
                if chunk:
                    chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            resp.close()

        logger.info("LLM 响应：status=%s，body %d 字节，共 %d 块",
                    resp.status_code, len(body), len(chunks))

        if resp.status_code >= 400:
            err_text = body.decode("utf-8", errors="replace")[:500]
            logger.error("LLM 接口返回 %s: %s", resp.status_code, err_text)
            raise RuntimeError(
                f"LLM 接口返回 {resp.status_code}: {err_text}"
            )

        if not body:
            logger.error("LLM 返回空响应 (status=%s)", resp.status_code)
            raise ValueError(
                f"LLM 返回空响应 (status={resp.status_code})"
            )

        try:
            data = json.loads(body.decode("utf-8"))
            message = data["choices"][0]["message"]
        except Exception as e:
            logger.error("解析 LLM 响应外层结构失败: %s（body 前 300 字符: %s）",
                         e, body.decode("utf-8", errors="replace")[:300])
            raise

        # 开启原生思考时，模型会先在 reasoning_content 里输出推理过程，
        # 最终的 JSON 仍在 content 里；这里只记录 CoT 长度，不做解析。
        reasoning = (message.get("reasoning_content") or "").strip()
        if reasoning:
            logger.info("模型原生思考过程 %d 字符，前 200 字符: %s",
                        len(reasoning), reasoning[:200].replace("\n", " "))
            if self.reasoning_cb is not None:
                try:
                    self.reasoning_cb(reasoning)
                except Exception as e:
                    # UI 显示失败不能影响处理流程
                    logger.warning("推送原生思考内容到界面失败: %s", e)

        content = (message.get("content") or "").strip()
        if not content:
            # 思考模式下 max_tokens 可能被推理过程吃光，导致正文为空；
            # 把情况说清楚，便于排查（也兼容返回 None 的接口）
            logger.error("LLM 返回正文为空（reasoning_content %d 字符）",
                         len(reasoning))
            raise ValueError(
                "LLM 返回正文为空"
                + ("（推理内容已返回，但最终答案缺失，"
                   "可尝试调大 max_tokens 或关闭原生思考）"
                   if reasoning else ""))
        logger.info("LLM 文本回复 %d 字符，前 200 字符: %s",
                    len(content), content[:200].replace("\n", " "))

        # 模型可能把 JSON 包在 ```json ``` 里，或前后夹带其它文字
        # 用括号计数的方式取第一个完整 JSON 对象（贪婪正则会在
        # LLM 输出多个 JSON 或字符串里含 {} 时出错）
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]

        json_str = _extract_first_json_object(content)
        if json_str is None:
            logger.error("LLM 返回内容中找不到 JSON 对象，原始内容: %s",
                         content[:500])
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")
        try:
            result = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error("LLM 返回的 JSON 无法解析: %s（片段: %s）",
                         e, json_str[:500])
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")
        logger.info("已解析 LLM JSON：%d 个键 %s",
                    len(result) if isinstance(result, dict) else 0,
                    list(result)[:20] if isinstance(result, dict) else type(result).__name__)
        return result