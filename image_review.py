"""AI 图像评价：多维度评分 + 雷达图 + 历史记录
-------------------------------------------
导入的话用 from image_review import ImageReviewWidget
9-13-26 li"""
import os
import json
import math
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
    QTextEdit, QFileDialog, QListWidget, QListWidgetItem, QMessageBox,
    QGroupBox, QSplitter
)
from PyQt6.QtGui import QPainter, QPen, QBrush, QColor, QPolygonF, QFont
from PyQt6.QtCore import Qt, QPointF, QRectF, QThread, pyqtSignal

from llm_handler import LLMClient


REVIEW_SYSTEM_PROMPT = """你是一位资深摄影评论家。请对用户提交的照片进行专业评价。

评价维度（每项 0-100 分）：
- 技术：曝光、对焦、清晰度、噪点控制等基本功
- 构图：画面结构、主体突出、视觉引导、平衡感
- 表达：情绪传达、故事性、主题明确度
- 完成度：后期处理、细节打磨、整体呈现

请严格返回 JSON 对象：
{
  "scores": {"技术": 0-100, "构图": 0-100, "表达": 0-100, "完成度": 0-100},
  "summary": "一段整体评价文字",
  "suggestions": ["改进建议1", "改进建议2", "改进建议3"]
}

只返回 JSON，不要任何其他文字。"""


class ReviewThread(QThread):
    result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, llm_client, image_path, description):
        super().__init__()
        self.llm_client = llm_client
        self.image_path = image_path
        self.description = description

    def run(self):
        try:
            user_prompt = "请评价这张照片。"
            if self.description:
                user_prompt += f"\n拍摄者的描述：{self.description}"
            result = self.llm_client.request_json(
                REVIEW_SYSTEM_PROMPT, user_prompt, self.image_path)
            self.result.emit(result)
        except Exception as e:
            self.error.emit(str(e))


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


class ImageReviewWidget(QWidget):
    HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".photomagic_reviews.json")

    def __init__(self, get_llm_client):
        super().__init__()
        self.get_llm_client = get_llm_client
        self.current_image = None
        self.history = self._load_history()
        self.review_thread = None
        self.is_dark = True
        self.init_ui()
        self.refresh_history_list()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_group = QGroupBox("照片评价")
        top_layout = QVBoxLayout(top_group)

        img_row = QHBoxLayout()
        self.image_path_edit = QLineEdit()
        self.image_path_edit.setReadOnly(True)
        self.image_path_edit.setPlaceholderText("未选择图片")
        img_row.addWidget(self.image_path_edit, 1)
        btn_pick = QPushButton("选择图片")
        btn_pick.clicked.connect(self.pick_image)
        img_row.addWidget(btn_pick)
        top_layout.addLayout(img_row)

        desc_row = QHBoxLayout()
        desc_row.addWidget(QLabel("描述:"))
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("用一句话描述这张照片（可选）")
        desc_row.addWidget(self.desc_edit, 1)
        top_layout.addLayout(desc_row)

        btn_row = QHBoxLayout()
        self.btn_submit = QPushButton("提交评价")
        self.btn_submit.clicked.connect(self.submit)
        btn_row.addWidget(self.btn_submit)
        btn_row.addStretch()
        top_layout.addLayout(btn_row)

        layout.addWidget(top_group)

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

        hist_group = QGroupBox("历史记录")
        hist_layout = QHBoxLayout(hist_group)
        self.history_list = QListWidget()
        self.history_list.currentRowChanged.connect(self.on_history_selected)
        hist_layout.addWidget(self.history_list, 1)

        hist_btns = QVBoxLayout()
        btn_view = QPushButton("查看")
        btn_view.clicked.connect(
            lambda: self.on_history_selected(self.history_list.currentRow()))
        hist_btns.addWidget(btn_view)
        btn_del = QPushButton("删除")
        btn_del.clicked.connect(self.delete_history)
        hist_btns.addWidget(btn_del)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self.clear_history)
        hist_btns.addWidget(btn_clear)
        hist_btns.addStretch()
        hist_layout.addLayout(hist_btns)

        layout.addWidget(hist_group)

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        self.radar.set_dark(is_dark)

    def pick_image(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择图片", "",
            "图片 (*.CR2 *.NEF *.ARW *.DNG *.ORF *.RAF *.RW2 *.PEF *.raw "
            "*.jpg *.jpeg *.png *.bmp *.tif *.tiff);;所有文件 (*)"
        )
        if files:
            self.current_image = files[0]
            self.image_path_edit.setText(files[0])

    def submit(self):
        if not self.current_image:
            QMessageBox.information(self, "提示", "请先选择图片。")
            return
        client = self.get_llm_client()
        if client is None:
            QMessageBox.information(self, "提示", "请先在主界面配置 API Key 和模型。")
            return

        self.btn_submit.setEnabled(False)
        self.summary_edit.setPlainText("评价中，请稍候...")
        self.suggestions_edit.clear()

        self.review_thread = ReviewThread(
            client, self.current_image, self.desc_edit.text().strip())
        self.review_thread.result.connect(self.on_review_done)
        self.review_thread.error.connect(self.on_review_error)
        self.review_thread.start()

    def on_review_done(self, result):
        self.btn_submit.setEnabled(True)
        scores = result.get("scores", {})
        summary = result.get("summary", "")
        suggestions = result.get("suggestions", [])

        self.radar.set_scores(scores)
        self.summary_edit.setPlainText(summary)
        self.suggestions_edit.setPlainText(
            "\n".join(f"· {s}" for s in suggestions) if suggestions else "")

        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "image_path": self.current_image,
            "description": self.desc_edit.text().strip(),
            "scores": scores,
            "summary": summary,
            "suggestions": suggestions,
        }
        self.history.insert(0, entry)
        self._save_history()
        self.refresh_history_list()
        self.history_list.setCurrentRow(0)

    def on_review_error(self, msg):
        self.btn_submit.setEnabled(True)
        self.summary_edit.setPlainText(f"评价失败: {msg}")
        QMessageBox.warning(self, "评价失败", msg)

    def refresh_history_list(self):
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for entry in self.history:
            ts = entry.get("timestamp", "")
            name = Path(entry.get("image_path", "")).name
            desc = entry.get("description", "")
            text = f"[{ts}] {name}"
            if desc:
                text += f"  —  {desc[:30]}"
            self.history_list.addItem(QListWidgetItem(text))
        self.history_list.blockSignals(False)

    def on_history_selected(self, row):
        if row < 0 or row >= len(self.history):
            return
        entry = self.history[row]
        self.radar.set_scores(entry.get("scores", {}))
        self.summary_edit.setPlainText(entry.get("summary", ""))
        suggestions = entry.get("suggestions", [])
        self.suggestions_edit.setPlainText(
            "\n".join(f"· {s}" for s in suggestions) if suggestions else "")
        self.image_path_edit.setText(entry.get("image_path", ""))
        self.desc_edit.setText(entry.get("description", ""))
        self.current_image = entry.get("image_path", "")

    def delete_history(self):
        row = self.history_list.currentRow()
        if row < 0:
            return
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