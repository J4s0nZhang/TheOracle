# TheOracle — Project Reference

## Overview
MTG draft helper that resolves OCR-scanned card identifiers (set code + collector number) to full card data via the Scryfall API. Intended as a Python backend package consumed by a frontend app (framework TBD).

## Stack
- **Language:** Python 3.12+
- **Package layout:** src layout — `src/theoracle/`
- **Build backend:** hatchling (`pyproject.toml`)
- **HTTP client:** requests
- **External API:** Scryfall (`https://api.scryfall.com`)
- **Test framework:** pytest 9+
- **Environment:** conda env named `oracle`

## Project Structure
```
src/theoracle/
    __init__.py
    card_parser.py      # OCR normalization + Scryfall lookup pipeline
tests/
    test_card_parser.py # 47 unit tests, all mocked (no network)
pyproject.toml          # dependencies, build config, pytest config
```

## Install & Run
```bash
# One-time setup (run from project root)
conda activate oracle
pip install -e ".[dev]"

# Run tests
pytest

# Run a specific test file
pytest tests/test_card_parser.py

# Run a single test
pytest tests/test_card_parser.py::test_function_name
```

## Core Module: `theoracle.card_parser`

### Public API
```python
from theoracle.card_parser import parse_card_identifier, CardData

card: CardData | None = parse_card_identifier("M21/123")
```

### `CardData` fields
| Field | Type | Source |
|---|---|---|
| `name` | str | Scryfall `name` |
| `mana_cost` | str | Scryfall `mana_cost` (face 0 for DFCs) |
| `type_line` | str | Scryfall `type_line` |
| `oracle_text` | str | Scryfall `oracle_text` (face 0 for DFCs) |
| `image_url` | str | `image_uris['normal']` (face 0 for DFCs) |

### Pipeline (in order)
1. **`_extract_set_and_number(raw)`** — permissive regex, accepts OCR-mangled input. Set code: 3–5 alphanumeric chars. Separator: `/`, `-`, or whitespace. Collector number: must start with a digit-like char.
2. **`_correct_collector_number(raw)`** — substitutes OCR digit-lookalikes (`O→0`, `l→1`, `I→1`, `Z→2`, `z→2`, `S→5`, `B→8`). Preserves valid Scryfall variant suffixes (`a`–`y` excluding OCR-mapped chars). Lowercase `s` is never corrected (showcase suffix).
3. **`_fuzzy_match_set(raw_set)`** — validates set code against Scryfall `/sets` list using `difflib.get_close_matches` (cutoff 0.6). Falls back to uppercased original if no match.
4. **`fetch_card(set_code, collector_number)`** — primary: `GET /cards/{set}/{number}`; fallback on 404: `GET /cards/search?q=set:X cn:Y`. Returns `None` if both miss.

### Scryfall set code cache
- Path: `src/theoracle/.scryfall_sets_cache.json`
- TTL: 30 days
- On stale/missing: fetches `GET /sets`, rewrites cache
- On network failure: degrades to stale cache data, or `[]` if no cache exists

### Error behaviour
- `RuntimeError` raised on: HTTP 429 (rate limited), HTTP 5xx (server error), network failure
- Non-404/non-200 primary responses abort without fallback (avoids misleading results)

## Code Style Rules
- No comments unless the WHY is non-obvious
- No type annotations on local variables — only on function signatures
- All external I/O (network, file, time) must be mockable — no bare `requests.get` calls outside `_safe_get`
- Tests must never hit the network — patch `theoracle.card_parser.requests.get` and `theoracle.card_parser.time.sleep`
- `autouse` fixture `no_sleep` suppresses `time.sleep` globally across the test suite

## Planned Next Steps
- Add web backend framework (FastAPI preferred) — expose `parse_card_identifier` as a POST endpoint
- Third Scryfall fallback: `GET /cards/named?fuzzy={name}` when OCR also emits a card name (marked TODO in `fetch_card`)
