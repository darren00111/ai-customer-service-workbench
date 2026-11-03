"""Shared icon and Windows taskbar identity for both desktop entry points."""
from pathlib import Path
import sys

APP_ICON = Path(__file__).resolve().parent / "assets" / "app-icon.ico"
APP_ID = "FeigeCustomerServiceCommunity.Desktop"


def configure_desktop_identity():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
