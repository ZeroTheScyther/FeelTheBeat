"""
The application icon, resolved once and shared by the tray and every window.
"""

from PyQt5.QtGui import QColor, QIcon, QPixmap

from paths import resource_path

_cached: QIcon | None = None


def app_icon() -> QIcon:
    """The app icon, falling back to a plain orange dot if the asset is missing."""
    global _cached
    if _cached is not None:
        return _cached

    path = resource_path("assets", "icon.png")
    icon = QIcon(str(path)) if path.exists() else QIcon()
    if icon.isNull():
        pm = QPixmap(32, 32)
        pm.fill(QColor(255, 90, 30))
        icon = QIcon(pm)

    _cached = icon
    return icon
