"""PhotoMagic Web 服务入口。

启动：
    python -m web.server
或：
    uvicorn web.server:app --reload

环境变量：
    PHOTOMAGIC_WEB_HOST  监听地址（默认 127.0.0.1）
    PHOTOMAGIC_WEB_PORT  监听端口（默认 8000）
"""
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import settings
import rt_processor
import log_setup
from web import services

log_setup.setup_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="PhotoMagic Web", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ============================ 系统信息 ============================
@app.get("/api/system")
async def api_system():
    log_file = log_setup.get_log_file()
    return {
        "version": "0.5.0",
        "config_dir": str(settings.CONFIG_DIR),
        "log_dir": str(settings.LOG_DIR),
        "log_file": str(log_file) if log_file else "",
        "keyring_available": settings.keyring_available(),
        "rt_cli": rt_processor.find_rt_cli() or "",
    }


# ============================ 设置 ============================
@app.get("/api/settings")
async def api_get_settings():
    data = settings.load_settings()
    return {
        "thinking_enabled": bool(data.get("thinking_enabled", True)),
        "output_dir": data.get("output_dir", ""),
        "output_format": int(data.get("output_format", 0)),
        "bit_depth": int(data.get("bit_depth", 1)),
        "jpeg_quality": int(data.get("jpeg_quality", 92)),
        "color_space": int(data.get("color_space", 0)),
        "parallel": int(data.get("parallel", 3)),
        "strength": int(data.get("strength", 100)),
        "unify_style": bool(data.get("unify_style", False)),
        "max_tokens": int(data.get("max_tokens", 1024)),
    }


class SettingsUpdate(BaseModel):
    thinking_enabled: bool | None = None
    max_tokens: int | None = None
    output_dir: str | None = None
    parallel: int | None = None
    strength: int | None = None
    unify_style: bool | None = None


@app.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    s = settings.load_settings()
    for k, v in data.model_dump(exclude_unset=True).items():
        if v is not None:
            s[k] = v
    settings.save_settings(s)
    return {"ok": True}


# ============================ 配置方案 ============================
@app.get("/api/profiles")
async def api_list_profiles():
    active = settings.get_active_profile_id()
    items = []
    for p in settings.list_api_profiles():
        items.append({
            "id": p["id"],
            "name": p["name"],
            "api_base": p["api_base"],
            "model": p["model"],
            "has_key": bool(settings.get_api_key(p["id"])),
            "active": p["id"] == active,
        })
    return {"profiles": items, "active": active}


class ProfileCreate(BaseModel):
    name: str
    api_base: str = ""
    model: str = ""


@app.post("/api/profiles")
async def api_create_profile(data: ProfileCreate):
    try:
        return settings.save_api_profile(data.name, data.api_base, data.model)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ProfileUpdate(BaseModel):
    name: str | None = None
    api_base: str | None = None
    model: str | None = None


@app.put("/api/profiles/{profile_id}")
async def api_update_profile(profile_id: str, data: ProfileUpdate):
    profile = settings.get_api_profile(profile_id)
    if profile is None:
        raise HTTPException(404, "方案不存在")
    try:
        payload = data.model_dump(exclude_unset=True)
        settings.save_api_profile(
            payload.get("name") or profile["name"],
            payload["api_base"] if "api_base" in payload else profile["api_base"],
            payload["model"] if "model" in payload else profile["model"],
            profile_id=profile_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return settings.get_api_profile(profile_id)


@app.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    if not settings.delete_api_profile(profile_id):
        raise HTTPException(400, "无法删除（可能是最后一套方案）")
    return {"ok": True}


class SetKey(BaseModel):
    api_key: str


@app.post("/api/profiles/{profile_id}/key")
async def api_set_key(profile_id: str, data: SetKey):
    ok = settings.set_api_key(data.api_key, profile_id)
    return {"ok": ok, "keyring": settings.keyring_available()}


@app.delete("/api/profiles/{profile_id}/key")
async def api_delete_key(profile_id: str):
    settings.delete_api_key(profile_id)
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/activate")
async def api_activate_profile(profile_id: str):
    if not settings.set_active_api_profile(profile_id):
        raise HTTPException(404, "方案不存在")
    return {"ok": True}


# ============================ RT 版本 ============================
@app.get("/api/rt-versions")
async def api_rt_versions():
    active = settings.get_active_rt_version_id()
    return {
        "versions": [
            {"id": v["id"], "name": v["name"], "path": v["path"],
             "active": v["id"] == active}
            for v in settings.list_rt_versions()
        ],
        "active": active,
        "resolved": rt_processor.find_rt_cli() or "",
    }


class RtCreate(BaseModel):
    name: str
    path: str = ""


@app.post("/api/rt-versions")
async def api_create_rt(data: RtCreate):
    try:
        return settings.save_rt_version(data.name, data.path)
    except ValueError as e:
        raise HTTPException(400, str(e))


class RtUpdate(BaseModel):
    name: str | None = None
    path: str | None = None


@app.put("/api/rt-versions/{vid}")
async def api_update_rt(vid: str, data: RtUpdate):
    version = settings.get_rt_version(vid)
    if version is None:
        raise HTTPException(404, "版本不存在")
    try:
        payload = data.model_dump(exclude_unset=True)
        settings.save_rt_version(
            payload.get("name") or version["name"],
            payload["path"] if "path" in payload else version["path"],
            version_id=vid,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return settings.get_rt_version(vid)


@app.delete("/api/rt-versions/{vid}")
async def api_delete_rt(vid: str):
    if not settings.delete_rt_version(vid):
        raise HTTPException(404, "删除失败")
    return {"ok": True}


@app.post("/api/rt-versions/{vid}/activate")
async def api_activate_rt(vid: str):
    if not settings.set_active_rt_version(vid):
        raise HTTPException(404, "版本不存在")
    return {"ok": True}


class DetectVersion(BaseModel):
    path: str = ""


@app.post("/api/rt-versions/detect")
async def api_detect_rt(data: DetectVersion):
    return {"version": rt_processor.get_rt_cli_version(data.path)}


# ============================ 上传 / 缩略图 ============================
@app.post("/api/uploads")
async def api_upload(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        data = await f.read()
        info = services.save_upload(f.filename or "upload.raw", data)
        try:
            info["thumb"] = services.make_thumbnail(info["path"])
        except Exception as e:
            logger.warning("缩略图生成失败 %s: %s", info["path"], e)
            info["thumb"] = ""
        results.append(info)
    return {"files": results}


@app.get("/api/uploads/{file_id}/thumb")
async def api_thumb(file_id: str):
    info = services.get_upload(file_id)
    if info is None:
        raise HTTPException(404, "文件不存在")
    try:
        return {"thumb": services.make_thumbnail(info["path"])}
    except Exception as e:
        raise HTTPException(500, str(e))


# ============================ 处理任务 ============================
class TaskStart(BaseModel):
    target_ids: list[str]
    groups: dict[str, list[str]] = {}
    unify: bool = False
    system_prompt: str = ""
    user_prompt: str = ""
    output_dir: str = ""
    output_format: str = "jpg"
    jpeg_quality: int = 92
    bit_depth: str = "16"
    output_profile: str = "RT_sRGB"
    strength: float = 1.0
    max_tokens: int = 1024
    profile_id: str = ""
    parallel: int = 3


@app.post("/api/tasks")
async def api_start_task(data: TaskStart):
    if not data.target_ids:
        raise HTTPException(400, "没有要处理的文件")
    task = services.create_task(data.model_dump())
    services.run_task_async(task)
    return {"task_id": task.id}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str, since: int = 0):
    task = services.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.snapshot(since=since)


@app.post("/api/tasks/{task_id}/stop")
async def api_stop_task(task_id: str):
    task = services.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    task.stop_event.set()
    return {"ok": True}


@app.get("/api/tasks/{task_id}/output/{file_id}")
async def api_task_output(task_id: str, file_id: str):
    task = services.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    with task.lock:
        path = task.processed.get(file_id)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "输出文件不存在")
    return FileResponse(path, filename=os.path.basename(path))


# ============================ 提示词 / 参数 ============================
@app.get("/api/prompts/default")
async def api_default_prompts():
    return {
        "system_prompt": services.build_system_prompt(),
        "user_prompt": services.DEFAULT_USER_PROMPT,
        "review_system_prompt": services.REVIEW_SYSTEM_PROMPT,
    }


@app.get("/api/params")
async def api_params():
    builtin = rt_processor.get_builtin_param_names()
    current = rt_processor.get_params()
    custom = [k for k in current if k not in builtin]
    return {"total": len(current), "custom_count": len(custom),
            "custom": custom}


@app.post("/api/params/import")
async def api_import_params(file: UploadFile = File(...)):
    data = await file.read()
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        Path(path).write_bytes(data)
        count, errors = rt_processor.load_custom_params(path)
        if count > 0:
            try:
                rt_processor.save_custom_params()
            except Exception as e:
                return {"count": count,
                        "errors": errors + [f"持久化失败: {e}"]}
        return {"count": count, "errors": errors}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@app.delete("/api/params/custom")
async def api_clear_params():
    rt_processor.clear_custom_params()
    return {"ok": True}


# ============================ 照片评价 ============================
class ReviewStart(BaseModel):
    file_id: str
    description: str = ""
    profile_id: str = ""


@app.post("/api/reviews")
async def api_start_review(data: ReviewStart):
    info = services.get_upload(data.file_id)
    if info is None:
        raise HTTPException(404, "文件不存在")
    return services.start_review(info["path"], data.description, data.profile_id)


@app.get("/api/reviews/{task_id}")
async def api_get_review(task_id: str):
    info = services.get_review_task(task_id)
    if info is None:
        raise HTTPException(404, "评价任务不存在")
    return info


@app.get("/api/reviews/history")
async def api_review_history():
    return {"items": services.load_review_history()}


@app.delete("/api/reviews/history")
async def api_clear_review_history():
    services.save_review_history([])
    return {"ok": True}


class HistorySelection(BaseModel):
    indexes: list[int]


@app.post("/api/reviews/history/delete")
async def api_delete_review_history(data: HistorySelection):
    history = services.load_review_history()
    for i in sorted(set(data.indexes), reverse=True):
        if 0 <= i < len(history):
            del history[i]
    services.save_review_history(history)
    return {"items": history}


@app.post("/api/reviews/export")
async def api_export_reviews(data: HistorySelection):
    history = services.load_review_history()
    rows = sorted(set(data.indexes))
    entries = [history[r] for r in rows if 0 <= r < len(history)]
    text = _build_markdown(entries)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="review_report_{stamp}.md"'
        },
    )


def _build_markdown(entries: list) -> str:
    lines = ["# 照片评价报告", ""]
    lines.append(f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"共 {len(entries)} 条记录")
    lines.append("")
    for i, entry in enumerate(entries, 1):
        ts = entry.get("timestamp", "")
        img = entry.get("image_path", "")
        lines.append(f"## {i}. {ts} — {Path(img).name}")
        lines.append("")
        lines.append(f"**文件路径：** `{img}`")
        lines.append("")
        desc = entry.get("description", "")
        if desc:
            lines.append(f"**拍摄者描述：** {desc}")
            lines.append("")
        scores = entry.get("scores", {})
        if scores:
            try:
                avg = sum(float(v) for v in scores.values()) / len(scores)
            except (TypeError, ValueError):
                avg = 0
            lines.append(f"**综合分：** {avg:.1f}")
            lines.append("")
            lines.append("| 维度 | 分数 |")
            lines.append("|------|------|")
            for k, v in scores.items():
                lines.append(f"| {k} | {v} |")
            lines.append("")
        summary = entry.get("summary", "")
        if summary:
            lines.append("**综合评价：**")
            lines.append("")
            lines.append(summary)
            lines.append("")
        suggestions = entry.get("suggestions", [])
        if suggestions:
            lines.append("**改进建议：**")
            lines.append("")
            for s in suggestions:
                lines.append(f"- {s}")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ============================ 启动入口 ============================
def main():
    import uvicorn

    host = os.environ.get("PHOTOMAGIC_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("PHOTOMAGIC_WEB_PORT", "8000"))
    logger.info("PhotoMagic Web 启动：http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()