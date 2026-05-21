"""Settings dialog for VentiPlayer."""

from pathlib import Path
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QPushButton, QCheckBox, QFileDialog, QMessageBox,
    QSlider,
)
from PySide6.QtCore import Signal, Qt

from src.config.settings import Settings


class SettingsDialog(QDialog):
    """Application settings dialog with cookie and display sections."""

    cookie_imported = Signal(str)  # path to cookie file
    thumbnail_mode_changed = Signal(bool)
    thumbnail_size_changed = Signal(int)  # width in pixels

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(400)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- Cookie section ---
        cookie_group = QGroupBox("Cookie 设置")
        cookie_layout = QVBoxLayout(cookie_group)

        # Cookie status row
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self._cookie_label = QLabel("Cookie 文件:")
        cookie_file = self._settings.get("cookie_file")
        if cookie_file:
            self._cookie_status_label = QLabel(Path(cookie_file).name)
            self._cookie_status_label.setStyleSheet("color: green;")
        else:
            self._cookie_status_label = QLabel("未配置")
            self._cookie_status_label.setStyleSheet("color: gray;")
        status_row.addWidget(self._cookie_label)
        status_row.addWidget(self._cookie_status_label, 1)
        cookie_layout.addLayout(status_row)

        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._cookie_btn = QPushButton("导入...")
        self._cookie_btn.setToolTip("导入 cookies.txt (Netscape 格式)")
        self._cookie_btn.clicked.connect(self._on_import_cookie)
        self._cookie_help_btn = QPushButton("帮助")
        self._cookie_help_btn.setToolTip("查看 Cookie 导出教程")
        self._cookie_help_btn.clicked.connect(self._on_cookie_help)
        btn_row.addWidget(self._cookie_btn)
        btn_row.addWidget(self._cookie_help_btn)
        btn_row.addStretch()
        cookie_layout.addLayout(btn_row)

        layout.addWidget(cookie_group)

        # --- Display section ---
        display_group = QGroupBox("显示设置")
        display_layout = QVBoxLayout(display_group)

        self._thumbnail_check = QCheckBox("启用缩略图模式")
        self._thumbnail_check.setChecked(self._settings.get("thumbnail_mode"))
        self._thumbnail_check.toggled.connect(self._on_thumbnail_toggled)
        display_layout.addWidget(self._thumbnail_check)

        # Thumbnail size slider
        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(QLabel("缩略图大小"))
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(60, 160)
        self._size_slider.setValue(self._settings.get("thumbnail_size"))
        self._size_slider.setFixedWidth(120)
        self._size_slider.valueChanged.connect(self._on_size_slider_changed)
        size_row.addWidget(self._size_slider)
        w = self._settings.get("thumbnail_size")
        h = round(w * 9 / 16)
        self._size_value_label = QLabel(f"{w}×{h}")
        self._size_value_label.setFixedWidth(50)
        size_row.addWidget(self._size_value_label)
        size_row.addStretch()
        display_layout.addLayout(size_row)

        # Disable slider when thumbnail mode is off
        self._size_slider.setEnabled(self._thumbnail_check.isChecked())
        self._size_value_label.setEnabled(self._thumbnail_check.isChecked())

        layout.addWidget(display_group)

        # --- Close button ---
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _on_import_cookie(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 Cookie 文件", "",
            "Cookie 文件 (*.txt);;所有文件 (*)",
        )
        if path:
            self._settings.set("cookie_file", path)
            self._settings.set("cookie_browser", "")
            self._cookie_status_label.setText(Path(path).name)
            self._cookie_status_label.setStyleSheet("color: green;")
            self.cookie_imported.emit(path)

    def _on_cookie_help(self):
        QMessageBox.information(
            self,
            "Cookie 导出教程",
            "获取普通用户或大会员画质需要导入 Cookie 文件：\n\n"
            "1. 在 Edge 中安装扩展 \"et cookies txt\"\n"
            "   (Edge 扩展商店搜索即可)\n\n"
            "2. 打开 bilibili.com 任意页面（确保已登录）\n\n"
            "3. 点击扩展图标 → 导出为 Netscape 格式\n"
            "   重要：选择 \"All Cookies\" 而非 \"Current Site\"\n"
            "   或者确保导出域名包含 .bilibili.com\n\n"
            "4. 保存 .txt 文件后，点击左侧\"导入...\"按钮选择该文件\n\n"
            "提示：导出的文件必须包含 SESSDATA 等认证 Cookie，\n"
            "仅导出 www.bilibili.com 的 Cookie 不包含登录信息。",
            QMessageBox.StandardButton.Ok,
        )

    def _on_thumbnail_toggled(self, checked: bool):
        self._settings.set("thumbnail_mode", checked)
        self._size_slider.setEnabled(checked)
        self._size_value_label.setEnabled(checked)
        self.thumbnail_mode_changed.emit(checked)

    def _on_size_slider_changed(self, value: int):
        h = round(value * 9 / 16)
        self._size_value_label.setText(f"{value}×{h}")
        self._settings.set("thumbnail_size", value)
        self.thumbnail_size_changed.emit(value)
