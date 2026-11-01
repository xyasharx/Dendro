# dendro/tests/test_flatpak_progress.py
"""Tests for Flatpak progress parsing and the streaming runner.

The sample lines come from a real ``flatpak --user update -y`` run (Flatpak 1.16.6)
with stdout piped and stdin closed, as Dendro runs it.
"""

from __future__ import annotations

import sys
import textwrap
import threading

import pytest

from core.flatpak_cancel import CancellableRunner, FlatpakCancelled
from core.flatpak_progress import (
    STAGE_COMPLETE,
    STAGE_LOOKING,
    STAGE_UPDATING,
    STAGE_WAITING,
    FlatpakProgress,
    ProgressTracker,
)

REAL_UPDATE_OUTPUT = [
    "Looking for updates…",
    " 1.\t   \torg.test.App\tstable\tu\tlocaltest\t< 40.0\xa0MB",
    "Updating…",
    "Updating…                        0%  0 bytes/s",
    "Updating… ▌                      3%",
    "Updating… ███████████████▍      77%  6.3\xa0MB/s",
    "Updating… ███████████████▍      77%  5.5\xa0MB/s  00:02",
    "Updating… ████████████████████ 100%  5.7\xa0MB/s  00:00",
    "Updates complete.",
]

# Nothing is printed for the planned steps under --noninteractive.
NONINTERACTIVE_OUTPUT = ["Updating app/org.test.App/x86_64/stable"]


def _feed_all(lines: list[str]) -> list:
    tracker = ProgressTracker()
    return [snap for line in lines if (snap := tracker.feed(line)) is not None]


def test_real_update_reports_steps_and_percent_in_order():
    snapshots = _feed_all(REAL_UPDATE_OUTPUT)
    assert snapshots[0].stage == STAGE_LOOKING
    assert snapshots[0].message == "Checking for updates…"

    listed = [s for s in snapshots if s.steps]
    assert listed[-1].steps == ("org.test.App stable",)

    percents = [
        s.percent
        for s in snapshots
        if s.stage == STAGE_UPDATING and s.percent is not None
    ]
    assert percents == [0, 3, 77, 77, 100]


def test_real_update_speed_and_eta_are_read_when_printed():
    snapshots = _feed_all(REAL_UPDATE_OUTPUT)
    fast = next(s for s in snapshots if s.speed == "6.3 MB/s")
    assert fast.percent == 77
    with_eta = [s for s in snapshots if s.eta]
    assert with_eta[0].eta == "00:02"


def test_zero_percent_line_reports_bytes_per_second():
    snapshots = _feed_all(["Updating…                        0%  0 bytes/s"])
    assert snapshots[0].speed == "0 bytes/s"


def test_real_update_completes_with_full_overall_percent():
    snapshots = _feed_all(REAL_UPDATE_OUTPUT)
    last = snapshots[-1]
    assert last.stage == STAGE_COMPLETE
    assert last.finished_ok
    assert last.overall_percent == 100
    assert last.message == "Update finished."


def test_single_step_overall_percent_matches_flatpak_percent():
    snapshots = _feed_all(REAL_UPDATE_OUTPUT)
    seventy_seven = next(s for s in snapshots if s.percent == 77)
    assert seventy_seven.overall_percent == 77
    assert seventy_seven.message == "Updating org.test.App (step 1 of 1)"


def test_overall_percent_advances_across_two_steps():
    lines = [
        "Looking for updates…",
        " 1.\t   \torg.test.App\tstable\tu\tlocaltest\t< 10.0\xa0MB",
        " 2.\t   \torg.test.Runtime\t24.08\tu\tlocaltest\t< 90.0\xa0MB",
        "Updating…",
        "Updating… ████████████████████ 100%",
        "Updating… ████████ 40%",
    ]
    snapshots = _feed_all(lines)
    second = snapshots[-1]
    assert second.current == 2
    assert second.completed_steps == 1
    # Half of the work is done after one of two steps is done and the other is 40% complete.
    assert second.overall_percent == 70
    assert second.message == "Updating org.test.Runtime (step 2 of 2)"


def test_overall_percent_never_reports_100_before_completion():
    lines = [
        "Looking for updates…",
        " 1.\t   \torg.test.App\tstable\tu\tlocaltest\t< 10.0\xa0MB",
        "Updating…",
        "Updating… ████████████████████ 100%",
    ]
    snapshots = _feed_all(lines)
    assert snapshots[-1].overall_percent == 99


def test_noninteractive_output_has_no_percent_or_steps():
    snapshots = _feed_all(NONINTERACTIVE_OUTPUT)
    assert snapshots[-1].stage == STAGE_UPDATING
    assert snapshots[-1].percent is None
    assert snapshots[-1].overall_percent is None
    assert snapshots[-1].message == "Updating org.test.App"


def test_nothing_to_do_is_a_finished_run_without_steps():
    snapshots = _feed_all(["Looking for updates…", "", "Nothing to do."])
    last = snapshots[-1]
    assert last.stage == STAGE_COMPLETE
    assert last.finished_ok
    assert last.steps == ()


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "flatpak:ERROR:../app/flatpak-main.c:865: assertion failed",
        "Error: Not enough disk space to complete this operation",
        "Some unrelated text 12%",
    ],
)
def test_unrecognised_lines_are_ignored(line):
    tracker = ProgressTracker()
    assert tracker.feed(line) is None


def test_unrecognised_lines_do_not_change_the_last_state():
    tracker = ProgressTracker()
    tracker.feed("Updating… ████████ 40%")
    assert tracker.feed("flatpak:ERROR: something") is None


def test_first_line_gives_looking_stage_and_default_is_waiting():
    tracker = ProgressTracker()
    assert tracker.feed("Looking for updates…").stage == STAGE_LOOKING
    assert FlatpakProgress().stage == STAGE_WAITING


def test_percent_is_capped_at_100():
    snapshots = _feed_all(["Updating… 250%"])
    assert snapshots[-1].percent == 100


# --- Streaming runner --------------------------------------------------------


def _python(script: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(script)]


def test_runner_delivers_lines_while_the_process_is_still_running():
    finished_before_second_line: list[bool] = []
    result_holder: dict = {}

    def on_line(text: str) -> None:
        if text == "first":
            # The command is still running when its first line arrives.
            finished_before_second_line.append("result" in result_holder)

    runner = CancellableRunner(on_line=on_line)
    script = "import time\nprint('first', flush=True)\ntime.sleep(0.5)\nprint('second')"
    result_holder["result"] = runner(_python(script))
    assert result_holder["result"].returncode == 0
    assert finished_before_second_line == [False]


def test_runner_keeps_full_stdout_and_stderr_and_returncode():
    runner = CancellableRunner()
    result = runner(
        _python(
            "import sys\nprint('out line')\nprint('err line', file=sys.stderr)\nsys.exit(3)"
        )
    )
    assert result.returncode == 3
    assert result.stdout == b"out line\n"
    assert result.stderr == b"err line\n"


def test_runner_splits_carriage_return_progress_updates():
    seen: list[str] = []
    runner = CancellableRunner(on_line=seen.append)
    runner(
        _python(
            "import sys\nsys.stdout.write('Updating… 10%\\rUpdating… 20%\\n')\nsys.stdout.flush()"
        )
    )
    assert seen == ["Updating… 10%", "Updating… 20%"]


def test_runner_delivers_a_final_line_without_newline():
    seen: list[str] = []
    runner = CancellableRunner(on_line=seen.append)
    runner(_python("import sys\nsys.stdout.write('Updates complete.')"))
    assert seen == ["Updates complete."]


def test_runner_callback_error_does_not_break_the_command():
    def broken(_text: str) -> None:
        raise RuntimeError("display failed")

    runner = CancellableRunner(on_line=broken)
    result = runner(_python("print('still runs')"))
    assert result.returncode == 0
    assert result.stdout == b"still runs\n"


def test_runner_still_reports_cancel_after_streaming_starts():
    started = threading.Event()

    def on_line(_text: str) -> None:
        started.set()

    runner = CancellableRunner(kill_after=0.5, on_line=on_line)
    script = "import time\nprint('started', flush=True)\ntime.sleep(30)"

    def cancel_when_started() -> None:
        started.wait(timeout=10)
        runner.cancel()

    threading.Thread(target=cancel_when_started, daemon=True).start()
    with pytest.raises(FlatpakCancelled):
        runner(_python(script))


# --- Status text, step list, and the line handler ----------------------------


def test_status_text_shows_step_percent_speed_and_time_left():
    snapshots = _feed_all(REAL_UPDATE_OUTPUT)
    fast = next(s for s in snapshots if s.speed == "6.3 MB/s" and s.percent == 77)
    assert fast.status_text == "Updating org.test.App (step 1 of 1) · 77% · 6.3 MB/s"
    timed = next(s for s in snapshots if s.eta == "00:02")
    assert timed.status_text.endswith("5.5 MB/s · 00:02 left")


def test_status_text_for_finished_run_has_no_percent():
    last = _feed_all(REAL_UPDATE_OUTPUT)[-1]
    assert last.status_text == "Update finished."


def test_steps_text_marks_done_current_and_pending_steps():
    lines = [
        "Looking for updates…",
        " 1.\t   \torg.test.App\tstable\tu\tlocaltest\t< 10.0\xa0MB",
        " 2.\t   \torg.test.Runtime\t24.08\tu\tlocaltest\t< 90.0\xa0MB",
        "Updating…",
        "Updating… ████████████████████ 100%",
        "Updating… ████████ 40%",
    ]
    second = _feed_all(lines)[-1]
    assert second.steps_text == "✓ org.test.App stable\n▶ org.test.Runtime 24.08"


def test_steps_text_marks_every_step_done_when_complete():
    last = _feed_all(REAL_UPDATE_OUTPUT)[-1]
    assert last.steps_text == "✓ org.test.App stable"


def test_line_handler_reports_only_recognised_lines_in_order():
    from core.flatpak_progress import progress_line_handler

    received: list[str] = []
    handle = progress_line_handler(lambda snap: received.append(snap.stage))
    for line in REAL_UPDATE_OUTPUT + ["flatpak:ERROR: noise"]:
        handle(line)
    assert received[0] == STAGE_LOOKING
    assert received[-1] == STAGE_COMPLETE
    assert len(received) == len(REAL_UPDATE_OUTPUT)


def test_runner_line_handler_can_be_set_before_the_run():
    seen: list[str] = []
    runner = CancellableRunner()
    runner.set_on_line(seen.append)
    runner(_python("print('hello')"))
    assert seen == ["hello"]


def test_streamed_run_reports_the_real_update_sequence_in_order():
    from core.flatpak_progress import progress_line_handler

    received: list = []
    runner = CancellableRunner()
    runner.set_on_line(progress_line_handler(received.append))
    lines = [
        line.replace("\\", "\\\\").replace("'", "\\'") for line in REAL_UPDATE_OUTPUT
    ]
    script = (
        "import sys, time\nfor line in "
        + repr(lines)
        + ":\n    print(line, flush=True)\n    time.sleep(0.02)\n"
    )
    result = runner(_python(script))

    assert result.returncode == 0
    assert received[-1].stage == STAGE_COMPLETE
    assert [s.percent for s in received if s.percent is not None][-1] == 100
    assert received[0].stage == STAGE_LOOKING
