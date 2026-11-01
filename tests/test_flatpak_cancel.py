"""Regression coverage for cancelling a running system-wide Flatpak command.

These tests run real child processes (small Python scripts), so signal handling is
exercised for real. They never touch the real flatpak binary.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core import flatpak_cancel as fc_module
from core.flatpak_cancel import CANCELLED_DETAIL, CancellableRunner, FlatpakCancelled
from core.flatpak_uninstall import FlatpakUninstallError

PY = sys.executable


def _script(code: str) -> list[str]:
    return [PY, "-c", code]


def _cancel_later(runner: CancellableRunner, delay: float) -> threading.Thread:
    def fire() -> None:
        time.sleep(delay)
        runner.cancel()

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    return thread


def test_normal_run_returns_output_and_return_code() -> None:
    runner = CancellableRunner()

    result = runner(_script("print('hello')"), stdin=subprocess.DEVNULL, timeout=20)

    assert result.returncode == 0
    assert result.stdout.decode().strip() == "hello"


def test_failure_without_cancel_is_returned_for_the_caller_to_read() -> None:
    runner = CancellableRunner()

    result = runner(_script("import sys; sys.stderr.write('denied'); sys.exit(3)"), timeout=20)

    assert result.returncode == 3
    assert b"denied" in result.stderr


def test_cancel_before_start_raises_and_never_starts_the_process(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    runner = CancellableRunner()
    runner.cancel()

    with pytest.raises(FlatpakCancelled):
        runner(
            _script(f"open({str(marker)!r}, 'w').close()"),
            timeout=20,
        )
    assert not marker.exists()


def test_cancel_stops_a_waiting_process_promptly() -> None:
    runner = CancellableRunner()
    _cancel_later(runner, 0.3)
    started = time.monotonic()

    with pytest.raises(FlatpakCancelled) as caught:
        runner(_script("import time; time.sleep(60)"), timeout=120)

    assert time.monotonic() - started < 10
    assert caught.value.detail == CANCELLED_DETAIL


def test_cancelled_error_is_a_user_presentable_flatpak_error() -> None:
    assert issubclass(FlatpakCancelled, FlatpakUninstallError)
    assert "may be partly applied" in CANCELLED_DETAIL
    assert "Refresh the list" in CANCELLED_DETAIL


def test_process_that_ignores_sigterm_is_killed_after_the_grace_period() -> None:
    runner = CancellableRunner(kill_after=0.5)
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    _cancel_later(runner, 1.0)
    started = time.monotonic()

    with pytest.raises(FlatpakCancelled):
        runner(_script(code), timeout=120)

    assert time.monotonic() - started < 15


def test_command_that_exits_cleanly_after_cancel_reports_its_real_success() -> None:
    runner = CancellableRunner()
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "time.sleep(60)\n"
    )
    _cancel_later(runner, 0.5)

    result = runner(_script(code), timeout=120)

    assert result.returncode == 0


def test_timeout_still_kills_the_process_and_raises_timeout() -> None:
    runner = CancellableRunner()

    with pytest.raises(subprocess.TimeoutExpired):
        runner(_script("import time; time.sleep(60)"), timeout=0.5)


def test_cancel_can_be_called_repeatedly_and_after_the_run() -> None:
    runner = CancellableRunner()
    runner(_script("pass"), timeout=20)

    runner.cancel()
    runner.cancel()

    assert runner.cancel_requested is True


def test_cancel_that_arrives_while_the_process_is_starting_still_stops_it(
    monkeypatch,
) -> None:
    runner = CancellableRunner()
    real_popen = fc_module.subprocess.Popen

    def popen_then_cancel(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        runner.cancel()  # the click lands after Popen returns, before it is recorded
        return process

    monkeypatch.setattr(fc_module.subprocess, "Popen", popen_then_cancel)
    started = time.monotonic()

    with pytest.raises(FlatpakCancelled):
        runner(_script("import time; time.sleep(60)"), timeout=120)

    assert time.monotonic() - started < 10
