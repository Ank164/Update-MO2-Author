"""Optional browser helper. JSON commands arrive on stdin; replies use stdout.

The browser owns its session. Passwords and cookies never cross this protocol.
Run in a separate interpreter so MO2's QApplication is not reconfigured.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urlsplit


def allowed_url(value):
    try:
        url = urlsplit(value)
    except (ValueError, TypeError):
        return False
    return (url.scheme == "https" and url.netloc == "dwemermods.com"
            and re.fullmatch(r"/mods(?:/\d+)?/?", url.path) is not None)


def emit(**message):
    print(json.dumps(message, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mo2-dir", required=True, type=Path)
    parser.add_argument("--profile-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.mo2_dir.resolve()
    os.environ["PATH"] = str(root / "dlls") + os.pathsep + str(root) + os.pathsep + os.environ.get("PATH", "")
    for key, relative in {
        "QT_QPA_PLATFORM_PLUGIN_PATH": "platforms",
        "QTWEBENGINEPROCESS_PATH": "QtWebEngineProcess.exe",
        "QTWEBENGINE_RESOURCES_PATH": "resources",
        "QTWEBENGINE_LOCALES_PATH": "translations/qtwebengine_locales",
    }.items():
        os.environ[key] = str(root / relative)
    dll_handles = [os.add_dll_directory(str(root / p))
                   for p in ("dlls", "plugins/plugin_python/dlls", ".")]
    sys.path.insert(0, str(root / "plugins/plugin_python/libs"))
    from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PyQt6.QtWebEngineWidgets import QWebEngineView

    app = QApplication(["Dwemer sign-in"])
    app.setQuitOnLastWindowClosed(False)
    app.addLibraryPath(str(root / "dlls"))
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    profile = QWebEngineProfile("mo2-dwemer-author", app)
    profile.setPersistentStoragePath(str(args.profile_dir / "storage"))
    profile.setCachePath(str(args.profile_dir / "cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
    profile.downloadRequested.connect(lambda download: download.cancel())

    class SitePage(QWebEnginePage):
        def acceptNavigationRequest(self, url, kind, main_frame):
            if not main_frame or url.toString() == "about:blank":
                return True
            return url.scheme() == "https" and url.host() in ("dwemermods.com", "www.dwemermods.com")

    class LoginWindow(QWidget):
        def closeEvent(self, event):
            self.hide()
            emit(event="login_closed")
            event.ignore()

    window = LoginWindow()
    window.setWindowTitle("Sign in to Dwemer — Author Metadata")
    window.resize(1050, 800)
    layout = QVBoxLayout(window)
    instructions = QLabel("Sign in on Dwemer's page and enable NSFW in your Dwemer profile settings. "
                          "Close this window when finished. Your session will be used for author searches.")
    instructions.setWordWrap(True)
    layout.addWidget(instructions)
    view = QWebEngineView(window)
    login_page = SitePage(profile, view)
    view.setPage(login_page)
    layout.addWidget(view)
    timer = QTimer(app)
    timer.setSingleShot(True)
    active = {}

    def finish(html="", error=""):
        request_id = active.pop("id", None)
        fetch_page = active.pop("page", None)
        timer.stop()
        if request_id is not None:
            emit(id=request_id, html=html, error=error)
        if fetch_page is not None:
            fetch_page.deleteLater()

    def timeout():
        fetch_page = active.get("page")
        finish(error="Dwemer browser request timed out")
        if fetch_page is not None:
            fetch_page.triggerAction(QWebEnginePage.WebAction.Stop)

    timer.timeout.connect(timeout)

    def loaded(fetch_page, ok):
        request_id = active.get("id")
        if request_id is None or active.get("page") is not fetch_page:
            return
        if not ok or not allowed_url(fetch_page.url().toString()):
            finish(error="Dwemer page could not be loaded; open Sign in to Dwemer to check your session")
            return
        def html_ready(html):
            if active.get("id") != request_id or active.get("page") is not fetch_page:
                return
            if len(html) > 5_000_000:
                finish(error="Dwemer page was too large")
            elif "just a moment" in fetch_page.title().casefold():
                finish(error="Dwemer requires browser verification; open Sign in to Dwemer")
            else:
                finish(html=html)
        fetch_page.toHtml(html_ready)

    class Commands(QObject):
        received = pyqtSignal(object)

        def handle(self, command):
            action = command.get("action")
            if action == "login":
                window.show()
                window.raise_()
                window.activateWindow()
                view.load(QUrl("https://dwemermods.com/login?lang=en"))
                emit(event="login_opened")
            elif action == "fetch":
                request_id, url = command.get("id"), command.get("url", "")
                if not allowed_url(url):
                    emit(id=request_id, html="", error="Not an allowed Dwemer mod URL")
                elif active:
                    emit(id=request_id, html="", error="Dwemer browser is already fetching a page")
                elif window.isVisible():
                    emit(id=request_id, html="", error="Close the Dwemer sign-in window before starting author searches")
                else:
                    fetch_page = SitePage(profile, app)
                    active["id"] = request_id
                    active["page"] = fetch_page
                    fetch_page.loadFinished.connect(lambda ok, target=fetch_page: loaded(target, ok))
                    fetch_page.renderProcessTerminated.connect(
                        lambda *unused, target=fetch_page: finish(error="Dwemer browser renderer stopped")
                        if active.get("page") is target else None)
                    timer.start(30000)
                    fetch_page.load(QUrl(url))
            elif action == "cancel" and active.get("id") == command.get("id"):
                fetch_page = active.get("page")
                active.clear()
                timer.stop()
                if fetch_page is not None:
                    fetch_page.triggerAction(QWebEnginePage.WebAction.Stop)
                    fetch_page.deleteLater()
            elif action == "shutdown":
                fetch_page = active.get("page")
                active.clear()
                if fetch_page is not None:
                    fetch_page.deleteLater()
                app.quit()

    commands = Commands()
    commands.received.connect(commands.handle)

    def read_commands():
        try:
            while True:
                line = sys.stdin.buffer.readline(16385)
                if not line:
                    commands.received.emit({"action": "shutdown"})
                    return
                if len(line) > 16384:
                    continue
                try:
                    command = json.loads(line)
                    if isinstance(command, dict):
                        commands.received.emit(command)
                        if command.get("action") == "shutdown":
                            return
                except (ValueError, UnicodeError):
                    continue
        except (OSError, RuntimeError):
            return

    threading.Thread(target=read_commands, daemon=True).start()
    emit(event="ready")
    app.exec()
    # Browser pages must be destroyed before their persistent profile.
    window.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    profile.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        emit(event="error", error="Could not start the Dwemer browser. Check Python 3.12 and matching PyQt6-WebEngine bindings.")
        raise SystemExit(1)
