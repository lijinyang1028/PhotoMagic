"""关于页：项目信息、合作者、API 注册指引"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox,
    QTextEdit, QPushButton, QScrollArea, QFrame
)
from PyQt6.QtGui import QDesktopServices, QFont, QPainter, QColor, QPen
from PyQt6.QtCore import Qt, QUrl, QRect


REPO_URL = "https://github.com/lijinyang1028/photomagic.git"
DEEPSEEK_URL = "https://platform.deepseek.com/sign_in"

VERSION = "0.3.0"

CONTRIBUTORS = [
    {
        "name": "李金洋",
        "github": "lijinyang1028",
        "url": "https://github.com/lijinyang1028",
        "role": "项目发起人 / 核心开发",
        "bio": "---",
    },
    {
        "name": "张瑞麟",
        "github": "",
        "url": "",
        "role": "打酱油的",
        "bio": "---",
    },
    {
        "name": "老老老陈醋@zhihu",
        "github": "Charliechen114514",
        "url": "https://github.com/Charliechen114514",
        "role": "来自知乎的大佬，贡献了测试以及LLM返回的正则处理",
        "bio": "---",
    },
]

API_STEPS = [
    ("1. 打开 DeepSeek 开放平台",
     "访问 https://www.deepseek.com，点击首页的「API 开放平台」入口。"),
    ("2. 注册 / 登录",
     "使用手机号接收验证码登录。未注册的手机号将自动完成注册。"),
    ("3. 充值（如需要）",
     "在左侧菜单点击「充值」，选择支付宝或微信支付。新账户通常有免费额度。"),
    ("4. 创建 API Key",
     "点击左侧菜单「API keys」→ 右上角「创建 API key」→ 复制以 sk- 开头的密钥。"),
    ("5. 填入本软件",
     "在「处理」页的 API Key 输入框中粘贴该密钥，"
     "API Base URL 填 https://api.deepseek.com/v1，"
     "Model 填 deepseek-chat（或支持视觉的模型名）。"),
]


class AvatarLabel(QLabel):
    def __init__(self, name, is_dark):
        super().__init__()
        self._name = name
        self._is_dark = is_dark
        self.setFixedSize(48, 48)

    def set_dark(self, is_dark):
        self._is_dark = is_dark
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor("#2563eb") if self._is_dark else QColor("#0067c0")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawEllipse(self.rect())

        initial = self._name[0] if self._name else "?"
        p.setPen(QColor("white"))
        f = p.font()
        f.setPointSize(18)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, initial)


class AboutWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.is_dark = True
        self._avatar_labels = []
        self.init_ui()

    def init_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # ---------- 标题 ----------
        title = QLabel("PhotoMagic")
        f = QFont()
        f.setPointSize(22)
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        subtitle = QLabel(f"RAW 照片 AI 处理助手  ·  v{VERSION}")
        subtitle.setObjectName("aboutSubtitle")
        layout.addWidget(subtitle)

        desc = QLabel(
            "利用 LLM 视觉模型分析 RAW 照片内容，自动生成 RawTherapee 后期参数，\n"
            "批量输出高质量 JPEG。附带 AI 照片评价、多维度雷达图与前后对比功能。")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        repo_row = QHBoxLayout()
        self.btn_repo = QPushButton("GitHub 仓库")
        self.btn_repo.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(REPO_URL)))
        repo_row.addWidget(self.btn_repo)
        repo_row.addStretch()
        layout.addLayout(repo_row)

        # ---------- 合作者 ----------
        contrib_group = QGroupBox("项目成员")
        contrib_layout = QVBoxLayout(contrib_group)
        for c in CONTRIBUTORS:
            contrib_layout.addWidget(self._build_contributor_row(c))
        layout.addWidget(contrib_group)

        # ---------- API 注册指引 ----------
        api_group = QGroupBox("LLM API 注册指引")
        api_layout = QVBoxLayout(api_group)

        api_intro = QLabel(
            "PhotoMagic 需要接入一个兼容 OpenAI 接口的视觉 LLM 服务。"
            "以下以 DeepSeek 为例，其他提供者（OpenAI、硅基流动等）流程类似。")
        api_intro.setWordWrap(True)
        api_layout.addWidget(api_intro)

        for step_title, step_desc in API_STEPS:
            step_label = QLabel(f"<b>{step_title}</b><br>{step_desc}")
            step_label.setWordWrap(True)
            step_label.setTextFormat(Qt.TextFormat.RichText)
            api_layout.addWidget(step_label)

        btn_row = QHBoxLayout()
        self.btn_ds = QPushButton("打开 DeepSeek 开放平台")
        self.btn_ds.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(DEEPSEEK_URL)))
        btn_row.addWidget(self.btn_ds)
        btn_row.addStretch()
        api_layout.addLayout(btn_row)

        layout.addWidget(api_group)

        # ---------- 许可 ----------
        lic_group = QGroupBox("许可")
        lic_layout = QVBoxLayout(lic_group)
        lic_label = QLabel("本项目基于 MIT 许可证开源。")
        lic_label.setWordWrap(True)
        lic_layout.addWidget(lic_label)
        layout.addWidget(lic_group)

        layout.addStretch()

    def _build_contributor_row(self, c):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 6, 0, 6)
        h.setSpacing(12)

        avatar = AvatarLabel(c["name"], self.is_dark)
        self._avatar_labels.append(avatar)
        h.addWidget(avatar)

        info = QVBoxLayout()
        info.setSpacing(2)

        github = c.get("github", "").strip()
        url = c.get("url", "").strip()

        name_text = f"<b>{c['name']}</b>"
        if github:
            if not url:
                url = f"https://github.com/{github}"
            name_text += f'  <a href="{url}" style="color:#4a90d9;">@{github}</a>'
        name_label = QLabel(name_text)
        name_label.setTextFormat(Qt.TextFormat.RichText)
        name_label.setOpenExternalLinks(True)
        name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        info.addWidget(name_label)

        role_label = QLabel(c["role"])
        role_label.setObjectName("aboutRole")
        info.addWidget(role_label)

        bio_label = QLabel(c["bio"])
        bio_label.setWordWrap(True)
        bio_label.setObjectName("aboutBio")
        info.addWidget(bio_label)

        h.addLayout(info, 1)
        return row

    def set_dark(self, is_dark):
        self.is_dark = is_dark
        for av in self._avatar_labels:
            av.set_dark(is_dark)
        # 样式由主窗口的 QSS 统一控制，这里只刷新自绘控件