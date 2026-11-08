"""PhotoMagic Web 服务包。

复用项目根目录下的无 GUI 依赖模块（settings / rt_processor /
llm_handler / grouping / log_setup），不引入 PyQt6。
"""

import sys
from pathlib import Path

# 获取当前文件所在目录（web/）的父目录（项目根目录）
ROOT_DIR = Path(__file__).resolve().parent.parent

# 如果根目录不在 sys.path 中，将其插入到最前面
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))