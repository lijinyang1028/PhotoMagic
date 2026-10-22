"""设置页面：多套 API 配置方案 / 自定义参数 / 存储信息"""
import logging

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
)


logger = logging.getLogger(__name__)


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


class SettingsPage(QWidget):
    """设置页：管理多套 API 配置方案、自定义参数。"""

    # 保存 / 切换 / 清除 API 配置后发出，供其他页面刷新
    api_config_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.is_dark = True
        self._api_key_visible = False
        self._loading_profile = False
        self._current_profile_id = ""
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
