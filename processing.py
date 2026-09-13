"""RAW 批处理页面：选择文件 -> LLM 参数 -> RawTherapee 处理"""
import os
import base64
import json
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QLineEdit, QFileDialog, QListWidget, QListWidgetItem, QMessageBox,
    QGroupBox, QFormLayout, QProgressBar, QStyledItemDelegate, QStyle
)

from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QFont
from PyQt6.QtCore import (
    Qt, QThreadPool, QRunnable, QObject, pyqtSignal, QSize, QRect, QPoint
)

from llm_handler import LLMClient, load_image_base64_from_raw
from rt_processor import generate_pp3, run_rawtherapee


DEFAULT_SYSTEM_PROMPT = """你是一个专业的摄影后期处理顾问。请仔细观察我发给你的照片，根据画面内容、光线、构图等，给出最佳的 RawTherapee 后期参数建议。

请严格返回一个 JSON 对象，不要包含任何其他文字。可选键如下（数值，超出范围会被自动钳位）：
- "exposure": 曝光补偿(EV)，-3.0 到 3.0
- "contrast": 对比度，-100 到 100
- "saturation": 饱和度，-100 到 100
- "highlight_compr": 高光压缩，0 到 100
- "shadow_compr": 阴影压缩，0 到 100
- "highlights": 高光恢复，0 到 100
- "shadows": 阴影提亮，0 到 100
- "temperature": 色温(K)，2000 到 12000
- "tint": 绿/品红倾向，0.5 到 2.0，1.0 为中性
- "sharpen_amount": 锐化强度，0 到 200
- "vibrance": 自然饱和度，0 到 100
- "distortion": 镜头畸变校正，-1.0 到 1.0

若某参数无需调整，可省略该键。"""

DEFAULT_USER_PROMPT = "请为这张照片建议最佳后期参数。"

RAW_FILTER = (
    "RAW 文件 (*.CR2 *.NEF *.ARW *.DNG *.ORF *.RAF *.RW2 *.PEF *.raw *.3fr *.bay "
    "*.cap *.dcs *.dcr *.drf *.eip *.erf *.fff *.iiq *.k25 *.kdc *.mdc *.mef *.mos "
    "*.mrw *.nrw *.pef *.ptx *.pxn *.r3d *.raf *.raw *.rw2 *.rwl *.rwz *.srf *.srw "
    "*.x3f);;所有文件 (*)"
)

THUMB_W, THUMB_H = 80, 80
ITEM_H = 96
MAX_PARALLEL = 3
BTN_AREA_W = 56

COLOR_PENDING = "#d0a000"
COLOR_RUNNING = "#2d8cf0"
COLOR_DONE = "#4caf50"
COLOR_FAIL = "#f44336"
COLOR_CANCEL = "#808080"


# ----------------------------- 后台任务 -----------------------------
class ImageLoadTask(QRunnable):
    class Signals(QObject):
        loaded = pyqtSignal(str, QPixmap)
        failed = pyqtSignal(str, str)

    def __init__(self, file_path, thumb_size):
        super().__init__()
        self.file_path = file_path
        self.thumb_size = thumb_size
        self.signals = ImageLoadTask.Signals()

    def run(self):
        try:
            b64 = load_image_base64_from_raw(self.file_path, thumb_size=self.thumb_size)
            pix = QPixmap()
            if pix.loadFromData(base64.b64decode(b64), "JPEG"):
                self.signals.loaded.emit(self.file_path, pix)
            else:
                self.signals.failed.emit(self.file_path, "解码失败")
        except Exception as e:
            self.signals.failed.emit(self.file_path, str(e))


class ProcessTask(QRunnable):
    class Signals(QObject):
        log = pyqtSignal(str)
        started = pyqtSignal(str)
        finished = pyqtSignal(str, bool, str)

    def __init__(self, raw_path, output_dir, llm_client,
                 system_prompt, user_prompt, stop_event):
        super().__init__()
        self.raw_path = raw_path
        self.output_dir = output_dir
        self.llm_client = llm_client
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.stop_event = stop_event
        self.signals = ProcessTask.Signals()

    def run(self):
        if self.stop_event.is_set():
            self.signals.finished.emit(self.raw_path, False, "已取消")
            return

        self.signals.started.emit(self.raw_path)
        self.signals.log.emit(f"正在处理: {os.path.basename(self.raw_path)}")
        try:
            params = self.llm_client.request_json(
                self.system_prompt, self.user_prompt, self.raw_path)
            self.signals.log.emit(
                f"  LLM 返回参数: {json.dumps(params, ensure_ascii=False)}")

            if self.stop_event.is_set():
                self.signals.finished.emit(self.raw_path, False, "已取消")
                return

            pp3_path = os.path.join(self.output_dir,
                                    f"{Path(self.raw_path).stem}.pp3")
            generate_pp3(params, pp3_path)
            run_rawtherapee(self.raw_path, pp3_path, self.output_dir)
            self.signals.log.emit(f"  已完成: {Path(self.raw_path).stem}.jpg")
            self.signals.finished.emit(self.raw_path, True, "完成")
        except Exception as e:
            self.signals.log.emit(f"  错误: {str(e)}")
            self.signals.finished.emit(self.raw_path, False, str(e))


# ----------------------------- 圆形启停按钮 -----------------------------
class CircleIconButton(QPushButton):
    PLAY = "play"
    STOP = "stop"

    def __init__(self, icon_type, color="#2563eb", parent=None):
        super().__init__(parent)
        self.icon_type = icon_type
        self._color = QColor(color)
        self._hover = False
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QPushButton { border: none; background: transparent; }")

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        c = QColor(self._color)
        if not self.isEnabled():
            c.setAlpha(70)
        elif self._hover:
            c = c.lighter(115)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(self.rect())

        cx = self.width() / 2
        cy = self.height() / 2

        if self.icon_type == self.PLAY:
            p.setPen(QPen(QColor("white"), 2.5,
                          Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawLine(int(cx), int(cy - 7), int(cx), int(cy + 7))
            p.drawLine(int(cx), int(cy - 7), int(cx - 6), int(cy - 1))
            p.drawLine(int(cx), int(cy - 7), int(cx + 6), int(cy - 1))
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            p.drawRoundedRect(QRect(int(cx - 6), int(cy - 6), 12, 12), 2, 2)


# ----------------------------- 前后对比视图 -----------------------------
class BeforeAfterView(QWidget):
    """拖动中缝对比原图/处理后"""
    def __init__(self):
        super().__init__()
        self._original = None
        self._processed = None
        self._split = 0.5
        self._dragging = False
        self.is_dark = True
        self.setMouseTracking(True)
        self.setMinimumSize(320, 320)

    def clear(self):
        self._original = None
        self._processed = None
        self._split = 0.5
        self.update()

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.update()

    def set_original(self, pixmap):
        self._original = pixmap
        self.update()

    def set_processed(self, pixmap):
        self._processed = pixmap
        self.update()

    def _ref_pixmap(self):
        if self._original is not None and not self._original.isNull():
            return self._original
        if self._processed is not None and not self._processed.isNull():
            return self._processed
        return None

    def _fit_rect(self):
        ref = self._ref_pixmap()
        if ref is None:
            return QRect()
        w, h = self.width(), self.height()
        pw, ph = ref.width(), ref.height()
        if pw <= 0 or ph <= 0:
            return QRect()
        scale = min(w / pw, h / ph)
        nw, nh = int(pw * scale), int(ph * scale)
        return QRect((w - nw) // 2, (h - nh) // 2, nw, nh)

    def _split_x(self, rect):
        return rect.x() + int(rect.width() * self._split)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            rect = self._fit_rect()
            if not rect.isNull():
                self._dragging = True
                self._set_split(e.position().x(), rect)
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        rect = self._fit_rect()
        if not rect.isNull():
            hx = self._split_x(rect)
            cy = rect.y() + rect.height() // 2
            near = abs(e.position().x() - hx) < 16 and abs(e.position().y() - cy) < 30
            self.setCursor(Qt.CursorShape.SizeHorCursor
                           if (near or self._dragging)
                           else Qt.CursorShape.ArrowCursor)
            if self._dragging:
                self._set_split(e.position().x(), rect)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._dragging = False
        super().mouseReleaseEvent(e)

    def _set_split(self, x, rect):
        if rect.width() <= 0:
            return
        rel = (x - rect.x()) / rect.width()
        self._split = max(0.0, min(1.0, rel))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        bg = QColor("#202020") if self.is_dark else QColor("#f5f5f5")
        p.fillRect(self.rect(), bg)

        has_orig = self._original is not None and not self._original.isNull()
        has_proc = self._processed is not None and not self._processed.isNull()

        if not has_orig and not has_proc:
            p.setPen(QColor("#808080"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "未选择图片")
            return

        rect = self._fit_rect()
        if rect.isNull():
            return

        if has_orig and has_proc:
            sx = self._split_x(rect)

            p.save()
            p.setClipRect(QRect(rect.x(), rect.y(),
                                max(0, sx - rect.x()), rect.height()))
            p.drawPixmap(rect, self._original)
            p.restore()

            p.save()
            p.setClipRect(QRect(sx, rect.y(),
                                max(0, rect.right() - sx + 1), rect.height()))
            p.drawPixmap(rect, self._processed)
            p.restore()

            p.setPen(QPen(QColor(255, 255, 255, 220), 2))
            p.drawLine(sx, rect.y(), sx, rect.bottom())

            cy = rect.y() + rect.height() // 2
            p.setPen(QPen(QColor(0, 0, 0, 50), 1))
            p.setBrush(QColor("white"))
            p.drawEllipse(QPoint(sx, cy), 14, 14)
            p.setPen(QPen(QColor("#333333"), 2, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(sx - 6, cy, sx - 3, cy - 4)
            p.drawLine(sx - 6, cy, sx - 3, cy + 4)
            p.drawLine(sx + 6, cy, sx + 3, cy - 4)
            p.drawLine(sx + 6, cy, sx + 3, cy + 4)

            self._tag(p, QRect(rect.x() + 10, rect.y() + 10, 52, 22), "原图")
            self._tag(p, QRect(rect.right() - 62, rect.y() + 10, 52, 22), "处理后")
        elif has_orig:
            p.drawPixmap(rect, self._original)
            self._tag(p, QRect(rect.x() + 10, rect.y() + 10, 52, 22), "原图")
        else:
            p.drawPixmap(rect, self._processed)
            self._tag(p, QRect(rect.right() - 62, rect.y() + 10, 52, 22), "处理后")

    def _tag(self, p, r, text):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor("white"))
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)


# ----------------------------- 列表 delegate -----------------------------
class FileListDelegate(QStyledItemDelegate):
    StatusRole = Qt.ItemDataRole.UserRole + 1
    StatusColorRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, owner_widget):
        super().__init__(owner_widget)
        self.owner = owner_widget
        self._font_name = QFont()
        self._font_name.setPointSize(10)
        self._font_status = QFont()
        self._font_status.setPointSize(9)

    def sizeHint(self, option, index):
        return QSize(0, ITEM_H)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        is_dark = self.owner.is_dark

        if option.state & QStyle.StateFlag.State_Selected:
            bg = QColor("#2d2d2d") if is_dark else QColor("#e8f0fe")
        elif option.state & QStyle.StateFlag.State_MouseOver:
            bg = QColor("#252525") if is_dark else QColor("#f2f2f2")
        else:
            bg = QColor("#202020") if is_dark else QColor("#ffffff")
        painter.fillRect(rect, bg)

        path = index.data(Qt.ItemDataRole.UserRole)

        # 复选框
        cb_size = 16
        cb_rect = QRect(rect.left() + 14,
                        rect.top() + (rect.height() - cb_size) // 2,
                        cb_size, cb_size)
        checked = path in self.owner.file_list_widget.checked_paths
        border = QColor("#888888") if is_dark else QColor("#999999")
        painter.setPen(QPen(border, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(cb_rect, 3, 3)
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#2563eb"))
            painter.drawRoundedRect(cb_rect, 3, 3)
            painter.setPen(QPen(QColor("white"), 2,
                                Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(cb_rect.left() + 3, cb_rect.center().y(),
                             cb_rect.center().x() - 1, cb_rect.bottom() - 3)
            painter.drawLine(cb_rect.center().x() - 1, cb_rect.bottom() - 3,
                             cb_rect.right() - 3, cb_rect.top() + 3)

        # 缩略图
        thumb_x = cb_rect.right() + 14
        thumb_rect = QRect(thumb_x,
                           rect.top() + (rect.height() - THUMB_H) // 2,
                           THUMB_W, THUMB_H)
        pixmap = self.owner._thumb_cache.get(path)
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(THUMB_W, THUMB_H,
                                   Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            px = thumb_rect.x() + (THUMB_W - scaled.width()) // 2
            py = thumb_rect.y() + (THUMB_H - scaled.height()) // 2
            painter.drawPixmap(px, py, scaled)
        else:
            ph_bg = QColor("#2a2a2a") if is_dark else QColor("#f0f0f0")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ph_bg)
            painter.drawRoundedRect(thumb_rect, 6, 6)

        # 文件名 + 状态
        info_x = thumb_rect.right() + 14
        info_w = rect.right() - BTN_AREA_W - info_x
        if info_w < 40:
            info_w = 40
        name_rect = QRect(info_x, rect.top() + 22, info_w, 22)
        status_rect = QRect(info_x, rect.top() + 48, info_w, 20)

        name = Path(path).name
        painter.setFont(self._font_name)
        painter.setPen(QColor("#ffffff") if is_dark else QColor("#1b1b1b"))
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            painter.fontMetrics().elidedText(
                name, Qt.TextElideMode.ElideMiddle, info_w))

        status = index.data(self.StatusRole) or "待处理"
        status_color = index.data(self.StatusColorRole) or "#888888"
        painter.setFont(self._font_status)
        painter.setPen(QColor(status_color))
        painter.drawText(status_rect,
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         status)

        painter.restore()


# ----------------------------- 自定义列表 -----------------------------
class FileListWidget(QListWidget):
    checkToggled = pyqtSignal(str)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.checked_paths = set()
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)

    def mousePressEvent(self, e):
        pos = e.pos()
        item = self.itemAt(pos)
        if item is not None:
            rect = self.visualItemRect(item)
            cb_rect = QRect(rect.left() + 14,
                            rect.top() + (rect.height() - 16) // 2,
                            16, 16)
            if cb_rect.contains(pos):
                path = item.data(Qt.ItemDataRole.UserRole)
                if path in self.checked_paths:
                    self.checked_paths.discard(path)
                else:
                    self.checked_paths.add(path)
                self.viewport().update()
                self.checkToggled.emit(path)
                return
        super().mousePressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.owner._scan_visible()


# ----------------------------- 处理页面 -----------------------------
class ProcessingWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.default_output = os.path.join(os.path.expanduser("~"),
                                          "rawtherapee_output")
        os.makedirs(self.default_output, exist_ok=True)
        self.output_dir = self.default_output

        self._thumb_cache = {}
        self._thumb_loading = set()
        self._preview_file = None
        self.failed_paths = set()

        self.stop_event = threading.Event()
        self.processing = False
        self.completed_count = 0
        self.total_count = 0
        self.is_dark = True

        self.thumb_pool = QThreadPool()
        self.thumb_pool.setMaxThreadCount(4)
        self.process_pool = QThreadPool()
        self.process_pool.setMaxThreadCount(MAX_PARALLEL)

        self.init_ui()

    # ---------------- UI ----------------
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        file_group = QGroupBox("1. 选择 RAW 照片")
        f_layout = QVBoxLayout(file_group)

        btn_layout = QHBoxLayout()
        self.btn_add = QPushButton("添加文件")
        self.btn_add.clicked.connect(self.add_files)
        self.btn_clear = QPushButton("清空列表")
        self.btn_clear.clicked.connect(self.clear_files)
        self.btn_check_all = QPushButton("全选")
        self.btn_check_all.clicked.connect(lambda: self._set_all_checks(True))
        self.btn_uncheck_all = QPushButton("全不选")
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all_checks(False))
        for b in (self.btn_add, self.btn_clear,
                  self.btn_check_all, self.btn_uncheck_all):
            btn_layout.addWidget(b)
        btn_layout.addStretch()
        f_layout.addLayout(btn_layout)

        list_preview_layout = QHBoxLayout()
        self.file_list_widget = FileListWidget(self)
        self.file_list_widget.setItemDelegate(FileListDelegate(self))
        self.file_list_widget.currentRowChanged.connect(self.update_preview)
        self.file_list_widget.verticalScrollBar().valueChanged.connect(
            self._scan_visible)
        list_preview_layout.addWidget(self.file_list_widget, 1)

        self.preview_view = BeforeAfterView()
        self.preview_view.setFixedSize(320, 320)
        list_preview_layout.addWidget(self.preview_view)

        f_layout.addLayout(list_preview_layout)
        layout.addWidget(file_group)

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

        prompt_group = QGroupBox("3. 提示词设置")
        prompt_layout = QFormLayout(prompt_group)
        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlainText(DEFAULT_SYSTEM_PROMPT)
        self.system_prompt_edit.setMaximumHeight(160)
        prompt_layout.addRow("System Prompt:", self.system_prompt_edit)
        self.user_prompt_edit = QLineEdit(DEFAULT_USER_PROMPT)
        prompt_layout.addRow("User Prompt:", self.user_prompt_edit)
        layout.addWidget(prompt_group)

        out_group = QGroupBox("4. 输出设置")
        out_layout = QHBoxLayout(out_group)
        out_layout.addWidget(QLabel("输出目录:"))
        self.out_dir_edit = QLineEdit(self.output_dir)
        out_layout.addWidget(self.out_dir_edit)
        self.btn_browse = QPushButton("浏览")
        self.btn_browse.clicked.connect(self.browse_output)
        out_layout.addWidget(self.btn_browse)
        layout.addWidget(out_group)

        action_layout = QHBoxLayout()
        self.btn_process = CircleIconButton(CircleIconButton.PLAY, "#2563eb")
        self.btn_process.setToolTip("开始处理")
        self.btn_process.clicked.connect(self.start_processing)
        action_layout.addWidget(self.btn_process)

        self.btn_stop = CircleIconButton(CircleIconButton.STOP, "#2563eb")
        self.btn_stop.setToolTip("停止")
        self.btn_stop.clicked.connect(self.stop_processing)
        self.btn_stop.setEnabled(False)
        action_layout.addWidget(self.btn_stop)

        self.btn_retry = QPushButton("重试失败项")
        self.btn_retry.clicked.connect(self.retry_failed)
        self.btn_retry.setEnabled(False)
        action_layout.addWidget(self.btn_retry)

        self.progress_bar = QProgressBar()
        action_layout.addWidget(self.progress_bar)
        layout.addLayout(action_layout)

        log_group = QGroupBox("处理日志")
        log_layout = QVBoxLayout(log_group)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        log_layout.addWidget(self.log_edit)
        layout.addWidget(log_group)

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.preview_view.set_dark(is_dark)
        self.file_list_widget.viewport().update()

    def get_llm_client(self):
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            return None
        return LLMClient(
            api_base=self.api_base_edit.text().strip(),
            api_key=api_key,
            model=self.model_edit.text().strip()
        )

    # ---------------- 缩略图懒加载 ----------------
    def _scan_visible(self):
        viewport_rect = self.file_list_widget.viewport().rect()
        for i in range(self.file_list_widget.count()):
            item = self.file_list_widget.item(i)
            rect = self.file_list_widget.visualItemRect(item)
            if not rect.intersects(viewport_rect):
                continue
            path = item.data(Qt.ItemDataRole.UserRole)
            if path in self._thumb_cache or path in self._thumb_loading:
                continue
            self._thumb_loading.add(path)
            task = ImageLoadTask(path, (THUMB_W * 2, THUMB_H * 2))
            task.signals.loaded.connect(self._on_thumb_loaded)
            task.signals.failed.connect(self._on_thumb_failed)
            self.thumb_pool.start(task)

    def _on_thumb_loaded(self, file_path, pixmap):
        self._thumb_loading.discard(file_path)
        self._thumb_cache[file_path] = pixmap
        self.file_list_widget.viewport().update()

    def _on_thumb_failed(self, file_path, err):
        self._thumb_loading.discard(file_path)
        self._thumb_cache[file_path] = QPixmap()

    # ---------------- 文件列表 ----------------
    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择 RAW 文件", "", RAW_FILTER)
        if not files:
            return
        existing = {self.file_list_widget.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self.file_list_widget.count())}
        new_files = [f for f in files if f not in existing]
        if not new_files:
            return

        lw = self.file_list_widget
        lw.setUpdatesEnabled(False)
        for f in new_files:
            item = QListWidgetItem()
            item.setText(Path(f).name)
            item.setData(Qt.ItemDataRole.UserRole, f)
            item.setData(FileListDelegate.StatusRole, "待处理")
            item.setData(FileListDelegate.StatusColorRole, COLOR_PENDING)
            lw.addItem(item)
            lw.checked_paths.add(f)
        lw.setUpdatesEnabled(True)
        lw.viewport().update()

        if lw.currentRow() < 0:
            lw.setCurrentRow(0)
        self._scan_visible()

    def _set_all_checks(self, checked):
        if self.processing:
            return
        lw = self.file_list_widget
        lw.checked_paths.clear()
        if checked:
            for i in range(lw.count()):
                lw.checked_paths.add(lw.item(i).data(Qt.ItemDataRole.UserRole))
        lw.viewport().update()

    def clear_files(self):
        if self.processing:
            return
        self.file_list_widget.clear()
        self.file_list_widget.checked_paths.clear()
        self._thumb_cache.clear()
        self._thumb_loading.clear()
        self.failed_paths.clear()
        self._preview_file = None
        self.btn_retry.setEnabled(False)
        self.preview_view.clear()

    # ---------------- 预览 / 对比 ----------------
    def update_preview(self, row):
        if row < 0 or row >= self.file_list_widget.count():
            self._preview_file = None
            self.preview_view.clear()
            return
        item = self.file_list_widget.item(row)
        f = item.data(Qt.ItemDataRole.UserRole)
        if f == self._preview_file:
            return
        self._preview_file = f
        self.preview_view.clear()

        task = ImageLoadTask(f, (800, 800))
        task.signals.loaded.connect(self._on_preview_original_loaded)
        task.signals.failed.connect(self._on_preview_failed)
        self.thumb_pool.start(task)

        self._load_processed_preview(f)

    def _load_processed_preview(self, raw_path):
        stem = Path(raw_path).stem
        out_dir = self.out_dir_edit.text().strip() or self.output_dir
        out_path = os.path.join(out_dir, f"{stem}.jpg")
        if not os.path.exists(out_path):
            self.preview_view.set_processed(None)
            return
        task = ImageLoadTask(out_path, (800, 800))
        task.signals.loaded.connect(self._on_preview_processed_loaded)
        task.signals.failed.connect(self._on_preview_failed)
        self.thumb_pool.start(task)

    def _on_preview_original_loaded(self, file_path, pixmap):
        if file_path != self._preview_file:
            return
        self.preview_view.set_original(pixmap)

    def _on_preview_processed_loaded(self, file_path, pixmap):
        if self._preview_file is None:
            return
        if Path(file_path).stem != Path(self._preview_file).stem:
            return
        self.preview_view.set_processed(pixmap)

    def _on_preview_failed(self, file_path, err):
        pass

    def browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.output_dir = d
            self.out_dir_edit.setText(d)

    # ---------------- 状态 ----------------
    def _set_item_status(self, file_path, status, color):
        lw = self.file_list_widget
        for i in range(lw.count()):
            item = lw.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == file_path:
                item.setData(FileListDelegate.StatusRole, status)
                item.setData(FileListDelegate.StatusColorRole, color)
                lw.viewport().update()
                return

    # ---------------- 处理控制 ----------------
    def start_processing(self):
        if self.processing:
            return

        lw = self.file_list_widget
        targets = []
        for i in range(lw.count()):
            item = lw.item(i)
            path = item.data(Qt.ItemDataRole.UserRole)
            if path in lw.checked_paths:
                targets.append(path)

        if not targets:
            QMessageBox.information(self, "提示", "请勾选要处理的文件。")
            return
        if not self.api_key_edit.text().strip():
            QMessageBox.information(self, "提示", "请输入 API Key。")
            return

        self.output_dir = self.out_dir_edit.text().strip()
        os.makedirs(self.output_dir, exist_ok=True)
        llm_client = self.get_llm_client()

        self.stop_event.clear()
        self.processing = True
        self.completed_count = 0
        self.total_count = len(targets)
        self.failed_paths.clear()
        self.progress_bar.setValue(0)
        self.log_edit.clear()
        self.btn_process.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_retry.setEnabled(False)
        for b in (self.btn_add, self.btn_clear,
                  self.btn_check_all, self.btn_uncheck_all):
            b.setEnabled(False)

        for f in targets:
            self._set_item_status(f, "排队中", COLOR_PENDING)

        system_prompt = self.system_prompt_edit.toPlainText()
        user_prompt = self.user_prompt_edit.text()

        for raw_path in targets:
            task = ProcessTask(raw_path, self.output_dir, llm_client,
                              system_prompt, user_prompt, self.stop_event)
            task.signals.started.connect(self._on_task_started)
            task.signals.log.connect(self.log_edit.append)
            task.signals.finished.connect(self._on_task_finished)
            self.process_pool.start(task)

    def _on_task_started(self, file_path):
        self._set_item_status(file_path, "处理中", COLOR_RUNNING)

    def stop_processing(self):
        if not self.processing:
            return
        self.stop_event.set()
        self.btn_stop.setEnabled(False)
        self.log_edit.append("正在停止...（进行中的任务跑完当前步骤后停下）")

    def retry_failed(self):
        if self.processing or not self.failed_paths:
            return
        lw = self.file_list_widget
        lw.checked_paths = set(self.failed_paths)
        lw.viewport().update()
        self.start_processing()

    def _on_task_finished(self, file_path, success, message):
        self.completed_count += 1
        if success:
            self._set_item_status(file_path, "完成", COLOR_DONE)
            if file_path == self._preview_file:
                self._load_processed_preview(file_path)
        elif message == "已取消":
            self._set_item_status(file_path, "已取消", COLOR_CANCEL)
        else:
            self._set_item_status(file_path, "失败", COLOR_FAIL)
            self.failed_paths.add(file_path)

        self.progress_bar.setValue(
            int(self.completed_count / self.total_count * 100))

        if self.completed_count >= self.total_count:
            self._on_all_finished()

    def _on_all_finished(self):
        self.processing = False
        self.btn_process.setEnabled(True)
        self.btn_stop.setEnabled(False)
        for b in (self.btn_add, self.btn_clear,
                  self.btn_check_all, self.btn_uncheck_all):
            b.setEnabled(True)
        self.btn_retry.setEnabled(bool(self.failed_paths))

        if self.stop_event.is_set():
            self.log_edit.append("处理已停止。")
        else:
            self.log_edit.append("全部处理完成！")