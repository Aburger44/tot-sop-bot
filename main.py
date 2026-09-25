"""The strategy.

    python main.py                       # play as fast as the bot can
    python main.py --click-interval 0.4  # the same rules at roughly human speed
    python main.py --url <arcade url>    # the timed, scored game

The bot opens the page and then waits. **You** load the game and pick the mode —
it will not touch anything. The moment the RideControl panel appears on screen the
game has begun, ``wait_for_panel()`` returns, and the loop below starts.

How it plays
============

The ride is a pipeline, and the track is its narrowest point:

    queue -> TV room -> preshow -> unload -> elevator queue -> elevator -> track

Only one elevator can be on the track at a time (a dispatched car is what locks
it), so the track sets the rate at which guests can be served, and every ride it
makes is worth as many guests as are aboard. Everything upstream exists to keep it
fed. That gives the whole strategy in four rules:

1. **Never leave the track idle.** The moment it reads Track Ready, dispatch the
   car that has been armed longest.
2. **Always have the next car ready.** Load elevators while another one is out on
   the track, so there is an armed car waiting when it comes back.
3. **Send full cars.** Hold an elevator (and a TV room) until 21 are waiting, so
   each trip carries a full load — *unless* holding would starve the stage after
   it, in which case go with who is there.
4. **Never keep a cue waiting.** Start Preshow and Unload TV Room are pressed the
   moment they turn orange: a room sitting finished is capacity thrown away.

How loading works, which the rules depend on: Load takes only the people waiting
at the moment it is pressed. If that is 21, the room (or car) is full and its
doors close by themselves. If it is fewer, the doors stay open until someone
closes them by hand — the Entrance button for a TV room, Doors for an elevator —
and until then the preshow cannot start and the car cannot be dispatched.
An elevator's doors must also be opened by hand, with Doors, before Load works.

Every rule below is one observable and one click, so each maps to one line of the
written procedure. The run log prints each decision with the reason, in words.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from tot import DEFAULT_URL, Game, LoadState, PreshowState, RuffleCrashed, UnloadState

# The Simulation Page only offers the Unlimited game. Pass --url for the TM Arcade.
URL = DEFAULT_URL

#: Where each run's final screenshot and stats are kept.
RUNS = Path(__file__).with_name("runs")

# --------------------------------------------------------------------------
# Tuning. The clock runs at about one game-minute per real second, so a whole
# 12-hour game is ~12 real minutes: cheap enough to A/B any of these.
# --------------------------------------------------------------------------

#: Put every TV room and elevator into service. More units never slow the track,
#: and they are what lets rule 2 have a car ready while another is out.
ENABLE_ALL_UNITS = True

#: Keep Automatic Doors on. With it on, a car that fills to 21 closes its own doors
#: and arms Dispatch; the bot only has to open them before Load, and close them
#: after a part-filled load.
KEEP_AUTOMATIC_DOORS = True

#: Seconds an elevator holds out for a full 21 before loading whoever is waiting.
#: The short one applies when nothing else could use the track next — no other car
#: is armed or loading — so holding would leave the track idle.
ELEVATOR_PATIENCE = 20.0
ELEVATOR_PATIENCE_WHEN_STARVED = 5.0

#: The same, for a TV room. "Starved" means the whole ride downstream has nothing
#: to do: the other room is idle and no elevator has anyone waiting, loading or
#: armed. Otherwise a room holds out for 21 for a long time.
#: Measured live, a TV room's queue takes about 30 s to reach 21 from empty, and a
#: preshow runs about 23 s, so holding out for a full room costs little.
TV_ROOM_PATIENCE = 60.0
TV_ROOM_PATIENCE_WHEN_STARVED = 40.0

#: After a partial load the doors are closed by hand once Waiting reads 0 —
#: everyone who was queued is in — or after this many seconds of boarding if new
#: arrivals keep the queue above 0. 21 people take about 2.5 s to walk in.
BOARDING_TIME = 4.0

#: A room or car loaded from a full queue closes itself. If it has not after this
#: long, close it by hand.
FULL_LOAD_GRACE = 8.0

#: How long a press is assumed to be on its way before the panel shows it. A frame
#: is a picture of the past, so without this the loop would dispatch a second car
#: into a track that still *looks* ready in a frame painted before the first press.
IN_FLIGHT = 2.0

#: Explain an elevator that has had people waiting this long with no change.
ELEVATOR_STUCK_AFTER = 30.0

#: Warn when nothing on the panel has changed for this long with the ride open.
STALL_WARNING = 45.0

#: How often a unit that will not come into service is re-pressed before giving up.
ENABLE_ATTEMPTS = 5


class Strategy:
    """The four rules, plus the little memory they need: how long each unit has
    been holding for a full load, and which presses are still in flight."""

    def __init__(
        self,
        log: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.log, self.clock = log, clock
        self.started = clock()
        self.holding: dict[str, float] = {}  # unit -> since when it has had guests to take
        self.armed_at: dict[int, float] = {}  # elevator -> since when Dispatch is green
        self.in_flight: dict[str, float] = {}  # button -> when it was pressed
        self.on_track: int | None = None  # the elevator out on the ride right now
        self.car_seen: dict[int, tuple] = {}  # elevator -> (buttons, since, reported)
        self.hold_said: dict[str, float] = {}  # unit -> which hold was announced
        self.load_kind: dict[str, str] = {}  # unit -> "full" / "partial", until its doors shut
        self.open_since: dict[str, float] = {}  # unit -> first frame its doors read open
        self.enable_presses: Counter[str] = Counter()
        self.stats: Counter[str] = Counter()
        self._last_snapshot: list[str] = []
        self._last_change = self.started
        self._warned_stall = False

    # -- the loop body --------------------------------------------------------
    def play(self, game: Game) -> None:
        """One pass, against a frame that has already been refreshed. Every
        property read here describes the same instant."""
        now = self.clock()
        self._watch_for_stall(game, now)

        if not self.setup(game):
            return
        self.dispatch(game, now)  # rule 1 — the bottleneck comes first
        self.run_elevators(game, now)  # rules 2 and 3
        self.run_tv_rooms(game, now)  # rules 3 and 4

    # -- opening the ride -----------------------------------------------------
    def setup(self, game: Game) -> bool:
        """Open the attraction and put everything in service. False while the
        ride is not open yet, in which case nothing else is worth reading."""
        ctl = game.control
        if not ctl.attraction_is_active:
            if ctl.toggle_attraction():
                self._say("Attraction", "open", "reads Closed")
            return False

        if KEEP_AUTOMATIC_DOORS and not ctl.automatic_doors_on:
            if ctl.toggle_automatic_doors():
                self._say("Automatic Doors", "turn on", "it was off")

        if ENABLE_ALL_UNITS:
            units = [*game.tv_rooms.values(), *game.elevators.values()]
            for unit in units:
                if unit.enabled or self.enable_presses[unit.prefix] >= ENABLE_ATTEMPTS:
                    continue
                if unit.toggle_enabled():
                    self.enable_presses[unit.prefix] += 1
                    self._say(_name(unit.prefix), "enable", "it was out of service")
        return True

    # -- rule 1: never leave the track idle -----------------------------------
    def dispatch(self, game: Game, now: float) -> None:
        for i, lift in game.elevators.items():
            if self._ready_to_go(lift):
                self.armed_at.setdefault(i, now)
            else:
                self.armed_at.pop(i, None)

        if game.track.is_locked:
            self.in_flight.pop("track", None)  # the last dispatch has landed
            return
        if self._pending("track", now):
            return  # a car was just sent; this frame predates the track locking
        # Track Ready: whichever car went last is back. Its Dispatch button stays
        # orange, but it is ready to open, load and go again like any other.
        self.on_track = None

        ready = [i for i, lift in game.elevators.items() if self._ready_to_go(lift)]
        if not ready:
            return
        i = min(ready, key=lambda k: self.armed_at[k])
        if game.elevators[i].dispatch():
            self.in_flight["track"] = now
            self.on_track = i
            self.load_kind.pop(game.elevators[i].prefix, None)  # this load has left
            self.stats["dispatches"] += 1
            self._say(f"Elevator {i}", "dispatch", "loaded, doors closed, Dispatch green, track ready")

    def _ready_to_go(self, lift) -> bool:
        """Loaded, shut, and cleared to leave.

        Dispatch Elevator turns green whenever a car is at the station — even
        empty with its doors open — so green alone does not mean ready. It also
        takes the doors shut and people aboard: a load this bot pressed, or a
        Loaded box that does not read 0.
        """
        return (
            lift.can_dispatch
            and lift.doors_are_closed
            and (lift.prefix in self.load_kind or not lift.loaded_is_zero)
        )

    # -- rules 2 and 3: always have the next car ready, and send it full ------
    def run_elevators(self, game: Game, now: float) -> None:
        """Open the doors, press Load (orange once the doors are open), and close
        the doors by hand if the car did not fill to 21."""

        # Is there already a car that will be able to take the track next?
        def next_up(i: int) -> bool:
            lift = game.elevators[i]
            return self._ready_to_go(lift) or lift.is_loading or lift.prefix in self.load_kind

        for i, lift in game.elevators.items():
            key = lift.prefix
            self._explain_if_stuck(game, i, now)
            if not lift.doors_are_closed:
                self.in_flight.pop(f"{key}.doors", None)
            if lift.is_loading:
                self.in_flight.pop(f"{key}.load", None)
            if lift.is_dispatched:
                # This car has been sent, so its last load is finished with.
                # Forget it: a car coming back may open its doors before a frame
                # ever shows them shut, and a leftover record would make it look
                # mid-load forever.
                self.load_kind.pop(key, None)

            # Dispatch stays orange after a car comes back, so it is not a sign
            # the car is still out. Only the last one sent is, while the track
            # reads Locked. A loaded car waiting its turn is left alone too.
            if not lift.enabled or i == self.on_track or self._ready_to_go(lift):
                self.holding.pop(key, None)
                self.open_since.pop(key, None)
                continue
            if lift.doors_are_moving:
                continue
            # Load Elevator orange with the doors open is the game saying this car
            # is empty and ready to board. Trust that over the Loaded box: its "0"
            # is the one reading that has been misread on a live screen.
            empty = lift.loaded_is_zero or (
                lift.doors_are_open and lift.load_button is LoadState.PROMPTED
            )
            if lift.doors_are_closed:
                self.open_since.pop(key, None)
                if key in self.load_kind:
                    continue  # loaded and shut; Dispatch will turn green
            else:
                self.open_since.setdefault(key, now)
                # Doors are only ever closed on a car this bot has just loaded.
                # Open doors with no load pressed yet means the car is back and
                # waiting to board: closing them would undo the opening.
                if key in self.load_kind:
                    self._close_doors(
                        f"Elevator {i}", key, now, lift.loaded_is_full,
                        lift.waiting_is_zero, lift.toggle_doors,
                    )
                    continue

            # Nobody aboard. Is there anyone to take?
            if lift.waiting_is_zero or not empty:
                self.holding.pop(key, None)
                continue
            held = now - self.holding.setdefault(key, now)

            # Load does nothing with the doors shut. Open them as soon as anyone is
            # waiting, so the car can board the moment the rule below says go.
            if lift.doors_are_closed:
                if not self._pending(f"{key}.doors", now) and lift.toggle_doors():
                    self.in_flight[f"{key}.doors"] = now
                    self._say(f"Elevator {i}", "open doors", "guests waiting; Load needs the doors open")
                continue
            if lift.is_loading or self._pending(f"{key}.load", now):
                continue

            if lift.waiting_is_full:
                reason, kind = "21 waiting: a full car", "full"
            else:
                starved = not any(next_up(j) for j in game.elevators if j != i)
                patience = ELEVATOR_PATIENCE_WHEN_STARVED if starved else ELEVATOR_PATIENCE
                if held < patience:
                    # Say so once, so a deliberate pause never looks like a stall.
                    if self.hold_said.get(key) != self.holding[key]:
                        self.hold_said[key] = self.holding[key]
                        self._say(
                            f"Elevator {i}", "hold",
                            f"fewer than 21 waiting; up to {patience:.0f}s for a fuller car",
                        )
                    continue
                reason = (
                    "no other car is ready for the track, so load who is here"
                    if starved
                    else f"held {held:.0f}s for a full car; load who is here"
                )
                kind = "partial"

            if lift.load():
                self.in_flight[f"{key}.load"] = now
                self.load_kind[key] = kind
                self.open_since[key] = now  # boarding starts now
                self.holding.pop(key, None)
                self.stats[f"elevator loads ({kind})"] += 1
                self._say(f"Elevator {i}", "load", reason)

    # -- rules 3 and 4: fill the TV rooms, and never keep a cue waiting -------
    def run_tv_rooms(self, game: Game, now: float) -> None:
        def working(room) -> bool:
            return (
                room.is_loading
                or not room.entrance_is_closed
                or room.preshow is not PreshowState.IDLE
                or room.unload_button is not UnloadState.IDLE
                or self._pending(f"{room.prefix}.load", now)
            )

        # Nothing downstream has anything to do: no elevator has people waiting,
        # boarding, or ready to go. Only then is a part-filled room worth sending.
        elevators_idle = all(
            lift.waiting_is_zero
            and not lift.is_loading
            and not self._ready_to_go(lift)
            and lift.prefix not in self.load_kind
            for lift in game.elevators.values()
            if lift.enabled
        )

        for i, room in game.tv_rooms.items():
            key = room.prefix
            if not room.enabled:
                self.holding.pop(key, None)
                continue

            # Start Preshow lights up while the entrance is still swinging shut,
            # but does nothing until it has.
            if room.preshow_ready:
                if room.entrance_is_closed and room.start_preshow():
                    self.stats["preshows"] += 1
                    self._say(f"TV Room {i}", "start preshow", "Start Preshow is orange, entrance shut")
                continue
            # Only one room can unload at a time: the other's Unload stays orange
            # until the first has finished, and pressing it meanwhile does nothing.
            if room.unload_ready:
                busy = any(
                    r.is_unloading or r.exit_is_open or r.exit_is_moving
                    for j, r in game.tv_rooms.items()
                    if j != i
                )
                if not busy and room.unload():
                    self._say(f"TV Room {i}", "unload", "Unload turned orange")
                continue

            if room.entrance_is_open:
                self.open_since.setdefault(key, now)
                self._close_doors(
                    f"TV Room {i}", key, now, room.loaded_is_full,
                    room.waiting_is_zero, room.toggle_entrance,
                )
                continue
            if room.entrance_is_moving:
                continue
            self.open_since.pop(key, None)

            if room.is_loading or room.preshow_running:
                self.in_flight.pop(f"{key}.load", None)
            idle = (
                room.can_load
                and room.preshow is PreshowState.IDLE
                and room.unload_button is UnloadState.IDLE
                and room.exit_is_closed
                and room.loaded_is_zero
                and not self._pending(f"{key}.load", now)
            )
            if not idle or room.waiting_is_zero:
                self.holding.pop(key, None)
                continue

            if room.waiting_is_full:
                reason, kind = "21 waiting: a full room", "full"
            else:
                starved = elevators_idle and not any(
                    working(r) for j, r in game.tv_rooms.items() if j != i
                )
                patience = TV_ROOM_PATIENCE_WHEN_STARVED if starved else TV_ROOM_PATIENCE
                held = now - self.holding.setdefault(key, now)
                if held < patience:
                    continue
                reason = (
                    "the rest of the ride is idle, so load who is here"
                    if starved
                    else f"held {held:.0f}s for a full room; load who is here"
                )
                kind = "partial"

            if room.load():
                self.in_flight[f"{key}.load"] = now
                self.load_kind[key] = kind
                self.holding.pop(key, None)
                self.stats[f"TV room loads ({kind})"] += 1
                self._say(f"TV Room {i}", "load", reason)

    # -- closing doors on a part-filled room or car ----------------------------
    def _close_doors(
        self,
        unit: str,
        key: str,
        now: float,
        full: bool,
        nobody_waiting: bool,
        press: Callable[[], bool],
    ) -> None:
        """Load takes only the people waiting when it is pressed. At 21 the doors
        shut by themselves; below that they stay open until closed by hand, and
        until then the preshow cannot start or the car cannot be dispatched.

        Waiting and Loaded move together as each person walks in, so Waiting
        reading 0 means everyone is in. New arrivals can keep it above 0, so
        after BOARDING_TIME the doors are closed regardless.
        """
        if full:
            return  # 21 in: it shuts on its own
        open_for = now - self.open_since[key]
        if self.load_kind.get(key) == "full":
            # It was loaded from a full queue and should fill and shut itself.
            if open_for < FULL_LOAD_GRACE:
                return
            why = f"still open {open_for:.0f}s after a full load"
        elif nobody_waiting:
            why = "everyone waiting is in; not a full 21"
        elif open_for >= BOARDING_TIME:
            why = f"boarded for {open_for:.0f}s; the rest arrived too late"
        else:
            return
        if press():
            self._say(unit, "close doors", why)

    def _explain_if_stuck(self, game: Game, i: int, now: float) -> None:
        """If a car has people waiting and nothing on it has changed for a while,
        print everything the bot sees on it, once, so a stall can be diagnosed."""
        lift = game.elevators[i]
        sig = (lift.enable_button, lift.doors, lift.load_button, lift.dispatch_button)
        seen = self.car_seen.get(i)
        if seen is None or seen[0] != sig or lift.waiting_is_zero:
            self.car_seen[i] = (sig, now, False)
            return
        _, since, told = seen
        if told or now - since < ELEVATOR_STUCK_AFTER:
            return
        self.car_seen[i] = (sig, since, True)
        self.log(
            f"\n!! Elevator {i} has had people waiting for {now - since:.0f}s with no change. "
            f"The bot sees:\n"
            f"   enable={lift.enable_button.name} doors={lift.doors.name} "
            f"load={lift.load_button.name} dispatch={lift.dispatch_button.name}\n"
            f"   waiting={lift.waiting.name} loaded={lift.loaded.name} "
            f"track={game.track.state.name} out_on_ride={self.on_track == i} "
            f"loading_now={self.load_kind.get(lift.prefix)} "
            f"enable_presses={self.enable_presses[lift.prefix]}\n"
            f"   Copy these lines to whoever maintains the bot.\n"
        )

    # -- plumbing -------------------------------------------------------------
    def _pending(self, key: str, now: float) -> bool:
        """Was this pressed so recently that the frame may not show it yet?"""
        t = self.in_flight.get(key)
        if t is None:
            return False
        if now - t >= IN_FLIGHT:
            del self.in_flight[key]
            return False
        return True

    def _watch_for_stall(self, game: Game, now: float) -> None:
        snapshot = game.describe()
        if snapshot != self._last_snapshot:
            self._last_snapshot, self._last_change = snapshot, now
            self._warned_stall = False
            return
        if (
            not self._warned_stall
            and now - self._last_change >= STALL_WARNING
            and game.control.attraction_is_active
        ):
            self._warned_stall = True
            self.log(
                f"\n!! Nothing on the panel has changed for {STALL_WARNING:.0f}s. "
                f"If guests are waiting, this is a state the rules do not cover:\n   "
                + "\n   ".join(snapshot)
                + "\n"
            )

    def _say(self, unit: str, action: str, why: str) -> None:
        self.log(f"{self.clock() - self.started:7.1f}s  {unit:<16} {action:<14} {why}")

    def summary(self) -> str:
        elapsed = self.clock() - self.started
        lines = [f"Played {elapsed / 60:.1f} real minutes."]
        lines += [f"  {k:<26} {v}" for k, v in sorted(self.stats.items())]
        return "\n".join(lines)


def _name(prefix: str) -> str:
    """``elevator2`` -> ``Elevator 2``, ``tv_room1`` -> ``TV Room 1``."""
    if prefix.startswith("tv_room"):
        return f"TV Room {prefix[-1]}"
    return f"Elevator {prefix[-1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Play Tower of Terror.")
    ap.add_argument("--url", default=URL, help="the game page (default: the Simulation Page)")
    ap.add_argument(
        "--click-interval",
        type=float,
        default=0.0,
        help="minimum seconds between any two clicks; 0.4 is about human speed",
    )
    ap.add_argument("--mute", action="store_true", help="play with the game's sound off")
    args = ap.parse_args()

    game = Game.launch(
        url=args.url,
        headless=False,
        mute=args.mute,
        # "raise" stops the run on a fill we have not seen before. There is no
        # "guess" mode on purpose: a misread colour is a wrong decision.
        on_unknown="raise",
        min_click_interval=args.click_interval,
        # How long a button may go unanswered before the bot presses it again.
        reclick_after=1.0,
    )
    strategy = Strategy()

    with game:
        print("Load the game and pick your mode. I start when I can see the panel.")
        if not game.wait_for_panel(timeout=600):
            print("No RideControl panel after 10 minutes — giving up.")
            print(game.backend.describe_page())
            return

        print(f"Panel found ({game.layout.region.w}x{game.layout.region.h} crop). Playing.\n")
        try:
            while game.refresh():
                strategy.play(game)
        except RuffleCrashed as exc:
            print(f"\n{exc}\nReload the page and run again.")
        except KeyboardInterrupt:
            pass

        if not game.panel_visible:
            print("\nThe panel went away — the game is over, or the emulator dropped it.")
        print("\n" + strategy.summary())
        save_result(game, strategy)

        # Leave the window up: the final score is on screen, and closing the
        # browser here would take it away before anyone could read it.
        try:
            input("\nThe browser stays open so you can read the score. Press Enter to close it.")
        except (EOFError, KeyboardInterrupt):
            pass


def save_result(game: Game, strategy: Strategy) -> None:
    """Keep a screenshot of the final screen, score and all, and the run's
    stats beside it, so runs can be compared later."""
    from PIL import Image

    RUNS.mkdir(exist_ok=True)
    stem = RUNS / time.strftime("%Y-%m-%d_%H-%M-%S")
    try:
        game.backend.set_region(None)  # the whole window, not just the panel
        Image.fromarray(game.backend.grab()).save(stem.with_suffix(".png"))
    except Exception as exc:  # a screenshot is a nice-to-have, never a crash
        print(f"Could not save a screenshot: {exc}")
    stem.with_suffix(".txt").write_text(strategy.summary() + "\n")
    print(f"Saved {stem.with_suffix('.png')} and {stem.with_suffix('.txt')}")


if __name__ == "__main__":
    main()
