"""
card_parser.py — MTG Draft Helper: OCR identifier normalization and Scryfall lookup.

Pipeline:
    raw OCR string
        -> _extract_set_and_number  (regex, permissive)
        -> _correct_collector_number (OCR digit substitution map)
        -> _fuzzy_match_set          (difflib against Scryfall /sets cache)
        -> fetch_card                (Scryfall primary + fallback)
        -> CardData
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from pathlib import Path

import requests
from requests import Response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRYFALL_BASE = "https://api.scryfall.com"
_RATE_LIMIT_DELAY = 0.1  # seconds between requests (Scryfall's requested minimum)

_SETS_CACHE_PATH = Path(__file__).parent / ".scryfall_sets_cache.json"
_CACHE_MAX_AGE = timedelta(days=30)

# Permissive extraction regex — allows OCR-mangled alphanumeric chars in both groups.
# Set code:       3–5 alphanumeric chars (e.g. NEO, M21, PLIST, 2XM)
# Separator:      one of  /  -  or any whitespace
# Collector num:  must begin with a character that looks like a digit (0-9 or any
#                 OCR digit-lookalike: O, o, l, I, Z, z, S, B), followed by 0-3
#                 more alphanumeric chars and an optional single trailing letter suffix.
#                 Requiring a digit-like first character prevents common English words
#                 (e.g. "not a") from being mistaken for card identifiers.
_IDENTIFIER_RE = re.compile(
    r"\b([A-Za-z0-9]{3,5})"
    r"\s*[-/\s]\s*"
    r"([0-9OolIZzSB][A-Za-z0-9]{0,3}[a-zA-Z]?)\b",
    re.IGNORECASE,
)

# OCR characters that are visually mistaken for digits.
# Lowercase 's' is intentionally omitted: Scryfall uses 's'-suffixed collector
# numbers for showcase variants and the translate call leaves unmapped chars intact.
_OCR_DIGIT_MAP = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "l": "1",
        "I": "1",
        "Z": "2",
        "z": "2",
        "S": "5",  # uppercase only; lowercase 's' is a genuine card suffix
        "B": "8",
    }
)

# Letters that are unambiguously genuine Scryfall card variant suffixes,
# i.e. NOT present in the OCR digit substitution set.
_KNOWN_SUFFIXES: frozenset[str] = frozenset("abcdefghijkmnpqrtuvwxy")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CardData:
    """Normalised card information returned by fetch_card."""

    name: str
    mana_cost: str
    type_line: str
    oracle_text: str
    image_url: str  # image_uris['normal'] from Scryfall JSON


# ---------------------------------------------------------------------------
# Step 1 — Regex extraction
# ---------------------------------------------------------------------------


def _extract_set_and_number(raw: str) -> tuple[str, str] | None:
    """
    Extract (set_code, collector_number) from a raw OCR string.

    Returns None if no identifier pattern is found.

    Examples:
        "M21/123"  -> ("M21", "123")
        "NEO-45a"  -> ("NEO", "45a")
        "M2l 12O"  -> ("M2L", "12O")   (OCR correction applied later)
    """
    m = _IDENTIFIER_RE.search(raw)
    if not m:
        return None
    return m.group(1).upper(), m.group(2)


# ---------------------------------------------------------------------------
# Step 2 — OCR collector number correction
# ---------------------------------------------------------------------------


def _correct_collector_number(raw: str) -> str:
    """
    Substitute OCR digit-lookalike characters in a collector number string.

    If the final character is an unambiguous Scryfall variant suffix letter
    (not itself an OCR digit substitute), it is preserved as-is and correction
    is applied only to the preceding digit portion.

    Examples:
        "12O"  -> "120"
        "l45a" -> "145a"
        "O45a" -> "045a"
        "45s"  -> "45s"   ('s' not in _KNOWN_SUFFIXES but also not in digit map)
    """
    if not raw:
        return raw

    last = raw[-1]
    if last.isalpha() and last.lower() in _KNOWN_SUFFIXES:
        return raw[:-1].translate(_OCR_DIGIT_MAP) + last
    return raw.translate(_OCR_DIGIT_MAP)


# ---------------------------------------------------------------------------
# Step 3 — Fuzzy set code matching (file-cached /sets list)
# ---------------------------------------------------------------------------


def _load_valid_set_codes() -> list[str]:
    """
    Return a list of uppercase valid MTG set codes.

    Cache strategy (file at .scryfall_sets_cache.json):
      - Fresh (< 30 days old): read from file, zero network calls.
      - Stale or missing: fetch GET /sets, rewrite cache file, return codes.
      - Network failure on stale cache: fall back to stale data rather than [].
    """
    if _SETS_CACHE_PATH.exists():
        try:
            payload = json.loads(_SETS_CACHE_PATH.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(payload["cached_at"])
            if datetime.now(timezone.utc).replace(tzinfo=None) - cached_at < _CACHE_MAX_AGE:
                return payload["codes"]
        except (KeyError, ValueError, OSError):
            pass  # malformed or unreadable cache — fall through to re-fetch

    try:
        time.sleep(_RATE_LIMIT_DELAY)
        resp = requests.get(f"{_SCRYFALL_BASE}/sets", timeout=10)
        resp.raise_for_status()
        codes = [s["code"].upper() for s in resp.json().get("data", [])]
        _SETS_CACHE_PATH.write_text(
            json.dumps({"cached_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(), "codes": codes}),
            encoding="utf-8",
        )
        return codes
    except Exception:
        # Degrade gracefully: use stale cache if available
        if _SETS_CACHE_PATH.exists():
            try:
                return json.loads(_SETS_CACHE_PATH.read_text(encoding="utf-8")).get("codes", [])
            except Exception:
                pass
        return []


def _fuzzy_match_set(raw_set: str) -> str:
    """
    Return the closest valid set code from the cached Scryfall set list.

    Falls back to the uppercased original if no candidate clears the similarity
    cutoff (0.6 tolerates single-character OCR errors, e.g. M2I -> M21).
    """
    valid_codes = _load_valid_set_codes()
    if not valid_codes:
        return raw_set.upper()
    matches = get_close_matches(raw_set.upper(), valid_codes, n=1, cutoff=0.6)
    return matches[0] if matches else raw_set.upper()


# ---------------------------------------------------------------------------
# Step 4 — Scryfall API integration
# ---------------------------------------------------------------------------


def _safe_get(url: str, params: dict | None = None) -> Response:
    """
    Rate-limited GET with structured error handling.

    Raises:
        RuntimeError: on 429 (rate limited) or 5xx (server error).
        RuntimeError: on network-level failure (timeout, DNS, etc.).
    """
    time.sleep(_RATE_LIMIT_DELAY)
    try:
        resp = requests.get(url, params=params, timeout=10)
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error fetching {url}: {exc}") from exc

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        raise RuntimeError(f"Scryfall rate-limited — retry after {retry_after}s")
    if resp.status_code >= 500:
        raise RuntimeError(f"Scryfall server error {resp.status_code} for {url}")

    return resp


def _parse_card_json(data: dict) -> CardData:
    """Extract CardData fields from a Scryfall card object, handling DFCs."""
    image_uris = data.get("image_uris", {})
    faces: list[dict] = data.get("card_faces", [{}])

    # Double-faced cards store per-face image_uris and oracle_text
    if not image_uris and faces:
        image_uris = faces[0].get("image_uris", {})

    return CardData(
        name=data.get("name", ""),
        mana_cost=data.get("mana_cost") or faces[0].get("mana_cost", ""),
        type_line=data.get("type_line", ""),
        oracle_text=data.get("oracle_text") or faces[0].get("oracle_text", ""),
        image_url=image_uris.get("normal", ""),
    )


def fetch_card(set_code: str, collector_number: str) -> CardData | None:
    """
    Resolve a set code + collector number to a CardData via Scryfall.

    Attempt order:
      1. Primary:  GET /cards/{set}/{number}      (exact lookup)
      2. Fallback: GET /cards/search?q=set:X+cn:Y (query-based)

    Returns None if neither attempt finds a card.

    Args:
        set_code:         Normalised, uppercase MTG set code (e.g. "M21").
        collector_number: Normalised collector number string (e.g. "120", "45a").
    """
    # --- Primary ---
    primary_url = f"{_SCRYFALL_BASE}/cards/{set_code.lower()}/{collector_number}"
    resp = _safe_get(primary_url)

    if resp.status_code == 200:
        return _parse_card_json(resp.json())

    if resp.status_code != 404:
        # Unexpected failure (e.g. 401, 422) — don't risk a misleading fallback
        return None

    # --- Fallback (triggered on 404) ---
    query = f"set:{set_code.lower()} cn:{collector_number}"
    resp = _safe_get(f"{_SCRYFALL_BASE}/cards/search", params={"q": query})

    if resp.status_code != 200:
        return None

    results = resp.json().get("data", [])
    if not results:
        # TODO: When upstream OCR also emits the card name, add a third fallback:
        # Call GET /cards/named?fuzzy={card_name} with the OCR'd name string.
        # Scryfall's /cards/named endpoint has native fuzzy matching, so minor
        # misreads (e.g. "Llanowar E1ves" -> "Llanowar Elves") resolve cleanly.
        return None

    return _parse_card_json(results[0])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_card_identifier(raw: str) -> CardData | None:
    """
    Full OCR normalization + Scryfall lookup pipeline.

    Stages:
        1. Regex extraction   — pull set code and collector number from dirty string
        2. OCR correction     — fix digit-lookalike characters in collector number
        3. Fuzzy set match    — correct set code against live Scryfall set list
        4. Scryfall fetch     — primary exact lookup, fallback to search query

    Args:
        raw: Dirty OCR string, e.g. ``"M2l 12O"`` or ``"NEO/O45a"``.

    Returns:
        :class:`CardData` if a card is found, ``None`` otherwise.

    Raises:
        RuntimeError: On Scryfall rate-limiting (429) or server errors (5xx).
    """
    extracted = _extract_set_and_number(raw)
    if extracted is None:
        return None

    raw_set, raw_num = extracted
    set_code = _fuzzy_match_set(raw_set)
    collector_number = _correct_collector_number(raw_num)

    return fetch_card(set_code, collector_number)


# ---------------------------------------------------------------------------
# __main__ — integration smoke-test with dirty OCR inputs
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import unittest
    from unittest.mock import MagicMock, patch

    # When running as __main__ the module lives under that name, not 'card_parser'.
    _THIS_MODULE = sys.modules[__name__]

    # ------------------------------------------------------------------
    # Unit tests (no network)
    # ------------------------------------------------------------------

    class TestExtractSetAndNumber(unittest.TestCase):
        def test_slash_separator(self):
            self.assertEqual(_extract_set_and_number("M21/123"), ("M21", "123"))

        def test_hyphen_separator(self):
            self.assertEqual(_extract_set_and_number("NEO-45a"), ("NEO", "45a"))

        def test_space_separator(self):
            self.assertEqual(_extract_set_and_number("ZNR 200"), ("ZNR", "200"))

        def test_ocr_chars_pass_through(self):
            # Regex is permissive — correction happens in a later stage
            self.assertEqual(_extract_set_and_number("M2l 12O"), ("M2L", "12O"))

        def test_neo_ocr_slash(self):
            self.assertEqual(_extract_set_and_number("NEO/O45a"), ("NEO", "O45a"))

        def test_no_match_returns_none(self):
            self.assertIsNone(_extract_set_and_number("not a card"))

    class TestCorrectCollectorNumber(unittest.TestCase):
        def test_O_to_zero(self):
            self.assertEqual(_correct_collector_number("12O"), "120")

        def test_l_to_one(self):
            self.assertEqual(_correct_collector_number("l45"), "145")

        def test_suffix_preserved(self):
            self.assertEqual(_correct_collector_number("l45a"), "145a")

        def test_O_prefix_with_suffix(self):
            self.assertEqual(_correct_collector_number("O45a"), "045a")

        def test_showcase_s_suffix_unchanged(self):
            # 's' is not in _OCR_DIGIT_MAP, so passes through untouched
            self.assertEqual(_correct_collector_number("45s"), "45s")

        def test_Z_to_two(self):
            self.assertEqual(_correct_collector_number("Z1"), "21")

        def test_B_to_eight(self):
            self.assertEqual(_correct_collector_number("B4"), "84")

        def test_uppercase_S_to_five(self):
            self.assertEqual(_correct_collector_number("1S"), "15")

    class TestFuzzyMatchSet(unittest.TestCase):
        def test_close_match(self):
            codes = ["M21", "NEO", "ZNR", "PLIST"]
            with patch.object(_THIS_MODULE, "_load_valid_set_codes", return_value=codes):
                self.assertEqual(_fuzzy_match_set("M2I"), "M21")

        def test_exact_match(self):
            codes = ["M21", "NEO", "ZNR"]
            with patch.object(_THIS_MODULE, "_load_valid_set_codes", return_value=codes):
                self.assertEqual(_fuzzy_match_set("NEO"), "NEO")

        def test_no_match_returns_original(self):
            codes = ["M21", "NEO", "ZNR"]
            with patch.object(_THIS_MODULE, "_load_valid_set_codes", return_value=codes):
                self.assertEqual(_fuzzy_match_set("XXXBAD"), "XXXBAD")

        def test_empty_codes_passthrough(self):
            with patch.object(_THIS_MODULE, "_load_valid_set_codes", return_value=[]):
                self.assertEqual(_fuzzy_match_set("m21"), "M21")

    class TestFetchCard(unittest.TestCase):
        _MOCK_CARD = {
            "name": "Island",
            "mana_cost": "",
            "type_line": "Basic Land — Island",
            "oracle_text": "({T}: Add {U}.)",
            "image_uris": {"normal": "https://example.com/island.jpg"},
        }

        def _mock_response(self, status: int, json_data: dict) -> MagicMock:
            m = MagicMock()
            m.status_code = status
            m.json.return_value = json_data
            m.headers = {}
            return m

        def test_primary_hit(self):
            with patch("card_parser.requests.get") as mock_get:
                mock_get.return_value = self._mock_response(200, self._MOCK_CARD)
                card = fetch_card("ZNR", "200")
            self.assertIsNotNone(card)
            self.assertEqual(card.name, "Island")

        def test_fallback_on_404(self):
            fallback_resp = self._mock_response(200, {"data": [self._MOCK_CARD]})
            with patch("card_parser.requests.get") as mock_get:
                mock_get.side_effect = [
                    self._mock_response(404, {}),
                    fallback_resp,
                ]
                card = fetch_card("ZNR", "999")
            self.assertIsNotNone(card)
            self.assertEqual(card.name, "Island")

        def test_both_miss_returns_none(self):
            with patch("card_parser.requests.get") as mock_get:
                mock_get.side_effect = [
                    self._mock_response(404, {}),
                    self._mock_response(200, {"data": []}),
                ]
                card = fetch_card("XXXBAD", "99")
            self.assertIsNone(card)

        def test_rate_limit_raises(self):
            m = self._mock_response(429, {})
            m.headers = {"Retry-After": "3"}
            with patch("card_parser.requests.get", return_value=m):
                with self.assertRaises(RuntimeError):
                    fetch_card("ZNR", "200")

    # ------------------------------------------------------------------
    # Run unit tests
    # ------------------------------------------------------------------

    print("=" * 60)
    print("Running unit tests …")
    print("=" * 60)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestExtractSetAndNumber)
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestCorrectCollectorNumber))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestFuzzyMatchSet))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestFetchCard))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # ------------------------------------------------------------------
    # Live integration smoke-test (hits real Scryfall)
    # ------------------------------------------------------------------

    print("\n" + "=" * 60)
    print("Integration smoke-test (live Scryfall) …")
    print("=" * 60)

    DIRTY_INPUTS: list[tuple[str, str]] = [
        ("M2l 12O",    "set='M2l'->M21, num='12O'->120"),
        ("NEO/O45a",   "set='NEO', num='O45a'->045a"),
        ("ZNR 200",    "clean input"),
        ("XXXBAD-99",  "invalid set — expect None"),
    ]

    for raw, note in DIRTY_INPUTS:
        print(f"\nInput:  {raw!r}  ({note})")
        try:
            card = parse_card_identifier(raw)
            if card:
                print(f"  Name:       {card.name}")
                print(f"  Mana Cost:  {card.mana_cost!r}")
                print(f"  Type:       {card.type_line}")
                print(f"  Image URL:  {card.image_url}")
            else:
                print("  Result: No card found.")
        except RuntimeError as exc:
            print(f"  Error: {exc}")

    if not result.wasSuccessful():
        raise SystemExit(1)
