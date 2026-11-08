import os
import sys
from pathlib import Path
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

import log_setup
from web import services, db
from web.prompts import build_system_prompt, DEFAULT_USER_PROMPT, REVIEW_SYSTEM_PROMPT

log_setup.setup_logging()
db.init_db()

app = FastAPI(title="PhotoMagic Web")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
(STATIC_DIR / "gallery").mkdir(exist_ok=True)
(STATIC_DIR / "backgrounds").mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def index(): 
    return FileResponse(str(STATIC_DIR / "index.html"))

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

# ================== 用户设置接口 ==================
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

# ================== 上传与处理接口 ==================
@app.post("/api/uploads")
async def upload(files: list[UploadFile] = File(...)):
    res = []
    for f in files:
        info = services.save_upload(f.filename or "img.raw", await f.read())
        try: info["thumb"] = services.make_thumbnail(info["path"])
        except Exception: info["thumb"] = ""
        res.append(info)
    return {"files": res}

@app.get("/api/uploads/{fid}/thumb")
async def get_thumb(fid: str):
    info = services.get_upload(fid)
    if not info: raise HTTPException(404, "Not found")
    return {"thumb": services.make_thumbnail(info["path"])}

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
    if not data.target_ids: raise HTTPException(400, "Empty files")
    task = services.create_task(data.model_dump())
    services.run_task_async(task)
    return {"task_id": task.id}

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

# ================== 评价接口 ==================
class ReviewStart(BaseModel):
    file_id: str
    description: str = ""
    api_base: str
    api_key: str
    model: str

@app.post("/api/reviews")
async def start_review(data: ReviewStart, x_user_id: str = Header(default="anonymous")):
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
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")