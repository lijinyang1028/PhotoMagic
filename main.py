"""主程序 GUI - PyQt6"""
import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QStackedWidget, QSplitter,
    QMessageBox
)
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtCore import Qt

from processing import ProcessingWidget     #处理页
from image_review import ImageReviewWidget     #图片评价
from rt_processor import check_rt_cli   #RT处理
from about import AboutWidget    #关于页


NAV_ITEMS = ["处理", "照片评价", "标签 2", "关于"]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RAW 照片 AI 处理助手")
        self.resize(1100, 760)
        self.sidebar_expanded_width = 240

        self.init_ui()
        self.apply_theme()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self.apply_theme)
        self.check_dependencies()

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top_bar = QWidget()
        top_bar.setFixedHeight(48)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 0, 12, 0)
        top_layout.setSpacing(12)

        self.btn_toggle = QPushButton("☰")
        self.btn_toggle.setFixedSize(32, 32)
        self.btn_toggle.setObjectName("toggleBtn")
        self.btn_toggle.clicked.connect(self.toggle_sidebar)
        top_layout.addWidget(self.btn_toggle)

        self.title_label = QLabel(NAV_ITEMS[0])
        self.title_label.setObjectName("titleLabel")
        top_layout.addWidget(self.title_label)
        top_layout.addStretch()
        root.addWidget(top_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(1)
        self.splitter.setChildrenCollapsible(True)
        root.addWidget(self.splitter)

        sidebar_widget = QWidget()
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.addItems(NAV_ITEMS)
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self.on_nav_changed)
        sidebar_layout.addWidget(self.sidebar)
        self.splitter.addWidget(sidebar_widget)

        self.stack = QStackedWidget()
        self.splitter.addWidget(self.stack)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([self.sidebar_expanded_width, 860])

        self.processing_widget = ProcessingWidget()
        self.review_widget = ImageReviewWidget(self.processing_widget.get_llm_client)
        self.about_widget = AboutWidget()
        self.stack.addWidget(self.processing_widget)
        self.stack.addWidget(self.review_widget)
        self.stack.addWidget(QWidget())
        self.stack.addWidget(self.about_widget)

    def apply_theme(self, scheme=None):
        if scheme is None:
            scheme = QGuiApplication.styleHints().colorScheme()
        is_dark = scheme == Qt.ColorScheme.Dark

        if is_dark:
            qss = """
            QMainWindow, QWidget { background: #191919; color: #ffffff; }
            #toggleBtn { background: transparent; border: none; color: #ffffff; font-size: 18px; border-radius: 4px; }
            #toggleBtn:hover { background: #2d2d2d; }
            #titleLabel { font-size: 14px; font-weight: 600; color: #ffffff; }
            QSplitter::handle { background: #333333; }
            QListWidget#sidebar { background: #202020; border: none; outline: none; padding: 4px; }
            QListWidget#sidebar::item { height: 40px; padding-left: 16px; border-radius: 6px; color: #ffffff; }
            QListWidget#sidebar::item:hover { background: #2d2d2d; }
            QListWidget#sidebar::item:selected { background: #333333; }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 2px 0;
            }
            QScrollBar::handle:vertical {
                background: #5a5a5a;
                border-radius: 4px;
                min-height: 32px;
            }
            QScrollBar::handle:vertical:hover {
                background: #6e6e6e;
            }
            QScrollBar::handle:vertical:pressed {
                background: #808080;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                background: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar::up-arrow:vertical,
            QScrollBar::down-arrow:vertical {
                width: 0px;
                height: 0px;
                background: none;
            }

            QScrollBar:horizontal {
                background: transparent;
                height: 8px;
                margin: 0 2px;
            }
            QScrollBar::handle:horizontal {
                background: #5a5a5a;
                border-radius: 4px;
                min-width: 32px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #6e6e6e;
            }
            QScrollBar::handle:horizontal:pressed {
                background: #808080;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
                background: none;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
            }
            QScrollBar::left-arrow:horizontal,
            QScrollBar::right-arrow:horizontal {
                width: 0px;
                height: 0px;
                background: none;
            }
            #aboutSubtitle { color: #a0a0a0; }
            #aboutRole { color: #a0a0a0; font-size: 11px; }
            #aboutBio { color: #c0c0c0; font-size: 12px; }
            """
        else:
            qss = """
            QMainWindow, QWidget { background: #f3f3f3; color: #1b1b1b; }
            #toggleBtn { background: transparent; border: none; color: #1b1b1b; font-size: 18px; border-radius: 4px; }
            #toggleBtn:hover { background: #e6e6e6; }
            #titleLabel { font-size: 14px; font-weight: 600; color: #1b1b1b; }
            QSplitter::handle { background: #d0d0d0; }
            QListWidget#sidebar { background: #f9f9f9; border: none; outline: none; padding: 4px; }
            QListWidget#sidebar::item { height: 40px; padding-left: 16px; border-radius: 6px; color: #1b1b1b; }
            QListWidget#sidebar::item:hover { background: #e6e6e6; }
            QListWidget#sidebar::item:selected { background: #ffffff; color: #0067c0; }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 2px 0;
            }
            QScrollBar::handle:vertical {
                background: #c1c1c1;
                border-radius: 4px;
                min-height: 32px;
            }
            QScrollBar::handle:vertical:hover {
                background: #a8a8a8;
            }
            QScrollBar::handle:vertical:pressed {
                background: #909090;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0px;
                background: none;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: none;
            }
            QScrollBar::up-arrow:vertical,
            QScrollBar::down-arrow:vertical {
                width: 0px;
                height: 0px;
                background: none;
            }

            QScrollBar:horizontal {
                background: transparent;
                height: 8px;
                margin: 0 2px;
            }
            QScrollBar::handle:horizontal {
                background: #c1c1c1;
                border-radius: 4px;
                min-width: 32px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #a8a8a8;
            }
            QScrollBar::handle:horizontal:pressed {
                background: #909090;
            }
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                width: 0px;
                background: none;
            }
            QScrollBar::add-page:horizontal,
            QScrollBar::sub-page:horizontal {
                background: none;
            }
            QScrollBar::left-arrow:horizontal,
            QScrollBar::right-arrow:horizontal {
                width: 0px;
                height: 0px;
                background: none;
            }
            #aboutSubtitle { color: #666666; }
            #aboutRole { color: #888888; font-size: 11px; }
            #aboutBio { color: #444444; font-size: 12px; }
            """
        self.setStyleSheet(qss)

        if hasattr(self, "processing_widget"):
            self.processing_widget.set_dark(is_dark)
        if hasattr(self, "review_widget"):
            self.review_widget.set_dark(is_dark)
        if hasattr(self, "about_widget"):
            self.about_widget.set_dark(is_dark)

    def toggle_sidebar(self):
        sizes = self.splitter.sizes()
        if sizes[0] > 0:
            self.sidebar_expanded_width = sizes[0]
            self.splitter.setSizes([0, sizes[1] + sizes[0]])
        else:
            self.splitter.setSizes([self.sidebar_expanded_width, sizes[1] - self.sidebar_expanded_width])

    def on_nav_changed(self, row):
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
        if 0 <= row < len(NAV_ITEMS):
            self.title_label.setText(NAV_ITEMS[row])

    def check_dependencies(self):
        if not check_rt_cli():
            QMessageBox.warning(self, "依赖缺失",
                "未检测到 rawtherapee-cli，请安装 RawTherapee 并将其路径添加到环境变量。")


def main():
    app = QApplication(sys.argv)
    app.setStyle("fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()