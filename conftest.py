# 让 test/ 下的测试能够 import 仓库根目录的模块（rt_processor / llm_handler / main），
# 无需把项目做 pip 安装。pytest 会自动把本文件所在目录加入 sys.path。
