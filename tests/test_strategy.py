"""The strategy in ``main.py``, run against the calibration screenshots.

Each test loads a real frame, plays one or more ticks, and checks which buttons
were pressed. The still-image backend records clicks instead of delivering them,
so a press is identified by which button's centre it landed on.
"""

from __future__ import annotations

from pathlib import Path

from main import Strategy
from tot import (
    Count, DispatchState, DoorState, EnableState, Game, LoadState, PreshowState, TrackState,
    UnloadState,
)

SHOTS = Path(__file__).resolve().parents[1] / "assets" / "screenshots"
CLOSED = SHOTS / "layout/00-attraction-closed-all-gray-153058.png"
PRESHOW_READY = SHOTS / "layout/02-tvr1-loaded-preshow-ready-153147.png"
PRESHOW_ACTIVE = SHOTS / "states/03-preshow-active-153205.png"
UNLOAD_READY = SHOTS / "states/04-preshow-done-unload-ready-153222.png"
ELEV_WAITING = SHOTS / "states/06-elev1-enabled-doors-closed-153255.png"
ELEV_LOADING = SHOTS / "states/08-elev1-doors-open-loading-153320.png"
ARMED = SHOTS / "states/10-dispatch-armed-doors-closed-153334.png"
TRACK_LOCKED = SHOTS / "states/11-dispatched-track-blocked-153341.png"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def setup(frame: Path) -> tuple[Game, Strategy, Clock]:
    clock = Clock()
    return Game.from_image(frame), Strategy(log=lambda _: None, clock=clock), clock


def pressed(game: Game) -> list[str]:
    """The layout key of every button clicked so far, in order."""
    r = game.layout.region
    at = {
        (int(b.cx) + r.x, int(b.cy) + r.y): key for key, b in game.layout.buttons.items()
    }
    return [at[c] for c in game.backend.clicks]


def test_a_closed_ride_is_opened_and_nothing_else_is_touched() -> None:
    g, s, _ = setup(CLOSED)
    s.play(g)
    assert pressed(g) == ["control.attraction"]


def test_every_unit_is_put_into_service() -> None:
    g, s, _ = setup(PRESHOW_READY)
    s.play(g)
    for key in ("tv_room2.enable", "elevator2.enable", "elevator3.enable"):
        assert key in pressed(g)
    assert "tv_room1.enable" not in pressed(g)  # already on: pressing would disable it


def test_start_preshow_is_pressed_the_moment_it_turns_orange() -> None:
    g, s, _ = setup(PRESHOW_READY)
    s.play(g)
    assert "tv_room1.preshow" in pressed(g)
    assert "tv_room1.load" not in pressed(g)


def test_a_room_mid_preshow_is_not_loaded_even_though_load_is_white() -> None:
    g, s, clock = setup(PRESHOW_ACTIVE)
    assert g.tv_room1.can_load and g.tv_room1.preshow_running
    s.play(g)
    clock.t += 60
    s.play(g)
    assert not {"tv_room1.load", "tv_room1.preshow", "tv_room1.unload"} & set(pressed(g))


def test_unload_is_pressed_the_moment_it_turns_orange() -> None:
    g, s, _ = setup(UNLOAD_READY)
    s.play(g)
    assert "tv_room1.unload" in pressed(g)


def test_an_armed_car_is_dispatched_onto_a_ready_track() -> None:
    g, s, _ = setup(ARMED)
    s.play(g)
    assert "elevator1.dispatch" in pressed(g)
    assert "elevator1.load" not in pressed(g)  # Load is white here, but the car is full


def test_nothing_is_dispatched_onto_a_locked_track() -> None:
    g, s, _ = setup(TRACK_LOCKED)
    s.play(g)
    assert not any(k.endswith(".dispatch") for k in pressed(g))


def test_a_loading_car_is_left_alone() -> None:
    g, s, _ = setup(ELEV_LOADING)
    s.play(g)
    assert not {"elevator1.load", "elevator1.dispatch"} & set(pressed(g))


def test_a_car_with_guests_waiting_has_its_doors_opened_before_load() -> None:
    g, s, _ = setup(ELEV_WAITING)  # 5 waiting, doors shut
    assert g.elevator1.doors_are_closed
    s.play(g)
    assert "elevator1.doors" in pressed(g)
    assert "elevator1.load" not in pressed(g)


def test_the_doors_are_pressed_once_even_when_the_frame_is_stale() -> None:
    g, s, clock = setup(ELEV_WAITING)
    s.play(g)
    clock.t += 0.5
    g._acted.clear()  # take the library's own guard out, to test the strategy's
    s.play(g)
    assert pressed(g).count("elevator1.doors") == 1


def test_a_partial_car_holds_briefly_then_goes_when_the_track_would_starve() -> None:
    g, s, clock = setup(ELEV_WAITING)  # 5 waiting, nothing else could use the track
    assert not g.elevator1.waiting_is_zero and not g.elevator1.waiting_is_full
    g._states["elevator1.doors"] = DoorState.OPEN
    s.play(g)
    assert "elevator1.load" not in pressed(g)
    clock.t += 6.0
    s.play(g)
    assert "elevator1.load" in pressed(g)


def test_a_partial_car_waits_longer_when_another_car_is_next_up() -> None:
    g, s, clock = setup(ELEV_WAITING)
    g._states["elevator1.doors"] = DoorState.OPEN
    _arm(g, 2)
    s.play(g)
    clock.t += 6.0
    s.play(g)
    assert "elevator1.load" not in pressed(g)
    clock.t += 20.0
    s.play(g)
    assert "elevator1.load" in pressed(g)


def test_one_dispatch_per_track_even_when_the_frame_is_stale() -> None:
    g, s, clock = setup(ARMED)
    _arm(g, 2)
    s.play(g)
    clock.t += 0.1
    s.play(g)  # same frame: the track still looks ready
    dispatches = [k for k in pressed(g) if k.endswith(".dispatch")]
    assert dispatches == ["elevator1.dispatch"]


def _boarded(
    g: Game, s: Strategy, clock: Clock, unit: str, door: str, kind: str,
    loaded: Count, waiting: Count, secs: float = 1.0,
) -> None:
    """``unit`` was loaded ``secs`` ago and its door is open."""
    s.load_kind[unit] = kind
    s.open_since[unit] = clock.t - secs
    g._states[f"{unit}.{door}"] = DoorState.OPEN
    g._counts[f"{unit}.loaded"] = loaded
    g._counts[f"{unit}.waiting"] = waiting


def _tv_room1_boarding(g: Game) -> None:
    g._states["tv_room1.preshow"] = PreshowState.IDLE
    g._states["tv_room1.load"] = LoadState.PROMPTED


def test_a_part_filled_tv_room_is_closed_once_everyone_waiting_is_in() -> None:
    g, s, clock = setup(PRESHOW_ACTIVE)
    _tv_room1_boarding(g)
    _boarded(g, s, clock, "tv_room1", "entrance", "partial", Count.OTHER, Count.ZERO)
    s.play(g)
    assert "tv_room1.entrance" in pressed(g)


def test_a_part_filled_tv_room_waits_while_people_are_still_walking_in() -> None:
    g, s, clock = setup(PRESHOW_ACTIVE)
    _tv_room1_boarding(g)
    _boarded(g, s, clock, "tv_room1", "entrance", "partial", Count.OTHER, Count.OTHER)
    s.play(g)
    assert "tv_room1.entrance" not in pressed(g)
    clock.t += 4.0  # the rest are new arrivals who missed this load
    s.play(g)
    assert "tv_room1.entrance" in pressed(g)


def test_a_full_tv_room_is_left_to_close_its_own_entrance() -> None:
    g, s, clock = setup(PRESHOW_ACTIVE)
    _tv_room1_boarding(g)
    _boarded(g, s, clock, "tv_room1", "entrance", "full", Count.FULL, Count.ZERO)
    s.play(g)
    clock.t += 30
    s.play(g)
    assert "tv_room1.entrance" not in pressed(g)


def test_start_preshow_waits_for_the_entrance_to_finish_closing() -> None:
    g, s, _ = setup(PRESHOW_READY)
    g._states["tv_room1.entrance"] = DoorState.MOVING
    s.play(g)
    assert "tv_room1.preshow" not in pressed(g)


def test_only_one_tv_room_unloads_at_a_time() -> None:
    g, s, _ = setup(UNLOAD_READY)
    g._states["tv_room2.enable"] = EnableState.ENABLED
    g._states["tv_room2.unload"] = UnloadState.UNLOADING
    g._states["tv_room2.exit"] = DoorState.OPEN
    s.play(g)
    assert "tv_room1.unload" not in pressed(g)


def test_load_elevator_is_pressed_when_it_turns_orange_with_the_doors_open() -> None:
    # The live panel: doors open, Load Elevator orange, a full queue.
    g, s, _ = setup(ELEV_WAITING)
    g._states["elevator1.doors"] = DoorState.OPEN
    g._states["elevator1.load"] = LoadState.PROMPTED
    g._counts["elevator1.waiting"] = Count.FULL
    s.play(g)
    assert "elevator1.load" in pressed(g)


def test_a_part_filled_car_has_its_doors_closed_by_hand() -> None:
    g, s, clock = setup(ELEV_LOADING)
    _boarded(g, s, clock, "elevator1", "doors", "partial", Count.OTHER, Count.ZERO)
    s.play(g)
    assert "elevator1.doors" in pressed(g)
    assert "elevator1.load" not in pressed(g)


def test_a_full_car_is_left_to_close_its_own_doors() -> None:
    g, s, clock = setup(ELEV_LOADING)
    _boarded(g, s, clock, "elevator1", "doors", "full", Count.OTHER, Count.OTHER)
    s.play(g)
    assert "elevator1.doors" not in pressed(g)
    clock.t += 10  # ...unless it never does
    s.play(g)
    assert "elevator1.doors" in pressed(g)


def test_a_car_back_from_its_ride_is_opened_and_loaded_again() -> None:
    # Dispatch stays orange after the ride; the track is ready again.
    g, s, _ = setup(ELEV_WAITING)
    g._states["elevator1.dispatch"] = DispatchState.IN_MOTION
    s.play(g)
    assert "elevator1.doors" in pressed(g)


def test_the_car_out_on_the_track_is_left_alone() -> None:
    g, s, clock = setup(ARMED)
    s.play(g)  # dispatches elevator 1
    g._states["elevator1.dispatch"] = DispatchState.IN_MOTION
    g._states["control.track"] = TrackState.LOCKED
    g._counts["elevator1.loaded"] = Count.ZERO
    g._counts["elevator1.waiting"] = Count.FULL
    clock.t += 5
    s.play(g)
    assert "elevator1.doors" not in pressed(g)


def test_a_car_that_comes_back_with_its_doors_open_is_loaded() -> None:
    # The stall: after a ride the doors were open and Load orange, but the bot
    # still remembered the previous trip's load and never pressed it.
    g, s, clock = setup(ARMED)
    s.load_kind["elevator1"] = "full"
    s.open_since["elevator1"] = clock.t - 60
    s.play(g)  # armed: dispatches it
    assert "elevator1.dispatch" in pressed(g)

    clock.t += 40  # the ride is over
    g._states["elevator1.dispatch"] = DispatchState.IN_MOTION
    g._states["control.track"] = TrackState.READY
    g._states["elevator1.doors"] = DoorState.OPEN
    g._states["elevator1.load"] = LoadState.PROMPTED
    g._counts["elevator1.loaded"] = Count.ZERO
    g._counts["elevator1.waiting"] = Count.FULL
    s.play(g)
    assert "elevator1.load" in pressed(g)
    assert "elevator1.doors" not in pressed(g)


def test_an_open_car_with_orange_load_is_loaded_even_if_its_zero_is_misread() -> None:
    # Elevator 2's "0" is the reading that went wrong live. Orange Load with the
    # doors open says the car is empty, whatever the Loaded box looks like.
    g, s, _ = setup(ELEV_WAITING)
    g._states["elevator1.doors"] = DoorState.OPEN
    g._states["elevator1.load"] = LoadState.PROMPTED
    g._counts["elevator1.loaded"] = Count.OTHER
    g._counts["elevator1.waiting"] = Count.FULL
    s.play(g)
    assert "elevator1.load" in pressed(g)
    assert "elevator1.doors" not in pressed(g)


def test_a_hold_for_a_fuller_car_is_announced_once() -> None:
    said: list[str] = []
    clock = Clock()
    g = Game.from_image(ELEV_WAITING)
    s = Strategy(log=said.append, clock=clock)
    g._states["elevator1.doors"] = DoorState.OPEN
    s.play(g)
    clock.t += 1
    s.play(g)
    assert sum("hold" in line for line in said) == 1


def test_open_doors_are_never_closed_on_a_car_that_was_not_loaded() -> None:
    # Doors just opened, Load not orange yet, and the Loaded "0" misread.
    g, s, clock = setup(ELEV_WAITING)
    g._states["elevator1.doors"] = DoorState.OPEN
    g._counts["elevator1.loaded"] = Count.OTHER
    for _ in range(4):
        s.play(g)
        clock.t += 10
    assert "elevator1.doors" not in pressed(g)


def test_an_empty_car_with_green_dispatch_is_loaded_not_skipped() -> None:
    # Reported live: doors open, Load orange, Dispatch green, 21 waiting, 0 aboard.
    g, s, _ = setup(ELEV_WAITING)
    g._states["elevator1.doors"] = DoorState.OPEN
    g._states["elevator1.load"] = LoadState.PROMPTED
    g._states["elevator1.dispatch"] = DispatchState.ARMED
    g._states["control.track"] = TrackState.LOCKED
    g._counts["elevator1.waiting"] = Count.FULL
    s.play(g)
    assert "elevator1.load" in pressed(g)


def test_an_empty_car_is_never_dispatched() -> None:
    g, s, _ = setup(ELEV_WAITING)
    g._states["elevator1.dispatch"] = DispatchState.ARMED  # doors shut, 0 aboard
    g._counts["elevator1.waiting"] = Count.ZERO
    s.play(g)
    assert "elevator1.dispatch" not in pressed(g)


def _arm(g: Game, i: int) -> None:
    """Make elevator ``i`` read as enabled, doors closed, Dispatch green."""
    g._states[f"elevator{i}.enable"] = EnableState.ENABLED
    g._states[f"elevator{i}.doors"] = DoorState.CLOSED
    g._states[f"elevator{i}.dispatch"] = DispatchState.ARMED
    g._counts[f"elevator{i}.loaded"] = Count.FULL
