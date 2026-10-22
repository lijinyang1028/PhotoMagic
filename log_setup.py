"""
PhotoMagic 日志配置

设计:
- 日志目录：~/.photomagic/logs/（与 settings.json 同处 ~/.photomagic 之下，唯一位置）
- 每次启动生成一个独立的日志文件：photomagic_YYYYmmdd_HHMMSS_<pid>.log
- 只保留最近 5 次运行的日志文件，更早的自动删除
- 落盘级别：INFO（INFO / WARNING / ERROR / CRITICAL 全部写入）
- 目录不可写时（只读安装目录、被沙箱限制等）记录 ERROR 并退化为只输出到控制台；
  不会改写到程序目录或临时目录——打包分发后那些位置用户找不到、也会被清理
- 同时兜住未捕获异常（主线程与子线程），保证崩溃前留下现场

其它模块只需要 `logger = logging.getLogger(__name__)`，
日志系统的初始化统一由 main.py 在程序入口调用 setup_logging() 完成。
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from settings import LOG_DIR

# ---------------- 常量 ----------------
LOG_PREFIX = "photomagic"
LOG_SUFFIX = ".log"
MAX_LOG_FILES = 5                      # 只保留最近 5 次运行
LOG_FORMAT = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False
_current_log_file = None
_current_log_dir = None
_lock = threading.RLock()

logger = logging.getLogger(__name__)


# ---------------- 文件管理 ----------------
def _resolve_dir(log_dir=None) -> Path:
    return Path(log_dir) if log_dir else Path(LOG_DIR)


def list_log_files(log_dir=None):
    """按修改时间从新到旧返回日志文件列表。"""
    directory = _resolve_dir(log_dir)
    if not directory.is_dir():
        return []
    files = []
    for p in directory.glob(f"{LOG_PREFIX}_*{LOG_SUFFIX}"):
        try:
            if p.is_file():
                files.append((p.stat().st_mtime, p.name, p))
        except OSError:
            continue
    files.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in files]


def prune_log_files(keep: int = MAX_LOG_FILES, log_dir=None):
    """只保留最近 keep 个日志文件，返回被删除的路径列表。"""
    try:
        keep = max(1, int(keep))
    except (TypeError, ValueError):
        keep = MAX_LOG_FILES

    removed = []
    for old in list_log_files(log_dir)[keep:]:
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            continue
    return removed


def new_log_file(log_dir=None) -> Path:
    """为本次运行创建一个不会重名的日志文件路径（不写入内容）。"""
    directory = _resolve_dir(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pid = os.getpid()
    for index in range(100):
        suffix = "" if index == 0 else f"_{index}"
        candidate = directory / f"{LOG_PREFIX}_{stamp}_{pid}{suffix}{LOG_SUFFIX}"
        if not candidate.exists():
            return candidate
    # 极端情况下退化为带微秒的时间戳
    return directory / (
        f"{LOG_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        f"_{pid}{LOG_SUFFIX}")


def get_log_file():
    """返回本次运行的日志文件路径；未初始化时返回 None。"""
    return _current_log_file


def get_log_dir() -> Path:
    """返回本次实际使用的日志目录（未初始化时返回默认目录）。"""
    return Path(_current_log_dir) if _current_log_dir else _resolve_dir(None)


# ---------------- 未捕获异常 ----------------
def _install_excepthooks():
    if getattr(sys, "_photomagic_excepthooks", False):
        return
    sys._photomagic_excepthooks = True

    previous_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("photomagic.crash").critical(
            "未捕获异常，程序即将退出", exc_info=(exc_type, exc_value, exc_tb))
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            if args.exc_type is SystemExit:
                return
            thread_name = getattr(args.thread, "name", "unknown")
            logging.getLogger("photomagic.crash").critical(
                "线程未捕获异常 [%s]", thread_name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

        threading.excepthook = _thread_hook


# ---------------- 初始化 ----------------
def _remove_own_handlers(root: logging.Logger):
    for handler in list(root.handlers):
        if getattr(handler, "_photomagic_handler", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _shutdown():
    if _configured:
        logger.info("日志系统关闭，本次日志：%s", _current_log_file)
    logging.shutdown()


def setup_logging(level: int = logging.INFO, log_dir=None,
                  keep: int = MAX_LOG_FILES, console: bool = True,
                  force: bool = False):
    """
    初始化日志系统（幂等）。

    - level：落盘级别，默认 INFO
    - log_dir：日志目录，默认 ~/.photomagic/logs
    - keep：保留最近多少次运行的日志，默认 5
    - console：是否同时输出到 stderr（PyInstaller 无控制台时自动跳过）
    - force：强制重新初始化（会换一个新的日志文件）

    返回本次运行的日志文件路径；目录不可写时返回 None
    （此时日志只输出到控制台，并在控制台留下 ERROR 说明原因）。
    """
    global _configured, _current_log_file, _current_log_dir

    with _lock:
        if _configured and not force:
            return _current_log_file

        formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
        root = logging.getLogger()
        root.setLevel(level)

        _remove_own_handlers(root)

        directory = _resolve_dir(log_dir)
        path = None
        file_handler = None
        file_error = None
        try:
            path = new_log_file(directory)
            file_handler = logging.FileHandler(
                path, mode="a", encoding="utf-8", delay=False)
        except OSError as e:
            # 目录不可写时退化为纯控制台日志：日志问题绝不能拖垮程序
            path = None
            file_handler = None
            file_error = e

        if file_handler is not None:
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler._photomagic_handler = True
            root.addHandler(file_handler)

        if console and sys.stderr is not None:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setLevel(level)
            stream_handler.setFormatter(formatter)
            stream_handler._photomagic_handler = True
            root.addHandler(stream_handler)

        _install_excepthooks()
        _configured = True
        _current_log_file = path
        _current_log_dir = directory

    removed = prune_log_files(keep, directory)
    logger.info(
        "===== PhotoMagic 日志启动 ===== 文件: %s | 级别: %s | 保留最近 %d 次",
        path or "（未落盘）", logging.getLevelName(level), keep)
    if removed:
        logger.info("清理旧日志 %d 个: %s", len(removed),
                    ", ".join(p.name for p in removed))
    if file_error is not None:
        logger.error(
            "日志目录不可写，本次日志不会落盘，仅输出到控制台: %s（%s）",
            directory, file_error)
    return path


atexit.register(_shutdown)
