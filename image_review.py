"""AI 图像评价：多维度评分 + 雷达图 + 历史记录
-------------------------------------------
导入的话用 from image_review import ImageReviewWidget

改进记录（v0.4）：
- 左侧新增图片预览面板（异步加载，支持 RAW）
- 提交后按钮变为「取消」，可中断正在进行的 LLM 请求
- 强化 System Prompt：评分锚点 + 要求拉开分差 + 可操作建议
- 历史记录支持多选，可导出 Markdown 报告
- 修复 QThread 生命周期 & 提交后输入框被改动污染结果的问题
"""
import base64
import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTextEdit, QFileDialog, QListWidget, QListWidgetItem, QMessageBox,
    QGroupBox, QSplitter, QScrollArea, QFrame
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QBrush, QColor, QPolygonF, QFont,
    QDesktopServices
)
from PyQt6.QtCore import (
    Qt, QPointF, QRectF, QThread, QThreadPool, QRunnable, QObject,
    pyqtSignal, QSize, QRect, QUrl
)

from llm_handler import LLMClient, load_image_base64_from_raw


# ---------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------
REVIEW_SYSTEM_PROMPT = """你是一位资深摄影评论家。请对用户提交的照片进行专业、客观的评价。

【评分维度】每项 0-100 分：
- 技术：曝光、对焦、清晰度、噪点控制、动态范围等摄影基本功
- 构图：画面结构、主体突出、视觉引导、平衡感、留白
- 表达：情绪传达、故事性、主题明确度、氛围营造
- 完成度：后期处理、色彩管理、细节打磨、整体呈现

【评分锚点】
- 90-100：优秀，专业水准，几乎无可挑剔
- 75-89：良好，有明确优点，细节可优化
- 60-74：中等，及格水平，存在明显问题
- 40-59：较弱，有硬伤需要重点改进
- 0-39：很差，基础层面存在严重问题

【要求】
1. 严格根据实际画面评分，不要都给 80 分左右，该低就低。
2. summary 用 2-4 句话概括核心优缺点，不要空话套话。
3. suggestions 给出 3-5 条具体、可操作的建议，每条说明「问题 + 改法」。
4. 若用户提供了描述，请结合描述判断表达维度。

请严格返回 JSON 对象：
{
  "scores": {"技术": 0-100, "构图": 0-100, "表达": 0-100, "完成度": 0-100},
  "summary": "一段整体评价文字",
  "suggestions": ["改进建议1", "改进建议2", "改进建议3"]
}

只返回 JSON，不要任何其他文字。"""


IMAGE_FILTER = (
    "图片 (*.CR2 *.NEF *.ARW *.DNG *.ORF *.RAF *.RW2 *.PEF *.raw "
    "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.webp);;所有文件 (*)"
)


# ---------------------------------------------------------------
# 异步加载图片（支持 RAW）
# ---------------------------------------------------------------
class PreviewLoadTask(QRunnable):
    """后台加载任意图片（含 RAW）为 QImage。"""
    class Signals(QObject):
        loaded = pyqtSignal(str, QImage)
        failed = pyqtSignal(str)

    def __init__(self, path, size=(900, 900)):
        super().__init__()
        self.path = path
        self.size = size
        self.signals = PreviewLoadTask.Signals()

    def run(self):
        try:
            b64 = load_image_base64_from_raw(self.path, thumb_size=self.size)
            img = QImage()
            if img.loadFromData(base64.b64decode(b64), "JPEG"):
                self.signals.loaded.emit(self.path, img)
            else:
                self.signals.failed.emit(self.path)
        except Exception:
            self.signals.failed.emit(self.path)


# ---------------------------------------------------------------
# 预览面板（自绘，按比例适配，居中）
# ---------------------------------------------------------------
class ImagePreviewLabel(QWidget):
    def __init__(self):
        super().__init__()
        self._pixmap = None
        self._loading = False
        self.is_dark = True
        self.setMinimumSize(280, 280)

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._loading = False
        self.update()

    def set_loading(self):
        self._pixmap = None
        self._loading = True
        self.update()

    def clear(self):
        self._pixmap = None
        self._loading = False
        self.update()

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        bg = QColor("#202020") if self.is_dark else QColor("#f5f5f5")
        p.fillRect(self.rect(), bg)

        if self._pixmap is not None and not self._pixmap.isNull():
            pw, ph = self._pixmap.width(), self._pixmap.height()
            w, h = self.width(), self.height()
            if pw > 0 and ph > 0:
                scale = min(w / pw, h / ph)
                nw, nh = int(pw * scale), int(ph * scale)
                x, y = (w - nw) // 2, (h - nh) // 2
                p.drawPixmap(x, y, nw, nh, self._pixmap)
            return

        text = "加载中..." if self._loading else "未选择图片"
        p.setPen(QColor("#808080"))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)


# ---------------------------------------------------------------
# LLM 评价线程
# ---------------------------------------------------------------
class ReviewThread(QThread):
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, llm_client, image_path, description, stop_event):
        super().__init__()
        self.llm_client = llm_client
        self.image_path = image_path
        self.description = description
        self.stop_event = stop_event

    def run(self):
        try:
            user_prompt = "请评价这张照片。"
            if self.description:
                user_prompt += f"\n拍摄者的描述：{self.description}"
            result = self.llm_client.request_json(
                REVIEW_SYSTEM_PROMPT, user_prompt, self.image_path,
                stop_event=self.stop_event)
            self.result.emit(result)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------
# 雷达图
# ---------------------------------------------------------------
class RadarChart(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(360, 360)
        self.scores = {}
        self.max_value = 100
        self.is_dark = True
        self._update_colors()

    def _update_colors(self):
        if self.is_dark:
            self.grid_color = QColor("#3a3a3a")
            self.text_color = QColor("#e0e0e0")
            self.line_color = QColor("#4a90d9")
            self.fill_color = QColor(74, 144, 217, 70)
        else:
            self.grid_color = QColor("#d0d0d0")
            self.text_color = QColor("#222222")
            self.line_color = QColor("#0067c0")
            self.fill_color = QColor(0, 103, 192, 70)

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self._update_colors()
        self.update()

    def set_scores(self, scores):
        self.scores = dict(scores) if scores else {}
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 80

        if not self.scores or radius <= 0:
            painter.setPen(self.text_color)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无评价数据")
            return

        labels = list(self.scores.keys())
        n = len(labels)
        angles = [-math.pi / 2 + 2 * math.pi * i / n for i in range(n)]

        painter.setPen(QPen(self.grid_color, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for level in range(1, 6):
            r = radius * level / 5
            poly = QPolygonF([
                QPointF(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles
            ])
            painter.drawPolygon(poly)

        for a in angles:
            painter.drawLine(
                QPointF(cx, cy),
                QPointF(cx + radius * math.cos(a), cy + radius * math.sin(a))
            )

        data_poly = QPolygonF()
        for i, label in enumerate(labels):
            value = float(self.scores.get(label, 0))
            ratio = max(0.0, min(1.0, value / self.max_value))
            r = radius * ratio
            a = angles[i]
            data_poly.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))

        painter.setBrush(QBrush(self.fill_color))
        painter.setPen(QPen(self.line_color, 2))
        painter.drawPolygon(data_poly)

        painter.setBrush(QBrush(self.line_color))
        for p in data_poly:
            painter.drawEllipse(p, 4, 4)

        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.text_color)
        for i, label in enumerate(labels):
            a = angles[i]
            x = cx + (radius + 30) * math.cos(a)
            y = cy + (radius + 30) * math.sin(a)
            value = int(self.scores.get(label, 0))
            rect = QRectF(x - 40, y - 22, 80, 44)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{label}\n{value}")


# ---------------------------------------------------------------
# 主控件
# ---------------------------------------------------------------
class ImageReviewWidget(QWidget):
    HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".photomagic_reviews.json")

    def __init__(self, get_llm_client):
        super().__init__()
        self.get_llm_client = get_llm_client
        self.current_image = None
        self.history = self._load_history()
        self.review_thread = None
        self.stop_event = threading.Event()
        self.is_dark = True
        self._preview_current = None
        self._pending_image = None
        self._pending_desc = ""

        self._thumb_pool = QThreadPool()
        self._thumb_pool.setMaxThreadCount(2)

        self.init_ui()
        self.refresh_history_list()

    # ---------------- UI ----------------
    def init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 页面级滚动区域，窗口高度不足时可滚动
        self.page_scroll = QScrollArea()
        self.page_scroll.setObjectName("pageScroll")
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.page_scroll)

        content = QWidget()
        self.page_scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # ---------- 顶部：预览 + 控件 ----------
        top_group = QGroupBox("照片评价")
        top_layout = QHBoxLayout(top_group)
        top_layout.setSpacing(12)

        self.preview = ImagePreviewLabel()
        top_layout.addWidget(self.preview, 1)

        right_col = QVBoxLayout()
        right_col.setSpacing(8)

        path_row = QHBoxLayout()
        self.image_path_edit = QLineEdit()
        self.image_path_edit.setReadOnly(True)
        self.image_path_edit.setPlaceholderText("未选择图片")
        path_row.addWidget(self.image_path_edit, 1)
        self.btn_pick = QPushButton("选择图片")
        self.btn_pick.clicked.connect(self.pick_image)
        path_row.addWidget(self.btn_pick)
        right_col.addLayout(path_row)

        right_col.addWidget(QLabel("描述:"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("用一句话描述这张照片（可选）")
        right_col.addWidget(self.desc_edit)

        btn_row = QHBoxLayout()
        self.btn_submit = QPushButton("提交评价")
        self.btn_submit.clicked.connect(self.submit)
        btn_row.addWidget(self.btn_submit)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_review)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        right_col.addLayout(btn_row)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("reviewStatus")
        self.status_label.setWordWrap(True)
        right_col.addWidget(self.status_label)

        right_col.addStretch()
        top_layout.addLayout(right_col, 1)

        layout.addWidget(top_group)

        # ---------- 中部：雷达图 + 文本 ----------
        mid_splitter = QSplitter(Qt.Orientation.Horizontal)

        chart_container = QWidget()
        chart_layout = QVBoxLayout(chart_container)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        self.radar = RadarChart()
        chart_layout.addWidget(self.radar)
        mid_splitter.addWidget(chart_container)

        text_container = QWidget()
        text_layout = QVBoxLayout(text_container)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(QLabel("综合评价"))
        self.summary_edit = QTextEdit()
        self.summary_edit.setReadOnly(True)
        text_layout.addWidget(self.summary_edit)
        text_layout.addWidget(QLabel("改进建议"))
        self.suggestions_edit = QTextEdit()
        self.suggestions_edit.setReadOnly(True)
        text_layout.addWidget(self.suggestions_edit)
        mid_splitter.addWidget(text_container)

        mid_splitter.setSizes([400, 500])
        layout.addWidget(mid_splitter, 1)

        # ---------- 底部：历史记录 ----------
        hist_group = QGroupBox("历史记录")
        hist_layout = QHBoxLayout(hist_group)

        self.history_list = QListWidget()
        self.history_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        self.history_list.currentRowChanged.connect(self.on_history_selected)
        self.history_list.itemSelectionChanged.connect(self._refresh_history_btns)
        hist_layout.addWidget(self.history_list, 1)

        hist_btns = QVBoxLayout()
        self.btn_view = QPushButton("查看")
        self.btn_view.clicked.connect(
            lambda: self.on_history_selected(self.history_list.currentRow()))
        hist_btns.addWidget(self.btn_view)

        self.btn_export = QPushButton("导出选中")
        self.btn_export.setToolTip("把选中的历史记录导出为 Markdown 报告")
        self.btn_export.clicked.connect(self.export_selected)
        hist_btns.addWidget(self.btn_export)

        self.btn_del = QPushButton("删除")
        self.btn_del.clicked.connect(self.delete_history)
        hist_btns.addWidget(self.btn_del)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self.clear_history)
        hist_btns.addWidget(self.btn_clear)

        hist_btns.addStretch()
        hist_layout.addLayout(hist_btns)

        layout.addWidget(hist_group)
        self._refresh_history_btns()

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.radar.set_dark(is_dark)
        self.preview.set_dark(is_dark)

    # ---------------- 图片选择 / 预览 ----------------
    def pick_image(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "", IMAGE_FILTER)
        if not files:
            return
        self.current_image = files[0]
        self.image_path_edit.setText(files[0])
        self._load_preview(files[0])
        self.status_label.setText("已选择图片，点击「提交评价」")

    def _load_preview(self, path):
        self._preview_current = path
        self.preview.set_loading()
        task = PreviewLoadTask(path)
        task.signals.loaded.connect(self._on_preview_loaded)
        task.signals.failed.connect(self._on_preview_failed)
        self._thumb_pool.start(task)

    def _on_preview_loaded(self, path, image):
        if path != self._preview_current:
            return
        self.preview.set_pixmap(QPixmap.fromImage(image))

    def _on_preview_failed(self, path):
        if path != self._preview_current:
            return
        self.preview.clear()
        self.status_label.setText("图片加载失败（可能已移动或损坏）")

    # ---------------- 提交 / 取消 ----------------
    def submit(self):
        if not self.current_image:
            QMessageBox.information(self, "提示", "请先选择图片。")
            return
        if self.review_thread is not None and self.review_thread.isRunning():
            return
        client = self.get_llm_client()
        if client is None:
            QMessageBox.information(
                self, "提示", "请先在设置界面配置 API Key 和模型。")
            return

        # 快照当前输入，防止提交后用户改动污染结果
        self._pending_image = self.current_image
        self._pending_desc = self.desc_edit.text().strip()

        self.stop_event.clear()
        self.btn_submit.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_pick.setEnabled(False)
        self.desc_edit.setEnabled(False)
        self.summary_edit.setPlainText("评价中，请稍候...")
        self.suggestions_edit.clear()
        self.status_label.setText("正在调用 LLM 评价...")

        self.review_thread = ReviewThread(
            client, self._pending_image, self._pending_desc, self.stop_event)
        self.review_thread.result.connect(self.on_review_done)
        self.review_thread.error.connect(self.on_review_error)
        self.review_thread.cancelled.connect(self.on_review_cancelled)
        self.review_thread.finished.connect(self._on_thread_finished)
        self.review_thread.start()

    def cancel_review(self):
        if self.review_thread is None or not self.review_thread.isRunning():
            return
        self.stop_event.set()
        self.status_label.setText("正在取消...")
        self.btn_cancel.setEnabled(False)

    def _on_thread_finished(self):
        """QThread 生命周期清理：完成后释放引用。"""
        self.review_thread = None
        self.btn_submit.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_pick.setEnabled(True)
        self.desc_edit.setEnabled(True)

    def _restore_buttons(self):
        self.btn_submit.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_pick.setEnabled(True)
        self.desc_edit.setEnabled(True)

    # ---------------- 结果回调 ----------------
    def on_review_done(self, result):
        self._restore_buttons()
        scores = result.get("scores", {})
        summary = result.get("summary", "")
        suggestions = result.get("suggestions", [])

        self.radar.set_scores(scores)
        self.summary_edit.setPlainText(summary)
        self.suggestions_edit.setPlainText(
            "\n".join(f"· {s}" for s in suggestions) if suggestions else "")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "timestamp": ts,
            "image_path": self._pending_image,
            "description": self._pending_desc,
            "scores": scores,
            "summary": summary,
            "suggestions": suggestions,
        }
        self.history.insert(0, entry)
        self._save_history()
        self.refresh_history_list()
        self.history_list.setCurrentRow(0)
        self.status_label.setText(f"评价完成 · {ts}")

    def on_review_error(self, msg):
        self._restore_buttons()
        self.summary_edit.setPlainText(f"评价失败: {msg}")
        self.status_label.setText("评价失败")
        QMessageBox.warning(self, "评价失败", msg)

    def on_review_cancelled(self):
        self._restore_buttons()
        self.summary_edit.setPlainText("已取消")
        self.status_label.setText("已取消")

    # ---------------- 历史记录 ----------------
    def refresh_history_list(self):
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for entry in self.history:
            ts = entry.get("timestamp", "")
            name = Path(entry.get("image_path", "")).name
            scores = entry.get("scores", {})
            avg = sum(scores.values()) / len(scores) if scores else 0
            text = f"[{ts}]  {name}   —   综合 {avg:.1f}"
            self.history_list.addItem(QListWidgetItem(text))
        self.history_list.blockSignals(False)
        self._refresh_history_btns()

    def _refresh_history_btns(self):
        has_sel = bool(self.history_list.selectedItems())
        self.btn_export.setEnabled(has_sel)
        self.btn_del.setEnabled(has_sel)
        self.btn_clear.setEnabled(bool(self.history))

    def on_history_selected(self, row):
        if row < 0 or row >= len(self.history):
            return
        entry = self.history[row]
        self.radar.set_scores(entry.get("scores", {}))
        self.summary_edit.setPlainText(entry.get("summary", ""))
        suggestions = entry.get("suggestions", [])
        self.suggestions_edit.setPlainText(
            "\n".join(f"· {s}" for s in suggestions) if suggestions else "")
        img_path = entry.get("image_path", "")
        self.image_path_edit.setText(img_path)
        self.desc_edit.setText(entry.get("description", ""))
        self.current_image = img_path
        if img_path and os.path.exists(img_path):
            self._load_preview(img_path)
        else:
            self.preview.clear()
            self.status_label.setText("历史记录：图片文件已不存在")
        ts = entry.get("timestamp", "")
        if ts:
            self.status_label.setText(f"查看历史记录 · {ts}")

    def delete_history(self):
        rows = sorted({self.history_list.row(item)
                       for item in self.history_list.selectedItems()},
                      reverse=True)
        if not rows:
            return
        if QMessageBox.question(
                self, "确认", f"删除选中的 {len(rows)} 条记录？"
        ) != QMessageBox.StandardButton.Yes:
            return
        for row in rows:
            del self.history[row]
        self._save_history()
        self.refresh_history_list()

    def clear_history(self):
        if not self.history:
            return
        if QMessageBox.question(
                self, "确认", "确定清空所有历史记录？"
        ) == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self._save_history()
            self.refresh_history_list()
            self.radar.set_scores({})
            self.summary_edit.clear()
            self.suggestions_edit.clear()
            self.status_label.setText("历史记录已清空")

    # ---------------- 导出 ----------------
    def export_selected(self):
        rows = sorted({self.history_list.row(item)
                       for item in self.history_list.selectedItems()})
        if not rows:
            QMessageBox.information(self, "提示", "请先选中要导出的历史记录。")
            return
        entries = [self.history[r] for r in rows]

        default_name = (
            f"review_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "导出 Markdown 报告", default_name,
            "Markdown 文件 (*.md);;所有文件 (*)")
        if not out_path:
            return

        try:
            self._write_markdown(entries, out_path)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return

        self.status_label.setText(f"已导出 {len(entries)} 条记录到 {out_path}")
        QMessageBox.information(self, "导出成功",
                                f"已导出 {len(entries)} 条记录。")

    def _write_markdown(self, entries, out_path):
        lines = ["# 照片评价报告", ""]
        lines.append(
            f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"共 {len(entries)} 条记录")
        lines.append("")

        for i, entry in enumerate(entries, 1):
            ts = entry.get("timestamp", "")
            img = entry.get("image_path", "")
            lines.append(f"## {i}. {ts} — {Path(img).name}")
            lines.append("")
            lines.append(f"**文件路径：** `{img}`")
            lines.append("")

            desc = entry.get("description", "")
            if desc:
                lines.append(f"**拍摄者描述：** {desc}")
                lines.append("")

            scores = entry.get("scores", {})
            if scores:
                try:
                    avg = sum(float(v) for v in scores.values()) / len(scores)
                except (TypeError, ValueError):
                    avg = 0
                lines.append(f"**综合分：** {avg:.1f}")
                lines.append("")
                lines.append("| 维度 | 分数 |")
                lines.append("|------|------|")
                for k, v in scores.items():
                    lines.append(f"| {k} | {v} |")
                lines.append("")

            summary = entry.get("summary", "")
            if summary:
                lines.append("**综合评价：**")
                lines.append("")
                lines.append(summary)
                lines.append("")

            suggestions = entry.get("suggestions", [])
            if suggestions:
                lines.append("**改进建议：**")
                lines.append("")
                for s in suggestions:
                    lines.append(f"- {s}")
                lines.append("")

            lines.append("---")
            lines.append("")

        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ---------------- 持久化 ----------------
    def _load_history(self):
        if not os.path.exists(self.HISTORY_FILE):
            return []
        try:
            with open(self.HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _save_history(self):
        try:
            with open(self.HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存历史失败: {e}")