"""
LLM 调用模块
支持标准 OpenAI 接口 (视觉模型), 发送图片并获取 JSON 参数
"""
import base64
import json
import requests
from io import BytesIO
from PIL import Image
import rawpy


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
            else:
                # 其他格式需转换为 RGB 再编码
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb)
    except Exception:
        # 如果 rawpy 无法处理，尝试用 Pillow 直接打开（可能是普通图片）
        img = Image.open(raw_path)

    # 统一转 RGB（P 模式 / 灰度 / CMYK 等都要转，否则 JPEG 保存会报错）
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 统一缩放并转为 JPEG base64
    img.thumbnail(thumb_size, Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


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

    def request_json(self, system_prompt: str, user_prompt: str,
                     image_path: str, max_tokens=1024,
                     stop_event=None) -> dict:
        """
        将图片（RAW）编码后发送给视觉 LLM，期望返回一个 JSON 对象。

        stop_event: 可选 threading.Event。
                    设置后会在下一个网络数据块到达时中止本次请求
                    （通常 1 秒内生效），抛出 InterruptedError。
        """
        base64_img = load_image_base64_from_raw(image_path)

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_img}",
                            "detail": "low"
                        }
                    }
                ]
            }
        ]

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

        # 用 stream=True 以便逐块读取，并在用户点停止时尽快中断。
        # timeout=(connect, read)：连接 10s，块间读取 60s。
        resp = requests.post(
            f"{self.api_base}/chat/completions",
            headers=headers, json=payload,
            timeout=(10, 60), stream=True,
        )

        try:
            chunks = []
            for chunk in resp.iter_content(chunk_size=4096):
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("请求已被用户中止")
                if chunk:
                    chunks.append(chunk)
            body = b"".join(chunks)
        finally:
            resp.close()

        if resp.status_code >= 400:
            err_text = body.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"LLM 接口返回 {resp.status_code}: {err_text}"
            )

        if not body:
            raise ValueError(
                f"LLM 返回空响应 (status={resp.status_code})"
            )

        data = json.loads(body.decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()

        # 模型可能把 JSON 包在 ```json ``` 里，或前后夹带其它文字
        # 用括号计数的方式取第一个完整 JSON 对象（贪婪正则会在
        # LLM 输出多个 JSON 或字符串里含 {} 时出错）
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]

        json_str = _extract_first_json_object(content)
        if json_str is None:
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")