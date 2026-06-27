"""
draft_arbiter.py — MTG booster draft session manager.

One DraftArbiter instance per live draft session. Holds all live state in RAM,
persists to a SQLite database via save(), and reconstructs from it via load().
Thread-safe: a single RLock serialises all mutations.

Pack contents are never known up front. They are reconstructed after a pack is
fully drafted from the ordered pick history.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ROUNDS: list[str] = ["left", "right", "left"]
_VALID_DIRECTIONS: frozenset[str] = frozenset({"left", "right"})

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PickEvent:
    pack_id: str
    seat: int
    pick_index: int
    card_name: str


@dataclass
class LogicalPack:
    pack_id: str
    round_number: int
    origin_seat: int
    pack_size: int
    pass_direction: str
    picks: list[str]

    @property
    def is_complete(self) -> bool:
        return len(self.picks) == self.pack_size


@dataclass
class PlayerStats:
    player_name: str
    picked_cards: list[str]
    wins: int
    losses: int

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    def card_count(self, card_name: str) -> int:
        return self.picked_cards.count(card_name)


@dataclass
class CardStats:
    card_name: str
    times_selected: int


# ---------------------------------------------------------------------------
# DraftArbiter
# ---------------------------------------------------------------------------

class DraftArbiter:
    def __init__(
        self,
        session_id: str,
        num_players: int,
        pack_size: int = 15,
        rounds: list[str] | None = None,
        player_names: list[str] | None = None,
        db_path: str | None = None,
    ) -> None:
        """
        Create a new draft session.

        Initialises all in-memory state and kicks off round 0. Does not touch
        the filesystem until save() is called.

        Args:
            session_id: Unique identifier for this draft session.
            num_players: Number of seats at the table (must be >= 2).
            pack_size: Cards per booster pack (default 15).
            rounds: Ordered list of pass directions, one per round. Defaults to
                ["left", "right", "left"]. Each value must be "left" or "right".
            player_names: Display names for seats 0..N-1. Defaults to
                ["seat_0", ..., "seat_N-1"]. Length must equal num_players.
            db_path: Path to the SQLite database file. If None, save() is unavailable.
        """
        if num_players < 2:
            raise ValueError("num_players must be >= 2")
        if pack_size < 1:
            raise ValueError("pack_size must be >= 1")

        resolved_rounds = rounds if rounds is not None else list(_DEFAULT_ROUNDS)
        for d in resolved_rounds:
            if d not in _VALID_DIRECTIONS:
                raise ValueError(f"invalid pass direction: {d!r}")

        resolved_names = (
            player_names if player_names is not None
            else [f"seat_{i}" for i in range(num_players)]
        )
        if len(resolved_names) != num_players:
            raise ValueError(
                f"player_names length {len(resolved_names)} != num_players {num_players}"
            )

        self._session_id = session_id
        self._num_players = num_players
        self._pack_size = pack_size
        self._rounds = resolved_rounds
        self._player_names = resolved_names
        self._conn = db.get_connection(db_path) if db_path is not None else None

        self._lock = threading.RLock()
        self._packs: dict[str, LogicalPack] = {}
        self._seat_queues: dict[int, deque[str]] = {i: deque() for i in range(num_players)}
        self._pick_events: list[PickEvent] = []
        self._results: list[int] = []
        self._player_stats: dict[str, PlayerStats] = {
            name: PlayerStats(player_name=name, picked_cards=[], wins=0, losses=0)
            for name in resolved_names
        }
        self._current_round = 0
        self._round_complete = False
        self._is_complete = False

        self._init_round(0)

    def _pack_id(self, round_number: int, origin_seat: int) -> str:
        """
        Build the canonical pack identifier string.

        Args:
            round_number: Zero-based round index.
            origin_seat: Seat that opened (originated) the pack.

        Returns:
            pack_id: e.g. "R0S2" for round 0, origin seat 2.
        """
        return f"R{round_number}S{origin_seat}"

    def _next_seat(self, seat: int, direction: str) -> int:
        """
        Compute the seat a pack passes to after the current holder picks.

        Args:
            seat: Current seat holding the pack.
            direction: Pass direction — "left" or "right".

        Returns:
            next_seat: Seat index the pack moves to.
        """
        if direction == "left":
            return (seat - 1) % self._num_players
        return (seat + 1) % self._num_players

    def _init_round(self, round_index: int) -> None:
        """
        Initialise packs and seat queues for the given round.

        Creates one LogicalPack per seat and enqueues each pack at its origin
        seat so every player starts the round with a pack in hand.

        Args:
            round_index: Zero-based index into self._rounds.
        """
        direction = self._rounds[round_index]
        for seat in range(self._num_players):
            pid = self._pack_id(round_index, seat)
            self._packs[pid] = LogicalPack(
                pack_id=pid,
                round_number=round_index,
                origin_seat=seat,
                pack_size=self._pack_size,
                pass_direction=direction,
                picks=[],
            )
            self._seat_queues[seat].append(pid)

    def _check_round_complete(self) -> None:
        """
        Update self._round_complete based on whether all packs in the current round are done.

        Called after every pick that completes a pack. Sets the flag that
        is_round_complete() exposes to callers.
        """
        round_packs = [
            self._packs[self._pack_id(self._current_round, s)]
            for s in range(self._num_players)
        ]
        self._round_complete = all(p.is_complete for p in round_packs)

    @staticmethod
    def _round_from_pack_id(pack_id: str) -> int:
        """
        Extract the round number encoded in a pack_id string.

        Args:
            pack_id: Pack identifier in "R{round}S{seat}" format.

        Returns:
            round_number: Zero-based round index.
        """
        return int(pack_id[1:pack_id.index("S")])

    # ------------------------------------------------------------------
    # Pick recording
    # ------------------------------------------------------------------

    def record_pick(self, seat: int, card_name: str) -> PickEvent:
        """
        Record a card pick for the given seat.

        Pops the front pack from the seat's queue, appends the card to that
        pack's pick list, updates player stats, and passes the pack to the next
        seat (or marks the round complete if the pack is now full).

        Args:
            seat: Zero-based seat index of the player picking.
            card_name: Name of the card being picked.

        Returns:
            event: Immutable PickEvent describing the pick.
        """
        with self._lock:
            if seat < 0 or seat >= self._num_players:
                raise ValueError(f"seat {seat} out of range [0, {self._num_players})")
            if self._is_complete:
                raise ValueError("draft is complete")
            if self._round_complete:
                raise ValueError("round is complete — call advance_round() before picking")
            if not self._seat_queues[seat]:
                raise ValueError(f"no pack available at seat {seat}")

            pack_id = self._seat_queues[seat].popleft()
            pack = self._packs[pack_id]
            pick_index = len(pack.picks)
            event = PickEvent(
                pack_id=pack_id,
                seat=seat,
                pick_index=pick_index,
                card_name=card_name,
            )
            pack.picks.append(card_name)
            self._pick_events.append(event)
            self._player_stats[self._player_names[seat]].picked_cards.append(card_name)

            if pack.is_complete:
                self._check_round_complete()
            else:
                next_seat = self._next_seat(seat, pack.pass_direction)
                self._seat_queues[next_seat].append(pack_id)

            return event

    # ------------------------------------------------------------------
    # Round management
    # ------------------------------------------------------------------

    def is_round_complete(self) -> bool:
        """
        Return whether every pack in the current round has been fully drafted.

        Returns:
            complete: True if all packs are done and advance_round() should be called.
        """
        with self._lock:
            return self._round_complete

    def advance_round(self) -> None:
        """
        Advance the session to the next round, or mark the draft complete.

        Must only be called after is_round_complete() returns True. If this was
        the final round, sets is_draft_complete() to True without initialising
        another round.
        """
        with self._lock:
            if self._is_complete:
                raise ValueError("draft is complete")
            if not self._round_complete:
                raise ValueError("round is not yet complete")
            next_round = self._current_round + 1
            if next_round >= len(self._rounds):
                self._is_complete = True
                return
            self._current_round = next_round
            self._round_complete = False
            self._init_round(self._current_round)

    def is_draft_complete(self) -> bool:
        """
        Return whether all rounds have been fully drafted.

        Returns:
            complete: True if advance_round() has moved past the final round.
        """
        with self._lock:
            return self._is_complete

    @property
    def current_round(self) -> int:
        """
        Zero-based index of the round currently being drafted.

        Returns:
            round: Current round index.
        """
        with self._lock:
            return self._current_round

    # ------------------------------------------------------------------
    # Reconstruction
    # ------------------------------------------------------------------

    def get_full_pack(self, pack_id: str) -> list[str]:
        """
        Return all cards picked from a pack, in pick order.

        The pack must be fully drafted before this can be called.

        Args:
            pack_id: Pack identifier in "R{round}S{seat}" format.

        Returns:
            picks: Ordered list of card names, length == pack_size.
        """
        with self._lock:
            if pack_id not in self._packs:
                raise ValueError(f"unknown pack_id: {pack_id!r}")
            pack = self._packs[pack_id]
            if not pack.is_complete:
                raise ValueError(f"pack {pack_id!r} is not yet complete")
            return list(pack.picks)

    def get_pack_before_pick(self, pack_id: str, pick_index: int) -> list[str]:
        """
        Reconstruct what the pack looked like when a specific pick was made.

        Returns the cards that were still available at pick_index (i.e. the
        card chosen plus everything picked after it).

        Args:
            pack_id: Pack identifier in "R{round}S{seat}" format.
            pick_index: Zero-based index of the pick to reconstruct around.

        Returns:
            cards: Remaining cards at the moment of pick_index, in draft order.
        """
        full = self.get_full_pack(pack_id)
        if pick_index < 0 or pick_index >= self._pack_size:
            raise ValueError(f"pick_index {pick_index} out of range [0, {self._pack_size})")
        return full[pick_index:]

    def get_pack_after_pick(self, pack_id: str, pick_index: int) -> list[str]:
        """
        Reconstruct what the pack looked like immediately after a specific pick.

        Returns the cards that were passed on to the next seat after pick_index.

        Args:
            pack_id: Pack identifier in "R{round}S{seat}" format.
            pick_index: Zero-based index of the pick that was just made.

        Returns:
            cards: Cards passed to the next seat after pick_index, in draft order.
        """
        full = self.get_full_pack(pack_id)
        if pick_index < 0 or pick_index >= self._pack_size:
            raise ValueError(f"pick_index {pick_index} out of range [0, {self._pack_size})")
        return full[pick_index + 1:]

    def replay_draft(self) -> list[PickEvent]:
        """
        Return all pick events in the order they were recorded.

        Returns:
            events: Snapshot of the full pick history as a list of PickEvents.
        """
        with self._lock:
            return list(self._pick_events)

    # ------------------------------------------------------------------
    # Result recording
    # ------------------------------------------------------------------

    def record_result(self, winner_seat: int) -> None:
        """
        Record the outcome of a match, updating win/loss counts for all seats.

        The winner gains one win; every other seat gains one loss.

        Args:
            winner_seat: Zero-based seat index of the match winner.
        """
        with self._lock:
            if winner_seat < 0 or winner_seat >= self._num_players:
                raise ValueError(f"winner_seat {winner_seat} out of range")
            self._results.append(winner_seat)
            winner_name = self._player_names[winner_seat]
            for i, name in enumerate(self._player_names):
                if i == winner_seat:
                    self._player_stats[name].wins += 1
                else:
                    self._player_stats[name].losses += 1

    # ------------------------------------------------------------------
    # Stats accessors
    # ------------------------------------------------------------------

    def get_player_stats(self, seat: int) -> PlayerStats:
        """
        Return stats for a single player, read from in-memory state.

        Args:
            seat: Zero-based seat index.

        Returns:
            stats: PlayerStats for the requested seat.
        """
        with self._lock:
            if seat < 0 or seat >= self._num_players:
                raise ValueError(f"seat {seat} out of range")
            return self._player_stats[self._player_names[seat]]

    def get_all_player_stats(self) -> list[PlayerStats]:
        """
        Return stats for all players in seat order, read from in-memory state.

        Returns:
            stats: List of PlayerStats, one per seat, ordered seat 0..N-1.
        """
        with self._lock:
            return [self._player_stats[name] for name in self._player_names]

    def get_card_stats(self, card_name: str) -> CardStats | None:
        """
        Return how many times a card has been picked across all sessions in the CSV.

        Combines historical CSV data (other sessions) with current in-memory picks.

        Args:
            card_name: Exact card name to look up.

        Returns:
            stats: CardStats for the card, or None if it has never been picked.
        """
        counts = self._aggregate_card_counts()
        times = counts.get(card_name)
        return CardStats(card_name=card_name, times_selected=times) if times is not None else None

    def get_all_card_stats(self) -> list[CardStats]:
        """
        Return pick counts for every card seen across all sessions in the CSV.

        Combines historical CSV data (other sessions) with current in-memory picks.

        Returns:
            stats: List of CardStats for every card that has been picked at least once.
        """
        counts = self._aggregate_card_counts()
        return [CardStats(card_name=c, times_selected=n) for c, n in counts.items()]

    def _aggregate_card_counts(self) -> dict[str, int]:
        """
        Aggregate pick counts across all sessions in the database and current RAM state.

        Reads picks from other sessions in the DB, then merges in the current
        session's in-memory events so the caller sees a unified view without
        double-counting the current session.

        Returns:
            counts: Mapping of card name to total times selected.
        """
        with self._lock:
            counts: dict[str, int] = {}
            if self._conn is not None:
                for row in self._conn.execute(
                    "SELECT card_name, COUNT(*) as n FROM picks "
                    "WHERE session_id != ? GROUP BY card_name",
                    (self._session_id,),
                ):
                    counts[row["card_name"]] = row["n"]
            for event in self._pick_events:
                counts[event.card_name] = counts.get(event.card_name, 0) + 1
            return counts

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """
        Persist the current session to the SQLite database.

        Safe to call multiple times; picks are inserted idempotently and session
        metadata is upserted. Results are deleted and reinserted on each call.
        """
        with self._lock:
            if self._conn is None:
                raise ValueError("no database path configured")
            conn = self._conn

            conn.execute("""
                INSERT INTO sessions (session_id, num_players, pack_size, current_round, is_complete)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    current_round = excluded.current_round,
                    is_complete   = excluded.is_complete
            """, (self._session_id, self._num_players, self._pack_size,
                  self._current_round, int(self._is_complete)))

            for i, direction in enumerate(self._rounds):
                conn.execute("""
                    INSERT OR IGNORE INTO session_rounds (session_id, round_index, pass_direction)
                    VALUES (?, ?, ?)
                """, (self._session_id, i, direction))

            for seat, name in enumerate(self._player_names):
                conn.execute("""
                    INSERT OR IGNORE INTO players (session_id, seat, player_name)
                    VALUES (?, ?, ?)
                """, (self._session_id, seat, name))

            seat_counts: dict[int, int] = {i: 0 for i in range(self._num_players)}
            for event in self._pick_events:
                player_pick_num = seat_counts[event.seat]
                s_idx = event.pack_id.index("S")
                round_num = int(event.pack_id[1:s_idx])
                origin_seat = int(event.pack_id[s_idx + 1:])
                conn.execute("""
                    INSERT OR IGNORE INTO picks
                        (session_id, round_number, origin_seat, seat,
                         pick_index, player_pick_num, card_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (self._session_id, round_num, origin_seat,
                      event.seat, event.pick_index, player_pick_num, event.card_name))
                seat_counts[event.seat] += 1

            conn.execute("DELETE FROM results WHERE session_id = ?", (self._session_id,))
            for winner_seat in self._results:
                conn.execute("""
                    INSERT INTO results (session_id, winner_seat) VALUES (?, ?)
                """, (self._session_id, winner_seat))

            conn.commit()

    @classmethod
    def load(cls, db_path: str, session_id: str) -> DraftArbiter:
        """
        Reconstruct a DraftArbiter from a session stored in the SQLite database.

        Replays all pick rows in insertion order, advancing rounds as needed,
        then replays result rows to restore win/loss counts.

        Args:
            db_path: Path to the SQLite database file previously written by save().
            session_id: Identifier of the session to load.

        Returns:
            arbiter: Fully restored DraftArbiter in the same state as when saved.
        """
        if not Path(db_path).exists():
            raise FileNotFoundError(f"no database at {db_path!r}")

        conn = db.get_connection(db_path)

        session_row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session_row is None:
            conn.close()
            raise ValueError(f"session {session_id!r} not found in {db_path!r}")

        rounds = [
            r["pass_direction"] for r in conn.execute(
                "SELECT pass_direction FROM session_rounds "
                "WHERE session_id = ? ORDER BY round_index",
                (session_id,),
            ).fetchall()
        ]
        player_names = [
            r["player_name"] for r in conn.execute(
                "SELECT player_name FROM players WHERE session_id = ? ORDER BY seat",
                (session_id,),
            ).fetchall()
        ]
        pick_rows = conn.execute(
            "SELECT seat, card_name, round_number FROM picks "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        result_rows = conn.execute(
            "SELECT winner_seat FROM results WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        is_complete = bool(session_row["is_complete"])
        conn.close()

        arbiter = cls(
            session_id=session_id,
            num_players=session_row["num_players"],
            pack_size=session_row["pack_size"],
            rounds=rounds,
            player_names=player_names,
            db_path=db_path,
        )

        for pick_row in pick_rows:
            while arbiter._current_round < pick_row["round_number"]:
                arbiter.advance_round()
            arbiter.record_pick(pick_row["seat"], pick_row["card_name"])

        if is_complete and arbiter._round_complete and not arbiter._is_complete:
            arbiter.advance_round()

        for result_row in result_rows:
            winner_seat = result_row["winner_seat"]
            arbiter._results.append(winner_seat)
            winner_name = arbiter._player_names[winner_seat]
            for i, name in enumerate(arbiter._player_names):
                if i == winner_seat:
                    arbiter._player_stats[name].wins += 1
                else:
                    arbiter._player_stats[name].losses += 1

        return arbiter

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        """Unique identifier for this draft session."""
        return self._session_id

    @property
    def player_names(self) -> list[str]:
        """Display names for all seats, in seat order. Returns a copy."""
        return list(self._player_names)
