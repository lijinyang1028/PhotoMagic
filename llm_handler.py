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
                # 缩略图已经是 JPEG，可直接使用
                return base64.b64encode(thumb.data).decode("utf-8")
            else:
                # 其他格式需转换为 RGB 再编码
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb)
    except Exception:
        # 如果 rawpy 无法处理，尝试用 Pillow 直接打开（可能是普通图片）
        img = Image.open(raw_path)

    # 统一缩放并转为 JPEG base64
    img.thumbnail(thumb_size, Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

class LLMClient:
    """兼容 OpenAI 的 LLM 客户端"""
    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model

    def request_json(self, system_prompt: str, user_prompt: str,
                     image_path: str, max_tokens=1024) -> dict:
        """
        将图片（RAW）编码后发送给视觉 LLM，期望返回一个 JSON 对象
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

        resp = requests.post(f"{self.api_base}/chat/completions",
                             headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # 尝试解析 JSON（可能被包裹在 ```json ``` 中）
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")