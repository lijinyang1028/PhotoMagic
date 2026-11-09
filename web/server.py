import os
import re
import sys
import ipaddress
import socket
from urllib.parse import urlparse
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 动态注入项目根目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import log_setup
from web import services, db
from web.prompts import (
    build_system_prompt,
    DEFAULT_USER_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    PREVIEW_EXTENSIONS,
)

log_setup.setup_logging()
db.init_db()

app = FastAPI(title="PhotoMagic Web")

# ================= 安全响应头 =================
# CSP 不包含 unsafe-eval / 内联脚本；前端只加载 /static 下的脚本。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'"
    ),
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # 限制 JSON 请求体大小，避免一次性构造超大 JSON 占用内存（上传接口单独处理）
    if request.url.path != "/api/uploads":
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_JSON_BODY:
            return JSONResponse({"detail": "请求体过大"}, status_code=413)

    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value
    # 接口响应可能包含用户数据，禁止中间层缓存
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


# ================= CORS =================
# 前端页面与 API 同源部署，默认完全不开放跨域。
# 旧的 allow_origins=["*"] 会让任意网站读取本机接口（历史记录、设置等），
# 并借用本服务向任意地址发起请求，等同于对外提供了一个 SSRF 代理。
# 如需独立域名部署，请通过环境变量显式列出可信来源，切勿使用 "*"。
_ALLOWED_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("PHOTOMAGIC_WEB_ALLOWED_ORIGINS", "").split(",")
    if origin.strip() and origin.strip() != "*"
]
if _ALLOWED_CORS_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-User-ID"],
        max_age=600,
    )

# 静态目录准备
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "gallery").mkdir(exist_ok=True)
(STATIC_DIR / "backgrounds").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ================= 全局限制常量 =================
MAX_JSON_BODY = 2 * 1024 * 1024        # 普通 JSON 接口请求体上限 2MB
MAX_FILE_SIZE = 150 * 1024 * 1024      # 单文件最大 150MB
MAX_FILES_PER_UPLOAD = 10              # 单次最多传 10 张
MAX_TARGETS_PER_TASK = 200             # 单任务最多处理的图片数
MAX_API_BASE_LEN = 2048
MAX_API_KEY_LEN = 512
MAX_MODEL_LEN = 200
MAX_PROMPT_LEN = 8000
MAX_DESCRIPTION_LEN = 2000
MAX_PARALLEL = 5
MAX_MAX_TOKENS = 8192

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def normalize_user_id(value: str | None) -> str:
    """限制 X-User-ID 的长度与字符集，避免任意超长/畸形 ID 写入数据库。"""
    value = (value or "").strip()
    return value if _USER_ID_RE.fullmatch(value) else "anonymous"


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ================= SSRF 防护检测 =================
# 明显指向本机 / 内网的域名后缀，直接拒绝，不必等待 DNS 解析。
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".home.arpa",
    ".lan",
)


def _ip_is_public(ip_obj) -> bool:
    """只允许公网单播地址。私有 / 回环 / 链路本地 / 保留 / 组播一律拒绝。"""
    # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）按其中的 IPv4 地址判断
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.is_site_local:
        return False
    return ip_obj.is_global


def _is_blocked_hostname(hostname: str) -> bool:
    host = hostname.strip().strip("[]").rstrip(".").lower()
    if not host or len(host) > 253:
        return True
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return True
    return False


def _resolve_ip_strings(hostname: str) -> list[str]:
    """解析域名，返回需要校验的全部 IP 字符串（IPv4 + IPv6 + 多 A 记录）。"""
    ips: list[str] = []
    # 1) 先做一次 IPv4 解析，兼容既有行为
    try:
        legacy_ip = socket.gethostbyname(hostname)
    except OSError:
        legacy_ip = None
    if legacy_ip:
        ips.append(legacy_ip)
    # 2) 再用 getaddrinfo 覆盖 IPv6 / 多条记录，避免只校验其中一个地址
    try:
        for info in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def is_safe_api_url(url: str) -> bool:
    """校验 API Base URL，防止内网 SSRF 攻击。

    - 只允许 http/https；
    - 拒绝携带用户名/密码、空白字符、反斜杠等容易被绕过的写法；
    - 域名解析后校验 **全部** 结果，只接受公网地址；
    - IPv4 / IPv6、十进制 / 八进制等 IP 写法经 socket + ipaddress 归一化后判断。
    """
    try:
        if not isinstance(url, str) or not url or len(url) > MAX_API_BASE_LEN:
            return False
        if any(ord(ch) < 0x20 or ch.isspace() or ch in "\\<>\"{}|^`" for ch in url):
            return False
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        # 访问 port 属性可同时校验非法端口
        _ = parsed.port
        hostname = parsed.hostname
        if not hostname or _is_blocked_hostname(hostname):
            return False

        # 字面量 IP 直接判断（含 IPv6 / 十进制写法归一化后的结果）
        try:
            literal = ipaddress.ip_address(hostname.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            return _ip_is_public(literal)

        resolved = _resolve_ip_strings(hostname)
        if not resolved:
            return False
        for ip in resolved:
            try:
                if not _ip_is_public(ipaddress.ip_address(ip)):
                    return False
            except ValueError:
                return False
        return True
    except Exception:
        return False


# ================= 媒体与系统状态 =================
@app.get("/api/gallery")
async def get_gallery():
    d = STATIC_DIR / "gallery"
    return {"images": [f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}]}

@app.get("/api/backgrounds")
async def get_backgrounds():
    d = STATIC_DIR / "backgrounds"
    return {"images": [f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}]}

@app.get("/api/system")
async def api_system():
    import rt_processor
    rt_path = rt_processor.find_rt_cli()
    rt_available = bool(rt_path)
    rt_version = ""
    if rt_available:
        try: rt_version = rt_processor.get_rt_cli_version(rt_path)
        except Exception: pass
    return {"version": "0.5.0", "rt_available": rt_available, "rt_version": rt_version or "未知版本"}

# ================= 用户设置接口 =================
@app.get("/api/settings")
async def get_settings(x_user_id: str = Header(default="anonymous")):
    defaults = {"user_prompt": DEFAULT_USER_PROMPT, "parallel": 3, "max_tokens": 1024, "strength": 100}
    return {**defaults, **db.get_user_settings(normalize_user_id(x_user_id))}

class SettingsUpdate(BaseModel):
    user_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_LEN)
    parallel: int | None = Field(default=None, ge=1, le=10)
    max_tokens: int | None = Field(default=None, ge=1, le=32768)
    strength: int | None = Field(default=None, ge=0, le=100)

@app.put("/api/settings")
async def update_settings(data: SettingsUpdate, x_user_id: str = Header(default="anonymous")):
    user_id = normalize_user_id(x_user_id)
    s = db.get_user_settings(user_id)
    s.update(data.model_dump(exclude_unset=True))
    db.save_user_settings(user_id, s)
    return {"ok": True}

# ================= 文件上传 =================
def _upload_extension(filename: str) -> str:
    return Path(filename or "").suffix.lower().lstrip(".")


async def _read_upload_limited(f: UploadFile, limit: int) -> bytes:
    """分块读取上传内容并限制大小，避免一次性把超大文件读进内存。"""
    declared = getattr(f, "size", None)
    if isinstance(declared, int) and declared > limit:
        raise HTTPException(413, f"文件 {f.filename} 超过 {limit // 1024 // 1024}MB 限制")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"文件 {f.filename} 超过 {limit // 1024 // 1024}MB 限制")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/uploads")
async def upload(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "没有收到文件")
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(400, f"单次最多只能上传 {MAX_FILES_PER_UPLOAD} 张图片")

    res = []
    for f in files:
        filename = f.filename or ""
        # 只接受明确的图片 / RAW 扩展名，避免把任意文件写进暂存目录
        if _upload_extension(filename) not in PREVIEW_EXTENSIONS:
            raise HTTPException(400, f"不支持的文件类型: {filename[:120]}")

        content = await _read_upload_limited(f, MAX_FILE_SIZE)
        info = services.save_upload(filename or "img.raw", content)
        try:
            info["thumb"] = services.make_thumbnail(info["path"])
        except Exception:
            info["thumb"] = ""
        res.append(info)
    return {"files": res}

@app.get("/api/uploads/{fid}/thumb")
async def get_thumb(fid: str):
    info = services.get_upload(fid)
    if not info: raise HTTPException(404, "Not found")
    return {"thumb": services.make_thumbnail(info["path"])}

# ================= 任务处理接口 =================
class TaskStart(BaseModel):
    target_ids: list[str] = Field(default_factory=list, max_length=MAX_TARGETS_PER_TASK)
    groups: dict = Field(default_factory=dict)
    user_prompt: str = Field(default="", max_length=MAX_PROMPT_LEN)
    parallel: int = 3
    max_tokens: int = 1024
    strength: int = 100
    thinking: bool = False
    api_base: str = Field(..., max_length=MAX_API_BASE_LEN)
    api_key: str = Field(..., max_length=MAX_API_KEY_LEN)
    model: str = Field(..., max_length=MAX_MODEL_LEN)


def _validate_llm_config(api_base: str, api_key: str, model: str) -> None:
    """统一校验前端传入的 LLM 连接参数。"""
    if not is_safe_api_url(api_base):
        raise HTTPException(400, "非法的 API 代理地址，禁止访问内网")
    if any(ch in api_key for ch in "\r\n\x00"):
        raise HTTPException(400, "API Key 含有非法字符")
    if not model.strip():
        raise HTTPException(400, "Model 不能为空")


@app.post("/api/tasks")
async def start_task(data: TaskStart):
    if not data.target_ids:
        raise HTTPException(400, "Empty files")

    _validate_llm_config(data.api_base, data.api_key, data.model)

    # 强制限制单次并发与 token 上限，防本地 CPU / 费用爆炸
    data.parallel = min(max(data.parallel, 1), MAX_PARALLEL)
    data.max_tokens = min(max(data.max_tokens, 1), MAX_MAX_TOKENS)
    data.strength = min(max(data.strength, 0), 100)

    try:
        task = services.create_task(data.model_dump())
        services.run_task_async(task)
        return {"task_id": task.id}
    except HTTPException as e:
        raise e

@app.get("/api/tasks/{tid}")
async def get_task(tid: str, since: int = 0):
    task = services.get_task(tid)
    if not task: raise HTTPException(404, "Not found")
    return task.snapshot(max(0, since))

@app.post("/api/tasks/{tid}/stop")
async def stop_task(tid: str):
    task = services.get_task(tid)
    if task: task.stop_event.set()
    return {"ok": True}

@app.get("/api/tasks/{tid}/output/{fid}")
async def get_output(tid: str, fid: str):
    task = services.get_task(tid)
    if not task: raise HTTPException(404, "Task not found")
    path = task.processed.get(fid)
    # 双重校验：输出文件必须真实存在且位于受控的输出目录内
    if not path or not os.path.isfile(path) or not services.is_within_output_dir(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=os.path.basename(path))

# ================= 评价接口 =================
class ReviewStart(BaseModel):
    file_id: str = Field(..., max_length=128)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_LEN)
    api_base: str = Field(..., max_length=MAX_API_BASE_LEN)
    api_key: str = Field(..., max_length=MAX_API_KEY_LEN)
    model: str = Field(..., max_length=MAX_MODEL_LEN)

@app.post("/api/reviews")
async def start_review(data: ReviewStart, x_user_id: str = Header(default="anonymous")):
    _validate_llm_config(data.api_base, data.api_key, data.model)

    info = services.get_upload(data.file_id)
    if not info: raise HTTPException(404, "Not found")
    return services.start_review(
        info["path"], data.description, data.model_dump(), normalize_user_id(x_user_id)
    )

@app.get("/api/reviews/{tid}")
async def get_review(tid: str):
    info = services.get_review_task(tid)
    if not info: raise HTTPException(404, "Not found")
    return info

@app.get("/api/reviews/history")
async def get_history(x_user_id: str = Header(default="anonymous")):
    return {"items": db.get_review_history(normalize_user_id(x_user_id))}

@app.delete("/api/reviews/history")
async def clear_history(x_user_id: str = Header(default="anonymous")):
    db.clear_review_history(normalize_user_id(x_user_id))
    return {"ok": True}

@app.get("/api/prompts/default")
async def default_prompts():
    return {"system_prompt": build_system_prompt(), "user_prompt": DEFAULT_USER_PROMPT}

if __name__ == "__main__":
    import uvicorn
    # 默认仅监听本机；如需局域网访问，请显式改为 "0.0.0.0" 并自行评估暴露风险
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
