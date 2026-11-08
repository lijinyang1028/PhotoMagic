"""Web 服务业务层。

不重复实现桌面版已有逻辑，只把 settings / rt_processor / llm_handler /
grouping 这些无 GUI 依赖的模块包装成 Web API 需要的形式。
"""
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import settings
import rt_processor
from llm_handler import LLMClient, load_image_base64_from_raw
from grouping import (
    partition_targets,
    normalize_group_response,
    merge_group_params,
    build_group_system_prompt,
    build_group_user_prompt,
)
from web.prompts import (
    DEFAULT_USER_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    build_system_prompt,
)

logger = logging.getLogger(__name__)

WEB_DATA_DIR = Path(settings.CONFIG_DIR) / "web"
UPLOAD_DIR = WEB_DATA_DIR / "uploads"
REVIEW_HISTORY_FILE = WEB_DATA_DIR / "reviews.json"


def ensure_web_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================ 上传管理 ============================
_uploads: dict = {}
_uploads_lock = threading.Lock()


def save_upload(filename: str, data: bytes) -> dict:
    """保存上传文件，返回 {id, name, path, size}。"""
    ensure_web_dirs()
    file_id = uuid.uuid4().hex
    safe = Path(filename or f"upload_{file_id}").name or f"upload_{file_id}"
    dest = UPLOAD_DIR / f"{file_id}_{safe}"
    dest.write_bytes(data)
    info = {"id": file_id, "name": safe, "path": str(dest), "size": len(data)}
    with _uploads_lock:
        _uploads[file_id] = info
    logger.info("上传 %s -> %s（%d 字节）", filename, dest, len(data))
    return info


def get_upload(file_id: str) -> Optional[dict]:
    return _uploads.get(file_id)


def make_thumbnail(path: str, size=(400, 400)) -> str:
    """返回 data URL 形式的缩略图。"""
    b64 = load_image_base64_from_raw(path, thumb_size=size)
    return f"data:image/jpeg;base64,{b64}"


# ============================ LLM 客户端 ============================
def make_llm_client(profile_id: str = "", reasoning_cb=None) -> Optional[LLMClient]:
    api_key = settings.get_api_key(profile_id)
    if not api_key:
        return None
    if profile_id:
        profile = settings.get_api_profile(profile_id)
        if profile is None:
            return None
        api_base, model = profile.get("api_base", ""), profile.get("model", "")
    else:
        data = settings.load_settings()
        api_base, model = data.get("api_base", ""), data.get("model", "")
    data = settings.load_settings()
    return LLMClient(
        api_base=api_base,
        api_key=api_key,
        model=model,
        thinking=bool(data.get("thinking_enabled", True)),
        reasoning_cb=reasoning_cb,
    )


# ============================ 处理任务 ============================
@dataclass
class Task:
    id: str
    config: dict
    total: int = 0
    completed: int = 0
    state: str = "pending"  # pending / running / finished / cancelled
    file_status: dict = field(default_factory=dict)
    log_lines: list = field(default_factory=list)
    failed_ids: list = field(default_factory=list)
    processed: dict = field(default_factory=dict)  # file_id -> 输出路径
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, message: str) -> None:
        with self.lock:
            self.log_lines.append(message)

    def set_status(self, fid: str, status: str) -> None:
        with self.lock:
            self.file_status[fid] = status

    def mark_done(self, fid: str, success: bool,
                  message: str = "", output_path: str = "") -> None:
        with self.lock:
            self.completed += 1
            if success:
                self.file_status[fid] = "done"
                if output_path:
                    self.processed[fid] = output_path
            elif message == "已取消":
                self.file_status[fid] = "cancelled"
            else:
                self.file_status[fid] = "failed"
                self.failed_ids.append(fid)

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "state": self.state,
                "total": self.total,
                "completed": self.completed,
                "log_total": len(self.log_lines),
                "new_lines": self.log_lines[since:],
                "file_status": dict(self.file_status),
                "failed_ids": list(self.failed_ids),
                "processed": dict(self.processed),
            }


_tasks: dict = {}
_tasks_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-task")


def create_task(config: dict) -> Task:
    task = Task(id=uuid.uuid4().hex, config=config)
    with _tasks_lock:
        _tasks[task.id] = task
    return task


def get_task(task_id: str) -> Optional[Task]:
    return _tasks.get(task_id)


def run_task_async(task: Task) -> None:
    _executor.submit(_run_task, task)


def _run_task(task: Task) -> None:
    try:
        _do_run(task)
    except Exception as e:
        logger.exception("任务 %s 崩溃: %s", task.id, e)
        task.log(f"❌ 任务异常: {e}")
        with task.lock:
            task.state = "finished"
            for fid, st in task.file_status.items():
                if st in ("pending", "running"):
                    task.file_status[fid] = "failed"
                    if fid not in task.failed_ids:
                        task.failed_ids.append(fid)


def _do_run(task: Task) -> None:
    cfg = task.config
    target_ids = cfg.get("target_ids") or []
    groups_by_id = cfg.get("groups") or {}
    unify = bool(cfg.get("unify", False))

    # 路径 / id 双映射
    id_to_info = {}
    for fid in target_ids:
        info = get_upload(fid)
        if info:
            id_to_info[fid] = info
        else:
            task.log(f"⚠ 找不到上传文件 {fid}，跳过")
    if not id_to_info:
        task.log("❌ 没有可处理的文件")
        with task.lock:
            task.state = "finished"
        return

    groups_by_path = {}
    for gname, ids in groups_by_id.items():
        members = [id_to_info[i]["path"] for i in ids if i in id_to_info]
        if members:
            groups_by_path[gname] = members
    paths = [id_to_info[i]["path"] for i in id_to_info]
    path_to_id = {id_to_info[i]["path"]: i for i in id_to_info}

    batches = partition_targets(paths, groups_by_path, unify)
    task.total = len(paths)
    with task.lock:
        task.state = "running"
        for fid in id_to_info:
            task.file_status[fid] = "pending"

    task.log(f"开始处理：{len(paths)} 个文件，{len(batches)} 个批次")

    client = make_llm_client(cfg.get("profile_id", ""))
    if client is None:
        task.log("❌ 未配置可用的 API 配置方案")
        with task.lock:
            task.state = "finished"
            for fid in id_to_info:
                task.file_status[fid] = "failed"
                task.failed_ids.append(fid)
        return

    system_prompt = cfg.get("system_prompt") or build_system_prompt()
    user_prompt = cfg.get("user_prompt") or DEFAULT_USER_PROMPT
    parallel = max(1, int(cfg.get("parallel", 3)))

    with ThreadPoolExecutor(max_workers=parallel,
                            thread_name_prefix="file") as pool:
        futures = []
        for batch_name, batch_paths in batches:
            if batch_name is not None and len(batch_paths) > 1:
                futures.append(pool.submit(
                    _process_group, task, batch_name, batch_paths,
                    path_to_id, client, system_prompt, user_prompt, cfg))
            else:
                for p in batch_paths:
                    futures.append(pool.submit(
                        _process_single, task, p, path_to_id.get(p),
                        client, system_prompt, user_prompt, cfg))
        for f in futures:
            try:
                f.result()
            except Exception as e:
                logger.exception("批处理子任务异常: %s", e)

    with task.lock:
        task.state = "cancelled" if task.stop_event.is_set() else "finished"
    task.log("批处理已结束" if task.stop_event.is_set() else "全部处理完成")


def _render_and_run(path: str, params: dict, cfg: dict) -> str:
    output_dir = cfg.get("output_dir") or os.path.join(
        os.path.expanduser("~"), "photomagic_web_output")
    os.makedirs(output_dir, exist_ok=True)
    stem = Path(path).stem
    pp3_path = os.path.join(output_dir, f"{stem}.pp3")
    used = rt_processor.generate_pp3(
        params, pp3_path,
        output_profile=cfg.get("output_profile", "RT_sRGB"),
        strength=cfg.get("strength", 1.0),
    )
    rt_processor.run_rawtherapee(
        path, pp3_path, output_dir,
        output_format=cfg.get("output_format", "jpg"),
        jpeg_quality=cfg.get("jpeg_quality", 92),
        bit_depth=cfg.get("bit_depth", "16"),
    )
    fmt = cfg.get("output_format", "jpg")
    ext = {"jpg": "jpg", "tiff": "tif", "png": "png"}.get(fmt, "jpg")
    out = os.path.join(output_dir, f"{stem}.{ext}")
    return out if os.path.exists(out) else ""


def _process_single(task, path, fid, client, system_prompt, user_prompt, cfg):
    if fid:
        task.set_status(fid, "running")
    name = os.path.basename(path)
    task.log(f"▶ 正在处理: {name}")
    try:
        params = client.request_json(
            system_prompt, user_prompt, path,
            max_tokens=cfg.get("max_tokens", 1024),
            stop_event=task.stop_event,
        )
        task.log(f"  LLM 返回: {json.dumps(params, ensure_ascii=False)}")
        if task.stop_event.is_set():
            if fid:
                task.mark_done(fid, False, "已取消")
            return
        out = _render_and_run(path, params, cfg)
        if fid:
            task.mark_done(fid, True, output_path=out)
        task.log(f"✓ 完成: {name}")
    except InterruptedError:
        if fid:
            task.mark_done(fid, False, "已取消")
    except Exception as e:
        logger.exception("处理失败 %s", path)
        task.log(f"✗ 失败: {name} — {e}")
        if fid:
            task.mark_done(fid, False, str(e))


def _process_group(task, name, paths, path_to_id, client,
                   system_prompt, user_prompt, cfg):
    for p in paths:
        fid = path_to_id.get(p)
        if fid:
            task.set_status(fid, "running")
    task.log(f"▶ 组图 [{name}]: {len(paths)} 张")
    try:
        raw = client.request_json_multi(
            build_group_system_prompt(system_prompt),
            build_group_user_prompt(user_prompt, paths),
            paths,
            max_tokens=cfg.get("max_tokens", 1024),
            stop_event=task.stop_event,
        )
        style, per_image = normalize_group_response(raw, paths)
        task.log(f"  统一风格: {json.dumps(style, ensure_ascii=False)}")
        for p, extra in zip(paths, per_image):
            fid = path_to_id.get(p)
            if task.stop_event.is_set():
                if fid:
                    task.mark_done(fid, False, "已取消")
                continue
            params = merge_group_params(style, extra)
            try:
                out = _render_and_run(p, params, cfg)
                if fid:
                    task.mark_done(fid, True, output_path=out)
                task.log(f"  ✓ {os.path.basename(p)}")
            except Exception as e:
                logger.exception("组图处理失败 %s", p)
                task.log(f"  ✗ {os.path.basename(p)} — {e}")
                if fid:
                    task.mark_done(fid, False, str(e))
    except InterruptedError:
        for p in paths:
            fid = path_to_id.get(p)
            if fid:
                task.mark_done(fid, False, "已取消")
    except Exception as e:
        logger.exception("组图请求失败 %s", name)
        task.log(f"  ✗ 组图请求失败: {e}")
        for p in paths:
            fid = path_to_id.get(p)
            if fid:
                task.mark_done(fid, False, str(e))


# ============================ 照片评价 ============================
_review_executor = ThreadPoolExecutor(max_workers=2,
                                      thread_name_prefix="web-review")
_review_tasks: dict = {}
_review_lock = threading.Lock()


def load_review_history() -> list:
    if not REVIEW_HISTORY_FILE.exists():
        return []
    try:
        with open(REVIEW_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("读取评价历史失败: %s", e)
        return []


def save_review_history(history: list) -> None:
    REVIEW_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(REVIEW_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存评价历史失败: %s", e)


def start_review(image_path: str, description: str, profile_id: str = "") -> dict:
    task_id = uuid.uuid4().hex
    info = {"id": task_id, "state": "pending", "result": None, "error": ""}
    with _review_lock:
        _review_tasks[task_id] = info
    _review_executor.submit(_run_review, info, image_path, description, profile_id)
    return {"task_id": task_id}


def get_review_task(task_id: str):
    return _review_tasks.get(task_id)


def _run_review(info, path, description, profile_id):
    try:
        info["state"] = "running"
        client = make_llm_client(profile_id)
        if client is None:
            info["state"] = "error"
            info["error"] = "未配置可用的 API 配置方案"
            return
        user_prompt = "请评价这张照片。"
        if description:
            user_prompt += f"\n拍摄者的描述：{description}"
        result = client.request_json(
            REVIEW_SYSTEM_PROMPT, user_prompt, path, max_tokens=4096)
        info["state"] = "done"
        info["result"] = result
        info["image_path"] = path
        info["description"] = description

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "image_path": path,
            "description": description,
            "scores": result.get("scores", {}),
            "summary": result.get("summary", ""),
            "suggestions": result.get("suggestions", []),
        }
        history = load_review_history()
        history.insert(0, entry)
        save_review_history(history)
    except Exception as e:
        logger.exception("评价失败: %s", e)
        info["state"] = "error"
        info["error"] = str(e)


# ============================ 任务清理 ============================
def cleanup_stale_tasks(max_age_seconds: int = 3600) -> None:
    now = time.time()
    with _tasks_lock:
        for tid in list(_tasks):
            if _tasks[tid].state in ("finished", "cancelled"):
                # 简化：不记录时间戳，直接跳过
                pass