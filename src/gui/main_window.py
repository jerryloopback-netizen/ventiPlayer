import sys
import logging
import threading
from pathlib import Path
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QSlider, QLabel, QComboBox,
    QCheckBox, QStatusBar, QSplitter, QMessageBox, QFileDialog,
)
from PySide6.QtCore import Qt, Slot, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut

from src.gui.player_widget import MpvPlayerWidget
from src.gui.enhance_panel import EnhancePanel
from src.core.stream import (
    StreamResolver, StreamInfo, CookieStatus,
    check_cookie_status,
)
from src.core.enhancer import Enhancer, EnhanceMode, Backend
from src.core.audio_pipe import AudioPipeline, PipelineState, PipelineStatus
from src.core.sync import SyncManager, SyncState, SyncStatus
from src.config.settings import Settings

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    _stream_resolved = Signal(object)
    _cookie_status_ready = Signal(object)
    _enhance_status_update = Signal(object)
    backend_ready = Signal()  # emitted when enhance panel backend info is set

    def __init__(self, predetected_device=None):
        super().__init__()
        self.setWindowTitle("VentiPlayer")
        self.setMinimumSize(960, 640)

        self._settings = Settings()
        self._resolver = self._create_resolver()
        self._current_stream: StreamInfo | None = None
        self._last_state: str = ""
        self._is_fullscreen = False

        # Enhancement engine
        self._enhancer = Enhancer()
        if predetected_device is not None:
            self._enhancer._device_info = predetected_device
        self._pipeline = AudioPipeline(self._enhancer)
        self._pipeline.set_status_callback(
            lambda s: self._enhance_status_update.emit(s)
        )

        # Sync manager
        self._sync = SyncManager()
        self._enhanced_playing = False  # True when enhanced audio is active

        self._setup_ui()
        self._setup_shortcuts()
        self._connect_signals()

        QTimer.singleShot(0, self._init_player)
        QTimer.singleShot(100, self._refresh_cookie_status)
        QTimer.singleShot(200, self._init_enhance_backend)

    def _create_resolver(self) -> StreamResolver:
        return StreamResolver(
            cookie_file=self._settings.get("cookie_file"),
            cookie_browser=self._settings.get("cookie_browser"),
        )

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        self._main_layout = QVBoxLayout(central)
        self._main_layout.setContentsMargins(8, 8, 8, 8)
        self._main_layout.setSpacing(6)

        # URL bar
        self._url_bar = QWidget()
        url_layout = QHBoxLayout(self._url_bar)
        url_layout.setContentsMargins(0, 0, 0, 0)
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("输入 YouTube / B站 URL...")
        self._url_input.setText(self._settings.get("last_url"))
        self._play_btn = QPushButton("播放")
        self._stop_btn = QPushButton("停止")
        url_layout.addWidget(self._url_input, 1)
        url_layout.addWidget(self._play_btn)
        url_layout.addWidget(self._stop_btn)
        self._main_layout.addWidget(self._url_bar)

        # Splitter: video + panel
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # Video area
        self._player_widget = MpvPlayerWidget()
        self._player_widget.setMinimumSize(480, 270)
        self._player_widget.mouseDoubleClickEvent = lambda e: self._toggle_fullscreen()
        self._splitter.addWidget(self._player_widget)

        # Right panel
        self._right_panel = QWidget()
        right_layout = QVBoxLayout(self._right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)

        # Audio device
        dev_layout = QHBoxLayout()
        dev_layout.addWidget(QLabel("输出设备:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(180)
        dev_layout.addWidget(self._device_combo, 1)
        right_layout.addLayout(dev_layout)

        # WASAPI exclusive
        self._exclusive_check = QCheckBox("WASAPI Exclusive")
        self._exclusive_check.setChecked(self._settings.get("audio_exclusive"))
        right_layout.addWidget(self._exclusive_check)

        # Cookie settings
        cookie_layout = QHBoxLayout()
        self._cookie_label = QLabel("Cookie:")
        cookie_file = self._settings.get("cookie_file")
        if cookie_file:
            self._cookie_status_label = QLabel(Path(cookie_file).name)
        else:
            self._cookie_status_label = QLabel("未配置")
        self._cookie_status_label.setStyleSheet("color: gray;")
        self._cookie_btn = QPushButton("导入...")
        self._cookie_btn.setToolTip("导入 cookies.txt (Netscape 格式)")
        self._cookie_btn.setFixedWidth(56)
        self._cookie_auto_btn = QPushButton("帮助")
        self._cookie_auto_btn.setToolTip("查看 Cookie 导出教程")
        self._cookie_auto_btn.setFixedWidth(42)
        cookie_layout.addWidget(self._cookie_label)
        cookie_layout.addWidget(self._cookie_status_label, 1)
        cookie_layout.addWidget(self._cookie_btn)
        cookie_layout.addWidget(self._cookie_auto_btn)
        right_layout.addLayout(cookie_layout)

        # Enhance panel
        self._enhance_panel = EnhancePanel()
        right_layout.addWidget(self._enhance_panel)

        right_layout.addStretch()
        self._splitter.addWidget(self._right_panel)
        self._splitter.setSizes([700, 260])
        self._main_layout.addWidget(self._splitter, 1)

        # Transport controls
        self._transport_bar = QWidget()
        transport_layout = QHBoxLayout(self._transport_bar)
        transport_layout.setContentsMargins(0, 0, 0, 0)

        self._pause_btn = QPushButton("⏸")
        self._pause_btn.setFixedWidth(36)
        self._pause_btn.setToolTip("暂停/继续 (Space)")

        self._speed_btn = QPushButton("1x")
        self._speed_btn.setFixedWidth(40)
        self._speed_btn.setToolTip("播放倍速")
        self._speed_btn.clicked.connect(self._cycle_speed)
        self._speed_options = [0.5, 1.0, 1.25, 1.5, 2.0, 3.0]
        self._speed_index = 1  # default 1x

        self._fullscreen_btn = QPushButton("⛶")
        self._fullscreen_btn.setFixedWidth(36)
        self._fullscreen_btn.setToolTip("全屏 (F)")

        self._pos_label = QLabel("00:00")
        self._pos_label.setFixedWidth(52)
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._dur_label = QLabel("00:00")
        self._dur_label.setFixedWidth(52)
        self._vol_label = QLabel("\U0001f50a")
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 150)
        self._vol_slider.setValue(self._settings.get("volume"))
        self._vol_slider.setFixedWidth(100)

        transport_layout.addWidget(self._pause_btn)
        transport_layout.addWidget(self._speed_btn)
        transport_layout.addWidget(self._pos_label)
        transport_layout.addWidget(self._seek_slider, 1)
        transport_layout.addWidget(self._dur_label)
        transport_layout.addSpacing(12)
        transport_layout.addWidget(self._vol_label)
        transport_layout.addWidget(self._vol_slider)
        transport_layout.addSpacing(8)
        transport_layout.addWidget(self._fullscreen_btn)
        self._main_layout.addWidget(self._transport_bar)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("就绪")
        self._status_bar.addWidget(self._status_label, 1)
        self._media_info_label = QLabel("")
        self._media_info_label.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self._status_bar.addPermanentWidget(self._media_info_label)
        self._audio_source_indicator = QLabel("")
        self._audio_source_indicator.setTextFormat(Qt.TextFormat.RichText)
        self._audio_source_indicator.setStyleSheet("font-size: 11px; margin-left: 6px;")
        self._status_bar.addPermanentWidget(self._audio_source_indicator)
        self._cookie_info_label = QLabel("")
        self._cookie_info_label.setStyleSheet("color: gray; margin-left: 8px;")
        self._status_bar.addPermanentWidget(self._cookie_info_label)

        # Media info state
        self._output_sr: int = 0  # actual output sample rate from mpv
        self._enhanced_duration_s: float = 0.0  # seconds of enhanced audio available

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._toggle_pause)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_play)
        QShortcut(QKeySequence(Qt.Key.Key_F), self, self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._exit_fullscreen)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self._seek_relative(-5))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self._seek_relative(5))

    def _connect_signals(self):
        self._play_btn.clicked.connect(self._on_play)
        self._stop_btn.clicked.connect(self._on_stop)
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._fullscreen_btn.clicked.connect(self._toggle_fullscreen)
        self._cookie_btn.clicked.connect(self._on_import_cookie)
        self._cookie_auto_btn.clicked.connect(self._on_auto_cookie)
        self._url_input.returnPressed.connect(self._on_play)
        self._vol_slider.valueChanged.connect(self._on_volume_changed)
        self._seek_slider.sliderReleased.connect(self._on_seek)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._exclusive_check.toggled.connect(self._on_exclusive_changed)
        self._stream_resolved.connect(self._handle_stream_resolved)
        self._cookie_status_ready.connect(self._handle_cookie_status)
        # Enhancement signals
        self._enhance_panel.enhance_requested.connect(self._on_enhance_requested)
        self._enhance_panel.cancel_requested.connect(self._on_enhance_cancel)
        self._enhance_panel.settings_changed.connect(self._on_enhance_settings_changed)
        self._enhance_status_update.connect(self._handle_enhance_status)
        self._exclusive_check.toggled.connect(self._update_media_info)

    @Slot()
    def _init_player(self):
        self._player_widget.init_mpv(
            audio_exclusive=self._settings.get("audio_exclusive"),
        )
        self._player_widget.position_changed.connect(self._update_position)
        self._player_widget.duration_changed.connect(self._update_duration)
        self._player_widget.state_changed.connect(self._update_state)
        self._player_widget.seek_performed.connect(self._on_seek_performed)
        self._player_widget.audio_output_changed.connect(self._on_audio_output_changed)
        self._player_widget.audio_source_detected.connect(self._on_audio_source_detected)
        self._refresh_devices()
        self._configure_sync()

    def _refresh_devices(self):
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        self._device_combo.addItem("自动", "auto")
        devices = self._player_widget.get_audio_device_list()
        saved_device = self._settings.get("audio_device")
        target_index = 0
        for dev in devices:
            if dev["name"] != "auto":
                self._device_combo.addItem(dev["description"], dev["name"])
                if dev["name"] == saved_device:
                    target_index = self._device_combo.count() - 1
        self._device_combo.setCurrentIndex(target_index)
        self._device_combo.blockSignals(False)
        if target_index > 0:
            self._player_widget.set_audio_device(saved_device)

    def _refresh_cookie_status(self):
        """Check cookie status in background thread."""
        cookie_file = self._settings.get("cookie_file")
        if not cookie_file:
            self._cookie_info_label.setText("Cookie: 未配置")
            return

        def _worker():
            status = check_cookie_status(cookie_file)
            self._cookie_status_ready.emit(status)

        threading.Thread(target=_worker, daemon=True).start()

    def _configure_sync(self):
        """Wire up the sync manager to player functions."""
        self._sync.configure(
            video_position_fn=lambda: self._player_widget.position,
            audio_position_fn=lambda: self._player_widget.get_audio_position(),
            seek_fn=lambda pos: self._player_widget.seek(pos),
            switch_audio_fn=self._sync_switch_audio,
            set_speed_fn=lambda s: self._player_widget.set_speed(s),
            get_speed_fn=lambda: self._speed_options[self._speed_index],
        )

    def _sync_switch_audio(self, path_or_url: str):
        """Called by SyncManager to switch audio source."""
        if path_or_url.startswith(("http://", "https://")):
            headers = self._current_stream.http_headers if self._current_stream else None
            self._player_widget.switch_audio_url(path_or_url, headers)
            self._enhanced_playing = False
        else:
            self._player_widget.switch_audio_file(path_or_url)
            self._enhanced_playing = True
        self._update_media_info()

    @Slot(float)
    def _on_seek_performed(self, position: float):
        """Notify sync manager when user seeks."""
        self._sync.notify_seek(position)
        # If enhanced audio is active but seek goes past coverage, fall back
        if self._enhanced_playing and self._enhanced_duration_s > 0:
            if position > self._enhanced_duration_s - 2.0:
                self._sync.deactivate_enhanced()
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText("播放位置超出已增强范围 — 回退到源音频")

    @Slot(object)
    def _handle_cookie_status(self, status: CookieStatus):
        if status.platform == "bilibili":
            if status.is_vip:
                text = f"B站: {status.username} (大会员)"
                self._cookie_info_label.setStyleSheet("color: #fb7299;")
            elif status.logged_in:
                text = f"B站: {status.username} (普通)"
                self._cookie_info_label.setStyleSheet("color: orange;")
            else:
                text = "B站: 未登录"
                self._cookie_info_label.setStyleSheet("color: gray;")
        elif status.platform == "youtube":
            text = "YouTube: 已登录"
            self._cookie_info_label.setStyleSheet("color: #ff0000;")
        else:
            text = "Cookie: 未识别"
            self._cookie_info_label.setStyleSheet("color: gray;")
        self._cookie_info_label.setText(text)

    @Slot()
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
            self._resolver = self._create_resolver()
            self._status_label.setText("Cookie 已导入")
            self._refresh_cookie_status()

    @Slot()
    def _on_auto_cookie(self):
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

    @Slot()
    def _on_play(self):
        url = self._url_input.text().strip()
        if not url:
            return
        self._status_label.setText("解析中...")
        self._play_btn.setEnabled(False)
        self._settings.set("last_url", url)
        self._resolver.resolve_async(url, lambda result: self._stream_resolved.emit(result))

    @Slot(object)
    def _handle_stream_resolved(self, result):
        self._play_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._status_label.setText(f"解析失败: {result}")
            QMessageBox.warning(self, "错误", f"无法解析 URL:\n{result}")
            return

        stream: StreamInfo = result
        self._current_stream = stream
        self.setWindowTitle(f"VentiPlayer — {stream.title}")

        self._output_sr = 0
        self._enhanced_duration_s = 0.0
        self._update_media_info()

        if stream.cookie_failed:
            self._status_label.setText("Cookie 读取失败 — 点击\"导入\"或\"自动\"按钮配置")

        if stream.video_url and stream.audio_url and stream.video_url != stream.audio_url:
            self._player_widget.play_av(stream.video_url, stream.audio_url, stream.http_headers)
        else:
            self._player_widget.play_url(stream.video_url or stream.audio_url, stream.http_headers)

        if not stream.cookie_failed:
            self._status_label.setText("播放中")

        # Store original audio URL for sync fallback
        original_audio = stream.audio_url or stream.video_url
        self._sync.set_original_audio(original_audio)
        self._enhanced_playing = False

        # Disable enhancement if source is lossless >= 48kHz (no benefit from super-res)
        # Lossy codecs at 48kHz (e.g. opus, aac) still have bandwidth below Nyquist
        _lossless_codecs = {"flac", "alac", "pcm", "wav", "pcm_s16le", "pcm_s24le", "pcm_f32le"}
        sr = stream.audio_sample_rate or 0
        codec = (stream.audio_codec or "").lower()
        is_lossless_hires = sr >= 48000 and codec in _lossless_codecs
        if is_lossless_hires:
            self._enhance_panel.set_enhance_blocked(True)
        else:
            self._enhance_panel.set_enhance_blocked(False)
            self._enhance_panel.set_enhance_enabled(True)

    @Slot()
    def _on_stop(self):
        self._player_widget.stop()
        self._status_label.setText("已停止")
        self._seek_slider.setValue(0)
        self._pos_label.setText("00:00")
        self._current_stream = None
        self._output_sr = 0
        self._enhanced_duration_s = 0.0
        self._enhanced_playing = False
        self._media_info_label.setText("")
        self._audio_source_indicator.setText("")

    @Slot(int)
    def _on_audio_output_changed(self, sr: int):
        """Called when mpv's actual output sample rate changes."""
        self._output_sr = sr
        self._update_media_info()

    @Slot(int)
    def _on_audio_source_detected(self, sr: int):
        """Called when mpv detects the source audio sample rate (from decoder)."""
        if self._current_stream and not self._current_stream.audio_sample_rate:
            self._current_stream.audio_sample_rate = sr
            self._update_media_info()

    def _update_media_info(self, *_args):
        """Rebuild the media info label: V-res-fps | A-sr-cutoff(→output) | exclusive"""
        stream = self._current_stream
        if not stream:
            self._media_info_label.setText("")
            self._audio_source_indicator.setText("")
            return

        parts = []

        # Video info: V-1080×720-30fps
        v_parts = []
        if stream.video_width and stream.video_height:
            v_parts.append(f"{stream.video_width}×{stream.video_height}")
        elif stream.video_resolution:
            v_parts.append(stream.video_resolution)
        if stream.video_fps:
            fps_val = stream.video_fps
            if fps_val == int(fps_val):
                v_parts.append(f"{int(fps_val)}fps")
            else:
                v_parts.append(f"{fps_val:.1f}fps")
        if v_parts:
            parts.append("V-" + "-".join(v_parts))

        # Audio info: A-44.1kHz-16kHz → 48kHz-24kHz
        a_str = self._format_audio_info(stream)
        if a_str:
            parts.append(a_str)

        # Exclusive mode
        if self._exclusive_check.isChecked():
            parts.append("独占")
        else:
            parts.append("非独占")

        self._media_info_label.setText(" | ".join(parts))
        self._update_audio_source_indicator()

    def _update_audio_source_indicator(self):
        """Update the audio source indicator: green dot + '升频' or gray dot + '源音频'."""
        if not self._current_stream:
            self._audio_source_indicator.setText("")
            return
        if self._enhanced_playing:
            self._audio_source_indicator.setText(
                '<span style="color: #4CAF50; font-size: 14px;">●</span> 升频'
            )
        else:
            self._audio_source_indicator.setText(
                '<span style="color: #9E9E9E; font-size: 14px;">●</span> 源音频'
            )

    def _format_audio_info(self, stream: StreamInfo) -> str:
        """Format audio section: A-44.1kHz-16kHz → 48kHz-24kHz"""
        src_sr = stream.audio_sample_rate
        if not src_sr:
            return ""

        src_sr_str = self._format_sr(src_sr)
        src_cutoff = self._estimate_cutoff(src_sr, stream.audio_bitrate, stream.audio_codec)
        src_cutoff_str = self._format_freq(src_cutoff) if src_cutoff else ""

        src_part = f"A-{src_sr_str}"
        if src_cutoff_str:
            src_part += f"-{src_cutoff_str}"

        if self._enhanced_playing:
            # Use enhancer's target SR (known from config, not dependent on mpv report)
            enhanced_sr = self._enhancer._target_sr
            out_sr_str = self._format_sr(enhanced_sr)
            out_cutoff = enhanced_sr // 2
            out_cutoff_str = self._format_freq(out_cutoff)
            return f"{src_part} → {out_sr_str}-{out_cutoff_str}"
        elif self._output_sr and self._output_sr != src_sr:
            out_sr_str = self._format_sr(self._output_sr)
            return f"{src_part}(out:{out_sr_str})"
        else:
            return src_part

    @staticmethod
    def _format_sr(sr: int) -> str:
        """Format sample rate: 44100 → '44.1kHz', 48000 → '48kHz'"""
        khz = sr / 1000
        if khz == int(khz):
            return f"{int(khz)}kHz"
        return f"{khz:.1f}kHz"

    @staticmethod
    def _format_freq(freq: int) -> str:
        """Format frequency: 16000 → '16kHz', 22050 → '22kHz'"""
        khz = freq / 1000
        if khz >= 10:
            return f"{int(round(khz))}kHz"
        return f"{khz:.1f}kHz"

    @staticmethod
    def _estimate_cutoff(sample_rate: int, bitrate: int | None, codec: str) -> int | None:
        """Estimate audio bandwidth cutoff from codec info.

        Returns estimated cutoff frequency in Hz, or None if unknown.
        """
        nyquist = sample_rate // 2

        if not bitrate:
            # No bitrate info — assume ~75% of Nyquist for lossy
            if codec and codec.lower() in ("opus", "vorbis", "flac", "alac", "pcm"):
                return nyquist
            return int(nyquist * 0.75)

        codec_lower = (codec or "").lower()

        if codec_lower in ("flac", "alac", "pcm", "pcm_s16le", "pcm_s24le"):
            return nyquist

        if codec_lower == "opus":
            if bitrate >= 128:
                return min(nyquist, 20000)
            elif bitrate >= 64:
                return min(nyquist, 18000)
            else:
                return min(nyquist, 12000)

        # AAC, MP3, Vorbis and other lossy codecs
        if bitrate >= 256:
            return min(nyquist, 20000)
        elif bitrate >= 192:
            return min(nyquist, 18000)
        elif bitrate >= 128:
            return min(nyquist, 16000)
        elif bitrate >= 96:
            return min(nyquist, 14000)
        elif bitrate >= 64:
            return min(nyquist, 12000)
        else:
            return min(nyquist, 8000)

    @Slot()
    def _toggle_pause(self):
        self._player_widget.toggle_pause()

    @Slot()
    def _cycle_speed(self):
        self._speed_index = (self._speed_index + 1) % len(self._speed_options)
        speed = self._speed_options[self._speed_index]
        label = f"{speed}x" if speed != int(speed) else f"{int(speed)}x"
        self._speed_btn.setText(label)
        self._player_widget.set_speed(speed)
        self._sync.notify_speed_change(speed)

    @Slot()
    def _toggle_fullscreen(self):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._is_fullscreen = True
        self._url_bar.hide()
        self._right_panel.hide()
        self.statusBar().hide()
        self.showFullScreen()

    @Slot()
    def _exit_fullscreen(self):
        if not self._is_fullscreen:
            return
        self._is_fullscreen = False
        self._url_bar.show()
        self._right_panel.show()
        self.statusBar().show()
        self.showNormal()

    def _seek_relative(self, seconds: float):
        if self._player_widget.duration > 0:
            target = max(0, self._player_widget.position + seconds)
            self._player_widget.seek(target)

    @Slot(int)
    def _on_volume_changed(self, value: int):
        self._player_widget.set_volume(value)
        self._settings.set("volume", value)

    @Slot()
    def _on_seek(self):
        if self._player_widget.duration > 0:
            ratio = self._seek_slider.value() / 1000.0
            target = ratio * self._player_widget.duration
            self._player_widget.seek(target)

    @Slot(int)
    def _on_device_changed(self, index: int):
        device = self._device_combo.itemData(index)
        if device:
            self._player_widget.set_audio_device(device)
            self._settings.set("audio_device", device)

    @Slot(bool)
    def _on_exclusive_changed(self, checked: bool):
        self._settings.set("audio_exclusive", checked)
        self._status_label.setText("WASAPI Exclusive 设置将在下次播放时生效")

    @Slot(float)
    def _update_position(self, pos: float):
        self._pos_label.setText(self._format_time(pos))
        if self._player_widget.duration > 0 and not self._seek_slider.isSliderDown():
            ratio = int(pos / self._player_widget.duration * 1000)
            self._seek_slider.setValue(ratio)
            # Update playback marker on enhance progress bar
            if self._progress_visible():
                self._enhance_panel.update_playback_marker(
                    pos / self._player_widget.duration
                )
        # Auto-fallback if playback approaches end of enhanced coverage
        if self._enhanced_playing and self._enhanced_duration_s > 0:
            pipeline_state = self._pipeline.status.state
            if pipeline_state != PipelineState.READY and pos > self._enhanced_duration_s - 3.0:
                self._sync.deactivate_enhanced()
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText("播放位置接近增强边界 — 回退到源音频")

    @Slot(float)
    def _update_duration(self, dur: float):
        self._dur_label.setText(self._format_time(dur))

    def _progress_visible(self) -> bool:
        return self._enhance_panel._progress.isVisible()

    @Slot(str)
    def _update_state(self, state: str):
        if state == self._last_state:
            return
        self._last_state = state
        state_map = {
            "playing": "播放中",
            "paused": "已暂停",
            "stopped": "已停止",
            "buffering": "缓冲中...",
        }
        self._status_label.setText(state_map.get(state, state))
        if state == "playing":
            self._pause_btn.setText("⏸")
        elif state == "paused":
            self._pause_btn.setText("▶")

    @staticmethod
    def _format_time(seconds: float) -> str:
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def closeEvent(self, event):
        self._settings.flush()
        self._sync.cleanup()
        self._pipeline.cleanup()
        self._enhancer.unload()
        self._player_widget.destroy()
        event.accept()

    # --- Enhancement integration ---

    @Slot(dict)
    def _on_enhance_settings_changed(self, settings: dict):
        """Handle enhance panel settings change — toggle enhanced audio on/off."""
        if not settings["enabled"] and self._enhanced_playing:
            self._sync.deactivate_enhanced()
            self._enhanced_playing = False
            self._update_media_info()
            self._status_label.setText("已切换回原始音频")
        elif settings["enabled"] and not self._enhanced_playing:
            status = self._pipeline.status
            if status.enhanced_file and status.state in (PipelineState.READY, PipelineState.ENHANCING):
                current_pos = self._player_widget.position
                if status.enhanced_duration_s >= current_pos:
                    self._sync.activate_enhanced(status.enhanced_file, current_pos)
                    self._enhanced_playing = True
                    self._update_media_info()
                    self._status_label.setText("已切换回增强音频")

    def _init_enhance_backend(self):
        """Detect GPU backend and model availability in background thread.

        All heavy imports (torch, audiosr) happen here off the main thread.
        Emits backend_ready when the panel has been updated.
        """
        def _worker():
            info = self._enhancer.device_info
            fw_avail = self._enhancer.is_model_available(EnhanceMode.REALTIME)
            asr_avail = self._enhancer.is_model_available(EnhanceMode.QUALITY)
            self._enhance_status_update.emit(
                ("backend_ready", info, fw_avail, asr_avail)
            )

        threading.Thread(target=_worker, daemon=True).start()

    @Slot()
    def _on_enhance_requested(self):
        """User clicked 'enhance current audio'."""
        if self._current_stream is None:
            QMessageBox.warning(self, "提示", "请先播放一个视频/音频")
            return

        settings = self._enhance_panel.get_settings()
        mode = EnhanceMode.REALTIME if settings["mode"] == "realtime" else EnhanceMode.QUALITY
        self._enhancer.set_target_sample_rate(settings["sample_rate"])
        self._enhancer.set_num_steps(settings["nfe_steps"])
        self._enhancer.set_ddim_steps(settings["ddim_steps"])

        # Load model in background, then start enhancement
        self._enhance_panel.show_progress(True)
        self._enhance_panel.update_progress(0.0, "正在加载模型...")

        def _load_and_enhance():
            if not self._enhancer.load_model(mode):
                self._enhance_status_update.emit(
                    PipelineStatus(state=PipelineState.ERROR,
                                   message="模型加载失败，请检查模型文件是否存在")
                )
                return

            self._enhance_status_update.emit(("model_loaded", mode))

            audio_url = self._current_stream.audio_url or self._current_stream.video_url
            headers = self._current_stream.http_headers

            if mode == EnhanceMode.QUALITY:
                self._pipeline.start_quality_enhance(audio_url, headers)
            else:
                self._pipeline.start_realtime_enhance(audio_url, headers)

        threading.Thread(target=_load_and_enhance, daemon=True).start()

    @Slot()
    def _on_enhance_cancel(self):
        self._pipeline.cancel()
        self._enhancer.unload()
        self._enhance_panel.show_progress(False)
        self._enhanced_duration_s = 0.0
        if self._enhanced_playing:
            self._sync.deactivate_enhanced()
            self._enhanced_playing = False
            self._update_media_info()
        self._status_label.setText("增强已取消")

    @Slot(object)
    def _handle_enhance_status(self, status):
        """Handle enhancement status updates on the main thread."""
        # Handle tuple messages from backend init
        if isinstance(status, tuple):
            msg_type = status[0]
            if msg_type == "backend_ready":
                _, info, fw_avail, asr_avail = status
                backend_text = {
                    Backend.ROCM: f"ROCm ({info.device_name})",
                    Backend.DIRECTML: "DirectML",
                    Backend.CPU: "CPU (慢)",
                }[info.backend]
                self._enhance_panel.set_backend_info(
                    backend_text, info.backend != Backend.CPU
                )
                if fw_avail or asr_avail:
                    parts = []
                    if fw_avail:
                        parts.append("FastWave")
                    if asr_avail:
                        parts.append("AudioSR")
                    self._enhance_panel.set_model_status(
                        f"可用: {', '.join(parts)}", True
                    )
                else:
                    self._enhance_panel.set_model_status("未找到模型文件", False)
                self.backend_ready.emit()
                return
            elif msg_type == "model_loaded":
                self._enhance_panel.set_model_status("已加载", True)
                return

        # Handle PipelineStatus
        if not isinstance(status, PipelineStatus):
            return

        self._enhance_panel.update_progress(status.progress, status.message)

        # Track how much enhanced audio is available
        if status.enhanced_duration_s > 0:
            self._enhanced_duration_s = status.enhanced_duration_s

        if status.state == PipelineState.READY and status.enhanced_file:
            self._enhance_panel.show_progress(False)
            self._enhanced_duration_s = status.enhanced_duration_s
            if not self._enhanced_playing:
                self._status_label.setText("增强完成 — 切换到增强音频")
                current_pos = self._player_widget.position
                self._sync.activate_enhanced(status.enhanced_file, current_pos)
                self._enhanced_playing = True
                self._update_media_info()

        elif status.state == PipelineState.ENHANCING and status.enhanced_file:
            # Auto-switch: if enhanced audio covers current position + 10s buffer
            if not self._enhanced_playing:
                current_pos = self._player_widget.position
                if self._enhanced_duration_s >= current_pos + 10.0:
                    self._status_label.setText("增强覆盖当前位置 — 切换到升频音频")
                    self._sync.activate_enhanced(status.enhanced_file, current_pos)
                    self._enhanced_playing = True
                    self._update_media_info()

        elif status.state == PipelineState.ERROR:
            self._enhance_panel.show_progress(False)
            if status.recoverable and self._enhanced_playing:
                self._sync.fallback_to_original(status.message)
                self._enhanced_playing = False
                self._update_media_info()
                self._status_label.setText(f"增强失败，已回退到原始音频: {status.message}")
            else:
                self._status_label.setText(f"增强失败: {status.message}")
                QMessageBox.warning(self, "增强失败", status.message)
