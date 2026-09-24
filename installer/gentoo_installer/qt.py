"""Qt 6 bindings: PySide6 if installed, PyQt6 otherwise."""

try:
    from PySide6 import QtCore, QtGui, QtWidgets

    Signal = QtCore.Signal
    BINDING = "PySide6"
except ImportError:  # pragma: no cover - depends on the installed binding
    from PyQt6 import QtCore, QtGui, QtWidgets

    Signal = QtCore.pyqtSignal
    BINDING = "PyQt6"

__all__ = ["BINDING", "QtCore", "QtGui", "QtWidgets", "Signal"]
