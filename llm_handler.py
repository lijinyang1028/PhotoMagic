"""
LLM 调用模块
支持标准 OpenAI 接口 (视觉模型), 发送图片并获取 JSON 参数
"""
import base64
import json
import logging
import requests
from io import BytesIO
from PIL import Image
import rawpy


logger = logging.getLogger(__name__)


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

    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        logger.info("创建 LLM 客户端：model=%s，api_base=%s，API Key %s",
                    self.model, self.api_base,
                    "已配置" if self.api_key else "未配置（请求将失败）")

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
        """
        paths = [p for p in (image_paths or []) if p]
        if not paths:
            logger.error("request_json_multi 调用失败：未提供任何图片")
            raise ValueError("request_json_multi 至少需要一张图片")

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
                    "stream=True，timeout=(10, 60)）",
                    url, self.model, max_tokens, len(messages),
                    text_chars, image_count, image_chars)

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
            content = data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error("解析 LLM 响应外层结构失败: %s（body 前 300 字符: %s）",
                         e, body.decode("utf-8", errors="replace")[:300])
            raise

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