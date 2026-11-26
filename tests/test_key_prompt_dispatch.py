"""The key prompt must be asked on the GUI thread, or the dialog cannot exist.

``repo_key_import_request`` arrives through Qt's generic D-Bus message slot, and
only a widget-thread caller may open a modal dialog.  This pins that contract at
the level the real client uses, not just where the callback happens to be called.
"""

from __future__ import annotations

import os
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot

from dendro.core import dnf5daemon


class _Recorder(QObject):
    """Stand-in for the signal that the daemon emits."""

    fired = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[threading.Thread] = []
        self.fired.connect(self._on_fired)

    def _on_fired(self, payload: object) -> None:
        self.threads.append(threading.current_thread())


def test_sink_delivers_to_the_thread_that_owns_it(qtbot) -> None:
    """A slot on a GUI-thread object runs on the GUI thread, not the emitter."""
    recorder = _Recorder()
    worker_threads: list[str] = []

    def emit_from_worker() -> None:
        worker_threads.append(threading.current_thread().name)
        recorder.fired.emit("payload")

    worker = threading.Thread(target=emit_from_worker, name="dbus-worker")
    worker.start()
    qtbot.waitUntil(lambda: len(recorder.threads) == 1, timeout=5000)
    worker.join(timeout=5)

    assert worker_threads == ["dbus-worker"]
    # Delivered on the main/Qt thread even though it was emitted from another.
    assert recorder.threads[0] is threading.main_thread()


def test_signal_sink_runs_presentation_callbacks_on_the_gui_thread(qtbot) -> None:
    from PyQt6.QtDBus import QDBusMessage

    sink = dnf5daemon._QtDbusSignalSink()
    seen: list[str] = []
    sink.callbacks[("/s/1", dnf5daemon.BASE_INTERFACE, "repo_key_import_request")] = {
        1: lambda arguments: seen.append(threading.current_thread().name)
    }

    message = QDBusMessage.createSignal(
        "/s/1", dnf5daemon.BASE_INTERFACE, "repo_key_import_request"
    )
    sink.receive(message)

    assert seen == [threading.main_thread().name]


def test_signal_sink_created_on_worker_thread_moves_to_application_thread(
    qtbot,
) -> None:
    """Workers that build a transport off-thread still get GUI-thread signal delivery."""
    from PyQt6.QtDBus import QDBusMessage

    class _QueuedSink(dnf5daemon._QtDbusSignalSink):
        @pyqtSlot(object)
        def deliver(self, msg: object) -> None:
            self.receive(msg)

    app = QCoreApplication.instance()
    assert app is not None
    holder: list[_QueuedSink] = []

    def build_sink() -> None:
        holder.append(_QueuedSink())

    worker = threading.Thread(target=build_sink, name="query-worker")
    worker.start()
    worker.join(timeout=5)
    assert len(holder) == 1
    sink = holder[0]
    assert sink.thread() == app.thread()

    seen: list[threading.Thread] = []
    sink.callbacks[("/s/1", dnf5daemon.BASE_INTERFACE, "repo_key_import_request")] = {
        1: lambda _arguments: seen.append(threading.current_thread())
    }
    message = QDBusMessage.createSignal(
        "/s/1", dnf5daemon.BASE_INTERFACE, "repo_key_import_request"
    )

    class _Bridge(QObject):
        delivered = pyqtSignal(object)

    bridge = _Bridge()
    bridge.delivered.connect(sink.deliver)

    def dispatch_from_dbus_thread() -> None:
        bridge.delivered.emit(message)

    dbus_thread = threading.Thread(target=dispatch_from_dbus_thread, name="dbus-thread")
    dbus_thread.start()
    dbus_thread.join(timeout=5)
    qtbot.waitUntil(lambda: len(seen) == 1, timeout=5000)

    assert seen == [threading.main_thread()]
