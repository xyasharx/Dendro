# dendro/core/dnf5daemon_transport.py
"""Typed QtDBus transport, signal sink, and argument marshalling for DNF5."""

from __future__ import annotations

import atexit
import itertools
import logging
import os
import threading
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from PyQt6.QtCore import (
    QByteArray,
    QCoreApplication,
    QMetaType,
    QObject,
    QThread,
    pyqtSlot,
)
from PyQt6.QtDBus import (
    QDBusArgument,
    QDBusConnection,
    QDBusMessage,
    QDBusObjectPath,
    QDBusSignature,
    QDBusUnixFileDescriptor,
    QDBusVariant,
)

from core.dnf5daemon_types import (
    NO_CLIENT_SIDE_TIMEOUT_MS,
    SERVICE_NAME,
    Dnf5DaemonCallError,
    Dnf5DaemonUnavailable,
    Dnf5DaemonUnsupportedError,
    Dnf5DaemonValidationError,
)

LOGGER = logging.getLogger(__name__)
_MAX_UNWRAP_DEPTH = 24

_UNAVAILABLE_DBUS_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.Disconnected",
    }
)
_UNSUPPORTED_DBUS_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownObject",
    }
)


class DbusTransport(Protocol):
    """Small transport seam that enables deterministic client tests."""

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Sequence[str],
        arguments: Sequence[Any],
    ) -> tuple[Any, ...]:
        """Call a typed D-Bus method and return its decoded output arguments."""

    def subscribe(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        """Subscribe to one signal and return an opaque unsubscription token."""

    def unsubscribe(self, token: object) -> None:
        """Remove a subscription returned by :meth:`subscribe`."""


@dataclass(frozen=True, slots=True)
class _Subscription:
    path: str
    interface: str
    signal: str
    callback_id: int
    is_background: bool = False


class _QtDbusSignalSink(QObject):
    """Receives all subscribed D-Bus signals through Qt's generic message slot."""

    def __init__(
        self,
        lock: threading.RLock | None = None,
        *,
        target_thread: QThread | None = None,
    ) -> None:
        super().__init__()
        if target_thread is not None:
            if self.thread() is not target_thread:
                self.moveToThread(target_thread)
        else:
            app = QCoreApplication.instance()
            if app is not None:
                app_thread = app.thread()
                if self.thread() is not app_thread:
                    self.moveToThread(app_thread)
        self._lock = lock or threading.RLock()
        self.callbacks: dict[
            tuple[str, str, str], dict[int, Callable[[tuple[Any, ...]], None]]
        ] = {}

    @pyqtSlot(QDBusMessage)
    def receive(self, message: QDBusMessage) -> None:
        key = (message.path(), message.interface(), message.member())
        with self._lock:
            callbacks = tuple(self.callbacks.get(key, {}).values())
        if not callbacks:
            return
        try:
            arguments = tuple(
                _unwrap_dbus_value(value) for value in message.arguments()
            )
        except (TypeError, ValueError):
            LOGGER.exception(
                "DNF5 progress signal %s.%s could not be decoded.", key[1], key[2]
            )
            return
        for callback in callbacks:
            try:
                callback(arguments)
            except Exception:
                LOGGER.exception(
                    "DNF5 progress callback failed for %s.%s.", key[1], key[2]
                )


_CONNECTION_SEQ = itertools.count(1)
_LIVE_TRANSPORTS: weakref.WeakSet[QtDbusTransport] = weakref.WeakSet()
_LIVE_TRANSPORTS_LOCK = threading.Lock()


@dataclass
class _TransportResources:
    lock: threading.RLock
    connection: QDBusConnection
    connection_name: str | None
    owns_connection: bool
    sink: _QtDbusSignalSink
    subscriptions: dict[int, _Subscription]
    bg_sink: _QtDbusSignalSink | None = None
    bg_thread: QThread | None = None
    closed: bool = False


def _close_transport_resources(resources: _TransportResources) -> None:
    """Disconnect subscriptions, stop background event loops, and close private bus."""
    with resources.lock:
        if resources.closed:
            return
        resources.closed = True
        resources.subscriptions.clear()
        sink_keys = tuple(resources.sink.callbacks.keys())
        resources.sink.callbacks.clear()
        bg_sink = resources.bg_sink
        bg_thread = resources.bg_thread
        resources.bg_sink = None
        resources.bg_thread = None
        if bg_sink is not None:
            bg_keys = tuple(bg_sink.callbacks.keys())
            bg_sink.callbacks.clear()
        else:
            bg_keys = ()

    for path, interface, signal in sink_keys:
        try:
            resources.connection.disconnect(
                SERVICE_NAME, path, interface, signal, resources.sink.receive
            )
        except Exception:
            LOGGER.debug(
                "Could not disconnect D-Bus signal %s.%s on close.",
                interface,
                signal,
                exc_info=True,
            )

    if bg_sink is not None:
        for path, interface, signal in bg_keys:
            try:
                resources.connection.disconnect(
                    SERVICE_NAME, path, interface, signal, bg_sink.receive
                )
            except Exception:
                LOGGER.debug(
                    "Could not disconnect background D-Bus signal %s.%s on close.",
                    interface,
                    signal,
                    exc_info=True,
                )

    if bg_thread is not None:
        if bg_sink is not None and bg_thread.isRunning():
            bg_sink.deleteLater()
        bg_thread.quit()
        bg_thread.wait(2000)

    if resources.owns_connection and resources.connection_name:
        try:
            QDBusConnection.disconnectFromBus(resources.connection_name)
        except Exception:
            LOGGER.debug(
                "Could not disconnect private D-Bus connection %s.",
                resources.connection_name,
                exc_info=True,
            )


def _close_live_transports() -> None:
    with _LIVE_TRANSPORTS_LOCK:
        transports = list(_LIVE_TRANSPORTS)
    for transport in transports:
        try:
            transport.close()
        except Exception:
            LOGGER.debug("Error closing live QtDbusTransport at exit.", exc_info=True)


atexit.register(_close_live_transports)


class QtDbusTransport:
    """QtDBus implementation with explicit D-Bus signatures for every call."""

    def __init__(self, connection: QDBusConnection | None = None) -> None:
        if connection is not None:
            conn = connection
            connection_name: str | None = None
            owns_connection = False
        else:
            connection_name = f"dendro-dbus-{os.getpid()}-{next(_CONNECTION_SEQ)}"
            conn = QDBusConnection.connectToBus(
                QDBusConnection.BusType.SystemBus, connection_name
            )
            owns_connection = True

        if not conn.isConnected():
            error = conn.lastError()
            detail = (
                error.message()
                or "The system D-Bus is unavailable to this application."
            )
            error_name = error.name()
            if owns_connection and connection_name is not None:
                QDBusConnection.disconnectFromBus(connection_name)
            raise Dnf5DaemonUnavailable(
                "DNF5 daemon is unavailable", detail, dbus_name=error_name
            )

        self._connection = conn
        self._lock = threading.RLock()
        self._sink = _QtDbusSignalSink(self._lock)
        self._next_callback_id = 0
        self._subscriptions: dict[int, _Subscription] = {}
        self._resources = _TransportResources(
            lock=self._lock,
            connection=conn,
            connection_name=connection_name,
            owns_connection=owns_connection,
            sink=self._sink,
            subscriptions=self._subscriptions,
        )
        self._finalizer = weakref.finalize(
            self, _close_transport_resources, self._resources
        )
        with _LIVE_TRANSPORTS_LOCK:
            _LIVE_TRANSPORTS.add(self)

    def close(self) -> None:
        """Close owned bus connections and background signal threads cleanly."""
        self._finalizer()

    def _ensure_background_sink(self) -> _QtDbusSignalSink:
        if (
            self._resources.bg_sink is not None
            and self._resources.bg_thread is not None
            and self._resources.bg_thread.isRunning()
        ):
            return self._resources.bg_sink
        bg_thread = QThread()
        bg_thread.setObjectName(f"{self._resources.connection_name or 'dendro'}-sig")
        bg_sink = _QtDbusSignalSink(self._lock, target_thread=bg_thread)
        bg_thread.start()
        self._resources.bg_thread = bg_thread
        self._resources.bg_sink = bg_sink
        return bg_sink

    def call(
        self,
        path: str,
        interface: str,
        method: str,
        signatures: Sequence[str],
        arguments: Sequence[Any],
    ) -> tuple[Any, ...]:
        """Send one typed method call over the primary bus connection."""
        if self._resources.closed:
            raise Dnf5DaemonUnavailable(
                "DNF5 daemon is unavailable",
                "This D-Bus transport has already been closed.",
            )
        if len(signatures) != len(arguments):
            raise Dnf5DaemonValidationError(
                "Internal D-Bus contract error",
                f"{interface}.{method} was given {len(arguments)} argument(s), expected {len(signatures)}.",
            )

        message = QDBusMessage.createMethodCall(SERVICE_NAME, path, interface, method)
        try:
            message.setArguments(
                [
                    _marshal_argument(signature, argument)
                    for signature, argument in zip(signatures, arguments, strict=True)
                ]
            )
        except (TypeError, ValueError) as exc:
            raise Dnf5DaemonValidationError("Invalid D-Bus request", str(exc)) from exc

        reply = self._connection.call(message, timeout=NO_CLIENT_SIDE_TIMEOUT_MS)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            error_name = reply.errorName()
            detail = (
                reply.errorMessage() or f"{interface}.{method} did not return a result."
            )
            if error_name in _UNAVAILABLE_DBUS_ERRORS:
                raise Dnf5DaemonUnavailable(
                    "DNF5 daemon is unavailable", detail, dbus_name=error_name
                )
            if error_name in _UNSUPPORTED_DBUS_ERRORS:
                raise Dnf5DaemonUnsupportedError(
                    "DNF5 daemon interface unsupported", detail, dbus_name=error_name
                )
            raise Dnf5DaemonCallError(
                "DNF5 daemon request failed", detail, dbus_name=error_name
            )
        if reply.type() != QDBusMessage.MessageType.ReplyMessage:
            raise Dnf5DaemonCallError(
                "DNF5 daemon request failed",
                f"{interface}.{method} returned an unexpected D-Bus message type.",
            )
        try:
            return tuple(_unwrap_dbus_value(value) for value in reply.arguments())
        except (TypeError, ValueError) as exc:
            raise Dnf5DaemonCallError(
                "DNF5 daemon returned an unreadable reply",
                f"{interface}.{method} reply could not be decoded: {exc}",
            ) from exc

    def _subscribe_on_sink(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
        *,
        is_background: bool,
    ) -> object:
        key = (path, interface, signal)
        with self._lock:
            if self._resources.closed:
                raise Dnf5DaemonUnavailable(
                    "DNF5 daemon is unavailable",
                    "This D-Bus transport has already been closed.",
                )
            sink = self._ensure_background_sink() if is_background else self._sink
            callbacks = sink.callbacks.setdefault(key, {})
            if not callbacks:
                connected = self._connection.connect(
                    SERVICE_NAME, path, interface, signal, sink.receive
                )
                if not connected:
                    error = self._connection.lastError()
                    sink.callbacks.pop(key, None)
                    raise Dnf5DaemonCallError(
                        "Could not subscribe to DNF5 progress",
                        error.message()
                        or f"D-Bus refused subscription to {interface}.{signal}.",
                        dbus_name=error.name(),
                    )
            self._next_callback_id += 1
            callback_id = self._next_callback_id
            callbacks[callback_id] = callback
            token = _Subscription(
                path,
                interface,
                signal,
                callback_id,
                is_background=is_background,
            )
            self._subscriptions[callback_id] = token
            return token

    def subscribe(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        return self._subscribe_on_sink(
            path, interface, signal, callback, is_background=False
        )

    def subscribe_background(
        self,
        path: str,
        interface: str,
        signal: str,
        callback: Callable[[tuple[Any, ...]], None],
    ) -> object:
        """Subscribe on a dedicated background signal thread when owned by Qt."""
        if not self._resources.owns_connection:
            return self.subscribe(path, interface, signal, callback)
        return self._subscribe_on_sink(
            path, interface, signal, callback, is_background=True
        )

    def unsubscribe(self, token: object) -> None:
        if not isinstance(token, _Subscription):
            return
        with self._lock:
            subscription = self._subscriptions.pop(token.callback_id, None)
            if subscription is None:
                return
            sink = (
                self._resources.bg_sink if subscription.is_background else self._sink
            )
            if sink is None:
                return
            key = (subscription.path, subscription.interface, subscription.signal)
            callbacks = sink.callbacks.get(key)
            if callbacks is None:
                return
            callbacks.pop(subscription.callback_id, None)
            if callbacks:
                return
            sink.callbacks.pop(key, None)
            self._connection.disconnect(
                SERVICE_NAME,
                subscription.path,
                subscription.interface,
                subscription.signal,
                sink.receive,
            )


def _marshal_argument(signature: str, value: Any) -> Any:
    if signature == "s":
        if not isinstance(value, str):
            raise TypeError("D-Bus string argument must be text.")
        return value
    if signature == "b":
        if not isinstance(value, bool):
            raise TypeError("D-Bus boolean argument must be bool.")
        return value
    if signature == "o":
        if not isinstance(value, str) or not value.startswith("/"):
            raise TypeError(
                "D-Bus object-path argument must be an absolute object path."
            )
        return QDBusObjectPath(value)
    if signature == "h":
        if isinstance(value, QDBusUnixFileDescriptor):
            if not value.isValid():
                raise ValueError("D-Bus Unix file descriptor is invalid.")
            return value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            descriptor = QDBusUnixFileDescriptor(value)
            if not descriptor.isValid():
                raise ValueError(
                    f"Could not duplicate file descriptor {value} for D-Bus."
                )
            return descriptor
        raise TypeError(
            "D-Bus file-descriptor argument must be a non-negative fd or QDBusUnixFileDescriptor."
        )
    if signature == "as":
        return _marshal_string_array(value)
    if signature == "a{sv}":
        if not isinstance(value, Mapping):
            raise TypeError("D-Bus options argument must be a mapping.")
        return _marshal_variant_map(value)
    raise TypeError(f"Unsupported typed D-Bus signature: {signature}")


def _marshal_string_array(values: Any) -> QDBusArgument:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("D-Bus string array must be a sequence of text values.")
    argument = QDBusArgument()
    argument.beginArray(QMetaType.Type.QString.value)
    for value in values:
        if not isinstance(value, str):
            raise TypeError("D-Bus string arrays cannot contain non-text values.")
        argument.add(value)
    argument.endArray()
    return argument


def _marshal_string_map(values: Mapping[str, Any]) -> QDBusArgument:
    argument = QDBusArgument()
    argument.beginMap(QMetaType.Type.QString.value, QMetaType.Type.QString.value)
    for key in sorted(values):
        value = values[key]
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("D-Bus string maps require text keys and values.")
        argument.beginMapEntry()
        argument.add(key)
        argument.add(value)
        argument.endMapEntry()
    argument.endMap()
    return argument


def _marshal_variant_value(value: Any) -> Any:
    if isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, Mapping):
        return _marshal_string_map(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _marshal_string_array(value)
    raise TypeError(f"Unsupported D-Bus variant value type: {type(value).__name__}")


def _marshal_variant_map(values: Mapping[str, Any]) -> QDBusArgument:
    argument = QDBusArgument()
    variant_meta_type = QMetaType.fromName(b"QDBusVariant")
    if not variant_meta_type.isValid():
        raise TypeError("QtDBus did not register QDBusVariant support.")
    argument.beginMap(QMetaType.Type.QString.value, variant_meta_type.id())
    for key in sorted(values):
        if not isinstance(key, str):
            raise TypeError("D-Bus option keys must be text.")
        argument.beginMapEntry()
        argument.add(key)
        argument.add(QDBusVariant(_marshal_variant_value(values[key])))
        argument.endMapEntry()
    argument.endMap()
    return argument


def _unwrap_dbus_value(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_UNWRAP_DEPTH:
        raise ValueError("D-Bus reply exceeded maximum nesting depth.")
    if isinstance(value, QDBusVariant):
        return _unwrap_dbus_value(value.variant(), depth=depth + 1)
    if isinstance(value, QDBusObjectPath):
        return value.path()
    if isinstance(value, QDBusSignature):
        return value.signature()
    if isinstance(value, QByteArray):
        return bytes(value.data())
    if isinstance(value, QDBusUnixFileDescriptor):
        return value.fileDescriptor()
    if isinstance(value, QDBusArgument):
        # Riverbank's PyQt6 exposes QDBusArgument purely for marshalling;
        # standard containers arrive pre-converted from QDBusMessage.arguments().
        raise TypeError("Cannot demarshal an opaque PyQt6 QDBusArgument in Python.")
    if isinstance(value, Mapping):
        return {
            str(_unwrap_dbus_value(key, depth=depth + 1)): _unwrap_dbus_value(
                item, depth=depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_unwrap_dbus_value(item, depth=depth + 1) for item in value)
    if isinstance(value, list):
        return [_unwrap_dbus_value(item, depth=depth + 1) for item in value]
    return value
