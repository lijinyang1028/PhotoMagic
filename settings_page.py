"""设置页面：多套 API 配置方案 / RawTherapee 路径 / 自定义参数 / 存储信息"""
import logging
import os
import shutil
import sys

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QGroupBox, QFormLayout, QFileDialog, QMessageBox, QScrollArea, QFrame,
    QComboBox, QInputDialog,
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt, pyqtSignal

import settings
import log_setup
from rt_processor import (
    get_params, get_builtin_param_names,
    load_custom_params, save_custom_params,
    clear_custom_params,
    normalize_rt_cli_path, rt_cli_file_candidates, is_rt_cli_executable,
    get_rt_cli_version,
)


logger = logging.getLogger(__name__)


def _rt_cli_name_hint() -> str:
    """当前系统上 rawtherapee-cli 的可执行文件名。"""
    return "rawtherapee-cli.exe" if sys.platform.startswith("win") else "rawtherapee-cli"


def _rt_cli_example_path() -> str:
    """按系统给出一个示例路径，用于输入框占位提示。"""
    if sys.platform.startswith("win"):
        return r"C:\Program Files\RawTherapee\rawtherapee-cli.exe"
    if sys.platform == "darwin":
        return "/Applications/RawTherapee.app/Contents/MacOS/rawtherapee-cli"
    return "/usr/bin/rawtherapee-cli"


def _rt_cli_dialog_filter() -> str:
    """文件选择框的过滤器（各系统可执行文件命名不同）。"""
    if sys.platform.startswith("win"):
        return ("rawtherapee-cli (rawtherapee-cli.exe);;"
                "可执行文件 (*.exe);;所有文件 (*)")
    if sys.platform == "darwin":
        return "rawtherapee-cli (rawtherapee-cli*);;所有文件 (*)"
    return "rawtherapee-cli (rawtherapee-cli);;所有文件 (*)"


def _log_status_text() -> str:
    """显示本次日志落盘情况；不可写时明确提示，不假装写到别处。"""
    log_file = log_setup.get_log_file()
    if log_file is not None:
        return f"本次日志文件：{log_file}"
    return (f"⚠ 本次日志未落盘：目录 {settings.LOG_DIR} 不可写，"
            "日志仅输出到控制台。")


def _profile_label(profile: dict) -> str:
    """下拉框里的显示文本：名称 · 模型。"""
    name = (profile.get("name") or "未命名").strip()
    model = (profile.get("model") or "").strip() or "未填写模型"
    return f"{name}  ·  {model}"


# 还没有记忆任何 RT 版本时下拉框里的占位项
_RT_EMPTY_VERSION_LABEL = "（尚未保存版本 · 自动从 PATH 查找）"


def _rt_version_label(version: dict) -> str:
    """RT 版本下拉框的显示文本：名称 · 可执行文件名。"""
    name = (version.get("name") or "未命名版本").strip()
    path = (version.get("path") or "").strip()
    if not path:
        return f"{name}  ·  自动从 PATH 查找"
    return f"{name}  ·  {os.path.basename(path) or path}"


class SettingsPage(QWidget):
    """设置页：管理多套 API 配置方案、多个 RawTherapee 版本、自定义参数。"""

    # 保存 / 切换 / 清除 API 配置后发出，供其他页面刷新
    api_config_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.is_dark = True
        self._api_key_visible = False
        self._loading_profile = False
        self._current_profile_id = ""
        self._loading_rt_version = False
        self._current_rt_version_id = ""
        self.init_ui()
        self.load_from_settings()
        logger.info("设置页初始化完成：配置目录 %s，钥匙串 %s",
                    settings.CONFIG_DIR,
                    "可用" if settings.keyring_available() else "不可用")

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
            "可保存多套 API 配置方案（名称 / 接口地址 / 模型）；"
            "每套方案的 API Key 分别保存在系统钥匙串，其余配置写入 settings.json。\n"
            "可同时记忆多个 RawTherapee 版本（名称 / rawtherapee-cli 路径），"
            "随时切换当前使用的版本。\n"
            f"日志目录：{settings.LOG_DIR}（每次运行一个文件，保留最近 5 次）"
        )
        subtitle.setObjectName("settingsSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # 实际日志文件可能与默认目录不同（默认目录不可写时会自动退回）
        self.log_status = QLabel(_log_status_text())
        self.log_status.setObjectName("settingsHint")
        self.log_status.setWordWrap(True)
        layout.addWidget(self.log_status)

        # ---------- LLM 接口 ----------
        llm_group = QGroupBox("LLM 接口配置")
        llm_form = QFormLayout(llm_group)

        # 配置方案：下拉选择即切换当前方案
        profile_row = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(280)
        self.profile_combo.currentIndexChanged.connect(
            self._on_profile_changed)
        profile_row.addWidget(self.profile_combo, 1)

        self.btn_new_profile = QPushButton("新建")
        self.btn_new_profile.clicked.connect(self.create_profile)
        profile_row.addWidget(self.btn_new_profile)

        self.btn_rename_profile = QPushButton("重命名")
        self.btn_rename_profile.clicked.connect(self.rename_profile)
        profile_row.addWidget(self.btn_rename_profile)

        self.btn_delete_profile = QPushButton("删除")
        self.btn_delete_profile.clicked.connect(self.delete_profile)
        profile_row.addWidget(self.btn_delete_profile)
        llm_form.addRow("配置方案:", profile_row)

        self.profile_name_edit = QLineEdit()
        self.profile_name_edit.setPlaceholderText(
            "给这套配置起个名字，例如「公司 GPT-4o」")
        llm_form.addRow("名称:", self.profile_name_edit)

        profile_hint = QLabel(
            "每套方案分别保存名称、API Base URL、Model 和 API Key；"
            "切换方案后，「处理」页与「照片评价」页会立即使用当前方案。")
        profile_hint.setObjectName("settingsHint")
        profile_hint.setWordWrap(True)
        llm_form.addRow("", profile_hint)

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
        self.btn_save_llm = QPushButton("保存当前配置")
        self.btn_save_llm.clicked.connect(self.save_all)
        btn_row.addWidget(self.btn_save_llm)

        self.btn_clear_key = QPushButton("清除 API Key")
        self.btn_clear_key.clicked.connect(self.clear_api_key)
        btn_row.addWidget(self.btn_clear_key)
        btn_row.addStretch()
        llm_form.addRow("", btn_row)

        layout.addWidget(llm_group)

        # ---------- RawTherapee（多版本） ----------
        rt_group = QGroupBox("RawTherapee（可记忆多个版本）")
        rt_form = QFormLayout(rt_group)

        # 版本下拉：选择即切换当前使用的版本
        rt_version_row = QHBoxLayout()
        self.rt_version_combo = QComboBox()
        self.rt_version_combo.setMinimumWidth(280)
        self.rt_version_combo.currentIndexChanged.connect(
            self._on_rt_version_changed)
        rt_version_row.addWidget(self.rt_version_combo, 1)

        self.btn_new_rt_version = QPushButton("新建")
        self.btn_new_rt_version.clicked.connect(self.create_rt_version)
        rt_version_row.addWidget(self.btn_new_rt_version)

        self.btn_rename_rt_version = QPushButton("重命名")
        self.btn_rename_rt_version.clicked.connect(self.rename_rt_version)
        rt_version_row.addWidget(self.btn_rename_rt_version)

        self.btn_delete_rt_version = QPushButton("删除")
        self.btn_delete_rt_version.clicked.connect(self.delete_rt_version)
        rt_version_row.addWidget(self.btn_delete_rt_version)
        rt_form.addRow("RT 版本:", rt_version_row)

        self.rt_name_edit = QLineEdit()
        self.rt_name_edit.setPlaceholderText(
            "给这个版本起个名字，例如「5.9 稳定版」「5.10 开发版」")
        rt_form.addRow("版本名称:", self.rt_name_edit)

        rt_path_row = QHBoxLayout()
        self.rt_path_edit = QLineEdit()
        self.rt_path_edit.setPlaceholderText(
            "留空则该版本自动从系统 PATH 查找；也可手动指定，例如 "
            + _rt_cli_example_path())
        rt_path_row.addWidget(self.rt_path_edit, 1)

        self.btn_browse_rt = QPushButton("浏览…")
        self.btn_browse_rt.clicked.connect(self.browse_rt_cli)
        rt_path_row.addWidget(self.btn_browse_rt)

        self.btn_detect_rt = QPushButton("检测版本")
        self.btn_detect_rt.clicked.connect(self.detect_rt_cli_version)
        rt_path_row.addWidget(self.btn_detect_rt)
        rt_form.addRow("rawtherapee-cli 路径:", rt_path_row)

        rt_hint = QLabel(
            "可同时保存多个不同版本的 RawTherapee（每个版本一条路径），"
            "用下拉框切换后，「处理」页会立即使用当前版本的 rawtherapee-cli。")
        rt_hint.setObjectName("settingsHint")
        rt_hint.setWordWrap(True)
        rt_form.addRow("", rt_hint)

        self.rt_status = QLabel("")
        self.rt_status.setObjectName("settingsHint")
        self.rt_status.setWordWrap(True)
        rt_form.addRow("", self.rt_status)

        rt_btn_row = QHBoxLayout()
        self.btn_save_rt = QPushButton("保存当前版本")
        self.btn_save_rt.clicked.connect(self.save_rt_version)
        rt_btn_row.addWidget(self.btn_save_rt)

        self.btn_autodetect_rt = QPushButton("自动检测")
        self.btn_autodetect_rt.clicked.connect(self.autodetect_rt_cli)
        rt_btn_row.addWidget(self.btn_autodetect_rt)
        rt_btn_row.addStretch()
        rt_form.addRow("", rt_btn_row)

        layout.addWidget(rt_group)

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
        self._reload_profiles()
        self._refresh_keyring_status()
        self._refresh_custom_status()
        self._reload_rt_versions()
        self._refresh_rt_status()

    # ---------------- RawTherapee：多版本 ----------------
    def _normalize_rt_cli_path(self, raw: str):
        """整理用户输入；目录会自动补上可执行文件名。返回 (路径, 错误信息)。"""
        path = normalize_rt_cli_path(raw)
        if not path:
            return "", ""
        if os.path.isdir(path):
            for candidate in rt_cli_file_candidates(path):
                if is_rt_cli_executable(candidate):
                    return os.path.abspath(candidate), ""
            message = ("指定的是目录，但目录中没有找到 rawtherapee-cli"
                       f"（{_rt_cli_name_hint()}）。")
            if path.rstrip("/\\").endswith(".app"):
                message += "\n应包含 Contents/MacOS/rawtherapee-cli。"
            return path, message
        if not is_rt_cli_executable(path):
            if os.path.isfile(path):
                return path, (
                    "指定的文件不可执行。Linux/macOS 上需要可执行权限，"
                    "可运行 chmod +x 后重试。")
            # 兼容 bash 习惯：只输入命令名（如 rawtherapee-cli）时按 PATH 查找
            if os.sep not in path and "/" not in path:
                found = shutil.which(path)
                if found:
                    return os.path.abspath(found), ""
            return path, "指定的文件不存在。"
        return os.path.abspath(path), ""

    def _reload_rt_versions(self, select_id: str = ""):
        """重建 RT 版本下拉框，并把选中版本载入表单。"""
        versions = settings.list_rt_versions()
        active_id = select_id or settings.get_active_rt_version_id()
        self._loading_rt_version = True
        try:
            self.rt_version_combo.clear()
            for version in versions:
                self.rt_version_combo.addItem(_rt_version_label(version),
                                              version["id"])
                self.rt_version_combo.setItemData(
                    self.rt_version_combo.count() - 1,
                    version.get("path", ""),
                    Qt.ItemDataRole.ToolTipRole)

            if not versions:
                self.rt_version_combo.addItem(_RT_EMPTY_VERSION_LABEL, "")

            index = self.rt_version_combo.findData(active_id)
            if index < 0:
                index = 0
                active_id = self.rt_version_combo.itemData(0) or ""
            if index >= 0:
                self.rt_version_combo.setCurrentIndex(index)

            version = settings.get_rt_version(active_id) if active_id else None
            if version is not None:
                self._load_rt_version_form(version)
            else:
                self._current_rt_version_id = ""
                self.rt_name_edit.clear()
                self.rt_path_edit.clear()
        finally:
            self._loading_rt_version = False
        logger.info("已载入 %d 个 RT 版本，当前版本 id=%s",
                    len(versions), self._current_rt_version_id)

    def _load_rt_version_form(self, version: dict):
        """把一条 RT 版本填入表单。"""
        self._loading_rt_version = True
        try:
            self.rt_name_edit.setText(version.get("name", ""))
            self.rt_path_edit.setText(
                normalize_rt_cli_path(version.get("path", "")))
            self._current_rt_version_id = version["id"]
        finally:
            self._loading_rt_version = False
        logger.info("设置页已载入 RT 版本「%s」：%s",
                    version.get("name", ""),
                    version.get("path") or "（自动从 PATH 查找）")

    def _restore_rt_combo_selection(self):
        """把下拉框恢复到当前版本，不触发切换逻辑。"""
        self._loading_rt_version = True
        try:
            index = self.rt_version_combo.findData(
                self._current_rt_version_id)
            if index >= 0:
                self.rt_version_combo.setCurrentIndex(index)
        finally:
            self._loading_rt_version = False

    def _rt_form_dirty(self) -> bool:
        """当前 RT 版本表单是否有未保存的修改。"""
        if not self._current_rt_version_id:
            return bool(self.rt_name_edit.text().strip()
                        or self.rt_path_edit.text().strip())
        version = settings.get_rt_version(self._current_rt_version_id)
        if version is None:
            return True
        if self.rt_name_edit.text().strip() != version.get("name", ""):
            return True
        return (normalize_rt_cli_path(self.rt_path_edit.text())
                != normalize_rt_cli_path(version.get("path", "")))

    def _confirm_rt_unsaved_changes(self) -> bool:
        """
        RT 版本表单有未保存修改时询问用户。

        返回 True 表示可以继续（已保存 / 用户选择不保存）；
        返回 False 表示用户取消。
        """
        if not self._rt_form_dirty():
            return True
        name = self.rt_name_edit.text().strip() or "当前版本"
        box = QMessageBox(self)
        box.setWindowTitle("未保存的修改")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"RT 版本「{name}」有未保存的修改，要先保存吗？")
        save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton(
            "不保存", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return self.save_rt_version()
        if clicked is discard_btn:
            # 表单恢复成已保存内容，避免「不保存」后仍以旧编辑值继续操作
            version = (settings.get_rt_version(self._current_rt_version_id)
                       if self._current_rt_version_id else None)
            if version is not None:
                self._load_rt_version_form(version)
            else:
                self._loading_rt_version = True
                try:
                    self.rt_name_edit.clear()
                    self.rt_path_edit.clear()
                finally:
                    self._loading_rt_version = False
            return True
        logger.info("用户取消切换 RT 版本（放弃未保存修改前先取消）")
        return False

    def _on_rt_version_changed(self, index: int):
        if self._loading_rt_version or index < 0:
            return
        target_id = self.rt_version_combo.itemData(index) or ""
        if target_id == self._current_rt_version_id:
            return
        if not self._confirm_rt_unsaved_changes():
            self._restore_rt_combo_selection()
            return
        if target_id and not settings.set_active_rt_version(target_id):
            QMessageBox.warning(self, "切换失败", "无法切换 RT 版本，请重试。")
            self._restore_rt_combo_selection()
            return
        version = settings.get_rt_version(target_id) if target_id else None
        if version is not None:
            self._current_rt_version_id = target_id
            self._load_rt_version_form(version)
            self.status_label.setText(
                f"已切换到 RT 版本「{version['name']}」。")
            logger.info("用户切换 RT 版本为「%s」（id=%s）",
                        version["name"], target_id)
        self._refresh_rt_status()

    def _refresh_rt_status(self):
        """显示当前 RT 版本与真正生效的 rawtherapee-cli。"""
        versions = settings.list_rt_versions()
        active = settings.get_active_rt_version()
        manual = normalize_rt_cli_path(
            (active or {}).get("path")
            or settings.get_rawtherapee_cli_path())
        if active is not None:
            head = (f"已记忆 {len(versions)} 个 RT 版本，"
                    f"当前使用「{active['name']}」。")
        else:
            head = "尚未记忆任何 RT 版本。"

        active_path = ""
        if manual:
            for candidate in rt_cli_file_candidates(manual):
                if is_rt_cli_executable(candidate):
                    active_path = candidate
                    break

        if active_path:
            detail = f"✓ 实际调用：{active_path}"
        elif manual:
            detail = (f"⚠ 当前版本的路径不可用：{manual}\n"
                      "程序会自动回退到系统 PATH 查找；"
                      "请重新选择路径并保存。")
        else:
            found = shutil.which("rawtherapee-cli")
            if found:
                detail = f"✓ 未指定路径，使用系统 PATH 中的：{found}"
            else:
                detail = (
                    "⚠ 未在系统 PATH 中找到 rawtherapee-cli。\n"
                    f"请点「浏览…」指定 {_rt_cli_name_hint()} 并保存为一个 "
                    "RT 版本，或把 RawTherapee 安装目录加入系统 PATH。")
        self.rt_status.setText(head + "\n" + detail)
        logger.info("RawTherapee 状态：%d 个版本，当前版本=%s，实际生效=%s",
                    len(versions), (active or {}).get("name", "无"),
                    active_path or "未使用手动路径")

    def browse_rt_cli(self):
        current = normalize_rt_cli_path(self.rt_path_edit.text())
        start_dir = os.path.dirname(current) if current else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 rawtherapee-cli 可执行文件", start_dir,
            _rt_cli_dialog_filter())
        if not path:
            logger.info("用户取消选择 rawtherapee-cli 路径")
            return
        self.rt_path_edit.setText(path)
        self.status_label.setText(
            "已选择 rawtherapee-cli 路径，可点「检测版本」自动填写名称，"
            "点「保存当前版本」后生效。")
        logger.info("用户选择 rawtherapee-cli 路径: %s", path)

    def detect_rt_cli_version(self):
        """调用 rawtherapee-cli --version，把版本号填进名称（名称空时）。"""
        path, error = self._normalize_rt_cli_path(self.rt_path_edit.text())
        if error:
            QMessageBox.warning(
                self, "路径无效",
                error + f"\n\n请输入 {_rt_cli_name_hint()} 的完整路径，"
                "或留空让程序自动从 PATH 查找。")
            logger.warning("检测 RawTherapee 版本失败：%s（%s）", error, path)
            return
        if path:
            self.rt_path_edit.setText(path)
        version = get_rt_cli_version(path)
        if not version:
            self.status_label.setText(
                "未能读取 RawTherapee 版本号，请手动填写版本名称。")
            QMessageBox.information(
                self, "未读取到版本号",
                "无法从该 rawtherapee-cli 读取版本号。\n"
                "请确认路径有效，或手动填写版本名称。")
            logger.warning("检测 RawTherapee 版本失败：%s",
                           path or "（PATH 中的 rawtherapee-cli）")
            return
        if not self.rt_name_edit.text().strip():
            self.rt_name_edit.setText(version)
        self.status_label.setText(
            f"检测到 RawTherapee {version}，点「保存当前版本」后生效。")
        logger.info("检测到 RawTherapee 版本 %s：%s",
                    version, path or "（PATH 中的 rawtherapee-cli）")

    def autodetect_rt_cli(self):
        found = shutil.which("rawtherapee-cli")
        if not found:
            self.status_label.setText(
                "系统 PATH 中未找到 rawtherapee-cli，请手动指定路径。")
            QMessageBox.information(
                self, "未找到 rawtherapee-cli",
                "系统 PATH 中未找到 rawtherapee-cli。\n"
                f"请点「浏览…」手动指定 {_rt_cli_name_hint()} 的路径。")
            logger.warning("自动检测 rawtherapee-cli：PATH 中未找到")
            return
        self.rt_path_edit.setText(found)
        if not self.rt_name_edit.text().strip():
            detected = get_rt_cli_version(found)
            if detected:
                self.rt_name_edit.setText(detected)
        self.status_label.setText(
            f"已自动检测到 rawtherapee-cli：{found}，"
            "点「保存当前版本」后生效。")
        logger.info("自动检测到 rawtherapee-cli: %s", found)

    def save_rt_version(self) -> bool:
        """把表单保存为一个 RT 版本，并设为当前使用的版本。"""
        path, error = self._normalize_rt_cli_path(self.rt_path_edit.text())
        if error:
            QMessageBox.warning(
                self, "路径无效",
                error + f"\n\n请输入 {_rt_cli_name_hint()} 的完整路径，"
                "或留空让程序自动从 PATH 查找。")
            logger.warning("保存 RT 版本失败：%s（%s）", error, path)
            return False
        name = self.rt_name_edit.text().strip()
        if not name:
            detected = get_rt_cli_version(path)
            if detected:
                name = detected
                self.rt_name_edit.setText(name)
            else:
                QMessageBox.warning(
                    self, "保存失败",
                    "请先填写版本名称（例如「5.9 稳定版」）。\n"
                    "也可以点「检测版本」自动读取当前路径的版本号。")
                self.rt_name_edit.setFocus()
                return False
        try:
            version = settings.save_rt_version(
                name=name,
                path=path,
                version_id=self._current_rt_version_id,
            )
        except ValueError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return False
        except Exception as e:
            logger.exception("保存 RT 版本失败: %s", e)
            QMessageBox.warning(self, "保存失败", f"无法写入配置文件：{e}")
            return False

        self.rt_path_edit.setText(path)
        self._reload_rt_versions(select_id=version["id"])
        self._refresh_rt_status()
        if path:
            self.status_label.setText(
                f"已保存 RT 版本「{name}」并设为当前版本：{path}")
        else:
            self.status_label.setText(
                f"已保存 RT 版本「{name}」并设为当前版本"
                "（未指定路径，从系统 PATH 查找）。")
        logger.info("RT 版本「%s」保存成功（id=%s）",
                    name, version["id"])
        return True

    # ---------------- RT 版本：新建 / 重命名 / 删除 ----------------
    def create_rt_version(self):
        # 还没有保存过版本时，表单里的名称 / 路径就是要新建的内容，不必先提示保存
        if (self._current_rt_version_id
                and not self._confirm_rt_unsaved_changes()):
            return
        existing = {v["name"].casefold()
                    for v in settings.list_rt_versions()}
        serial = 1
        while f"版本 {serial}".casefold() in existing:
            serial += 1
        suggested = (f"版本 {serial}" if self._current_rt_version_id
                     else (self.rt_name_edit.text().strip()
                           or f"版本 {serial}"))
        name, ok = QInputDialog.getText(
            self, "新建 RT 版本", "版本名称：", text=suggested)
        if not ok:
            logger.info("用户取消新建 RT 版本")
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "新建失败", "版本名称不能为空。")
            return
        # 新版本沿用当前表单里的路径，方便先浏览选好再新建
        path, error = self._normalize_rt_cli_path(self.rt_path_edit.text())
        if error:
            QMessageBox.warning(self, "新建失败", error)
            return
        try:
            version = settings.save_rt_version(name=name, path=path)
        except ValueError as e:
            QMessageBox.warning(self, "新建失败", str(e))
            return
        except Exception as e:
            logger.exception("新建 RT 版本失败: %s", e)
            QMessageBox.warning(self, "新建失败", f"无法写入配置文件：{e}")
            return
        self._reload_rt_versions(select_id=version["id"])
        self._refresh_rt_status()
        self.status_label.setText(
            f"已新建 RT 版本「{name}」，可在路径框中修改后点「保存当前版本」。")
        logger.info("已新建 RT 版本「%s」（id=%s）", name, version["id"])

    def rename_rt_version(self):
        version = (settings.get_rt_version(self._current_rt_version_id)
                   if self._current_rt_version_id else None)
        if version is None:
            QMessageBox.information(self, "无法重命名",
                                    "请先保存一个 RT 版本。")
            return
        if not self._confirm_rt_unsaved_changes():
            return
        # 确认过程中可能保存过表单，重新取一次最新的版本数据
        version = settings.get_rt_version(self._current_rt_version_id)
        if version is None:
            return
        name, ok = QInputDialog.getText(
            self, "重命名 RT 版本", "新名称：", text=version["name"])
        if not ok:
            logger.info("用户取消重命名 RT 版本")
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "重命名失败", "版本名称不能为空。")
            return
        if name == version["name"]:
            return
        try:
            settings.rename_rt_version(version["id"], name)
        except ValueError as e:
            QMessageBox.warning(self, "重命名失败", str(e))
            return
        except Exception as e:
            logger.exception("重命名 RT 版本失败: %s", e)
            QMessageBox.warning(self, "重命名失败", f"无法写入配置文件：{e}")
            return
        self._reload_rt_versions(select_id=version["id"])
        self.status_label.setText(f"已将 RT 版本重命名为「{name}」。")
        logger.info("已把 RT 版本「%s」重命名为「%s」",
                    version["name"], name)

    def delete_rt_version(self):
        version = (settings.get_rt_version(self._current_rt_version_id)
                   if self._current_rt_version_id else None)
        if version is None:
            QMessageBox.information(self, "无法删除", "请先保存一个 RT 版本。")
            return
        if not self._confirm_rt_unsaved_changes():
            return
        version = settings.get_rt_version(self._current_rt_version_id)
        if version is None:
            return
        if QMessageBox.question(
            self, "确认删除",
            f"删除 RT 版本「{version['name']}」？\n"
            "删除后不再记忆该版本路径；若它是当前版本，"
            "会自动切换到剩余版本（没有剩余版本时回退到系统 PATH）。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            logger.info("用户取消删除 RT 版本「%s」", version["name"])
            return
        if not settings.delete_rt_version(version["id"]):
            self.status_label.setText("删除 RT 版本失败，请重试。")
            logger.warning("删除 RT 版本失败：%s", version["id"])
            return
        self._reload_rt_versions()
        self._refresh_rt_status()
        remaining = settings.get_active_rt_version()
        if remaining is not None:
            self.status_label.setText(
                f"已删除 RT 版本「{version['name']}」，"
                f"当前版本：「{remaining['name']}」。")
        else:
            self.status_label.setText(
                f"已删除 RT 版本「{version['name']}」，"
                "将从系统 PATH 查找 rawtherapee-cli。")
        logger.info("已删除 RT 版本「%s」，当前版本为 %s",
                    version["name"],
                    remaining["name"] if remaining else "（PATH）")

    # ---------------- 配置方案：加载 / 切换 ----------------
    def _reload_profiles(self, select_id: str = ""):
        """重建下拉框，并把选中方案载入表单。"""
        profiles = settings.list_api_profiles()
        active_id = select_id or settings.get_active_profile_id()
        self._loading_profile = True
        try:
            self.profile_combo.clear()
            for profile in profiles:
                self.profile_combo.addItem(_profile_label(profile),
                                           profile["id"])

            index = self.profile_combo.findData(active_id)
            if index < 0:
                index = 0
                active_id = self.profile_combo.itemData(0)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)

            profile = settings.get_api_profile(active_id)
            if profile is not None:
                self._load_profile_form(profile)
        finally:
            self._loading_profile = False
        logger.info("已载入 %d 套 API 配置方案，当前方案 id=%s",
                    len(profiles), self._current_profile_id)

    def _load_profile_form(self, profile: dict):
        """把一套配置方案填入表单（含该方案自己的 API Key）。"""
        self._loading_profile = True
        try:
            self.profile_name_edit.setText(profile.get("name", ""))
            self.api_base_edit.setText(profile.get("api_base", ""))
            self.model_edit.setText(profile.get("model", ""))
            self.api_key_edit.setText(settings.get_api_key(profile["id"]))
            self._current_profile_id = profile["id"]
            self._api_key_visible = False
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_toggle_key.setText("显示")
        finally:
            self._loading_profile = False
        logger.info("设置页已载入配置方案「%s」：api_base=%s，model=%s，API Key %s",
                    profile.get("name", ""), profile.get("api_base", ""),
                    profile.get("model", ""),
                    "已配置" if self.api_key_edit.text() else "未配置")

    def _select_combo(self, profile_id: str):
        """把下拉框定位到指定方案，但不触发切换逻辑。"""
        self._loading_profile = True
        try:
            index = self.profile_combo.findData(profile_id)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        finally:
            self._loading_profile = False

    def _restore_combo_selection(self):
        self._select_combo(self._current_profile_id)

    def _current_profile(self):
        return settings.get_api_profile(self._current_profile_id)

    def _is_dirty(self) -> bool:
        """当前表单是否有未保存的修改（含 API Key）。"""
        profile = self._current_profile()
        if profile is None:
            return True
        if self.profile_name_edit.text().strip() != profile.get("name", ""):
            return True
        if self.api_base_edit.text().strip() != profile.get("api_base", ""):
            return True
        if self.model_edit.text().strip() != profile.get("model", ""):
            return True
        saved_key = settings.get_api_key(profile["id"])
        return self.api_key_edit.text().strip() != saved_key

    def _confirm_unsaved_changes(self) -> bool:
        """
        有未保存修改时询问用户。

        返回 True 表示可以继续（已保存 / 用户选择不保存）；
        返回 False 表示用户取消。
        """
        if not self._is_dirty():
            return True
        profile = self._current_profile()
        name = profile.get("name", "") if profile else "当前配置"
        box = QMessageBox(self)
        box.setWindowTitle("未保存的修改")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"配置方案「{name}」有未保存的修改，要先保存吗？")
        save_btn = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton(
            "不保存", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return self.save_all()
        if clicked is discard_btn:
            # 表单恢复成已保存内容，避免「不保存」后仍以旧编辑值继续操作
            profile = self._current_profile()
            if profile is not None:
                self._load_profile_form(profile)
            return True
        logger.info("用户取消切换配置方案（放弃未保存修改前先取消）")
        return False

    def _on_profile_changed(self, index: int):
        if self._loading_profile or index < 0:
            return
        target_id = self.profile_combo.itemData(index)
        if not target_id or target_id == self._current_profile_id:
            return
        if not self._confirm_unsaved_changes():
            self._restore_combo_selection()
            return
        if not settings.set_active_api_profile(target_id):
            QMessageBox.warning(self, "切换失败", "无法切换配置方案，请重试。")
            self._restore_combo_selection()
            return
        self._select_combo(target_id)
        profile = settings.get_api_profile(target_id)
        if profile is not None:
            self._load_profile_form(profile)
            self.status_label.setText(
                f"已切换到配置方案「{profile['name']}」。")
            logger.info("用户切换配置方案为「%s」（id=%s）",
                        profile["name"], target_id)
        self._refresh_keyring_status()
        self.api_config_changed.emit()
        logger.info("已发出 api_config_changed 信号，通知其它页面刷新")

    # ---------------- 配置方案：新建 / 重命名 / 删除 ----------------
    def create_profile(self):
        if not self._confirm_unsaved_changes():
            return
        existing = {p["name"].casefold()
                    for p in settings.list_api_profiles()}
        serial = 1
        while f"配置 {serial}".casefold() in existing:
            serial += 1
        suggested = f"配置 {serial}"
        name, ok = QInputDialog.getText(
            self, "新建配置方案", "配置名称：", text=suggested)
        if not ok:
            logger.info("用户取消新建配置方案")
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "新建失败", "配置名称不能为空。")
            return
        try:
            # 新方案沿用当前填写的接口地址与模型，但不会复制 API Key
            profile = settings.save_api_profile(
                name=name,
                api_base=self.api_base_edit.text().strip(),
                model=self.model_edit.text().strip(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "新建失败", str(e))
            return
        except Exception as e:
            logger.exception("新建配置方案失败: %s", e)
            QMessageBox.warning(self, "新建失败", f"无法写入配置文件：{e}")
            return
        self._reload_profiles(select_id=profile["id"])
        self.status_label.setText(
            f"已新建配置方案「{name}」，填写 API Key 后点「保存当前配置」。")
        logger.info("已新建配置方案「%s」（id=%s），等待填写 API Key",
                    name, profile["id"])
        self.api_config_changed.emit()

    def rename_profile(self):
        if self._current_profile() is None:
            return
        if not self._confirm_unsaved_changes():
            return
        # 确认过程中可能保存过表单，重新取一次最新的方案数据
        profile = self._current_profile()
        if profile is None:
            return
        name, ok = QInputDialog.getText(
            self, "重命名配置方案", "新名称：", text=profile["name"])
        if not ok:
            logger.info("用户取消重命名配置方案")
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "重命名失败", "配置名称不能为空。")
            return
        if name == profile["name"]:
            return
        try:
            settings.rename_api_profile(profile["id"], name)
        except ValueError as e:
            QMessageBox.warning(self, "重命名失败", str(e))
            return
        except Exception as e:
            logger.exception("重命名配置方案失败: %s", e)
            QMessageBox.warning(self, "重命名失败", f"无法写入配置文件：{e}")
            return
        self._reload_profiles(select_id=profile["id"])
        self.status_label.setText(
            f"已将配置方案重命名为「{name}」。")
        logger.info("已把配置方案「%s」重命名为「%s」",
                    profile["name"], name)

    def delete_profile(self):
        if self._current_profile() is None:
            return
        if len(settings.list_api_profiles()) <= 1:
            QMessageBox.information(
                self, "无法删除", "至少需要保留一套配置方案。")
            return
        if not self._confirm_unsaved_changes():
            return
        # 确认过程中可能保存过表单，重新取一次最新的方案数据
        profile = self._current_profile()
        if profile is None:
            return
        if QMessageBox.question(
            self, "确认删除",
            f"删除配置方案「{profile['name']}」？\n"
            "该方案在系统钥匙串中保存的 API Key 也会一并删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            logger.info("用户取消删除配置方案「%s」", profile["name"])
            return
        if not settings.delete_api_profile(profile["id"]):
            self.status_label.setText("删除配置方案失败，请重试。")
            logger.warning("删除配置方案失败：%s", profile["id"])
            return
        self._reload_profiles()
        active = settings.get_active_profile()
        self.status_label.setText(
            f"已删除配置方案「{profile['name']}」，"
            f"当前方案：「{active['name']}」。")
        logger.info("已删除配置方案「%s」，当前方案为「%s」",
                    profile["name"], active["name"])
        self.api_config_changed.emit()
        logger.info("已发出 api_config_changed 信号，通知其它页面刷新")

    # ---------------- 状态提示 ----------------
    def _refresh_keyring_status(self):
        if settings.keyring_available():
            self.keyring_status.setText(
                "✓ 系统钥匙串可用，每套配置方案的 API Key 会分别安全保存。")
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
        except Exception as e:
            custom_count = 0
            logger.error("统计自定义参数失败: %s", e)
        if custom_count:
            self.custom_status.setText(f"已加载 {custom_count} 个自定义参数。")
        else:
            self.custom_status.setText("当前没有自定义参数。")
        logger.info("自定义参数状态：%d 个（存储位置 %s）",
                    custom_count, settings.CUSTOM_PARAMS_FILE)

    def _toggle_key_visible(self):
        self._api_key_visible = not self._api_key_visible
        if self._api_key_visible:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_toggle_key.setText("隐藏")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_toggle_key.setText("显示")
        logger.info("API Key 显示状态切换为: %s",
                    "明文" if self._api_key_visible else "掩码")

    # ---------------- 保存 / 清除 ----------------
    def save_all(self) -> bool:
        """保存当前表单为一套配置方案，并设为当前方案。"""
        name = self.profile_name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "保存失败", "请先给这套配置起个名称。")
            self.profile_name_edit.setFocus()
            return False
        try:
            profile = settings.save_api_profile(
                name=name,
                api_base=self.api_base_edit.text().strip(),
                model=self.model_edit.text().strip(),
                profile_id=self._current_profile_id,
            )
        except ValueError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return False
        except Exception as e:
            logger.exception("写入配置文件失败: %s", e)
            QMessageBox.warning(self, "保存失败", f"无法写入配置文件：{e}")
            return False

        profile_id = profile["id"]
        api_key = self.api_key_edit.text().strip()
        ok = settings.set_api_key(api_key, profile_id)
        if api_key and not ok:
            self.status_label.setText(
                "已保存配置方案，但 API Key 未能写入钥匙串（仅本次会话有效）。")
            logger.warning("API Key 未能写入钥匙串，仅本次会话有效")
        else:
            self.status_label.setText(
                f"已保存配置方案「{name}」并设为当前方案。")
            logger.info("配置方案「%s」保存成功（API Key %s）",
                        name, "已写入钥匙串" if ok and api_key else "为空")

        # 刷新下拉框显示（名称 / 模型可能变化），保持选中当前方案
        self._reload_profiles(select_id=profile_id)
        self._refresh_keyring_status()
        self.api_config_changed.emit()
        logger.info("已发出 api_config_changed 信号，通知其它页面刷新")
        return True

    def clear_api_key(self):
        profile = self._current_profile()
        if profile is None:
            return
        if QMessageBox.question(
            self, "确认",
            f"清除配置方案「{profile['name']}」的 API Key？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            logger.info("用户取消清除 API Key")
            return
        settings.delete_api_key(profile["id"])
        self.api_key_edit.clear()
        self.status_label.setText(
            f"已清除配置方案「{profile['name']}」的 API Key。")
        self.api_config_changed.emit()
        logger.info("已清除配置方案「%s」的 API Key 并通知其它页面刷新",
                    profile["name"])

    # ---------------- 自定义参数 ----------------
    def import_custom_params(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择参数定义 JSON", "", "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            logger.info("用户取消导入自定义参数")
            return
        logger.info("设置页开始导入自定义参数: %s", path)
        count, errors = load_custom_params(path)
        if count == 0:
            QMessageBox.warning(
                self, "导入失败",
                "没有有效的参数被导入。\n\n" + "\n".join(errors[:5]))
            self.status_label.setText(
                f"导入失败：{errors[0] if errors else '未知错误'}")
            logger.warning("设置页导入自定义参数失败 %s：%s",
                           path, errors[0] if errors else "未知错误")
            return
        try:
            save_custom_params()
        except Exception as e:
            self.status_label.setText(f"导入成功，但持久化失败：{e}")
            logger.error("自定义参数已导入但持久化失败: %s", e)
        else:
            if errors:
                self.status_label.setText(
                    f"已导入 {count} 个参数（{len(errors)} 个被跳过，"
                    f"首个错误：{errors[0]}）")
            else:
                self.status_label.setText(f"已导入 {count} 个参数。")
            logger.info("设置页导入自定义参数成功：%d 个（跳过 %d 个）",
                        count, len(errors))
        self._refresh_custom_status()

    def clear_custom(self):
        if QMessageBox.question(
                self, "确认", "移除所有已导入的自定义参数？"
        ) != QMessageBox.StandardButton.Yes:
            logger.info("用户取消清空自定义参数")
            return
        clear_custom_params()
        self.status_label.setText("已清空自定义参数。")
        self._refresh_custom_status()
        logger.info("设置页已清空全部自定义参数")
