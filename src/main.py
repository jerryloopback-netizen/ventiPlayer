import sys
import os
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_exception_hooks():
    """Install global exception hooks so background thread crashes are logged."""
    def _sys_excepthook(exc_type, exc_value, exc_tb):
        if exc_type is KeyboardInterrupt:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    def _threading_excepthook(args):
        if args.exc_type is SystemExit:
            return
        logger.critical(
            f"Unhandled exception in thread '{args.thread.name if args.thread else '?'}'",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _threading_excepthook


_setup_exception_hooks()

# libmpv-2.dll lives in project root
_project_root = str(Path(__file__).resolve().parent.parent)
os.environ["PATH"] = _project_root + os.pathsep + os.environ.get("PATH", "")

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# ROCm MIOpen cache path (avoid non-ASCII username issues)
os.environ.setdefault("MIOPEN_USER_DB_PATH", "C:/temp/miopen_cache")
os.environ.setdefault("MIOPEN_CUSTOM_CACHE_DIR", "C:/temp/miopen_cache")
os.makedirs("C:/temp/miopen_cache", exist_ok=True)

# PyTorch memory management for 16GB VRAM
os.environ.setdefault("PYTORCH_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("PYTORCH_HIP_ALLOC_CONF", "garbage_collection_threshold:0.6,max_split_size_mb:128")

# HuggingFace mirror for China mainland
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Suppress timm deprecation warning
import warnings
warnings.filterwarnings("ignore", message="Importing from.*is deprecated.*timm.layers",
                        category=FutureWarning)

from PySide6.QtWidgets import QApplication, QSplashScreen
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QPixmap, QFont, QColor, QPainter

from src.gui.main_window import MainWindow


def _create_splash() -> QSplashScreen:
    pixmap = QPixmap(420, 200)
    pixmap.fill(QColor(30, 30, 30))
    painter = QPainter(pixmap)
    painter.setPen(QColor(220, 220, 220))
    font = QFont("Segoe UI", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "VentiPlayer")
    font2 = QFont("Segoe UI", 10)
    painter.setFont(font2)
    painter.setPen(QColor(160, 160, 160))
    painter.drawText(
        pixmap.rect().adjusted(0, 60, 0, 0),
        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
        "正在检测推理后端...",
    )
    painter.end()
    splash = QSplashScreen(pixmap)
    splash.setWindowFlags(
        Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
    )
    return splash


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("VentiPlayer")
    app.setOrganizationName("VentiPlayer")

    splash = _create_splash()
    splash.show()
    app.processEvents()

    # Create main window hidden — it will do backend detection in background thread
    window = MainWindow()
    # Don't show yet — wait for backend detection to complete

    def _on_backend_ready():
        splash.close()
        window.showMaximized()

    window.backend_ready.connect(_on_backend_ready)

    # Safety timeout: if detection hangs for 30s, show window anyway
    def _timeout():
        if not window.isVisible():
            splash.close()
            window.showMaximized()

    timeout_timer = QTimer()
    timeout_timer.setSingleShot(True)
    timeout_timer.timeout.connect(_timeout)
    timeout_timer.start(30000)
    app._timeout_timer = timeout_timer
    app._main_window = window

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
