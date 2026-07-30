"""
主程序 GUI - PyQt6
功能：选择 RAW 照片 -> 配置 LLM -> 批量处理
"""
import sys
import os
import json
import threading
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QLineEdit, QFileDialog,
    QListWidget, QMessageBox, QGroupBox, QFormLayout, QSpinBox,
    QProgressBar, QCheckBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from llm_handler import LLMClient
from rt_processor import generate_pp3, run_rawtherapee, check_rt_cli
import rawpy

DEFAULT_SYSTEM_PROMPT = """你是一个专业的摄影后期处理顾问。请仔细观察我发给你的照片，根据画面内容、光线、构图等，给出最佳的RawTherapee后期参数建议。

请严格返回一个JSON对象，不要包含任何其他文字。JSON应包含以下可选键（数值）：
- "Exposure": 曝光补偿，范围 -3.0 到 3.0
- "Contrast": 对比度，范围 -100 到 100
- "Saturation": 饱和度，范围 -100 到 100
- "Highlights": 高光恢复，范围 0 到 100
- "Shadows": 阴影提亮，范围 0 到 100
- "WhiteBalance": 对象，包含 "Temperature" (开尔文，例如5500) 和 "Tint" (色调，-5.0到5.0)

若某参数无需调整，可省略该键。"""

DEFAULT_USER_PROMPT = "请为这张照片建议最佳后期参数。"

class ProcessingThread(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal()

    def __init__(self, file_list, output_dir, llm_client, system_prompt, user_prompt):
        super().__init__()
        self.file_list = file_list
        self.output_dir = output_dir
        self.llm_client = llm_client
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt

    def run(self):
        total = len(self.file_list)
        for idx, raw_path in enumerate(self.file_list):
            self.log.emit(f"正在处理: {os.path.basename(raw_path)}")
            try:
                # 1. 获取 LLM 处理参数
                params = self.llm_client.request_json(
                    self.system_prompt, self.user_prompt, raw_path)
                self.log.emit(f"  LLM 返回参数: {json.dumps(params, ensure_ascii=False)}")

                # 2. 生成 .pp3 文件
                pp3_path = os.path.join(self.output_dir, f"{Path(raw_path).stem}.pp3")
                generate_pp3(params, pp3_path)

                # 3. 调用 RawTherapee 处理
                run_rawtherapee(raw_path, pp3_path, self.output_dir)
                self.log.emit(f"  已完成: {Path(raw_path).stem}.jpg")

            except Exception as e:
                self.log.emit(f"  错误: {str(e)}")

            # 更新进度
            self.progress.emit(int((idx + 1) / total * 100))

        self.log.emit("全部处理完成！")
        self.finished.emit()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RAW 照片 AI 处理助手 (基于 RawTherapee)")
        self.resize(900, 700)

        # 默认输出目录
        self.default_output = os.path.join(os.path.expanduser("~"), "rawtherapee_output")
        os.makedirs(self.default_output, exist_ok=True)

        self.file_list = []      # 实际 RAW 文件路径列表
        self.output_dir = self.default_output
        self.llm_client = None
        self.worker = None

        self.init_ui()
        self.check_dependencies()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # 1. 文件选择区域
        file_group = QGroupBox("1. 选择 RAW 照片")
        f_layout = QVBoxLayout(file_group)
        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("添加文件")
        self.btn_add.clicked.connect(self.add_files)
        self.btn_clear = QPushButton("清空列表")
        self.btn_clear.clicked.connect(self.clear_files)
        btn_layout.addWidget(self.btn_add)
        btn_layout.addWidget(self.btn_clear)
        btn_layout.addStretch()
        f_layout.addLayout(btn_layout)
        self.file_list_widget = QListWidget()
        f_layout.addWidget(self.file_list_widget)
        layout.addWidget(file_group)

        # 2. LLM 配置区域
        llm_group = QGroupBox("2. LLM 配置 (OpenAI 兼容接口)")
        llm_layout = QFormLayout(llm_group)
        self.api_base_edit = QLineEdit("https://api.openai.com/v1")
        llm_layout.addRow("API Base URL:", self.api_base_edit)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        llm_layout.addRow("API Key:", self.api_key_edit)
        self.model_edit = QLineEdit("gpt-4o")
        llm_layout.addRow("Model:", self.model_edit)
        layout.addWidget(llm_group)

        # 3. Prompt 区域
        prompt_group = QGroupBox("3. 提示词设置")
        prompt_layout = QFormLayout(prompt_group)
        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self.system_prompt_edit.setMaximumHeight(160)
        prompt_layout.addRow("System Prompt:", self.system_prompt_edit)
        self.user_prompt_edit = QLineEdit(DEFAULT_USER_PROMPT)
        prompt_layout.addRow("User Prompt:", self.user_prompt_edit)
        layout.addWidget(prompt_group)

        # 4. 输出设置
        out_group = QGroupBox("4. 输出设置")
        out_layout = QHBoxLayout(out_group)
        out_layout.addWidget(QLabel("输出目录:"))
        self.out_dir_edit = QLineEdit(self.output_dir)
        out_layout.addWidget(self.out_dir_edit)
        self.btn_browse = QPushButton("浏览")
        self.btn_browse.clicked.connect(self.browse_output)
        out_layout.addWidget(self.btn_browse)
        layout.addWidget(out_group)

        # 5. 操作按钮与进度
        action_layout = QHBoxLayout()
        self.btn_process = QPushButton("开始处理")
        self.btn_process.clicked.connect(self.start_processing)
        action_layout.addWidget(self.btn_process)
        self.progress_bar = QProgressBar()
        action_layout.addWidget(self.progress_bar)
        layout.addLayout(action_layout)

        # 6. 日志区域
        log_group = QGroupBox("处理日志")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group)

    def check_dependencies(self):
        """检查 RawTherapee 是否已安装"""
        if not check_rt_cli():
            QMessageBox.warning(self, "依赖缺失",
                "未检测到 rawtherapee-cli，请安装 RawTherapee 并将其路径添加到环境变量。")

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择 RAW 文件", "",
            "RAW 文件 (*.CR2 *.NEF *.ARW *.DNG *.ORF *.RAF *.RW2 *.PEF *.raw *.3fr *.bay "
            "*.cap *.dcs *.dcr *.drf *.eip *.erf *.fff *.iiq *.k25 *.kdc *.mdc *.mef *.mos "
            "*.mrw *.nrw *.pef *.ptx *.pxn *.r3d *.raf *.raw *.rw2 *.rwl *.rwz *.srf *.srw "
            "*.x3f);;所有文件 (*)"
        )
        if files:
            for f in files:
                if f not in self.file_list:
                    self.file_list.append(f)
                    self.file_list_widget.addItem(f)

    def clear_files(self):
        self.file_list.clear()
        self.file_list_widget.clear()

    def browse_output(self):
        dir = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if dir:
            self.output_dir = dir
            self.out_dir_edit.setText(dir)

    def start_processing(self):
        if not self.file_list:
            QMessageBox.information(self, "提示", "请先添加 RAW 文件。")
            return
        if not self.api_key_edit.text().strip():
            QMessageBox.information(self, "提示", "请输入 API Key。")
            return

        self.output_dir = self.out_dir_edit.text().strip()
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

        # 创建 LLM 客户端
        self.llm_client = LLMClient(
            api_base=self.api_base_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            model=self.model_edit.text().strip()
        )

        self.log_edit.clear()
        self.btn_process.setEnabled(False)

        # 开启处理线程
        self.worker = ProcessingThread(
            file_list=self.file_list.copy(),
            output_dir=self.output_dir,
            llm_client=self.llm_client,
            system_prompt=self.system_prompt_edit.toPlainText(),
            user_prompt=self.user_prompt_edit.text()
        )
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_processing_finished)
        self.worker.start()

    def append_log(self, msg):
        self.log_edit.append(msg)

    def on_processing_finished(self):
        self.btn_process.setEnabled(True)
        self.worker = None

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()