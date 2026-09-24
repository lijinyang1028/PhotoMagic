"""
LLM 调用模块
支持标准 OpenAI 接口 (视觉模型), 发送图片并获取 JSON 参数
"""
import base64
import json
import re
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

        # 模型可能把 JSON 包在 ```json ``` 里，或前后夹带其它文字
        # 这个地方用正则来筛，LLM存在幻觉，网络传输也有风险，所以的话使用更加鲁棒的
        # 正则解析
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
        match = re.search(r"\{.*\}", content, re.DOTALL)  # 取第一个 { 到最后一个 }
        if not match:
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            raise ValueError(f"LLM 返回内容不是有效 JSON:\n{content}")