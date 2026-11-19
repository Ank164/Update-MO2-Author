"""Update installed-mod authors from Nexus, Dwemer Mods, or manual entry."""
from pathlib import Path
from datetime import datetime
import json
import os
import shutil
import tempfile
import uuid
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urljoin

import mobase
from PyQt6.QtCore import QObject, QEvent, QProcess, QSettings, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel,
                            QInputDialog, QLineEdit, QMenu, QMessageBox, QProgressDialog, QVBoxLayout)


DWEMER = "https://dwemermods.com"


class DwemerBrowser(QObject):
    """Own one optional browser helper. No credentials cross this interface."""
    problem = pyqtSignal(str)

    def __init__(self, root, profile_dir, parent=None):
        super().__init__(parent)
        launcher = shutil.which("py")
        script = Path(__file__).parent / "nexus_author_support" / "dwemer_browser.py"
        if not launcher or not script.is_file():
            raise RuntimeError("Dwemer sign-in requires Python 3.12 and the nexus_author_support folder.")
        self.login_open = False
        self.ready = False
        self.error = ""
        self.closing = False
        self.buffer = bytearray()
        self.pending = {}
        self.request_timers = {}
        self.queue = []
        self.process = QProcess(self)
        self.process.setProgram(launcher)
        self.process.setArguments(["-3.12", "-s", str(script), "--mo2-dir", str(root), "--profile-dir", str(profile_dir)])
        self.process.setWorkingDirectory(str(root))
        self.process.readyReadStandardOutput.connect(self.read_messages)
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.process.errorOccurred.connect(lambda unused: self.fail("Dwemer browser could not start. Check Python 3.12 and the browser bindings."))
        self.process.finished.connect(lambda *unused: self.fail("Dwemer browser closed. Reopen Sign in to Dwemer to reconnect."))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.fail("Dwemer browser did not start in time."))
        self.timer.start(20000)
        self.process.start()

    def send(self, message):
        if self.ready:
            self.process.write((json.dumps(message) + "\n").encode("utf-8"))
        else:
            self.queue.append(message)

    def read_messages(self):
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            self.receive(message)

    def receive(self, message):
        if self.error or self.closing:
            return
        event = message.get("event")
        if event == "ready":
            self.timer.stop()
            self.ready = True
            queue, self.queue = self.queue, []
            for command in queue:
                self.send(command)
        elif event in ("login_opened", "login_closed"):
            self.login_open = event == "login_opened"
        elif event == "error":
            self.fail(message.get("error", "Dwemer browser failed"))
        else:
            callback = self.pending.pop(message.get("id"), None)
            self.stop_request_timer(message.get("id"))
            if callback:
                callback(message.get("html", ""), message.get("error", ""))

    def get(self, url, callback):
        if self.error:
            callback("", self.error)
            return None
        request_id = uuid.uuid4().hex
        self.pending[request_id] = callback
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self.request_timeout(request_id))
        self.request_timers[request_id] = timer
        timer.start(45000)
        self.send({"action": "fetch", "id": request_id, "url": url})
        return request_id

    def sign_in(self):
        self.login_open = True
        self.send({"action": "login"})

    def cancel(self, request_id):
        self.pending.pop(request_id, None)
        self.stop_request_timer(request_id)
        self.queue = [item for item in self.queue if item.get("id") != request_id]
        self.send({"action": "cancel", "id": request_id})

    def stop_request_timer(self, request_id):
        timer = self.request_timers.pop(request_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def request_timeout(self, request_id):
        callback = self.pending.get(request_id)
        self.cancel(request_id)
        if callback:
            callback("", "Dwemer browser did not answer in time")

    def fail(self, error):
        self.timer.stop()
        if self.error or self.closing:
            return
        interactive = self.login_open
        self.error = error
        self.ready = False
        self.login_open = False
        self.queue.clear()
        callbacks, self.pending = self.pending, {}
        for request_id in list(self.request_timers):
            self.stop_request_timer(request_id)
        for callback in callbacks.values():
            callback("", error)
        if interactive:
            self.problem.emit(error)

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        self.pending.clear()
        for request_id in list(self.request_timers):
            self.stop_request_timer(request_id)
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.send({"action": "shutdown"})
            self.process.closeWriteChannel()
            if not self.process.waitForFinished(1500):
                self.process.kill()
                self.process.waitForFinished(1000)


def dwemer_url(value):
    """Accept only a Dwemer mod-detail URL, never an arbitrary metadata URL."""
    try:
        parts = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in ("dwemermods.com", "www.dwemermods.com"):
        return ""
    match = re.fullmatch(r"/mods/(\d+)/?", parts.path)
    return f"{DWEMER}/mods/{match[1]}?lang=en" if match else ""


def normalized_name(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def search_names(row):
    names = [row["mod"]]
    archive = re.split(r"[/\\]", str(row.get("installationFile", "")))[-1]
    archive = re.sub(r"\.(?:zip|7z|rar)$", "", archive, flags=re.I)
    if archive:
        names.append(archive)
    queries = []
    for name in names:
        # Remove archive version/ID suffixes for searching, not for auto-matching.
        name = re.sub(r"[-_ ](?:v?\d[\d._-]*)(?:[-_ ].*)?$", "", name, flags=re.I)
        name = re.sub(r"[_]+", " ", name).strip()
        if len(normalized_name(name)) >= 4 and name.casefold() not in [q.casefold() for q in queries]:
            queries.append(name[:150])
    return names, queries[:2]


class DwemerPage(HTMLParser):
    """Read titles from search cards and creator credits from the detail header."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.candidates = {}
        self.creators = []
        self.more = False
        self.title_depth = 0
        self.anchor = None
        self.div_depth = 0
        self.meta_depth = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            if "mod-meta-row" in attrs.get("class", "").split():
                self.meta_depth = self.div_depth
        if tag == "h3" and "mod-title" in attrs.get("class", "").split():
            self.title_depth += 1
        if tag == "a":
            href = attrs.get("href", "")
            url = dwemer_url(urljoin(DWEMER, href))
            creator = (self.meta_depth is not None and href.startswith("/known-authors/")
                       and "mod-category-link" in attrs.get("class", "").split())
            if (self.title_depth and url) or creator:
                self.anchor = (url if not creator else "creator", [])
            if re.search(r"[?&]page=\d+", href):
                self.more = True

    def handle_data(self, data):
        if self.anchor:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "div":
            if self.meta_depth == self.div_depth:
                self.meta_depth = None
            self.div_depth = max(0, self.div_depth - 1)
        if tag == "h3":
            self.title_depth = 0
        if tag == "a" and self.anchor:
            key, chunks = self.anchor
            text = " ".join("".join(chunks).split())
            if text:
                if key == "creator":
                    self.creators.append(text)
                else:
                    self.candidates[key] = {"title": text, "url": key}
            self.anchor = None

    @property
    def author(self):
        return ", ".join(dict.fromkeys(self.creators))


def choose_dwemer(parent, name, candidates):
    labels = ["Enter author manually"] + [f"{c['title']} — {c['url']}" for c in candidates]
    selected, accepted = QInputDialog.getItem(
        parent, "Choose Dwemer mod", f"Which page matches {name}?", labels, 0, False)
    if not accepted:
        return None
    return "" if selected == labels[0] else candidates[labels.index(selected) - 1]["url"]


class DwemerClient(QObject):
    """Sequential asynchronous requests, with batch caching and bounded timeouts."""
    def __init__(self, parent=None, browser=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.cache = {}
        self.reply = None
        self.closed = False
        self.unavailable = ""
        self.browser = browser
        self.browser_request = None

    def get(self, url, callback):
        if self.closed:
            return
        if self.unavailable:
            callback("", self.unavailable)
            return
        if url in self.cache:
            callback(*self.cache[url])
            return
        if self.browser is not None:
            def browser_result(html, error):
                self.browser_request = None
                if self.closed:
                    return
                self.cache[url] = (html, error)
                callback(html, error)
            self.browser_request = self.browser.get(url, browser_result)
            return
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", b"Mozilla/5.0 (compatible; MO2 Author Metadata)")
        request.setTransferTimeout(15000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy)
        reply = self.network.get(request)
        self.reply = reply
        watchdog = QTimer(reply)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(reply.abort)
        watchdog.start(20000)

        def finished():
            watchdog.stop()
            if self.reply is reply:
                self.reply = None
            if not self.closed:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                error = "" if reply.error() == QNetworkReply.NetworkError.NoError else reply.errorString()
                if status in (401, 403, 429) or status is None or (error and status == 200):
                    self.unavailable = f"Dwemer unavailable ({status or error})"
                    error = self.unavailable
                data = bytes(reply.readAll()).decode("utf-8", errors="replace") if not error else ""
                self.cache[url] = (data, error)
                callback(data, error)
            reply.deleteLater()

        reply.finished.connect(finished)

    def close(self):
        self.closed = True
        if self.browser is not None and self.browser_request is not None:
            self.browser.cancel(self.browser_request)
        if self.reply is not None:
            self.reply.abort()

    def lookup(self, row, callback):
        for key in ("url", "website", "customURL"):
            url = dwemer_url(row.get(key))
            if url:
                self.detail(url, callback)
                return
        if str(row.get("game", "")).casefold() not in ("", "skyrimse", "skyrimvr", "skyrimspecialedition"):
            callback({"error": "Dwemer name search supports Skyrim SE/VR"})
            return
        names, queries = search_names(row)
        candidates = {}
        paginated = False

        def next_search():
            if not queries:
                exact = [c for c in candidates.values()
                         if normalized_name(c["title"]) in {normalized_name(n) for n in names}]
                if len(exact) == 1 and not paginated:
                    self.detail(exact[0]["url"], callback)
                else:
                    callback({"candidates": list(candidates.values())[:20]})
                return
            query = queries.pop(0)
            self.get(f"{DWEMER}/mods?" + urlencode({"search": query, "search_in": "title", "lang": "en"}), searched)

        def searched(html, error):
            nonlocal paginated
            if error:
                callback({"error": error})
                return
            page = DwemerPage(html)
            candidates.update(page.candidates)
            paginated = paginated or page.more
            QTimer.singleShot(350, next_search)

        next_search()

    def detail(self, url, callback):
        def parsed(html, error):
            author = DwemerPage(html).author if not error else ""
            callback({"author": author, "url": url, "error": error or ("" if author else "Dwemer returned no creator credit")})
        self.get(url, parsed)


class AuthorDialog(QDialog):
    def __init__(self, parent, mod, reason, remaining):
        super().__init__(parent)
        self.setWindowTitle("Enter mod author")
        self.action = "cancel"
        layout = QVBoxLayout(self)
        label = QLabel(f"Mod: {mod}\n{reason}\n\n"
                       f"{remaining} mods remaining. OK sets this mod; All sets all remaining mods.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.author = QLineEdit(self)
        self.author.setPlaceholderText("Author")
        layout.addWidget(self.author)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.all = buttons.addButton("All", QDialogButtonBox.ButtonRole.ActionRole)
        self.all.setEnabled(False)
        self.all.setVisible(remaining > 1)
        self.ok.setEnabled(False)
        self.author.textChanged.connect(lambda text: self.enable_buttons(bool(text.strip())))
        buttons.accepted.connect(lambda: self.choose("ok"))
        buttons.rejected.connect(self.reject)
        self.all.clicked.connect(lambda: self.choose("all"))
        layout.addWidget(buttons)

    def enable_buttons(self, enabled):
        self.ok.setEnabled(enabled)
        self.all.setEnabled(enabled)

    def choose(self, action):
        if self.author.text().strip():
            self.action = action
            self.accept()


def ask_author(parent, mod, reason, remaining):
    dialog = AuthorDialog(parent, mod, reason, remaining)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return "cancel", ""
    return dialog.action, dialog.author.text().strip()


def confirm_overwrite(parent, rows):
    dialog = QMessageBox(parent)
    dialog.setWindowTitle("Overwrite existing authors?")
    dialog.setIcon(QMessageBox.Icon.Question)
    dialog.setText(f"{len(rows)} selected mods already have an author.")
    dialog.setInformativeText("Overwrite allows their authors to be replaced by Nexus, Dwemer, or manual entry. "
                              "Cancel stops the entire batch without changes.")
    dialog.setDetailedText("\n".join(f"{row['mod']}: {row['original_author']}" for row in rows))
    overwrite = dialog.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole)
    cancel = dialog.addButton(QMessageBox.StandardButton.Cancel)
    dialog.setDefaultButton(cancel)
    dialog.setEscapeButton(cancel)
    dialog.exec()
    return dialog.clickedButton() is overwrite


def metadata(path):
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    return {key: settings.value(key, "") for key in
            ("author", "modid", "gameName", "url", "website", "customURL", "installationFile")}


def nexus_skip_reason(game, modid, repository):
    if modid <= 0 or not game:
        return "Skipped: no Nexus game/mod ID"
    if str(repository or "").strip().casefold() not in ("", "nexus"):
        return "Skipped: non-Nexus repository"
    return ""


def write_author(path, author, expected_author=None):
    """Use Qt's INI codec (including escaped strings), with atomic replacement."""
    path = Path(path)
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise
        original = None
    current = str(metadata(path)["author"])
    if expected_author is None:
        if current.strip():
            return False
    elif current != expected_author:
        raise ValueError("Author changed since confirmation; skipped")
    if current == author:
        return False
    handle, temporary = tempfile.mkstemp(prefix=".author-", suffix=".ini", dir=path.parent)
    os.close(handle)
    try:
        if original is not None:
            shutil.copyfile(path, temporary)
        settings = QSettings(temporary, QSettings.Format.IniFormat)
        settings.setValue("author", author)
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            raise OSError("Could not save author metadata")
        del settings
        if metadata(temporary)["author"] != author:
            raise OSError("Author read-back did not match")
        if original is None:
            # Publish the verified file without replacing one created meanwhile.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise OSError("Metadata created during update; skipped") from None
        else:
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
        self.dwemer = None
        self.browser = None

    def name(self):
        return "Nexus Author Metadata"

    def author(self):
        return "Ron / OpenAI"

    def description(self):
        return "Update selected mod authors from Nexus or Dwemer Mods, confirming existing authors first."

    def version(self):
        return mobase.VersionInfo(1, 6, 0)

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
        QApplication.instance().aboutToQuit.connect(self.close_browser)

    def close_browser(self):
        if self.browser is not None:
            self.browser.shutdown()

    def browser_profile_path(self):
        return Path(self.organizer.pluginDataPath()) / "nexus_author_metadata" / "dwemer-browser"

    def get_browser(self):
        if self.browser is not None and self.browser.error:
            self.browser.shutdown()
            self.browser.deleteLater()
            self.browser = None
        if self.browser is None:
            self.browser = DwemerBrowser(QApplication.applicationDirPath(), self.browser_profile_path())
            self.browser.problem.connect(lambda error: QMessageBox.warning(self.window, "Dwemer sign-in", error))
        return self.browser

    def sign_in_dwemer(self):
        if self.busy:
            return
        try:
            self.get_browser().sign_in()
        except Exception as error:
            QMessageBox.warning(self.window, "Dwemer sign-in", str(error))

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
        action = QAction("Update authors from Nexus / Dwemer", menu)
        action.setEnabled(bool(names) and not self.busy)
        action.triggered.connect(lambda checked=False: QTimer.singleShot(0, lambda: self.start(names)))
        menu.addSeparator()
        menu.addAction(action)
        login = QAction("Sign in to Dwemer…", menu)
        login.setEnabled(not self.busy)
        login.triggered.connect(lambda checked=False: self.sign_in_dwemer())
        menu.addAction(login)

    def start(self, names):
        if self.busy:
            return
        if self.browser is not None and self.browser.login_open:
            QMessageBox.information(self.window, self.name(), "Finish signing in and close the Dwemer window before updating authors.")
            return
        self.busy = True
        self.rows = []
        self.groups = {}
        self.results = {}
        self.dwemer_started = False
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
                    row["original_author"] = str(info["author"])
                    game, modid = mod.gameName(), mod.nexusId()
                    row.update(game=game, modid=modid)
                    row.update({key: info[key] for key in ("url", "website", "customURL", "installationFile")})
                    if any(dwemer_url(row.get(key)) for key in ("url", "website", "customURL")):
                        row["status"] = "Pending: Dwemer link"
                        continue
                    reason = nexus_skip_reason(game, modid, mod.repository())
                    if reason:
                        row["status"] = reason
                        continue
                    self.groups.setdefault((game, modid), []).append(row)
                except Exception as error:
                    row["status"] = f"Failed: {error}"
            existing = [row for row in self.rows if row.get("original_author", "").strip()]
            if existing and not confirm_overwrite(self.window, existing):
                self.cancel()
                return
            self.queue = list(self.groups)
            self.total = len(self.queue)
            self.progress = QProgressDialog("Fetching authors from Nexus…", "Cancel", 0, self.total, self.window)
            self.progress.setWindowTitle(self.name())
            self.progress.setMinimumDuration(0)
            self.progress.setAutoClose(False)
            self.progress.setAutoReset(False)
            self.progress.canceled.connect(self.cancel)
            if self.queue:
                try:
                    self.bridge = self.organizer.createNexusBridge()
                except Exception as error:
                    for rows in self.groups.values():
                        for row in rows:
                            row["status"] = f"Failed: Nexus unavailable: {error}"
                    self.queue.clear()
                if self.bridge is not None:
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
            self.current = None
            self.start_dwemer()
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
        for key in self.queue:
            for row in self.groups[key]:
                row["status"] = "Skipped: Nexus timeout stopped batch"
        self.queue.clear()
        self.current = None
        QTimer.singleShot(0, self.next_request)

    def start_dwemer(self):
        if self.dwemer_started:
            return
        self.dwemer_started = True
        self.dwemer_queue = [row for row in self.rows
                             if row["status"] not in ("Ready", "Skipped: author already present")
                             and "game" in row and "modid" in row]
        self.dwemer_total = len(self.dwemer_queue)
        browser = self.browser
        if browser is None and self.browser_profile_path().is_dir():
            try:
                browser = self.get_browser()
            except RuntimeError:
                browser = None
        self.dwemer = DwemerClient(self.progress, browser=browser)
        self.progress.setRange(0, self.dwemer_total)
        self.next_dwemer(self.token)

    def next_dwemer(self, token):
        if not self.busy or token != self.token:
            return
        if not self.dwemer_queue:
            self.progress.hide()
            if not self.prompt_missing_authors():
                self.cancel()
                return
            self.progress.setLabelText("Saving author metadata…")
            self.organizer.onNextRefresh(self.apply, True)
            return
        row = self.dwemer_queue.pop(0)
        self.progress.setLabelText(f"Searching Dwemer Mods: {row['mod']}")
        self.progress.setValue(self.dwemer_total - len(self.dwemer_queue) - 1)
        self.dwemer.lookup(row, lambda result: self.dwemer_result(token, row, result))

    def dwemer_result(self, token, row, result):
        if not self.busy or token != self.token:
            return
        candidates = result.get("candidates", [])
        if candidates:
            self.progress.hide()
            url = choose_dwemer(self.window, row["mod"], candidates)
            if url is None:
                self.cancel()
                return
            self.progress.show()
            if url:
                self.dwemer.detail(url, lambda detail: self.dwemer_result(token, row, detail))
                return
        if result.get("author"):
            row.update(author=result["author"], status="Ready", source="dwemer", source_url=result["url"])
        else:
            row["dwemer_status"] = result.get("error") or "No Dwemer match selected or found"
        QTimer.singleShot(350, lambda: self.next_dwemer(token))

    def prompt_missing_authors(self):
        pending = [row for row in self.rows
                   if row["status"] not in ("Ready", "Skipped: author already present")
                   and "game" in row and "modid" in row]
        for index, row in enumerate(pending):
            reason = row["status"]
            if row.get("dwemer_status"):
                reason += "\n" + row["dwemer_status"]
            if row.get("original_author", "").strip():
                reason += "\nCurrent author: " + row["original_author"]
            action, author = ask_author(self.window, row["mod"], reason, len(pending) - index)
            if action == "cancel":
                return False
            targets = pending[index:] if action == "all" else [row]
            for target in targets:
                target.update(author=author, status="Ready", source="manual", nexus_status=target["status"])
            if action == "all":
                break
        return True

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
                    updated = write_author(Path(row["path"]), row["author"], row.get("original_author", ""))
                    row["status"] = "Updated" if updated else "Skipped: author unchanged"
                except Exception as error:
                    row["status"] = f"Failed: {error}"
        finally:
            if any(row["status"] == "Updated" for row in self.rows):
                self.organizer.refresh()
            self.finish()

    def cleanup(self):
        self.busy = False
        if self.dwemer is not None:
            self.dwemer.close()
            self.dwemer = None
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
