"""Unit tests for card_parser.py — all external I/O is mocked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import theoracle.card_parser as card_parser
from theoracle.card_parser import (
    CardData,
    _correct_collector_number,
    _extract_set_and_number,
    _fuzzy_match_set,
    _load_valid_set_codes,
    _parse_card_json,
    fetch_card,
    parse_card_identifier,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

SAMPLE_CARD_JSON = {
    "name": "Island",
    "mana_cost": "",
    "type_line": "Basic Land — Island",
    "oracle_text": "({T}: Add {U}.)",
    "image_uris": {"normal": "https://example.com/island.jpg"},
}


def _make_response(status: int, json_data=None, headers=None) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data or {}
    m.headers = headers or {}
    return m


@pytest.fixture(autouse=True)
def no_sleep():
    """Suppress all time.sleep calls in card_parser to keep the suite fast."""
    with patch("theoracle.card_parser.time.sleep"):
        yield


# ---------------------------------------------------------------------------
# _extract_set_and_number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("M21/123",   ("M21", "123")),    # slash separator
        ("NEO-45a",   ("NEO", "45a")),    # hyphen separator
        ("ZNR 200",   ("ZNR", "200")),    # space separator
        ("M2l 12O",   ("M2L", "12O")),    # OCR chars pass through (correction is later)
        ("NEO/O45a",  ("NEO", "O45a")),   # digit-like prefix on collector number
        ("PLIST/100", ("PLIST", "100")),  # 5-char set code
        ("2XM-250",   ("2XM", "250")),    # numeric-leading set code
    ],
)
def test_extract_set_and_number_valid(raw, expected):
    assert _extract_set_and_number(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "not a card",
        "hello world",
        "AB/123",     # set code too short (< 3 chars)
    ],
)
def test_extract_set_and_number_no_match(raw):
    assert _extract_set_and_number(raw) is None


# ---------------------------------------------------------------------------
# _correct_collector_number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12O",  "120"),   # O → 0
        ("l45",  "145"),   # l → 1
        ("I45",  "145"),   # I → 1
        ("Z1",   "21"),    # Z → 2
        ("z1",   "21"),    # z → 2
        ("1S",   "15"),    # uppercase S → 5
        ("B4",   "84"),    # B → 8
        ("l45a", "145a"),  # suffix 'a' preserved; preceding digits corrected
        ("O45a", "045a"),  # OCR prefix corrected; suffix preserved
        ("45s",  "45s"),   # 's' is a valid Scryfall showcase suffix — untouched
        ("",     ""),      # empty string edge case
    ],
)
def test_correct_collector_number(raw, expected):
    assert _correct_collector_number(raw) == expected


# ---------------------------------------------------------------------------
# _fuzzy_match_set
# ---------------------------------------------------------------------------

_KNOWN_CODES = ["M21", "NEO", "ZNR", "PLIST"]


def test_fuzzy_match_exact(monkeypatch):
    monkeypatch.setattr(card_parser, "_load_valid_set_codes", lambda: _KNOWN_CODES)
    assert _fuzzy_match_set("NEO") == "NEO"


def test_fuzzy_match_ocr_error(monkeypatch):
    # uppercase I instead of 1 should still resolve to "M21"
    monkeypatch.setattr(card_parser, "_load_valid_set_codes", lambda: _KNOWN_CODES)
    assert _fuzzy_match_set("M2I") == "M21"


def test_fuzzy_match_no_close_match_returns_original(monkeypatch):
    monkeypatch.setattr(card_parser, "_load_valid_set_codes", lambda: _KNOWN_CODES)
    assert _fuzzy_match_set("XXXBAD") == "XXXBAD"


def test_fuzzy_match_input_lowercased_normalises(monkeypatch):
    monkeypatch.setattr(card_parser, "_load_valid_set_codes", lambda: _KNOWN_CODES)
    assert _fuzzy_match_set("neo") == "NEO"


def test_fuzzy_match_empty_codes_returns_uppercased_original(monkeypatch):
    monkeypatch.setattr(card_parser, "_load_valid_set_codes", lambda: [])
    assert _fuzzy_match_set("m21") == "M21"


# ---------------------------------------------------------------------------
# _parse_card_json
# ---------------------------------------------------------------------------


def test_parse_normal_card():
    card = _parse_card_json(SAMPLE_CARD_JSON)
    assert card.name == "Island"
    assert card.mana_cost == ""
    assert card.type_line == "Basic Land — Island"
    assert card.oracle_text == "({T}: Add {U}.)"
    assert card.image_url == "https://example.com/island.jpg"


def test_parse_double_faced_card():
    dfc_json = {
        "name": "Delver of Secrets // Insectile Aberration",
        "mana_cost": None,
        "type_line": "Creature — Human Wizard // Creature — Human Insect",
        "oracle_text": None,
        "card_faces": [
            {
                "mana_cost": "{U}",
                "oracle_text": "At the beginning of your upkeep, look at the top card of your library.",
                "image_uris": {"normal": "https://example.com/delver.jpg"},
            },
            {
                "mana_cost": "",
                "oracle_text": "Flying",
                "image_uris": {"normal": "https://example.com/aberration.jpg"},
            },
        ],
    }
    card = _parse_card_json(dfc_json)
    assert card.name == "Delver of Secrets // Insectile Aberration"
    assert card.mana_cost == "{U}"
    assert "upkeep" in card.oracle_text
    assert card.image_url == "https://example.com/delver.jpg"


def test_parse_missing_fields_returns_empty_strings():
    card = _parse_card_json({})
    assert card.name == ""
    assert card.mana_cost == ""
    assert card.type_line == ""
    assert card.oracle_text == ""
    assert card.image_url == ""


# ---------------------------------------------------------------------------
# fetch_card
# ---------------------------------------------------------------------------


def test_fetch_card_primary_hit():
    with patch("theoracle.card_parser.requests.get") as mock_get:
        mock_get.return_value = _make_response(200, SAMPLE_CARD_JSON)
        card = fetch_card("ZNR", "200")
    assert card is not None
    assert card.name == "Island"
    assert mock_get.call_count == 1  # no fallback needed


def test_fetch_card_fallback_on_404():
    with patch("theoracle.card_parser.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(404, {}),
            _make_response(200, {"data": [SAMPLE_CARD_JSON]}),
        ]
        card = fetch_card("ZNR", "999")
    assert card is not None
    assert card.name == "Island"
    assert mock_get.call_count == 2


def test_fetch_card_both_miss_returns_none():
    with patch("theoracle.card_parser.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(404, {}),
            _make_response(200, {"data": []}),
        ]
        card = fetch_card("XXXBAD", "99")
    assert card is None


def test_fetch_card_404_then_non200_returns_none():
    with patch("theoracle.card_parser.requests.get") as mock_get:
        mock_get.side_effect = [
            _make_response(404, {}),
            _make_response(404, {}),
        ]
        card = fetch_card("ZNR", "999")
    assert card is None


def test_fetch_card_unexpected_primary_status_skips_fallback():
    # A 422 on the primary should not trigger the search fallback.
    with patch("theoracle.card_parser.requests.get") as mock_get:
        mock_get.return_value = _make_response(422, {})
        card = fetch_card("ZNR", "200")
    assert card is None
    assert mock_get.call_count == 1


def test_fetch_card_rate_limit_raises():
    m = _make_response(429, {}, headers={"Retry-After": "3"})
    with patch("theoracle.card_parser.requests.get", return_value=m):
        with pytest.raises(RuntimeError, match="rate-limited"):
            fetch_card("ZNR", "200")


def test_fetch_card_server_error_raises():
    with patch("theoracle.card_parser.requests.get", return_value=_make_response(500, {})):
        with pytest.raises(RuntimeError, match="server error"):
            fetch_card("ZNR", "200")


def test_fetch_card_network_error_raises():
    import requests as _req

    with patch("theoracle.card_parser.requests.get", side_effect=_req.RequestException("timeout")):
        with pytest.raises(RuntimeError, match="Network error"):
            fetch_card("ZNR", "200")


# ---------------------------------------------------------------------------
# _load_valid_set_codes
# ---------------------------------------------------------------------------


def _write_cache(path: Path, codes: list[str], age_days: int = 0) -> None:
    cached_at = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)).isoformat()
    path.write_text(json.dumps({"cached_at": cached_at, "codes": codes}), encoding="utf-8")


def test_load_sets_fresh_cache_no_network(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, ["NEO", "M21"], age_days=1)
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get") as mock_get:
        codes = _load_valid_set_codes()

    assert codes == ["NEO", "M21"]
    mock_get.assert_not_called()


def test_load_sets_stale_cache_refetches_and_rewrites(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, ["OLD"], age_days=31)
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get",
               return_value=_make_response(200, {"data": [{"code": "neo"}, {"code": "m21"}]})):
        codes = _load_valid_set_codes()

    assert codes == ["NEO", "M21"]
    saved = json.loads(cache_file.read_text())
    assert saved["codes"] == ["NEO", "M21"]


def test_load_sets_no_cache_fetches_and_writes(tmp_path, monkeypatch):
    cache_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get",
               return_value=_make_response(200, {"data": [{"code": "znr"}]})):
        codes = _load_valid_set_codes()

    assert codes == ["ZNR"]
    assert cache_file.exists()


def test_load_sets_network_fail_falls_back_to_stale(tmp_path, monkeypatch):
    import requests as _req

    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, ["STALE"], age_days=60)
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get", side_effect=_req.RequestException("offline")):
        codes = _load_valid_set_codes()

    assert codes == ["STALE"]


def test_load_sets_network_fail_no_cache_returns_empty(tmp_path, monkeypatch):
    import requests as _req

    cache_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get", side_effect=_req.RequestException("offline")):
        codes = _load_valid_set_codes()

    assert codes == []


def test_load_sets_malformed_cache_refetches(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("not valid json {{", encoding="utf-8")
    monkeypatch.setattr(card_parser, "_SETS_CACHE_PATH", cache_file)

    with patch("theoracle.card_parser.requests.get",
               return_value=_make_response(200, {"data": [{"code": "neo"}]})):
        codes = _load_valid_set_codes()

    assert codes == ["NEO"]


# ---------------------------------------------------------------------------
# parse_card_identifier  (full pipeline)
# ---------------------------------------------------------------------------


def test_parse_card_identifier_full_pipeline(monkeypatch):
    expected_card = CardData(
        name="Lightning Bolt",
        mana_cost="{R}",
        type_line="Instant",
        oracle_text="Lightning Bolt deals 3 damage to any target.",
        image_url="https://example.com/bolt.jpg",
    )
    monkeypatch.setattr(card_parser, "_fuzzy_match_set", lambda s: "M21")
    monkeypatch.setattr(card_parser, "fetch_card", lambda s, n: expected_card)

    card = parse_card_identifier("M21/123")
    assert card is not None
    assert card.name == "Lightning Bolt"


def test_parse_card_identifier_no_regex_match():
    assert parse_card_identifier("not a card identifier") is None


def test_parse_card_identifier_fetch_returns_none(monkeypatch):
    monkeypatch.setattr(card_parser, "_fuzzy_match_set", lambda s: "M21")
    monkeypatch.setattr(card_parser, "fetch_card", lambda s, n: None)
    assert parse_card_identifier("M21/123") is None


def test_parse_card_identifier_ocr_correction_applied(monkeypatch):
    # "M2l 12O" → set "M2L"→"M21", num "12O"→"120"
    received: list = []

    def capture_fetch(set_code, collector_number):
        received.append((set_code, collector_number))
        return None

    monkeypatch.setattr(card_parser, "_fuzzy_match_set", lambda s: "M21")
    monkeypatch.setattr(card_parser, "fetch_card", capture_fetch)

    parse_card_identifier("M2l 12O")
    assert received == [("M21", "120")]
