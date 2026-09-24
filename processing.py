"""RAW 批处理页面：选择文件 -> LLM 参数 -> RawTherapee 处理"""
import os
import base64
import json
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTextEdit,
    QLineEdit, QFileDialog, QListWidget, QListWidgetItem, QMessageBox,
    QGroupBox, QFormLayout, QProgressBar, QStyledItemDelegate, QStyle,
    QComboBox, QSpinBox, QScrollArea, QFrame, QSlider
)

from PyQt6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QFont
from PyQt6.QtCore import (
    Qt, QThreadPool, QRunnable, QObject, pyqtSignal, QSize, QRect, QPoint
)

from llm_handler import LLMClient, load_image_base64_from_raw
from rt_processor import (
    generate_pp3, run_rawtherapee,
    describe_params_for_prompt, reload_params, get_params,
    get_builtin_param_names, merge_params,
    load_custom_params, save_custom_params,
    autoload_custom_params, clear_custom_params,
)
from settings import load_settings, save_settings, get_api_key


SYSTEM_PROMPT_HEADER = """你是一个专业的摄影后期处理顾问。请仔细观察我发给你的照片，结合拍摄参数和画面内容，给出最佳的 RawTherapee 后期参数建议。

【美学约束 · 重要】
1. 保持通透：优先保留原始对比度与层次感，避免画面发灰、发闷、发蒙。
2. 慎用压缩类参数：ShadowCompr / HighlightCompr / Shadows / Highlights 容易压平动态范围，除非原片严重欠曝或过曝，否则建议保持在 30 以内。
3. 联动补偿：如果你确实需要较强的阴影提亮（Shadows > 40）或高光压缩（HighlightCompr > 30），必须同时给出正向的 Contrast（建议 10~25）或 LocalContrast，避免画面平淡。
4. 保守原则：如果照片曝光与色彩已经准确，请只返回 1~3 个关键参数，甚至直接返回 {}，不要为了"完成任务"强行增加参数。
5. 优先"还原"而非"风格化"：除非用户明确要求某种风格，请以中性还原为目标。

请严格返回一个 JSON 对象，不要包含任何其他文字。可选键如下（数值，超出范围会被自动钳位）："""

SYSTEM_PROMPT_FOOTER = """
若某参数无需调整，可省略该键。只返回 JSON。"""


def build_system_prompt() -> str:
    return (SYSTEM_PROMPT_HEADER
            + "\n"
            + describe_params_for_prompt()
            + "\n"
            + SYSTEM_PROMPT_FOOTER)


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

FORMAT_MAP = {0: "jpg", 1: "tiff", 2: "png"}
FORMAT_EXT = {"jpg": "jpg", "tiff": "tif", "png": "png"}
DEPTH_MAP = {0: "8", 1: "16", 2: "16f", 3: "32"}
CS_MAP = {0: "RT_sRGB", 1: "RT_Medium_GSH_2.4", 2: "RT_Large_gsRGB"}


# ----------------------------- 后台任务 -----------------------------
class ImageLoadTask(QRunnable):
    """
    后台加载 RAW / 图片缩略图。
    注意：Qt 规定 QPixmap 只能在 GUI 线程中使用，因此这里只产出 QImage，
    由主线程在槽函数里转换为 QPixmap。
    """
    class Signals(QObject):
        loaded = pyqtSignal(str, QImage)
        failed = pyqtSignal(str, str)

    def __init__(self, file_path, thumb_size):
        super().__init__()
        self.file_path = file_path
        self.thumb_size = thumb_size
        self.signals = ImageLoadTask.Signals()

    def run(self):
        try:
            b64 = load_image_base64_from_raw(self.file_path, thumb_size=self.thumb_size)
            img = QImage()
            if img.loadFromData(base64.b64decode(b64), "JPEG"):
                self.signals.loaded.emit(self.file_path, img)
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
                 system_prompt, user_prompt, stop_event,
                 output_format="jpg", jpeg_quality=92,
                 bit_depth="16", output_profile="RT_sRGB",
                 strength=1.0):
        super().__init__()
        self.raw_path = raw_path
        self.output_dir = output_dir
        self.llm_client = llm_client
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.stop_event = stop_event
        self.output_format = output_format
        self.jpeg_quality = jpeg_quality
        self.bit_depth = bit_depth
        self.output_profile = output_profile
        self.strength = strength
        self.signals = ProcessTask.Signals()

    def run(self):
        if self.stop_event.is_set():
            self.signals.finished.emit(self.raw_path, False, "已取消")
            return

        self.signals.started.emit(self.raw_path)
        self.signals.log.emit(f"正在处理: {os.path.basename(self.raw_path)}")
        try:
            # 把 stop_event 传进 LLM 层，用户点停止时可中断网络请求
            params = self.llm_client.request_json(
                self.system_prompt, self.user_prompt, self.raw_path,
                stop_event=self.stop_event)
            self.signals.log.emit(
                f"  LLM 返回参数: {json.dumps(params, ensure_ascii=False)}")

            if self.stop_event.is_set():
                self.signals.finished.emit(self.raw_path, False, "已取消")
                return

            stem = Path(self.raw_path).stem
            pp3_path = os.path.join(self.output_dir, f"{stem}.pp3")
            # generate_pp3 内部会做强度缩放 + 防发灰护栏，返回实际写入的参数
            used_params = generate_pp3(
                params, pp3_path,
                output_profile=self.output_profile,
                strength=self.strength,
            )
            if used_params != params:
                self.signals.log.emit(
                    f"  护栏/缩放后: {json.dumps(used_params, ensure_ascii=False)}")

            run_rawtherapee(
                self.raw_path, pp3_path, self.output_dir,
                output_format=self.output_format,
                jpeg_quality=self.jpeg_quality,
                bit_depth=self.bit_depth,
            )
            ext = FORMAT_EXT.get(self.output_format, "jpg")
            self.signals.log.emit(f"  已完成: {stem}.{ext}")
            self.signals.finished.emit(self.raw_path, True, "完成")
        except InterruptedError:
            self.signals.log.emit("  已取消（网络请求被中止）")
            self.signals.finished.emit(self.raw_path, False, "已取消")
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
    # 请求主窗口跳到设置页
    open_settings_requested = pyqtSignal()

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

        # 启动时自动加载自定义参数（若存在）
        self._custom_loaded, self._custom_errors = autoload_custom_params()

        self.init_ui()
        self._restore_output_settings()
        self._update_api_status()

    # ---------------- UI ----------------
    def init_ui(self):
        # 页面级滚动区域：窗口高度不足时整个处理页可纵向滚动
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.page_scroll = QScrollArea()
        self.page_scroll.setObjectName("pageScroll")
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(self.page_scroll)

        content = QWidget()
        self.page_scroll.setWidget(content)

        layout = QVBoxLayout(content)
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

        # 预览视图放进滚动区域，随窗口弹性伸缩，必要时提供滚动条
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setObjectName("previewScroll")
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.preview_scroll.setMinimumSize(320, 320)
        self.preview_scroll.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self.preview_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.preview_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self.preview_view = BeforeAfterView()
        self.preview_scroll.setWidget(self.preview_view)
        list_preview_layout.addWidget(self.preview_scroll, 1)

        f_layout.addLayout(list_preview_layout)
        layout.addWidget(file_group)

        # 2. LLM 接口（只读状态 + 前往设置）
        llm_group = QGroupBox("2. LLM 接口")
        llm_layout = QVBoxLayout(llm_group)

        self.api_status_label = QLabel("")
        self.api_status_label.setObjectName("apiStatus")
        self.api_status_label.setWordWrap(True)
        llm_layout.addWidget(self.api_status_label)

        goto_row = QHBoxLayout()
        self.btn_goto_settings = QPushButton("前往设置")
        self.btn_goto_settings.setToolTip("在「设置」页配置 API Base URL、Model 与 API Key")
        self.btn_goto_settings.clicked.connect(
            lambda: self.open_settings_requested.emit())
        goto_row.addWidget(self.btn_goto_settings)
        goto_row.addStretch()
        llm_layout.addLayout(goto_row)

        layout.addWidget(llm_group)

        prompt_group = QGroupBox("3. 提示词设置")
        prompt_layout = QFormLayout(prompt_group)
        self.system_prompt_edit = QTextEdit()
        self.system_prompt_edit.setPlainText(build_system_prompt())
        self.system_prompt_edit.setMaximumHeight(160)
        prompt_layout.addRow("System Prompt:", self.system_prompt_edit)
        self.user_prompt_edit = QLineEdit(DEFAULT_USER_PROMPT)
        prompt_layout.addRow("User Prompt:", self.user_prompt_edit)

        reload_row = QHBoxLayout()
        self.btn_reload_schema = QPushButton("重载内置参数")
        self.btn_reload_schema.setToolTip(
            "修改 params_schema.json 后点击，重新生成 System Prompt（保留自定义）")
        self.btn_reload_schema.clicked.connect(self.reload_schema)
        reload_row.addWidget(self.btn_reload_schema)

        self.btn_import_params = QPushButton("导入自定义 JSON")
        self.btn_import_params.setToolTip(
            "从外部 JSON 文件补充参数定义，可叠加多次导入（也可在「设置」页操作）")
        self.btn_import_params.clicked.connect(self.import_custom_params)
        reload_row.addWidget(self.btn_import_params)

        self.btn_clear_params = QPushButton("清空自定义")
        self.btn_clear_params.setToolTip("移除所有已导入的自定义参数")
        self.btn_clear_params.clicked.connect(self.clear_custom)
        reload_row.addWidget(self.btn_clear_params)

        reload_row.addStretch()
        prompt_layout.addRow("", reload_row)

        self.lbl_reload_hint = QLabel("")
        self.lbl_reload_hint.setObjectName("reloadHint")
        self.lbl_reload_hint.setWordWrap(True)
        prompt_layout.addRow("", self.lbl_reload_hint)

        layout.addWidget(prompt_group)

        out_group = QGroupBox("4. 输出设置")
        out_form = QFormLayout(out_group)

        dir_row = QHBoxLayout()
        self.out_dir_edit = QLineEdit(self.output_dir)
        dir_row.addWidget(self.out_dir_edit, 1)
        self.btn_browse = QPushButton("浏览")
        self.btn_browse.clicked.connect(self.browse_output)
        dir_row.addWidget(self.btn_browse)
        out_form.addRow("输出目录:", dir_row)

        fmt_row = QHBoxLayout()
        self.format_combo = QComboBox()
        self.format_combo.addItems(["JPEG (.jpg)", "TIFF (.tif)", "PNG (.png)"])
        fmt_row.addWidget(self.format_combo)

        fmt_row.addWidget(QLabel("位深:"))
        self.bit_depth_combo = QComboBox()
        self.bit_depth_combo.addItems(["8", "16", "16f", "32"])
        self.bit_depth_combo.setCurrentIndex(1)
        fmt_row.addWidget(self.bit_depth_combo)

        fmt_row.addWidget(QLabel("JPEG 质量:"))
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(1, 100)
        self.quality_spin.setValue(92)
        fmt_row.addWidget(self.quality_spin)
        fmt_row.addStretch()
        out_form.addRow("格式:", fmt_row)

        cs_row = QHBoxLayout()
        self.color_space_combo = QComboBox()
        self.color_space_combo.addItems(["sRGB", "Adobe RGB", "ProPhoto RGB"])
        cs_row.addWidget(self.color_space_combo)

        cs_row.addWidget(QLabel("并发数:"))
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(MAX_PARALLEL)
        cs_row.addWidget(self.parallel_spin)
        cs_row.addStretch()
        out_form.addRow("色彩空间:", cs_row)

        # AI 强度：0% = 原图，100% = 完整应用 LLM 返回的参数
        strength_row = QHBoxLayout()
        self.strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.strength_slider.setRange(0, 100)
        self.strength_slider.setValue(100)
        self.strength_slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self.strength_slider.setToolTip(
            "0% = 不使用 AI 参数（等同原图），100% = 完整应用 LLM 建议值\n"
            "觉得处理过头/发灰时可以调低，无需重新调用 LLM")
        strength_row.addWidget(self.strength_slider, 1)

        self.strength_label = QLabel("100%")
        self.strength_label.setFixedWidth(48)
        self.strength_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                          Qt.AlignmentFlag.AlignVCenter)
        strength_row.addWidget(self.strength_label)

        self.strength_slider.valueChanged.connect(
            lambda v: self.strength_label.setText(f"{v}%"))
        out_form.addRow("AI 强度:", strength_row)

        layout.addWidget(out_group)

        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        self._on_format_changed(0)

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

        # 显示自动加载自定义参数的结果
        if self._custom_loaded:
            msg = f"已自动加载 {self._custom_loaded} 个自定义参数"
            if self._custom_errors:
                msg += f"，{len(self._custom_errors)} 个被跳过"
            self.lbl_reload_hint.setText(msg)
        elif self._custom_errors:
            self.lbl_reload_hint.setText(
                "自定义参数文件有问题：" + self._custom_errors[0])

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.preview_view.set_dark(is_dark)
        self.file_list_widget.viewport().update()

    # ---------------- API 状态 ----------------
    def reload_api_config(self):
        """设置页保存后由主窗口调用，刷新顶部状态。"""
        self._update_api_status()

    def _update_api_status(self):
        s = load_settings()
        api_base = s.get("api_base", "").strip()
        model = s.get("model", "").strip()
        api_key = get_api_key()

        if not api_base or not model:
            self.api_status_label.setText(
                "⚠ 尚未配置 API 接口。请前往「设置」页填写 API Base URL 与 Model。")
        elif not api_key:
            self.api_status_label.setText(
                f"当前接口：{model} @ {api_base}\n"
                "⚠ 尚未配置 API Key。请前往「设置」页填写。")
        else:
            self.api_status_label.setText(
                f"当前接口：{model} @ {api_base}\n"
                "API Key：已配置")

    # ---------------- 输出设置持久化 ----------------
    def _restore_output_settings(self):
        s = load_settings()
        od = s.get("output_dir", "").strip()
        if od and os.path.isdir(od):
            self.output_dir = od
        self.out_dir_edit.setText(self.output_dir)

        self.format_combo.setCurrentIndex(int(s.get("output_format", 0)))
        self.bit_depth_combo.setCurrentIndex(int(s.get("bit_depth", 1)))
        self.quality_spin.setValue(int(s.get("jpeg_quality", 92)))
        self.color_space_combo.setCurrentIndex(int(s.get("color_space", 0)))
        self.parallel_spin.setValue(int(s.get("parallel", MAX_PARALLEL)))

        strength = int(s.get("strength", 100))
        self.strength_slider.setValue(strength)
        self.strength_label.setText(f"{strength}%")

    def _persist_output_settings(self):
        try:
            s = load_settings()
            s["output_dir"] = self.out_dir_edit.text().strip()
            s["output_format"] = self.format_combo.currentIndex()
            s["bit_depth"] = self.bit_depth_combo.currentIndex()
            s["jpeg_quality"] = self.quality_spin.value()
            s["color_space"] = self.color_space_combo.currentIndex()
            s["parallel"] = self.parallel_spin.value()
            s["strength"] = self.strength_slider.value()
            save_settings(s)
        except Exception:
            pass

    def _on_format_changed(self, index):
        is_jpeg = (index == 0)
        self.quality_spin.setEnabled(is_jpeg)
        self.bit_depth_combo.setEnabled(not is_jpeg)

    # ---------------- 参数表管理 ----------------
    def reload_schema(self):
        """重新读取内置 params_schema.json，保留已导入的自定义参数。"""
        try:
            builtin_names = get_builtin_param_names()
            custom_snapshot = {
                k: v for k, v in get_params().items()
                if k not in builtin_names
            }
            reload_params()
            if custom_snapshot:
                merge_params(custom_snapshot)

            self.system_prompt_edit.setPlainText(build_system_prompt())
            self.lbl_reload_hint.setText(
                f"已重载内置参数，自定义参数保留 {len(custom_snapshot)} 个")
        except Exception as e:
            self.lbl_reload_hint.setText(f"重载失败: {e}")

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
            self.lbl_reload_hint.setText(
                f"导入失败：{errors[0] if errors else '未知错误'}")
            return

        try:
            save_custom_params()
        except Exception as e:
            self.lbl_reload_hint.setText(f"导入成功，但持久化失败: {e}")
        else:
            if errors:
                self.lbl_reload_hint.setText(
                    f"已导入 {count} 个参数（{len(errors)} 个被跳过，"
                    f"首个错误：{errors[0]}）")
            else:
                self.lbl_reload_hint.setText(f"已导入 {count} 个参数")

        self.system_prompt_edit.setPlainText(build_system_prompt())

    def clear_custom(self):
        if QMessageBox.question(
                self, "确认", "移除所有已导入的自定义参数？"
        ) != QMessageBox.StandardButton.Yes:
            return
        clear_custom_params()
        self.system_prompt_edit.setPlainText(build_system_prompt())
        self.lbl_reload_hint.setText("已清空自定义参数")

    def get_llm_client(self):
        """从 settings 读取最新 API 配置；被处理页与评价页共用。"""
        api_key = get_api_key()
        if not api_key:
            return None
        s = load_settings()
        return LLMClient(
            api_base=s.get("api_base", "").strip(),
            api_key=api_key,
            model=s.get("model", "").strip(),
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

    def _on_thumb_loaded(self, file_path, image):
        self._thumb_loading.discard(file_path)
        self._thumb_cache[file_path] = QPixmap.fromImage(image)
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

        fmt = FORMAT_MAP.get(self.format_combo.currentIndex(), "jpg")
        candidates = [FORMAT_EXT.get(fmt, "jpg")]
        for v in FORMAT_EXT.values():
            if v not in candidates:
                candidates.append(v)

        out_path = None
        for ext in candidates:
            p = os.path.join(out_dir, f"{stem}.{ext}")
            if os.path.exists(p):
                out_path = p
                break

        if out_path is None:
            self.preview_view.set_processed(None)
            return

        task = ImageLoadTask(out_path, (800, 800))
        task.signals.loaded.connect(self._on_preview_processed_loaded)
        task.signals.failed.connect(self._on_preview_failed)
        self.thumb_pool.start(task)

    def _on_preview_original_loaded(self, file_path, image):
        if file_path != self._preview_file:
            return
        self.preview_view.set_original(QPixmap.fromImage(image))

    def _on_preview_processed_loaded(self, file_path, image):
        if self._preview_file is None:
            return
        if Path(file_path).stem != Path(self._preview_file).stem:
            return
        self.preview_view.set_processed(QPixmap.fromImage(image))

    def _on_preview_failed(self, file_path, err):
        pass

    def browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.output_dir = d
            self.out_dir_edit.setText(d)
            self._persist_output_settings()

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
        if not get_api_key():
            QMessageBox.information(
                self, "提示",
                "尚未配置 API Key。请前往「设置」页填写。")
            return

        self.output_dir = self.out_dir_edit.text().strip()
        os.makedirs(self.output_dir, exist_ok=True)
        llm_client = self.get_llm_client()

        output_format = FORMAT_MAP.get(self.format_combo.currentIndex(), "jpg")
        bit_depth = DEPTH_MAP.get(self.bit_depth_combo.currentIndex(), "16")
        output_profile = CS_MAP.get(self.color_space_combo.currentIndex(), "RT_sRGB")
        jpeg_quality = self.quality_spin.value()
        strength = self.strength_slider.value() / 100.0

        # 记住本批输出设置
        self._persist_output_settings()

        self.process_pool.setMaxThreadCount(self.parallel_spin.value())

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
            task = ProcessTask(
                raw_path, self.output_dir, llm_client,
                system_prompt, user_prompt, self.stop_event,
                output_format=output_format,
                jpeg_quality=jpeg_quality,
                bit_depth=bit_depth,
                output_profile=output_profile,
                strength=strength,
            )
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