# TheOracle — Project Reference

## Overview
MTG draft tracking website backend. Two core capabilities: (1) resolves OCR-scanned card identifiers to full card data via Scryfall, and (2) manages booster draft sessions end-to-end. Intended as a Python backend package exposed via FastAPI (planned).

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
    card_parser.py        # OCR normalization + Scryfall lookup pipeline
    draft_arbiter.py      # MTG booster draft session manager
tests/
    test_card_parser.py   # 47 unit tests, all mocked (no network)
    test_draft_arbiter.py # 54 unit tests, pure in-memory
pyproject.toml            # dependencies, build config, pytest config
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

---

## Module: `theoracle.card_parser`

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

---

## Module: `theoracle.draft_arbiter`

One `DraftArbiter` instance per live draft session. Holds all live state in RAM; persists to a CSV file via `save()`. Thread-safe — a single `threading.RLock` serialises all mutations. Pack contents are never known up front; they are reconstructed from the ordered pick history after a pack is fully drafted.

### Architecture decisions
- **One arbiter per session** — correct granularity for a web backend (route by `session_id`, each arbiter owns its lock).
- **No session registry here** — managing multiple live arbiters belongs to the FastAPI layer (planned separately).
- **CSV persistence is a stepping stone** — intentionally thin so it can be swapped for a proper SQL schema later.
- **Explicit round advancement** — `record_pick` never auto-advances rounds. The coordinator calls `advance_round()` after `is_round_complete()` returns `True`, giving the web layer a natural gate for "round over" UI.

### Public API
```python
from theoracle.draft_arbiter import DraftArbiter, PickEvent, PlayerStats, CardStats

arbiter = DraftArbiter(
    session_id="draft-2026-06-20",
    num_players=8,
    pack_size=15,                        # default
    rounds=["left", "right", "left"],    # default
    player_names=["Alice", "Bob", ...],  # default: ["seat_0", ..., "seat_N-1"]
    save_path="drafts.csv",              # default: "{session_id}.csv"
)

event: PickEvent = arbiter.record_pick(seat=0, card_name="Lightning Bolt")

if arbiter.is_round_complete():
    arbiter.advance_round()

arbiter.record_result(winner_seat=2)
arbiter.save()

arbiter2 = DraftArbiter.load("drafts.csv")
```

### Data classes
| Class | Key fields |
|---|---|
| `PickEvent` | `pack_id`, `seat`, `pick_index`, `card_name` — **frozen** (immutable record) |
| `LogicalPack` | `pack_id`, `round_number`, `origin_seat`, `pack_size`, `pass_direction`, `picks` |
| `PlayerStats` | `player_name`, `picked_cards: list[str]` (primary), `wins`, `losses`, `win_rate`, `card_count(name)` |
| `CardStats` | `card_name`, `times_selected` — computed from CSV on demand |

### Pack identity
`pack_id = f"R{round_number}S{origin_seat}"` — e.g. `"R0S2"` is the pack that originated at seat 2 in round 0.

### Pass directions
- `"left"` → next seat = `(current - 1) % num_players`
- `"right"` → next seat = `(current + 1) % num_players`

### Reconstruction methods (require pack to be complete)
```python
arbiter.get_full_pack("R0S2")              # all N picks in order
arbiter.get_pack_before_pick("R0S2", k)   # full_pack[k:]
arbiter.get_pack_after_pick("R0S2", k)    # full_pack[k+1:]
arbiter.replay_draft()                     # list[PickEvent] in recording order
```

### Stats
- `get_player_stats(seat)` / `get_all_player_stats()` — from RAM; `picked_cards` is the ordered pick history (primary).
- `get_card_stats(name)` / `get_all_card_stats()` — reads CSV, combines with current session RAM (no double-counting).

### CSV schema
Single file, `row_type` discriminator: `METADATA` | `PICK` | `RESULT`. Multiple sessions can share one file; `save()` preserves other sessions' rows.

### Error behaviour
- `ValueError` on all structural violations (invalid seat, round not complete, pack not complete, etc.)
- `get_card_stats` returns `None` for unknown cards (not an error)
- No card name legality validation (structural correctness only)

---

## Code Style Rules
- No comments unless the WHY is non-obvious
- No type annotations on local variables — only on function signatures
- All external I/O (network, file, time) must be mockable — no bare `requests.get` calls outside `_safe_get`
- Tests must never hit the network — patch `theoracle.card_parser.requests.get` and `theoracle.card_parser.time.sleep`
- `autouse` fixture `no_sleep` suppresses `time.sleep` globally in `test_card_parser.py`
- Draft arbiter tests use `tmp_path` fixture for CSV I/O; no mocking needed (pure in-memory except persistence tests)

---

## Planned Next Steps

### Priority 1 — SQL schema design
Replace CSV persistence in `DraftArbiter` with a proper relational schema. The CSV layer is intentionally thin (`save()` / `load()` isolated to ~60 lines) to make this swap straightforward.

### Priority 2 — FastAPI web backend
Session registry + HTTP endpoints. Key design: one `DraftArbiter` per live session, registry maps `session_id → DraftArbiter`, loaded from DB on server startup.
- `POST /sessions` — create session
- `POST /sessions/{id}/picks` — record pick
- `POST /sessions/{id}/advance` — advance round
- `GET /sessions/{id}/stats` — player stats
- `POST /cards/identify` — expose `parse_card_identifier`

### Priority 3 — Third Scryfall fallback
`GET /cards/named?fuzzy={name}` when OCR also emits a card name (marked TODO in `fetch_card`).
