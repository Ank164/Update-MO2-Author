"""Fill missing installed-mod authors from Nexus via the left-pane context menu."""
from pathlib import Path
from datetime import datetime
import json
import os
import shutil
import tempfile
import uuid

import mobase
from PyQt6.QtCore import QObject, QEvent, QSettings, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QProgressDialog


def metadata(path):
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    return {key: settings.value(key, "") for key in ("author", "modid", "gameName")}


def nexus_skip_reason(game, modid, repository):
    if modid <= 0 or not game:
        return "Skipped: no Nexus game/mod ID"
    if str(repository or "").strip().casefold() not in ("", "nexus"):
        return "Skipped: non-Nexus repository"
    return ""


def write_author(path, author):
    """Use Qt's INI codec (including escaped strings), with atomic replacement."""
    path = Path(path)
    if str(metadata(path)["author"]).strip():
        return False
    original = path.read_bytes()
    handle, temporary = tempfile.mkstemp(prefix=".author-", suffix=".ini", dir=path.parent)
    os.close(handle)
    try:
        shutil.copyfile(path, temporary)
        settings = QSettings(temporary, QSettings.Format.IniFormat)
        settings.setValue("author", author)
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            raise OSError("Could not save author metadata")
        del settings
        if metadata(temporary)["author"] != author:
            raise OSError("Author read-back did not match")
        if path.read_bytes() != original:
            raise OSError("Metadata changed during update; skipped")
        os.replace(temporary, path)
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class MenuFilter(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QMenu):
            parent = watched.parent()
            if parent is not None and parent.objectName() == "modList":
                self.plugin.add_menu(watched, parent)
        return False


class NexusAuthorMetadata(mobase.IPlugin):
    def __init__(self):
        super().__init__()
        self.organizer = None
        self.window = None
        self.busy = False
        self.bridge = None

    def name(self):
        return "Nexus Author Metadata"

    def author(self):
        return "Ron / OpenAI"

    def description(self):
        return "Right-click selected installed mods to fill missing Nexus authors."

    def version(self):
        return mobase.VersionInfo(1, 0, 3)

    def settings(self):
        return []

    def init(self, organizer):
        self.organizer = organizer
        self.filter = MenuFilter(self)
        organizer.onUserInterfaceInitialized(self.attach)
        return True

    def attach(self, window):
        self.window = window
        QApplication.instance().installEventFilter(self.filter)

    def add_menu(self, menu, view):
        if menu.property("nexusAuthorMetadataAdded"):
            return
        menu.setProperty("nexusAuthorMetadataAdded", True)
        names = []
        selection = view.selectionModel()
        if selection is not None:
            for index in selection.selectedRows(0):
                name = str(index.data() or "")
                mod = self.organizer.modList().getMod(name)
                if mod and not any((mod.isSeparator(), mod.isForeign(), mod.isBackup(), mod.isOverwrite())):
                    names.append(name)
        action = QAction("Fill missing authors from Nexus", menu)
        action.setEnabled(bool(names) and not self.busy)
        action.triggered.connect(lambda checked=False: QTimer.singleShot(0, lambda: self.start(names)))
        menu.addSeparator()
        menu.addAction(action)

    def start(self, names):
        if self.busy:
            return
        self.busy = True
        self.rows = []
        self.groups = {}
        self.results = {}
        self.token = uuid.uuid4().hex
        try:
            for name in dict.fromkeys(names):
                mod = self.organizer.modList().getMod(name)
                if mod is None:
                    continue
                path = Path(mod.absolutePath()) / "meta.ini"
                row = {"mod": name, "path": str(path), "status": ""}
                self.rows.append(row)
                try:
                    info = metadata(path)
                    if str(info["author"]).strip():
                        row["status"] = "Skipped: author already present"
                        continue
                    game, modid = mod.gameName(), mod.nexusId()
                    reason = nexus_skip_reason(game, modid, mod.repository())
                    if reason:
                        row["status"] = reason
                        continue
                    row.update(game=game, modid=modid)
                    self.groups.setdefault((game, modid), []).append(row)
                except Exception as error:
                    row["status"] = f"Failed: {error}"
            self.queue = list(self.groups)
            self.total = len(self.queue)
            self.progress = QProgressDialog("Fetching missing authors from Nexus…", "Cancel", 0, self.total, self.window)
            self.progress.setWindowTitle(self.name())
            self.progress.setMinimumDuration(0)
            self.progress.setAutoClose(False)
            self.progress.setAutoReset(False)
            self.progress.canceled.connect(self.cancel)
            self.bridge = self.organizer.createNexusBridge()
            self.bridge.descriptionAvailable.connect(self.received)
            self.bridge.requestFailed.connect(self.failed)
            self.timer = QTimer(self.progress)
            self.timer.setSingleShot(True)
            self.timer.timeout.connect(self.timeout)
            self.current = None
            self.next_request()
        except Exception as error:
            self.cleanup()
            QMessageBox.critical(self.window, self.name(), str(error))

    def next_request(self):
        if not self.busy:
            return
        if not self.queue:
            self.progress.setLabelText("Saving author metadata…")
            # Wait for an existing refresh, or apply now; do not start another.
            self.organizer.onNextRefresh(self.apply, True)
            return
        self.current = self.queue.pop(0)
        game, modid = self.current
        self.progress.setLabelText(f"Fetching: {self.groups[self.current][0]['mod']}")
        self.timer.start(60000)
        try:
            self.bridge.requestDescription(game, modid, self.token)
        except Exception as error:
            self.failed(game, modid, 0, self.token, 0, str(error))

    def received(self, game, modid, token, result):
        # MO2 requests use SkyrimSE, but replies use skyrimspecialedition.
        # The batch token and sole in-flight mod ID identify this response.
        if not self.busy or token != self.token or self.current is None or modid != self.current[1]:
            return
        self.timer.stop()
        author = str(result.get("author") or "").strip()
        for row in self.groups[self.current]:
            row["status"] = "Ready" if author else "Skipped: Nexus returned no author"
            row["author"] = author
        self.current = None
        self.progress.setValue(self.total - len(self.queue))
        QTimer.singleShot(250, self.next_request)

    def failed(self, game, modid, fileid, token, code, message):
        if not self.busy or token != self.token or self.current is None or modid != self.current[1]:
            return
        self.timer.stop()
        for row in self.groups[self.current]:
            row["status"] = f"Failed: {message}"
        self.current = None
        if int(code) in (401, 403, 429):
            for key in self.queue:
                for row in self.groups[key]:
                    row["status"] = "Skipped: Nexus login/access/rate limit stopped batch"
            self.queue.clear()
        self.progress.setValue(self.total - len(self.queue))
        QTimer.singleShot(250, self.next_request)

    def timeout(self):
        # Stop rather than queue more requests behind a stalled Nexus connection.
        if self.current:
            for row in self.groups[self.current]:
                row["status"] = "Failed: Nexus request timed out"
        self.cancel()

    def cancel(self):
        if not self.busy:
            return
        for row in self.rows:
            if row["status"] in ("", "Ready"):
                row["status"] = "Cancelled: no change made"
        self.finish()

    def apply(self):
        if not self.busy:
            return
        self.run_dir = Path(self.organizer.pluginDataPath()) / "nexus_author_metadata" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + self.token[:8])
        try:
            for number, row in enumerate(self.rows):
                if row["status"] != "Ready":
                    continue
                try:
                    mod = self.organizer.modList().getMod(row["mod"])
                    if mod is None or str(Path(mod.absolutePath()) / "meta.ini") != row["path"] or mod.nexusId() != row["modid"] or mod.gameName() != row["game"]:
                        raise ValueError("Mod changed since lookup; skipped")
                    updated = write_author(Path(row["path"]), row["author"])
                    row["status"] = "Updated" if updated else "Skipped: author already present"
                except Exception as error:
                    row["status"] = f"Failed: {error}"
        finally:
            if any(row["status"] == "Updated" for row in self.rows):
                self.organizer.refresh()
            self.finish()

    def cleanup(self):
        self.busy = False
        if hasattr(self, "timer"):
            self.timer.stop()
        if hasattr(self, "progress"):
            self.progress.close()
        if self.bridge is not None:
            self.bridge.descriptionAvailable.disconnect(self.received)
            self.bridge.requestFailed.disconnect(self.failed)
            self.bridge = None

    def finish(self):
        self.cleanup()
        report_dir = getattr(self, "run_dir", None)
        if report_dir is None:
            report_dir = Path(self.organizer.pluginDataPath()) / "nexus_author_metadata" / self.token
        self.run_dir = None
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            report = report_dir / "report.json"
            report.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            print(f"[{self.name()}] Could not save report: {error}")


def createPlugin():
    return NexusAuthorMetadata()
