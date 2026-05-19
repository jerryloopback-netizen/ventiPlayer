from PySide6.QtWidgets import (
    QWidget, QGroupBox, QVBoxLayout, QHBoxLayout,
    QCheckBox, QRadioButton, QComboBox, QLabel, QButtonGroup,
    QProgressBar, QPushButton, QSlider,
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QPainter, QColor


class PlaybackMarkerProgressBar(QProgressBar):
    """Progress bar with a red dot indicating current playback position."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._playback_ratio = -1.0  # -1 means hidden

    def set_playback_ratio(self, ratio: float):
        """Set playback position as 0.0-1.0 ratio, or -1 to hide."""
        self._playback_ratio = ratio
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._playback_ratio < 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = int(self._playback_ratio * self.width())
        y = self.height() // 2
        painter.setBrush(QColor(220, 40, 40))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(x - 5, y - 5, 10, 10)
        painter.end()


class EnhancePanel(QWidget):
    """Audio enhancement control panel."""

    settings_changed = Signal(dict)
    enhance_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blocked = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        group = QGroupBox("音频增强")
        group_layout = QVBoxLayout(group)

        self._enable_check = QCheckBox("启用带宽截止修复+采样率超分")
        self._enable_check.stateChanged.connect(self._on_settings_changed)
        group_layout.addWidget(self._enable_check)

        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("模式:"))
        self._mode_group = QButtonGroup(self)
        self._realtime_radio = QRadioButton("快速 (FastWave)")
        self._quality_radio = QRadioButton("精听 (AudioSR)")
        self._realtime_radio.setChecked(True)
        self._mode_group.addButton(self._realtime_radio)
        self._mode_group.addButton(self._quality_radio)
        self._realtime_radio.toggled.connect(self._on_settings_changed)
        mode_layout.addWidget(self._realtime_radio)
        mode_layout.addWidget(self._quality_radio)
        mode_layout.addStretch()
        group_layout.addLayout(mode_layout)

        sr_layout = QHBoxLayout()
        sr_layout.addWidget(QLabel("输出采样率:"))
        self._sr_combo = QComboBox()
        self._sr_combo.addItems([
            "44100 Hz (重采样)",
            "48000 Hz (native)",
            "96000 Hz (仅插值)",
            "192000 Hz (仅插值)",
        ])
        self._sr_combo.setCurrentIndex(1)
        self._sr_combo.currentIndexChanged.connect(self._on_settings_changed)
        sr_layout.addWidget(self._sr_combo)
        sr_layout.addStretch()
        group_layout.addLayout(sr_layout)

        # NFE steps slider (FastWave only)
        self._nfe_widget = QWidget()
        nfe_layout = QHBoxLayout(self._nfe_widget)
        nfe_layout.setContentsMargins(0, 0, 0, 0)
        nfe_layout.addWidget(QLabel("采样步数:"))
        self._nfe_slider = QSlider(Qt.Orientation.Horizontal)
        self._nfe_slider.setRange(2, 16)
        self._nfe_slider.setValue(4)
        self._nfe_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._nfe_slider.setTickInterval(2)
        self._nfe_slider.valueChanged.connect(self._on_nfe_changed)
        self._nfe_label = QLabel("4 步")
        self._nfe_label.setFixedWidth(36)
        nfe_layout.addWidget(self._nfe_slider)
        nfe_layout.addWidget(self._nfe_label)
        group_layout.addWidget(self._nfe_widget)

        # DDIM steps slider (AudioSR only)
        self._ddim_widget = QWidget()
        ddim_layout = QHBoxLayout(self._ddim_widget)
        ddim_layout.setContentsMargins(0, 0, 0, 0)
        ddim_layout.addWidget(QLabel("DDIM 步数:"))
        self._ddim_slider = QSlider(Qt.Orientation.Horizontal)
        self._ddim_slider.setRange(10, 100)
        self._ddim_slider.setValue(50)
        self._ddim_slider.setSingleStep(10)
        self._ddim_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._ddim_slider.setTickInterval(10)
        self._ddim_slider.valueChanged.connect(self._on_ddim_changed)
        self._ddim_label = QLabel("50 步")
        self._ddim_label.setFixedWidth(36)
        ddim_layout.addWidget(self._ddim_slider)
        ddim_layout.addWidget(self._ddim_label)
        group_layout.addWidget(self._ddim_widget)
        self._ddim_widget.hide()

        # Backend status
        backend_layout = QHBoxLayout()
        backend_layout.addWidget(QLabel("推理后端:"))
        self._backend_label = QLabel("检测中...")
        self._backend_label.setStyleSheet("color: gray;")
        backend_layout.addWidget(self._backend_label)
        backend_layout.addStretch()
        group_layout.addLayout(backend_layout)

        # Model status
        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel("模型状态:"))
        self._model_label = QLabel("未加载")
        self._model_label.setStyleSheet("color: gray;")
        model_layout.addWidget(self._model_label)
        model_layout.addStretch()
        group_layout.addLayout(model_layout)

        # Progress bar with playback marker
        self._progress = PlaybackMarkerProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("%p%")
        self._progress.setMinimumHeight(20)
        self._progress.hide()
        group_layout.addWidget(self._progress)

        # Status message
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #666; font-size: 11px;")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        group_layout.addWidget(self._status_label)

        # Action buttons
        btn_layout = QHBoxLayout()
        self._enhance_btn = QPushButton("增强当前音频")
        self._enhance_btn.clicked.connect(self.enhance_requested.emit)
        self._enhance_btn.setEnabled(False)
        btn_layout.addWidget(self._enhance_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.cancel_requested.emit)
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.hide()
        btn_layout.addWidget(self._cancel_btn)
        group_layout.addLayout(btn_layout)

        layout.addWidget(group)

    def _on_nfe_changed(self, value):
        self._nfe_label.setText(f"{value} 步")
        self._on_settings_changed()

    def _on_ddim_changed(self, value):
        self._ddim_label.setText(f"{value} 步")
        self._on_settings_changed()

    def _on_settings_changed(self):
        # Toggle slider visibility based on mode
        is_realtime = self._realtime_radio.isChecked()
        self._nfe_widget.setVisible(is_realtime)
        self._ddim_widget.setVisible(not is_realtime)
        self.settings_changed.emit(self.get_settings())

    def get_settings(self) -> dict:
        sr_text = self._sr_combo.currentText().split()[0]
        return {
            "enabled": self._enable_check.isChecked(),
            "mode": "realtime" if self._realtime_radio.isChecked() else "quality",
            "sample_rate": int(sr_text),
            "nfe_steps": self._nfe_slider.value(),
            "ddim_steps": self._ddim_slider.value(),
        }

    def set_backend_info(self, text: str, available: bool):
        self._backend_label.setText(text)
        self._backend_label.setStyleSheet(
            "color: green;" if available else "color: orange;"
        )

    def set_model_status(self, text: str, loaded: bool):
        self._model_label.setText(text)
        self._model_label.setStyleSheet(
            "color: green;" if loaded else "color: gray;"
        )
        self._enhance_btn.setEnabled(loaded and self._enable_check.isChecked())

    def set_enhance_enabled(self, enabled: bool):
        self._enhance_btn.setEnabled(
            enabled and self._enable_check.isChecked()
            and not self._blocked
        )

    def set_enhance_blocked(self, blocked: bool):
        """Block enhancement when source audio >= 48kHz."""
        self._blocked = blocked
        self._enable_check.setEnabled(not blocked)
        if blocked:
            self._enable_check.setChecked(False)
            self._enable_check.setText(
                "启用带宽截止修复+采样率超分 (原始采样率≥模型原生输出)"
            )
            self._enhance_btn.setEnabled(False)
        else:
            self._enable_check.setText("启用带宽截止修复+采样率超分")

    def show_progress(self, visible: bool):
        is_quality = self._quality_radio.isChecked()
        # In AudioSR mode: hide progress bar, only show status text
        self._progress.setVisible(visible and not is_quality)
        self._status_label.setVisible(visible)
        self._cancel_btn.setVisible(visible)
        self._cancel_btn.setEnabled(visible)
        if visible:
            self._enhance_btn.setText("重新增强")
        else:
            self._enhance_btn.setText("增强当前音频")
            self._progress.setValue(0)
            self._progress.set_playback_ratio(-1.0)
            self._status_label.setText("")

    def update_progress(self, progress: float, message: str = ""):
        self._progress.setValue(int(progress * 100))
        if message:
            self._status_label.setText(message)
            self._status_label.show()

    def update_playback_marker(self, ratio: float):
        """Update the red dot position on the progress bar (0.0-1.0)."""
        self._progress.set_playback_ratio(ratio)
