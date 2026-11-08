import os
import sys
import ipaddress
import socket
from urllib.parse import urlparse
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 动态注入项目根目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: 
    sys.path.insert(0, str(ROOT))

import log_setup
from web import services, db
from web.prompts import build_system_prompt, DEFAULT_USER_PROMPT, REVIEW_SYSTEM_PROMPT

log_setup.setup_logging()
db.init_db()

app = FastAPI(title="PhotoMagic Web")
app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_methods=["*"], 
    allow_headers=["*"]
)

# 静态目录准备
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "gallery").mkdir(exist_ok=True)
(STATIC_DIR / "backgrounds").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def index(): 
    return FileResponse(str(STATIC_DIR / "index.html"))

# ================= 新增：SSRF 防护检测 =================
def is_safe_api_url(url: str) -> bool:
    """校验 API Base URL，防止内网 SSRF 攻击"""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        # 解析域名获取真实 IP
        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)
        # 拒绝内网 IP、回环 IP(127.0.0.1)、链路本地 IP(169.254.x.x)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            return False
        return True
    except Exception:
        return False

# ================= 媒体与系统状态 =================
@app.get("/api/gallery")
async def get_gallery():
    d = STATIC_DIR / "gallery"
    return {"images": [f.name for f in d.iterdir() if f.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}]}

@app.get("/api/backgrounds")
async def get_backgrounds():
    d = STATIC_DIR / "backgrounds"
    return {"images": [f.name for f in d.iterdir() if f.suffix.lower() in {'.jpg','.jpeg','.png','.webp'}]}

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
    return {**defaults, **db.get_user_settings(x_user_id)}

class SettingsUpdate(BaseModel):
    user_prompt: str | None = None
    parallel: int | None = None
    max_tokens: int | None = None
    strength: int | None = None

@app.put("/api/settings")
async def update_settings(data: SettingsUpdate, x_user_id: str = Header(default="anonymous")):
    s = db.get_user_settings(x_user_id)
    s.update(data.model_dump(exclude_unset=True))
    db.save_user_settings(x_user_id, s)
    return {"ok": True}

# ================= 文件上传限制 =================
MAX_FILE_SIZE = 150 * 1024 * 1024  # 单文件最大 150MB
MAX_FILES_PER_UPLOAD = 10          # 单次最多传 10 张

@app.post("/api/uploads")
async def upload(files: list[UploadFile] = File(...)):
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(400, f"单次最多只能上传 {MAX_FILES_PER_UPLOAD} 张图片")
    
    res = []
    for f in files:
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(413, f"文件 {f.filename} 超过 {MAX_FILE_SIZE//1024//1024}MB 限制")
        
        info = services.save_upload(f.filename or "img.raw", content)
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
    target_ids: list[str]
    groups: dict = {}
    user_prompt: str = ""
    parallel: int = 3
    max_tokens: int = 1024
    strength: int = 100
    thinking: bool = False
    api_base: str
    api_key: str
    model: str

@app.post("/api/tasks")
async def start_task(data: TaskStart):
    if not data.target_ids: 
        raise HTTPException(400, "Empty files")
    
    if not is_safe_api_url(data.api_base):
        raise HTTPException(400, "非法的 API 代理地址，禁止访问内网")

    # 强制限制单次并发上限，防本地 CPU 爆炸
    if data.parallel < 1 or data.parallel > 5:
        data.parallel = 3 

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
    return task.snapshot(since)

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
    if not path or not os.path.exists(path): raise HTTPException(404, "File not found")
    return FileResponse(path, filename=os.path.basename(path))

# ================= 评价接口 =================
class ReviewStart(BaseModel):
    file_id: str
    description: str = ""
    api_base: str
    api_key: str
    model: str

@app.post("/api/reviews")
async def start_review(data: ReviewStart, x_user_id: str = Header(default="anonymous")):
    if not is_safe_api_url(data.api_base):
        raise HTTPException(400, "非法的 API 代理地址，禁止访问内网")
        
    info = services.get_upload(data.file_id)
    if not info: raise HTTPException(404, "Not found")
    return services.start_review(info["path"], data.description, data.model_dump(), x_user_id)

@app.get("/api/reviews/{tid}")
async def get_review(tid: str):
    info = services.get_review_task(tid)
    if not info: raise HTTPException(404, "Not found")
    return info

@app.get("/api/reviews/history")
async def get_history(x_user_id: str = Header(default="anonymous")):
    return {"items": db.get_review_history(x_user_id)}

@app.delete("/api/reviews/history")
async def clear_history(x_user_id: str = Header(default="anonymous")):
    db.clear_review_history(x_user_id)
    return {"ok": True}

@app.get("/api/prompts/default")
async def default_prompts():
    return {"system_prompt": build_system_prompt(), "user_prompt": DEFAULT_USER_PROMPT}

if __name__ == "__main__":
    import uvicorn
    # 默认 127.0.0.1，如果想局域网访问可改为 "0.0.0.0"
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")