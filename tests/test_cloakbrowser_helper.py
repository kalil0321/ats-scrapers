"""Unit tests for the cloakbrowser helper used by Tesla / Meta."""

from __future__ import annotations

import logging

import pytest

from jobhive.scrapers import _cloakbrowser as cb


def test_evomi_proxy_returns_none_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PROXY", raising=False)
    assert cb.evomi_proxy_from_env() is None


def test_evomi_proxy_parses_4_colon_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXY", "http://core.evomi.com:1000:myuser:mypass")
    assert cb.evomi_proxy_from_env() == {
        "server": "http://core.evomi.com:1000",
        "username": "myuser",
        "password": "mypass",
    }


def test_evomi_proxy_accepts_no_scheme_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same env var without ``http://`` must still parse — the
    library tolerates both shapes for back-compat with hand-edited
    .env files."""
    monkeypatch.setenv("PROXY", "host.example.com:1234:user:pwd")
    assert cb.evomi_proxy_from_env() == {
        "server": "http://host.example.com:1234",
        "username": "user",
        "password": "pwd",
    }


def test_evomi_proxy_returns_none_for_malformed(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Wrong number of colons → warn + return None so callers can
    decide whether to no-op or fall back to direct."""
    monkeypatch.setenv("PROXY", "http://just:two:parts")
    with caplog.at_level(logging.WARNING):
        assert cb.evomi_proxy_from_env() is None
    assert any("doesn't match" in r.getMessage() for r in caplog.records)


def test_warn_disabled_emits_install_hint(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        cb.warn_disabled("TestATS")
    assert any(
        "TestATS" in r.getMessage() and "cloakbrowser" in r.getMessage()
        for r in caplog.records
    )


def test_is_enabled_returns_bool() -> None:
    """Sanity: ``is_enabled`` returns a real boolean (not raising)."""
    assert isinstance(cb.is_enabled(), bool)
