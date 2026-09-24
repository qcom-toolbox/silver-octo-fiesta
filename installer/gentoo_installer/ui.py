"""Qt 6 user interface of the installer: a simple step-by-step wizard."""

from __future__ import annotations

import datetime
import os
import sys
import traceback

from . import backend, system
from .qt import QtCore, QtGui, QtWidgets, Signal

LOG_FILE = "/var/log/gentoo-installer.log"

Qt = QtCore.Qt
QMessageBox = QtWidgets.QMessageBox


def _heading(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    font = label.font()
    font.setPointSizeF(font.pointSizeF() * 1.6)
    font.setBold(True)
    label.setFont(font)
    return label


def _note(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    return label


def _banner(text: str, severity: str) -> QtWidgets.QLabel:
    colors = {"error": ("#5c1a1a", "#ffd7d7"), "warning": ("#5c4a1a", "#fff1c7"), "info": ("#1a3d5c", "#d7ecff")}
    fg, bg = colors[severity]
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"QLabel {{ color: {fg}; background: {bg}; border-radius: 6px; padding: 10px; }}")
    return label


class Page(QtWidgets.QWidget):
    title = ""

    def __init__(self, wizard: "InstallerWindow"):
        super().__init__()
        self.wizard = wizard
        self.layout_ = QtWidgets.QVBoxLayout(self)
        self.layout_.setContentsMargins(24, 16, 24, 16)
        self.layout_.setSpacing(12)

    @property
    def cfg(self) -> backend.InstallConfig:
        return self.wizard.cfg

    def on_enter(self) -> None:
        """Called every time the page is shown."""

    def validate(self) -> str | None:
        """Return an error message to stay on the page."""
        return None

    def apply(self) -> None:
        """Store the page's values in the InstallConfig."""


# ---------------------------------------------------------------------------
class WelcomePage(Page):
    title = "Welcome"

    def __init__(self, wizard):
        super().__init__(wizard)
        ed = wizard.edition
        self.layout_.addWidget(_heading(f"Welcome to {ed['DISTRO_NAME']}"))
        self.layout_.addWidget(_note(
            "This assistant installs the KDE Plasma desktop you are using right now onto your "
            "computer. It only takes a few minutes and a few questions. Nothing is changed "
            "until you confirm on the summary page."))

        cpu = system.cpu_info()
        gpus = system.detect_gpus()
        mem = system.mem_total_bytes()
        form = QtWidgets.QFormLayout()
        form.addRow("<b>This image:</b>", QtWidgets.QLabel(
            f"{ed['CPU_DESC']}<br>{ed['GPU_DESC']}"))
        form.addRow("<b>Your processor:</b>", QtWidgets.QLabel(f"{cpu.model} ({cpu.threads} threads)"))
        gpu_text = "<br>".join(g.name for g in gpus) or "not detected"
        form.addRow("<b>Your graphics:</b>", QtWidgets.QLabel(gpu_text))
        form.addRow("<b>Memory:</b>", QtWidgets.QLabel(system.human_size(mem) if mem else "unknown"))
        form.addRow("<b>Boot mode:</b>", QtWidgets.QLabel("UEFI" if wizard.cfg.uefi else "Legacy BIOS"))
        box = QtWidgets.QGroupBox("Your computer")
        box.setLayout(form)
        self.layout_.addWidget(box)

        self.confirm_incompatible = None
        compat = system.cpu_compatibility(ed, cpu)
        if compat:
            severity, message = compat
            self.layout_.addWidget(_banner(message, severity))
            if severity == "error":
                self.confirm_incompatible = QtWidgets.QCheckBox("I understand, install anyway")
                self.layout_.addWidget(self.confirm_incompatible)
        has_nvidia = any(g.vendor == "NVIDIA" for g in gpus)
        if ed.get("GPU_ID") == "nvidia" and not has_nvidia:
            self.layout_.addWidget(_banner(
                "This is the NVIDIA edition but no NVIDIA card was found. It works fine with "
                "AMD and Intel graphics too; the mesa edition is a bit smaller.", "info"))
        if ed.get("GPU_ID") == "mesa" and has_nvidia:
            self.layout_.addWidget(_banner(
                "An NVIDIA graphics card was found, but this edition has no NVIDIA driver. "
                "Use the NVIDIA edition for GeForce RTX cards.", "warning"))

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("<b>Language of the installed system:</b>"))
        self.locale = QtWidgets.QComboBox()
        self.locale.addItems(system.list_locales())
        self.locale.setCurrentText(wizard.cfg.locale)
        row.addWidget(self.locale, 1)
        self.layout_.addLayout(row)
        self.layout_.addStretch(1)

    def validate(self):
        if self.confirm_incompatible is not None and not self.confirm_incompatible.isChecked():
            return ("This image does not match your processor. Download the edition for your "
                    "CPU, or tick the checkbox to install anyway.")
        return None

    def apply(self):
        self.cfg.locale = self.locale.currentText()


# ---------------------------------------------------------------------------
class LocationPage(Page):
    title = "Location"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.layout_.addWidget(_heading("Time zone and keyboard"))

        form = QtWidgets.QFormLayout()
        self.timezones = system.list_timezones()
        self.timezone = self._searchable_combo(self.timezones)
        tz = system.current_timezone()
        self.timezone.setCurrentText(tz if tz in self.timezones else "UTC")
        form.addRow("Time zone:", self.timezone)

        self.layouts = system.list_keyboard_layouts()
        self.layout_combo = self._searchable_combo([f"{desc} ({code})" for code, desc in self.layouts])
        codes = [code for code, _ in self.layouts]
        self.layout_combo.setCurrentIndex(codes.index("us") if "us" in codes else 0)
        form.addRow("Keyboard layout:", self.layout_combo)

        self.variant = QtWidgets.QLineEdit()
        self.variant.setPlaceholderText("optional, e.g. nodeadkeys, dvorak, intl")
        form.addRow("Keyboard variant:", self.variant)
        self.layout_.addLayout(form)
        self.layout_.addWidget(_note(
            "<i>The keyboard layout is applied to the installed system. You can change it "
            "later in System Settings &gt; Keyboard.</i>"))
        self.layout_.addStretch(1)

    @staticmethod
    def _searchable_combo(items: list[str]) -> QtWidgets.QComboBox:
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        combo.addItems(items)
        combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        completer = combo.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
        return combo

    def validate(self):
        if self.timezone.currentText() not in self.timezones:
            return "Please pick a time zone from the list (type a city to search)."
        if self.layout_combo.findText(self.layout_combo.currentText()) < 0:
            return "Please pick a keyboard layout from the list."
        return None

    def apply(self):
        self.cfg.timezone = self.timezone.currentText()
        index = self.layout_combo.findText(self.layout_combo.currentText())
        self.cfg.keyboard_layout = self.layouts[index][0]
        self.cfg.keyboard_variant = self.variant.text().strip()


# ---------------------------------------------------------------------------
class DiskPage(Page):
    title = "Disk"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.layout_.addWidget(_heading("Where should it be installed?"))

        self.erase = QtWidgets.QRadioButton("Erase a disk and install there (recommended)")
        self.erase.setChecked(True)
        self.manual = QtWidgets.QRadioButton("Use existing partitions (dual boot, advanced)")
        self.layout_.addWidget(self.erase)

        self.disk = QtWidgets.QComboBox()
        indent = QtWidgets.QHBoxLayout()
        indent.addSpacing(28)
        indent.addWidget(self.disk, 1)
        self.layout_.addLayout(indent)
        self.erase_warning = _banner("", "warning")
        self.layout_.addWidget(self.erase_warning)

        self.layout_.addWidget(self.manual)
        self.manual_box = QtWidgets.QWidget()
        mform = QtWidgets.QFormLayout(self.manual_box)
        mform.setContentsMargins(28, 0, 0, 0)
        self.root_part = QtWidgets.QComboBox()
        self.efi_part = QtWidgets.QComboBox()
        self.format_efi = QtWidgets.QCheckBox("Format the EFI partition (erases other systems' boot loaders)")
        mform.addRow("System partition (will be formatted):", self.root_part)
        mform.addRow("EFI system partition:", self.efi_part)
        mform.addRow("", self.format_efi)
        buttons = QtWidgets.QHBoxLayout()
        partman = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("partitionmanager"), "Open KDE Partition Manager")
        partman.clicked.connect(self._open_partition_manager)
        refresh = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("view-refresh"), "Refresh")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(partman)
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        mform.addRow("", buttons)
        self.layout_.addWidget(self.manual_box)

        fs_row = QtWidgets.QHBoxLayout()
        fs_row.addWidget(QtWidgets.QLabel("File system:"))
        self.fs = QtWidgets.QComboBox()
        self.fs.addItem("Btrfs - compression and snapshots (recommended)", "btrfs")
        self.fs.addItem("ext4 - classic and simple", "ext4")
        fs_row.addWidget(self.fs, 1)
        self.layout_.addLayout(fs_row)
        self.layout_.addStretch(1)

        self.erase.toggled.connect(self._update_mode)
        self.disk.currentIndexChanged.connect(self._update_warning)
        self.disks: list[system.Disk] = []
        self.partitions: list[system.Partition] = []
        self._update_mode()

    def on_enter(self):
        self.refresh()

    def refresh(self):
        self.disks = [d for d in system.list_disks() if not d.is_live_medium]
        self.partitions = [p for d in self.disks for p in d.partitions]
        self.disk.clear()
        for d in self.disks:
            self.disk.addItem(QtGui.QIcon.fromTheme("drive-harddisk"), d.describe(), d.path)
        self.root_part.clear()
        self.efi_part.clear()
        for p in self.partitions:
            self.root_part.addItem(p.describe(), p.path)
        efis = [p for p in self.partitions if p.is_efi] or self.partitions
        for p in efis:
            self.efi_part.addItem(p.describe(), p.path)
        self._update_warning()

    def _update_mode(self):
        erase = self.erase.isChecked()
        self.disk.setEnabled(erase)
        self.erase_warning.setVisible(erase)
        self.manual_box.setEnabled(not erase)
        self.efi_part.setEnabled(self.cfg.uefi and not erase)
        self.format_efi.setEnabled(self.cfg.uefi and not erase)

    def _update_warning(self):
        if not self.disks:
            self.erase_warning.setText("No disk found. Connect a disk and press Refresh.")
            return
        disk = self.disks[max(self.disk.currentIndex(), 0)]
        parts = ", ".join(p.describe() for p in disk.partitions) or "no partitions"
        self.erase_warning.setText(
            f"<b>Everything on {disk.path} will be deleted</b> ({parts}).")
        self.erase_warning.setTextFormat(Qt.TextFormat.RichText)

    def _open_partition_manager(self):
        if not QtCore.QProcess.startDetached("partitionmanager", []):
            QMessageBox.warning(self, "Partition Manager", "KDE Partition Manager could not be started.")

    def validate(self):
        if self.erase.isChecked():
            if not self.disks:
                return "No disk available for the installation."
            disk = self.disks[self.disk.currentIndex()]
            if disk.size < backend.MIN_DISK_BYTES:
                return f"{disk.path} is too small, at least {system.human_size(backend.MIN_DISK_BYTES)} are needed."
            return None
        root = self.root_part.currentData()
        if not root:
            return "Select the partition to install to (create one with KDE Partition Manager)."
        part = next(p for p in self.partitions if p.path == root)
        if part.size < backend.MIN_DISK_BYTES - system.GIB:
            return f"{root} is too small, at least {system.human_size(backend.MIN_DISK_BYTES)} are needed."
        if self.cfg.uefi:
            efi = self.efi_part.currentData()
            if not efi:
                return "An EFI system partition is required (FAT32, at least 300 MiB)."
            if efi == root:
                return "The system partition and the EFI partition must be different."
        return None

    def apply(self):
        c = self.cfg
        c.filesystem = self.fs.currentData()
        if self.erase.isChecked():
            c.mode = "erase"
            c.disk = self.disk.currentData()
            c.root_partition = c.efi_partition = ""
            c.format_efi = True
        else:
            c.mode = "manual"
            c.root_partition = self.root_part.currentData()
            c.efi_partition = self.efi_part.currentData() if c.uefi else ""
            c.format_efi = self.format_efi.isChecked()
            part = next(p for p in self.partitions if p.path == c.root_partition)
            c.disk = part.disk  # needed for grub-install on BIOS machines


# ---------------------------------------------------------------------------
class UserPage(Page):
    title = "User"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.layout_.addWidget(_heading("Who will use this computer?"))
        form = QtWidgets.QFormLayout()
        self.full_name = QtWidgets.QLineEdit()
        self.username = QtWidgets.QLineEdit()
        self.hostname = QtWidgets.QLineEdit(wizard.cfg.hostname)
        self.password = QtWidgets.QLineEdit()
        self.confirm = QtWidgets.QLineEdit()
        for field in (self.password, self.confirm):
            field.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("Your name:", self.full_name)
        form.addRow("User name:", self.username)
        form.addRow("Computer name:", self.hostname)
        form.addRow("Password:", self.password)
        form.addRow("Repeat password:", self.confirm)
        self.layout_.addLayout(form)
        self.root_same = QtWidgets.QCheckBox("Use the same password for the administrator (root) account")
        self.autologin = QtWidgets.QCheckBox("Log in automatically without asking for the password")
        self.layout_.addWidget(self.root_same)
        self.layout_.addWidget(self.autologin)
        self.layout_.addWidget(_note(
            "<i>Your user can administer the system with <tt>sudo</tt>. "
            "If you leave the first box unticked, direct root logins stay disabled.</i>"))
        self.layout_.addStretch(1)

        self._username_edited = False
        self.full_name.textChanged.connect(self._suggest)
        self.username.textEdited.connect(lambda _: setattr(self, "_username_edited", True))

    def _suggest(self, text: str):
        if not self._username_edited:
            self.username.setText(backend.suggest_username(text))

    def validate(self):
        return (backend.validate_username(self.username.text())
                or backend.validate_hostname(self.hostname.text())
                or backend.validate_password(self.password.text(), self.confirm.text()))

    def apply(self):
        c = self.cfg
        c.full_name = self.full_name.text().strip()
        c.username = self.username.text()
        c.hostname = self.hostname.text()
        c.password = self.password.text()
        c.root_password_same = self.root_same.isChecked()
        c.autologin = self.autologin.isChecked()


# ---------------------------------------------------------------------------
class SummaryPage(Page):
    title = "Summary"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.layout_.addWidget(_heading("Ready to install"))
        self.table = QtWidgets.QLabel()
        self.table.setTextFormat(Qt.TextFormat.RichText)
        self.table.setWordWrap(True)
        self.layout_.addWidget(self.table)
        self.warning = _banner("", "error")
        self.layout_.addWidget(self.warning)
        self.layout_.addStretch(1)

    def on_enter(self):
        rows = "".join(
            f"<tr><td style='padding:4px 16px 4px 0'><b>{key}</b></td><td style='padding:4px 0'>{value}</td></tr>"
            for key, value in self.cfg.summary()
        )
        self.table.setText(f"<table>{rows}</table>")
        if self.cfg.mode == "erase":
            self.warning.setText(f"All data on {self.cfg.disk} will be erased when you click Install.")
        else:
            self.warning.setText(f"{self.cfg.root_partition} will be formatted when you click Install.")


# ---------------------------------------------------------------------------
class InstallWorker(QtCore.QThread):
    progress = Signal(float, str)
    log = Signal(str)
    failed = Signal(str)
    succeeded = Signal()

    def __init__(self, cfg: backend.InstallConfig, edition: dict, dry_run: bool):
        super().__init__()
        self.cfg, self.edition, self.dry_run = cfg, edition, dry_run

    def run(self):
        runner = backend.Runner(dry_run=self.dry_run, log=self.log.emit)
        try:
            backend.Installer(self.cfg, runner, progress=self.progress.emit, edition=self.edition).run()
        except backend.InstallError as exc:
            self.failed.emit(str(exc))
            return
        except Exception:  # noqa: BLE001 - show every crash to the user
            self.failed.emit(traceback.format_exc())
            return
        self.succeeded.emit()


class InstallPage(Page):
    title = "Install"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.layout_.addWidget(_heading(f"Installing {wizard.edition['DISTRO_NAME']}"))
        self.status = QtWidgets.QLabel("Preparing...")
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.layout_.addWidget(self.status)
        self.layout_.addWidget(self.bar)
        self.details = QtWidgets.QPushButton("Show details")
        self.details.setCheckable(True)
        self.details.setFlat(True)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self.log.setVisible(False)
        self.details.toggled.connect(self.log.setVisible)
        self.details.toggled.connect(lambda on: self.details.setText("Hide details" if on else "Show details"))
        self.layout_.addWidget(self.details, 0, Qt.AlignmentFlag.AlignLeft)
        self.layout_.addWidget(self.log, 1)
        self.layout_.addStretch(0)

    def set_progress(self, fraction: float, text: str):
        self.bar.setValue(int(fraction * 1000))
        self.status.setText(text)

    def append_log(self, line: str):
        self.log.appendPlainText(line)
        self.wizard.write_log(line)


class FinishPage(Page):
    title = "Finish"

    def __init__(self, wizard):
        super().__init__(wizard)
        self.heading = _heading("")
        self.text = _note("")
        self.layout_.addWidget(self.heading)
        self.layout_.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        self.reboot = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("system-reboot"), "Restart now")
        self.reboot.clicked.connect(self._reboot)
        row.addWidget(self.reboot)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.layout_.addStretch(1)

    def show_result(self, ok: bool, message: str = ""):
        name = self.wizard.edition["DISTRO_NAME"]
        if ok:
            self.heading.setText("All done!")
            self.text.setText(
                f"{name} has been installed. Restart the computer and remove the USB stick.<br><br>"
                "Tips for the new system:<ul>"
                "<li><tt>gentoo-update</tt> updates everything (packages and Flatpak apps).</li>"
                "<li>Discover installs apps like Steam or Discord from Flathub.</li>"
                "<li><tt>neofetch</tt> and <tt>htop</tt> are ready in Konsole.</li></ul>")
            self.reboot.setVisible(True)
        else:
            self.heading.setText("The installation failed")
            self.text.setText(
                f"<pre>{_escape(message)}</pre>The full log is in <tt>{LOG_FILE}</tt>. "
                "Nothing was changed on disks other than the one you selected.")
            self.reboot.setVisible(False)

    def _reboot(self):
        if self.wizard.dry_run:
            QMessageBox.information(self, "Dry run", "Would restart the computer now.")
            return
        for cmd in (["loginctl", "reboot"], ["reboot"]):
            if QtCore.QProcess.startDetached(cmd[0], cmd[1:]):
                return


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
class InstallerWindow(QtWidgets.QMainWindow):
    def __init__(self, edition: dict, dry_run: bool = False):
        super().__init__()
        self.edition = edition
        self.dry_run = dry_run
        self.cfg = backend.InstallConfig(
            uefi=system.is_uefi(), hostname=f"{edition['DISTRO_ID']}-pc",
        )
        self.worker: InstallWorker | None = None
        self._log = None
        self.installing = False

        title = f"Install {edition['DISTRO_NAME']}" + (" (dry run)" if dry_run else "")
        self.setWindowTitle(title)
        self.setWindowIcon(QtGui.QIcon.fromTheme("system-software-install"))
        self.resize(900, 620)

        central = QtWidgets.QWidget()
        outer = QtWidgets.QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        sidebar.setStyleSheet(
            "#sidebar { background: #54487a; } #sidebar QLabel { color: #e8e4f5; padding: 6px 18px; }")
        side = QtWidgets.QVBoxLayout(sidebar)
        side.setContentsMargins(0, 18, 0, 18)
        logo = QtWidgets.QLabel(f"<b style='font-size:15pt'>{edition['DISTRO_NAME']}</b>"
                                f"<br><span style='font-size:9pt'>{edition['EDITION']}</span>")
        logo.setTextFormat(Qt.TextFormat.RichText)
        side.addWidget(logo)
        side.addSpacing(18)

        self.pages: list[Page] = [
            WelcomePage(self), LocationPage(self), DiskPage(self), UserPage(self),
            SummaryPage(self), InstallPage(self), FinishPage(self),
        ]
        self.step_labels = []
        for page in self.pages:
            label = QtWidgets.QLabel(page.title)
            side.addWidget(label)
            self.step_labels.append(label)
        side.addStretch(1)
        if dry_run:
            side.addWidget(QtWidgets.QLabel("<i>Dry run: nothing<br>will be changed</i>"))
        outer.addWidget(sidebar)

        right = QtWidgets.QVBoxLayout()
        self.stack = QtWidgets.QStackedWidget()
        for page in self.pages:
            self.stack.addWidget(page)
        right.addWidget(self.stack, 1)

        nav = QtWidgets.QHBoxLayout()
        nav.setContentsMargins(24, 8, 24, 16)
        self.cancel = QtWidgets.QPushButton("Cancel")
        self.back = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("go-previous"), "Back")
        self.next = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("go-next"), "Next")
        self.next.setDefault(True)
        nav.addWidget(self.cancel)
        nav.addStretch(1)
        nav.addWidget(self.back)
        nav.addWidget(self.next)
        right.addLayout(nav)
        outer.addLayout(right, 1)
        self.setCentralWidget(central)

        self.cancel.clicked.connect(self.close)
        self.back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next.clicked.connect(self.on_next)
        self.go(0)

    # navigation ------------------------------------------------------------
    def current(self) -> Page:
        return self.pages[self.stack.currentIndex()]

    def go(self, index: int):
        index = max(0, min(index, len(self.pages) - 1))
        self.stack.setCurrentIndex(index)
        page = self.pages[index]
        page.on_enter()
        for i, label in enumerate(self.step_labels):
            label.setStyleSheet("font-weight: bold; background: #6f63a0;" if i == index else "")
        is_summary = isinstance(page, SummaryPage)
        final = isinstance(page, (InstallPage, FinishPage))
        self.back.setVisible(not final)
        self.back.setEnabled(index > 0)
        self.next.setVisible(not final)
        self.next.setText("Install" if is_summary else "Next")
        self.next.setIcon(QtGui.QIcon.fromTheme("run-install" if is_summary else "go-next"))
        self.cancel.setText("Close" if isinstance(page, FinishPage) else "Cancel")
        self.cancel.setEnabled(not isinstance(page, InstallPage))

    def on_next(self):
        page = self.current()
        error = page.validate()
        if error:
            QMessageBox.warning(self, page.title, error)
            return
        page.apply()
        if isinstance(page, SummaryPage):
            self.start_install()
        else:
            self.go(self.stack.currentIndex() + 1)

    # installation ----------------------------------------------------------
    def start_install(self):
        target = self.cfg.disk if self.cfg.mode == "erase" else self.cfg.root_partition
        answer = QMessageBox.warning(
            self, "Start the installation?",
            f"{target} will be erased and {self.edition['DISTRO_NAME']} installed. This cannot be undone.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        install_page = next(p for p in self.pages if isinstance(p, InstallPage))
        self.go(self.pages.index(install_page))
        self.installing = True
        self.write_log(f"--- installation started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
        self.worker = InstallWorker(self.cfg, self.edition, self.dry_run)
        self.worker.progress.connect(install_page.set_progress)
        self.worker.log.connect(install_page.append_log)
        self.worker.failed.connect(lambda msg: self.finished(False, msg))
        self.worker.succeeded.connect(lambda: self.finished(True))
        self.worker.start()

    def finished(self, ok: bool, message: str = ""):
        self.installing = False
        self.write_log(f"--- installation {'finished' if ok else 'FAILED: ' + message}")
        finish = next(p for p in self.pages if isinstance(p, FinishPage))
        finish.show_result(ok, message)
        self.go(self.pages.index(finish))

    def write_log(self, line: str):
        if self.dry_run:
            return
        try:
            if self._log is None:
                self._log = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115
            self._log.write(line + "\n")
            self._log.flush()
        except OSError:
            pass

    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self.installing:
            QMessageBox.information(self, "Please wait", "The installation is still running.")
            event.ignore()
            return
        if not isinstance(self.current(), FinishPage):
            answer = QMessageBox.question(self, "Quit", "Quit the installer? Nothing has been changed yet.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def run_app(dry_run: bool = False, edition_path: str | None = None, qt_args: list[str] | None = None) -> int:
    app = QtWidgets.QApplication(qt_args or sys.argv)
    app.setApplicationName("gentoo-installer")
    app.setDesktopFileName("gentoo-installer")
    edition = system.read_edition(edition_path or system.EDITION_FILE)
    window = InstallerWindow(edition, dry_run=dry_run or os.environ.get("GENTOO_INSTALLER_DRY_RUN") == "1")
    window.show()
    return app.exec()
