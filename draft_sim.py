#!/usr/bin/env python3
"""Temporary interactive draft simulation script."""

import sys
from theoracle.draft_arbiter import DraftArbiter, PickEvent


def _prompt(msg: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    raw = input(f"{msg}{suffix}: ").strip()
    return raw if raw else (default or "")


def _get_int(msg: str, default: int, min_val: int) -> int:
    while True:
        raw = _prompt(msg, str(default))
        try:
            val = int(raw)
            if val < min_val:
                print(f"  Must be >= {min_val}.")
                continue
            return val
        except ValueError:
            print("  Enter a whole number.")


def _get_direction(msg: str, default: str) -> str:
    while True:
        d = _prompt(msg, default).lower()
        if d in ("left", "right"):
            return d
        print("  Must be 'left' or 'right'.")


def _separator(char: str = "-", width: int = 60) -> None:
    print(char * width)


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def _init_arbiter() -> DraftArbiter:
    print("=== MTG Draft Simulator ===\n")

    session_id = _prompt("Session ID", "test-draft")
    num_players = _get_int("Number of players", 4, 2)
    pack_size = _get_int("Cards per pack", 5, 1)
    num_rounds = _get_int("Number of rounds", 3, 1)

    default_dirs = ["left", "right", "left"]
    rounds: list[str] = []
    for i in range(num_rounds):
        default_dir = default_dirs[i] if i < len(default_dirs) else "left"
        rounds.append(_get_direction(f"  Round {i + 1} pass direction", default_dir))

    print()
    player_names: list[str] = []
    for i in range(num_players):
        player_names.append(_prompt(f"Name for seat {i}", f"seat_{i}"))

    print()
    return DraftArbiter(
        session_id=session_id,
        num_players=num_players,
        pack_size=pack_size,
        rounds=rounds,
        player_names=player_names,
    )


# ---------------------------------------------------------------------------
# Draft loop
# ---------------------------------------------------------------------------

def _run_draft(arbiter: DraftArbiter, player_names: list[str], rounds: list[str], pack_size: int) -> None:
    num_players = len(player_names)
    for round_idx, direction in enumerate(rounds):
        _separator()
        print(f"Round {round_idx + 1} of {len(rounds)}  (passing {direction})")
        _separator()
        for pick_slot in range(pack_size):
            print(f"\n  Pick {pick_slot + 1} of {pack_size}:")
            for seat in range(num_players):
                while True:
                    card = input(f"    {player_names[seat]} (seat {seat}): ").strip()
                    if card:
                        break
                    print("    Card name cannot be empty.")
                arbiter.record_pick(seat, card)

        if arbiter.is_round_complete():
            arbiter.advance_round()
        print()


# ---------------------------------------------------------------------------
# Summary display
# ---------------------------------------------------------------------------

def _print_summary(arbiter: DraftArbiter, player_names: list[str], rounds: list[str]) -> None:
    num_players = len(player_names)
    num_rounds = len(rounds)

    print("\n")
    _separator("=")
    print("DRAFT SUMMARY")
    _separator("=")

    # 1. Original packs — all cards in the order they were picked
    print("\n[1] Original Packs (cards listed in pick order)\n")
    for r in range(num_rounds):
        print(f"  Round {r + 1}:")
        for s in range(num_players):
            pack_id = f"R{r}S{s}"
            cards = arbiter.get_full_pack(pack_id)
            opener = player_names[s]
            print(f"    {pack_id}  (opened by {opener})")
            for i, card in enumerate(cards):
                print(f"      {i + 1:>2}. {card}")
        print()

    # 2. Per-player picks organised by pack
    print("[2] Player Picks by Pack\n")
    events = arbiter.replay_draft()

    # Group events by (seat, pack_id), preserving original order within each group
    picks_by_key: dict[tuple[int, str], list[PickEvent]] = {}
    for ev in events:
        key = (ev.seat, ev.pack_id)
        picks_by_key.setdefault(key, []).append(ev)

    for seat in range(num_players):
        name = player_names[seat]
        print(f"  {name} (seat {seat}):")
        seat_events = [ev for ev in events if ev.seat == seat]
        # Sort by round then by pick_index within pack so output is chronological
        seat_events.sort(key=lambda e: (int(e.pack_id[1:e.pack_id.index("S")]), e.pick_index))
        for ev in seat_events:
            r = int(ev.pack_id[1:ev.pack_id.index("S")])
            print(
                f"    R{r + 1} {ev.pack_id}  pick #{ev.pick_index + 1} in pack"
                f"  →  {ev.card_name}"
            )
        print()

    # 3. Final card pools
    print("[3] Final Card Pools\n")
    for stat in arbiter.get_all_player_stats():
        total = len(stat.picked_cards)
        print(f"  {stat.player_name}  ({total} card{'s' if total != 1 else ''}):")
        for card in stat.picked_cards:
            print(f"    - {card}")
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        arbiter = _init_arbiter()
        player_names = arbiter.player_names
        rounds = arbiter._rounds
        pack_size = arbiter._pack_size

        print(
            f"Starting draft: {len(player_names)} players, "
            f"{pack_size} cards/pack, {len(rounds)} round(s).\n"
        )

        _run_draft(arbiter, player_names, rounds, pack_size)
        _print_summary(arbiter, player_names, rounds)

    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
