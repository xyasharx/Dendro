"""The appearance rules, tested without a window or a display server.

Three independent inputs change the theme -- Qt's ``colorSchemeChanged``, the
FreeDesktop portal's ``SettingChanged``, and the Preferences dialog -- and they
can arrive in the same event-loop turn.  The coalescing that reconciles them is
the part worth pinning, and it is now a plain dataclass rather than four fields
scattered through the window.
"""

from __future__ import annotations

import pytest

from dendro.ui.theme_coordinator import (
    RESOLVED_THEME_KEYS,
    THEME_CHOICES,
    ThemeState,
    resolve_theme_choice,
)


class _Settings:
    """In-memory QSettings stand-in that records writes."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    def value(self, key: str, default: object = None, *, type: type | None = None):
        value = self.values.get(key, default)
        return type(value) if type is not None and value is not None else value

    def setValue(self, key: str, value: object) -> None:
        self.values[key] = value


class TestStoredPreference:
    @pytest.mark.parametrize("choice", sorted(THEME_CHOICES))
    def test_a_supported_preference_is_kept(self, choice: str) -> None:
        settings = _Settings({"theme": choice})
        assert resolve_theme_choice(settings.value("theme"), settings) == choice
        assert settings.values["theme"] == choice

    @pytest.mark.parametrize("stored", ["", "Dark", "midnight", "auto ", None, 7])
    def test_an_unsupported_preference_is_repaired(self, stored: object) -> None:
        """A hand-edited or future settings value must not leave the window unstyled."""

        settings = _Settings({"theme": stored})
        assert resolve_theme_choice(settings.value("theme"), settings) == "auto"
        # And the repair is persisted, so the bad value is not re-read forever.
        assert settings.values["theme"] == "auto"


class TestReentrancyGuard:
    def test_the_guard_admits_one_apply_at_a_time(self) -> None:
        """Applying a theme emits signals that can request another apply."""

        state = ThemeState()
        assert state.begin_apply() is True
        assert state.begin_apply() is False
        state.end_apply()
        assert state.begin_apply() is True

    def test_the_guard_is_released_after_a_failure(self) -> None:
        """A raising apply must not wedge the window into an unthemed state."""

        state = ThemeState()
        state.begin_apply()
        try:
            raise RuntimeError("stylesheet failed")
        except RuntimeError:
            state.end_apply()
        assert state.applying is False


class TestCoalescing:
    def test_nothing_is_queued_by_default(self) -> None:
        assert ThemeState().take_queued() == (False, None)

    def test_the_last_key_wins(self) -> None:
        """The portal value is authoritative and arrives after Qt's own."""

        state = ThemeState()
        state.queue("dark")
        state.queue("light")
        assert state.take_queued() == (True, "light")

    @pytest.mark.parametrize("key", [None, "dark", "light"])
    def test_a_queued_refresh_is_consumed_exactly_once(self, key: str | None) -> None:
        state = ThemeState()
        state.queue(key)
        assert state.take_queued() == (True, key)
        assert state.take_queued() == (False, None)

    def test_consuming_clears_the_key(self) -> None:
        """A stale key applied on the next event would undo the newest one."""

        state = ThemeState()
        state.queue("dark")
        state.take_queued()
        assert state.pending_key is None
        assert state.queued is False

    def test_requeueing_after_a_consume_starts_a_new_refresh(self) -> None:
        state = ThemeState()
        state.queue("dark")
        state.take_queued()
        state.queue("light")
        assert state.take_queued() == (True, "light")

    def test_a_keyless_event_does_not_clear_a_pending_key(self) -> None:
        """Qt's signal carries no key; it must not erase the portal's value."""

        state = ThemeState()
        state.queue("dark")
        state.queue(None)
        assert state.take_queued() == (True, "dark")


def test_the_accepted_choices_are_the_ones_the_preferences_offer() -> None:
    """A mismatch would offer a preference the coordinator silently refuses."""

    from dendro.ui.settings_dialog import THEME_DISPLAY_OPTIONS

    offered = {key for key, _label in THEME_DISPLAY_OPTIONS}
    assert offered == THEME_CHOICES, offered ^ THEME_CHOICES


def test_high_contrast_resolved_keys_are_accepted_by_theme_state() -> None:
    """High-contrast resolved keys pass through ThemeState coalescing intact."""
    assert {"dark", "light", "dark_hc", "light_hc"} <= RESOLVED_THEME_KEYS

    state = ThemeState(choice="dark")
    state.queue("dark_hc")
    assert state.take_queued() == (True, "dark_hc")

    state.queue("light_hc")
    state.queue(None)
    assert state.take_queued() == (True, "light_hc")
