"""设置页面：API 配置 / 自定义参数 / 存储信息"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QGroupBox, QFormLayout, QFileDialog, QMessageBox, QScrollArea, QFrame
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt, pyqtSignal

import settings
from rt_processor import (
    get_params, get_builtin_param_names,
    load_custom_params, save_custom_params,
    clear_custom_params,
)


class SettingsPage(QWidget):
    """设置页：修改 API 配置、管理自定义参数。"""

    # 保存 API 配置后发出，供其他页面刷新
    api_config_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.is_dark = True
        self._api_key_visible = False
        self.init_ui()
        self.load_from_settings()

    # ---------------- UI ----------------
    def init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("设置")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        subtitle = QLabel(
            f"配置文件目录：{settings.CONFIG_DIR}\n"
            "API Key 保存在系统钥匙串，其余配置写入 settings.json。"
        )
        subtitle.setObjectName("settingsSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # ---------- LLM 接口 ----------
        llm_group = QGroupBox("LLM 接口配置")
        llm_form = QFormLayout(llm_group)

        self.api_base_edit = QLineEdit()
        self.api_base_edit.setPlaceholderText("https://api.openai.com/v1")
        llm_form.addRow("API Base URL:", self.api_base_edit)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("gpt-4o")
        llm_form.addRow("Model:", self.model_edit)

        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        key_row.addWidget(self.api_key_edit, 1)

        self.btn_toggle_key = QPushButton("显示")
        self.btn_toggle_key.setFixedWidth(56)
        self.btn_toggle_key.clicked.connect(self._toggle_key_visible)
        key_row.addWidget(self.btn_toggle_key)
        llm_form.addRow("API Key:", key_row)

        self.keyring_status = QLabel("")
        self.keyring_status.setWordWrap(True)
        self.keyring_status.setObjectName("keyringStatus")
        llm_form.addRow("", self.keyring_status)

        btn_row = QHBoxLayout()
        self.btn_save_llm = QPushButton("保存")
        self.btn_save_llm.clicked.connect(self.save_all)
        btn_row.addWidget(self.btn_save_llm)

        self.btn_clear_key = QPushButton("清除 API Key")
        self.btn_clear_key.clicked.connect(self.clear_api_key)
        btn_row.addWidget(self.btn_clear_key)
        btn_row.addStretch()
        llm_form.addRow("", btn_row)

        layout.addWidget(llm_group)

        # ---------- 自定义参数 ----------
        custom_group = QGroupBox("自定义参数")
        custom_layout = QVBoxLayout(custom_group)

        self.custom_status = QLabel("")
        self.custom_status.setWordWrap(True)
        custom_layout.addWidget(self.custom_status)

        custom_path_label = QLabel(
            f"存储位置：{settings.CUSTOM_PARAMS_FILE}")
        custom_path_label.setObjectName("settingsHint")
        custom_path_label.setWordWrap(True)
        custom_layout.addWidget(custom_path_label)

        custom_btn_row = QHBoxLayout()
        self.btn_import = QPushButton("导入 JSON")
        self.btn_import.clicked.connect(self.import_custom_params)
        custom_btn_row.addWidget(self.btn_import)

        self.btn_clear_custom = QPushButton("清空自定义参数")
        self.btn_clear_custom.clicked.connect(self.clear_custom)
        custom_btn_row.addWidget(self.btn_clear_custom)

        custom_btn_row.addStretch()
        custom_layout.addLayout(custom_btn_row)

        layout.addWidget(custom_group)

        # ---------- 状态栏 ----------
        self.status_label = QLabel("")
        self.status_label.setObjectName("settingsStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch()

    # ---------------- 状态 ----------------
    def set_dark(self, is_dark):
        self.is_dark = is_dark

    def load_from_settings(self):
        s = settings.load_settings()
        self.api_base_edit.setText(s.get("api_base", ""))
        self.model_edit.setText(s.get("model", ""))
        self.api_key_edit.setText(settings.get_api_key())
        self._refresh_keyring_status()
        self._refresh_custom_status()

    def _refresh_keyring_status(self):
        if settings.keyring_available():
            self.keyring_status.setText(
                "✓ 系统钥匙串可用，API Key 会安全保存。")
        else:
            self.keyring_status.setText(
                "⚠ 未检测到系统钥匙串。API Key 不会被持久化，"
                "重启后需要重新输入。\n"
                "  安装 keyring 后可启用持久化：pip install keyring")

    def _refresh_custom_status(self):
        try:
            builtin = get_builtin_param_names()
            current = get_params()
            custom_count = sum(1 for k in current if k not in builtin)
        except Exception:
            custom_count = 0
        if custom_count:
            self.custom_status.setText(f"已加载 {custom_count} 个自定义参数。")
        else:
            self.custom_status.setText("当前没有自定义参数。")

    def _toggle_key_visible(self):
        self._api_key_visible = not self._api_key_visible
        if self._api_key_visible:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_toggle_key.setText("隐藏")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_toggle_key.setText("显示")

    # ---------------- 保存 / 清除 ----------------
    def save_all(self):
        s = settings.load_settings()
        s["api_base"] = self.api_base_edit.text().strip()
        s["model"] = self.model_edit.text().strip()
        try:
            settings.save_settings(s)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"无法写入配置文件：{e}")
            return

        api_key = self.api_key_edit.text().strip()
        ok = settings.set_api_key(api_key)
        if api_key and not ok:
            self.status_label.setText(
                "已保存接口配置，但 API Key 未能写入钥匙串（仅本次会话有效）。")
        else:
            self.status_label.setText("已保存。")
        self._refresh_keyring_status()
        self.api_config_changed.emit()

    def clear_api_key(self):
        if QMessageBox.question(
                self, "确认", "从系统钥匙串删除 API Key？"
        ) != QMessageBox.StandardButton.Yes:
            return
        settings.delete_api_key()
        self.api_key_edit.clear()
        self.status_label.setText("已清除 API Key。")
        self.api_config_changed.emit()

    # ---------------- 自定义参数 ----------------
    def import_custom_params(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择参数定义 JSON", "", "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        count, errors = load_custom_params(path)
        if count == 0:
            QMessageBox.warning(
                self, "导入失败",
                "没有有效的参数被导入。\n\n" + "\n".join(errors[:5]))
            self.status_label.setText(
                f"导入失败：{errors[0] if errors else '未知错误'}")
            return
        try:
            save_custom_params()
        except Exception as e:
            self.status_label.setText(f"导入成功，但持久化失败：{e}")
        else:
            if errors:
                self.status_label.setText(
                    f"已导入 {count} 个参数（{len(errors)} 个被跳过，"
                    f"首个错误：{errors[0]}）")
            else:
                self.status_label.setText(f"已导入 {count} 个参数。")
        self._refresh_custom_status()

    def clear_custom(self):
        if QMessageBox.question(
                self, "确认", "移除所有已导入的自定义参数？"
        ) != QMessageBox.StandardButton.Yes:
            return
        clear_custom_params()
        self.status_label.setText("已清空自定义参数。")
        self._refresh_custom_status()