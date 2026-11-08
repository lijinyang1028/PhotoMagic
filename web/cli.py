import argparse
import subprocess
import sys
import os
import shutil
import time
from pathlib import Path

# 确保可以导入根目录
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from web import db
from web.services import WEB_DATA_DIR

def cmd_start(args):
    print(f"🚀 启动 PhotoMagic Web 服务 (host={args.host}, port={args.port})...")
    os.environ["PHOTOMAGIC_WEB_HOST"] = args.host
    os.environ["PHOTOMAGIC_WEB_PORT"] = str(args.port)
    try:
        import uvicorn
        from web.server import app
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        print("\n🛑 服务已停止。")

def cmd_clean(args):
    print("🧹 正在清理服务器缓存...")
    uploads_cleaned = 0
    if WEB_DATA_DIR.exists():
        for f in WEB_DATA_DIR.iterdir():
            try:
                if f.is_file(): f.unlink(); uploads_cleaned += 1
                elif f.is_dir(): shutil.rmtree(f); uploads_cleaned += 1
            except Exception as e:
                print(f"  [跳过] {f.name}: {e}")
    print(f"  - 清理了 {uploads_cleaned} 个临时上传文件/目录。")
    
    out_dir = Path(os.path.expanduser("~")) / "photomagic_web_output"
    if out_dir.exists() and args.all:
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"  - 已彻底删除处理输出目录 {out_dir}")

def cmd_users(args):
    print(f"{'User ID':<40} | {'Review Count':<15}")
    print("-" * 60)
    users = db.get_all_users()
    if not users:
        print("  (无数据)")
    for u in users:
        print(f"{u['user_id']:<40} | {u.get('review_count', 0):<15}")

def main():
    parser = argparse.ArgumentParser(description="PhotoMagic Web 管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_start = subparsers.add_parser("start", help="启动 Web 服务")
    p_start.add_argument("--host", default="0.0.0.0", help="监听地址 (Linux服务器建议0.0.0.0)")
    p_start.add_argument("--port", type=int, default=8000, help="监听端口")
    p_start.set_defaults(func=cmd_start)

    p_clean = subparsers.add_parser("clean", help="清理临时上传文件")
    p_clean.add_argument("--all", action="store_true", help="同时清理所有处理后的输出文件")
    p_clean.set_defaults(func=cmd_clean)

    p_users = subparsers.add_parser("users", help="查看多用户使用统计")
    p_users.set_defaults(func=cmd_users)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()