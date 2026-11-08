import json
import logging
import os
import threading
import time
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import settings
import rt_processor
from llm_handler import LLMClient, load_image_base64_from_raw
from grouping import (
    partition_targets, normalize_group_response,
    merge_group_params, build_group_system_prompt, build_group_user_prompt,
)
from web.prompts import DEFAULT_USER_PROMPT, REVIEW_SYSTEM_PROMPT, build_system_prompt
from web import db

logger = logging.getLogger(__name__)

WEB_DATA_DIR = Path(settings.CONFIG_DIR) / "web_uploads"
WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

_uploads: dict = {}
_uploads_lock = threading.Lock()

def save_upload(filename: str, data: bytes) -> dict:
    file_id = uuid.uuid4().hex
    safe = Path(filename).name if filename else f"upload_{file_id}.raw"
    dest = WEB_DATA_DIR / f"{file_id}_{safe}"
    dest.write_bytes(data)
    info = {"id": file_id, "name": safe, "path": str(dest), "size": len(data), "created_at": time.time()}
    with _uploads_lock:
        _uploads[file_id] = info
    return info

def get_upload(file_id: str) -> Optional[dict]:
    return _uploads.get(file_id)

def make_thumbnail(path: str, size=(400, 400)) -> str:
    b64 = load_image_base64_from_raw(path, thumb_size=size)
    return f"data:image/jpeg;base64,{b64}"

def cleanup_old_uploads(max_age_seconds=86400):
    """清理超过 24 小时的上传文件，防止 Linux 服务器磁盘溢出"""
    now = time.time()
    with _uploads_lock:
        for fid in list(_uploads.keys()):
            info = _uploads[fid]
            if now - info.get("created_at", 0) > max_age_seconds:
                try:
                    Path(info["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
                del _uploads[fid]

# ----------------- 处理任务 -----------------
@dataclass
class Task:
    id: str
    config: dict
    total: int = 0
    completed: int = 0
    state: str = "pending"
    file_status: dict = field(default_factory=dict)
    log_lines: list = field(default_factory=list)
    failed_ids: list = field(default_factory=list)
    processed: dict = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, msg: str):
        with self.lock: self.log_lines.append(msg)

    def set_status(self, fid: str, status: str):
        with self.lock: self.file_status[fid] = status

    def mark_done(self, fid: str, success: bool, message: str = "", output_path: str = ""):
        with self.lock:
            self.completed += 1
            if success:
                self.file_status[fid] = "done"
                if output_path: self.processed[fid] = output_path
            elif message == "已取消":
                self.file_status[fid] = "cancelled"
            else:
                self.file_status[fid] = "failed"
                self.failed_ids.append(fid)

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id, "state": self.state, "total": self.total,
                "completed": self.completed, "log_total": len(self.log_lines),
                "new_lines": self.log_lines[since:], "file_status": dict(self.file_status),
                "failed_ids": list(self.failed_ids), "processed": dict(self.processed),
            }

_tasks: dict = {}
_tasks_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-task")

def create_task(config: dict) -> Task:
    task = Task(id=uuid.uuid4().hex, config=config)
    with _tasks_lock: _tasks[task.id] = task
    return task

def get_task(task_id: str) -> Optional[Task]:
    return _tasks.get(task_id)

def run_task_async(task: Task):
    _executor.submit(_run_task, task)

def _run_task(task: Task):
    try: _do_run(task)
    except Exception as e:
        logger.exception("Task crashed")
        task.log(f"❌ 任务异常: {e}")
        with task.lock:
            task.state = "finished"
            for fid, st in task.file_status.items():
                if st in ("pending", "running"):
                    task.file_status[fid] = "failed"
                    if fid not in task.failed_ids: task.failed_ids.append(fid)

def _do_run(task: Task):
    cfg = task.config
    target_ids = cfg.get("target_ids") or []
    groups = cfg.get("groups") or {}
    
    id_to_info = {fid: get_upload(fid) for fid in target_ids if get_upload(fid)}
    if not id_to_info:
        task.log("❌ 没有可处理的文件"); return

    groups_by_path = {g: [id_to_info[i]["path"] for i in ids if i in id_to_info] for g, ids in groups.items()}
    paths = [id_to_info[i]["path"] for i in id_to_info]
    path_to_id = {id_to_info[i]["path"]: i for i in id_to_info}

    batches = partition_targets(paths, groups_by_path, cfg.get("unify", False))
    task.total = len(paths)
    with task.lock: task.state = "running"
    
    client = LLMClient(
        api_base=cfg["api_base"], api_key=cfg["api_key"], 
        model=cfg["model"], thinking=cfg.get("thinking", False)
    )
    sys_prompt = cfg.get("system_prompt") or build_system_prompt()
    usr_prompt = cfg.get("user_prompt") or DEFAULT_USER_PROMPT

    task.log(f"开始处理 {len(paths)} 张图片...")
    with ThreadPoolExecutor(max_workers=cfg.get("parallel", 3)) as pool:
        for name, b_paths in batches:
            if name and len(b_paths) > 1:
                pool.submit(_process_group, task, name, b_paths, path_to_id, client, sys_prompt, usr_prompt, cfg)
            else:
                for p in b_paths:
                    pool.submit(_process_single, task, p, path_to_id.get(p), client, sys_prompt, usr_prompt, cfg)
                    
    task.log("全部处理完成" if not task.stop_event.is_set() else "处理已停止")
    with task.lock: task.state = "cancelled" if task.stop_event.is_set() else "finished"

def _render_and_run(path: str, params: dict, cfg: dict) -> str:
    out_dir = os.path.join(os.path.expanduser("~"), "photomagic_web_output")
    os.makedirs(out_dir, exist_ok=True)
    pp3 = os.path.join(out_dir, f"{Path(path).stem}.pp3")
    rt_processor.generate_pp3(params, pp3, output_profile=cfg.get("output_profile", "RT_sRGB"), strength=cfg.get("strength", 1.0))
    rt_processor.run_rawtherapee(path, pp3, out_dir, output_format=cfg.get("output_format", "jpg"))
    ext = {"jpg": "jpg", "tiff": "tif", "png": "png"}.get(cfg.get("output_format", "jpg"), "jpg")
    out_path = os.path.join(out_dir, f"{Path(path).stem}.{ext}")
    return out_path if os.path.exists(out_path) else ""

def _process_single(task, path, fid, client, sys, usr, cfg):
    if fid: task.set_status(fid, "running")
    try:
        params = client.request_json(sys, usr, path, max_tokens=cfg.get("max_tokens", 1024), stop_event=task.stop_event)
        if task.stop_event.is_set(): task.mark_done(fid, False, "已取消"); return
        out = _render_and_run(path, params, cfg)
        task.mark_done(fid, True, output_path=out)
    except InterruptedError: task.mark_done(fid, False, "已取消")
    except Exception as e:
        task.log(f"✗ {Path(path).name}: {e}")
        task.mark_done(fid, False, str(e))

def _process_group(task, name, paths, path_to_id, client, sys, usr, cfg):
    for p in paths:
        if path_to_id.get(p): task.set_status(path_to_id[p], "running")
    try:
        raw = client.request_json_multi(build_group_system_prompt(sys), build_group_user_prompt(usr, paths), paths, max_tokens=cfg.get("max_tokens", 1024), stop_event=task.stop_event)
        style, per_img = normalize_group_response(raw, paths)
        for p, extra in zip(paths, per_img):
            fid = path_to_id.get(p)
            if task.stop_event.is_set(): task.mark_done(fid, False, "已取消"); continue
            try:
                out = _render_and_run(p, merge_group_params(style, extra), cfg)
                task.mark_done(fid, True, output_path=out)
            except Exception as e:
                task.mark_done(fid, False, str(e))
    except InterruptedError:
        for p in paths:
            if path_to_id.get(p): task.mark_done(path_to_id[p], False, "已取消")
    except Exception as e:
        for p in paths:
            if path_to_id.get(p): task.mark_done(path_to_id[p], False, str(e))

# ----------------- 评价任务 -----------------
_review_executor = ThreadPoolExecutor(max_workers=2)
_review_tasks = {}

def start_review(image_path: str, desc: str, api_cfg: dict, user_id: str) -> dict:
    tid = uuid.uuid4().hex
    info = {"id": tid, "state": "pending", "result": None, "error": ""}
    _review_tasks[tid] = info
    _review_executor.submit(_run_review, info, image_path, desc, api_cfg, user_id)
    return {"task_id": tid}

def get_review_task(tid: str): return _review_tasks.get(tid)

def _run_review(info, path, desc, api_cfg, user_id):
    try:
        info["state"] = "running"
        client = LLMClient(api_base=api_cfg["api_base"], api_key=api_cfg["api_key"], model=api_cfg["model"])
        res = client.request_json(REVIEW_SYSTEM_PROMPT, f"请评价这张照片。\n{desc}", path, max_tokens=4096)
        info["state"] = "done"; info["result"] = res
        db.add_review_history(user_id, {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "image_path": path, "description": desc,
            "scores": res.get("scores", {}), "summary": res.get("summary", ""),
            "suggestions": res.get("suggestions", [])
        })
    except Exception as e:
        info["state"] = "error"; info["error"] = str(e)