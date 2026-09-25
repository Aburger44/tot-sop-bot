"""The three families of control: TV rooms, elevators, and RideControl.

Every property maps to exactly one thing on screen — one button's colour, or one
counter box being empty or full. Nothing here combines two observables, because a
compound property would hide the decision that the written procedure is supposed
to make explicit.

The counters expose only ``_is_zero`` and ``_is_full``. Those are the two readings
that change what a player does: zero means there is nothing to move, 21 means a
full load is ready. Every other number means the same thing — wait — so the bot
does not spend a frame working out which one it is.

Actions click and nothing else: no precondition check, no waiting for animations,
no refresh afterwards. All of that belongs in the strategy file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .states import (
    AttractionState,
    DispatchState,
    DoorState,
    EnableState,
    LoadState,
    PreshowState,
    ToggleState,
    TrackState,
    UnloadState,
)

if TYPE_CHECKING:
    from .counters import Count
    from .game import Game


class _Unit:
    """Shared plumbing for a component that owns buttons and counters."""

    def __init__(self, game: Game, prefix: str):
        self.game, self.prefix = game, prefix

    def _state(self, name: str) -> Any:
        return self.game._state(f"{self.prefix}.{name}")

    def _count(self, name: str) -> Count:
        return self.game._count(f"{self.prefix}.{name}")

    def _press(self, name: str) -> bool:
        """True if the button was pressed, False if the panel has not yet
        answered the last press of it (see ``Game._click``)."""
        return self.game._click(f"{self.prefix}.{name}")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.prefix}>"


class _Queued(_Unit):
    """A unit with a Waiting and a Loaded box."""

    @property
    def waiting(self) -> Count:
        """This unit's Waiting box: ZERO, FULL, or OTHER."""
        return self._count("waiting")

    @property
    def loaded(self) -> Count:
        """This unit's Loaded box: ZERO, FULL, or OTHER."""
        return self._count("loaded")

    @property
    def waiting_is_zero(self) -> bool:
        """Nobody is queued for this unit."""
        return self._count("waiting").is_zero

    @property
    def waiting_is_full(self) -> bool:
        """21 queued — a full load is available."""
        return self._count("waiting").is_full

    @property
    def loaded_is_zero(self) -> bool:
        """This unit is empty."""
        return self._count("loaded").is_zero

    @property
    def loaded_is_full(self) -> bool:
        """21 aboard — this unit will take no more."""
        return self._count("loaded").is_full


class TVRoom(_Queued):
    """One of the two TV rooms: its queue, its doors, and its preshow."""

    def __init__(self, game: Game, index: int):
        super().__init__(game, f"tv_room{index}")
        self.index = index

    # -- buttons --------------------------------------------------------------
    @property
    def load_button(self) -> LoadState:
        return self._state("load")

    @property
    def unload_button(self) -> UnloadState:
        return self._state("unload")

    @property
    def entrance(self) -> DoorState:
        return self._state("entrance")

    @property
    def exit(self) -> DoorState:
        return self._state("exit")

    @property
    def enable_button(self) -> EnableState:
        return self._state("enable")

    @property
    def preshow(self) -> PreshowState:
        return self._state("preshow")

    # -- boolean aliases, one observable each ---------------------------------
    @property
    def can_load(self) -> bool:
        """Load TV Room is white — available to press."""
        return self.load_button is LoadState.READY

    @property
    def is_loading(self) -> bool:
        """Load TV Room is green — guests are moving in."""
        return self.load_button is LoadState.LOADING

    @property
    def unload_ready(self) -> bool:
        """Unload TV Room is orange — the cue to unload."""
        return self.unload_button is UnloadState.READY

    @property
    def is_unloading(self) -> bool:
        return self.unload_button is UnloadState.UNLOADING

    @property
    def entrance_is_closed(self) -> bool:
        return self.entrance is DoorState.CLOSED

    @property
    def entrance_is_moving(self) -> bool:
        return self.entrance is DoorState.MOVING

    @property
    def entrance_is_open(self) -> bool:
        return self.entrance is DoorState.OPEN

    @property
    def exit_is_closed(self) -> bool:
        return self.exit is DoorState.CLOSED

    @property
    def exit_is_moving(self) -> bool:
        return self.exit is DoorState.MOVING

    @property
    def exit_is_open(self) -> bool:
        return self.exit is DoorState.OPEN

    @property
    def enabled(self) -> bool:
        return self.enable_button is EnableState.ENABLED

    @property
    def preshow_ready(self) -> bool:
        """Start Preshow is orange — the room is loaded and the cue is live."""
        return self.preshow is PreshowState.READY

    @property
    def preshow_running(self) -> bool:
        """The button reads Preshow Active."""
        return self.preshow is PreshowState.RUNNING

    # -- actions --------------------------------------------------------------
    def load(self) -> bool:
        return self._press("load")

    def unload(self) -> bool:
        return self._press("unload")

    def start_preshow(self) -> bool:
        return self._press("preshow")

    def toggle_enabled(self) -> bool:
        return self._press("enable")

    def toggle_entrance(self) -> bool:
        """Close (or open) the entrance by hand. A room that reaches 21 closes it
        on its own; a partly filled one needs this before the preshow can start."""
        return self._press("entrance")

    def toggle_exit(self) -> bool:
        return self._press("exit")


class Elevator(_Queued):
    """One of the three elevators: its queue, its doors, and its dispatch."""

    def __init__(self, game: Game, index: int):
        super().__init__(game, f"elevator{index}")
        self.index = index

    @property
    def enable_button(self) -> EnableState:
        return self._state("enable")

    @property
    def doors(self) -> DoorState:
        return self._state("doors")

    @property
    def load_button(self) -> LoadState:
        return self._state("load")

    @property
    def dispatch_button(self) -> DispatchState:
        return self._state("dispatch")

    @property
    def enabled(self) -> bool:
        return self.enable_button is EnableState.ENABLED

    @property
    def doors_are_closed(self) -> bool:
        return self.doors is DoorState.CLOSED

    @property
    def doors_are_moving(self) -> bool:
        return self.doors is DoorState.MOVING

    @property
    def doors_are_open(self) -> bool:
        return self.doors is DoorState.OPEN

    @property
    def can_load(self) -> bool:
        return self.load_button is LoadState.READY

    @property
    def is_loading(self) -> bool:
        return self.load_button is LoadState.LOADING

    @property
    def can_dispatch(self) -> bool:
        """Dispatch is bright green — this elevator can be sent right now."""
        return self.dispatch_button is DispatchState.ARMED

    @property
    def is_dispatched(self) -> bool:
        """Dispatch is bright orange — this elevator is out on the track."""
        return self.dispatch_button is DispatchState.IN_MOTION

    def load(self) -> bool:
        return self._press("load")

    def dispatch(self) -> bool:
        return self._press("dispatch")

    def toggle_enabled(self) -> bool:
        return self._press("enable")

    def toggle_doors(self) -> bool:
        return self._press("doors")


class Track(_Unit):
    """The shaft the elevators share."""

    def __init__(self, game: Game):
        super().__init__(game, "control")

    @property
    def state(self) -> TrackState:
        return self._state("track")

    @property
    def is_ready(self) -> bool:
        """The button reads Track Ready and is green."""
        return self.state is TrackState.READY

    @property
    def is_locked(self) -> bool:
        """The button reads Track Locked and is red. No elevator can be launched."""
        return self.state is TrackState.LOCKED


class RideControl(_Unit):
    """The global panel: the attraction switch, the six toggles, the queue counts."""

    def __init__(self, game: Game):
        super().__init__(game, "control")

    @property
    def front_waiting(self) -> Count:
        return self._count("front_waiting")

    @property
    def back_waiting(self) -> Count:
        return self._count("back_waiting")

    @property
    def front_queue_is_full(self) -> bool:
        """21 in the front queue."""
        return self._count("front_waiting").is_full

    @property
    def back_queue_is_full(self) -> bool:
        """21 in the back queue."""
        return self._count("back_waiting").is_full

    @property
    def attraction(self) -> AttractionState:
        return self._state("attraction")

    @property
    def attraction_is_active(self) -> bool:
        return self.attraction is AttractionState.ACTIVE

    @property
    def attraction_is_closed(self) -> bool:
        return self.attraction is AttractionState.CLOSED

    @property
    def automatic_doors(self) -> ToggleState:
        return self._state("automatic_doors")

    @property
    def automatic_doors_on(self) -> bool:
        return self.automatic_doors is ToggleState.ON

    @property
    def daytime(self) -> ToggleState:
        return self._state("daytime")

    @property
    def daytime_on(self) -> bool:
        return self.daytime is ToggleState.ON

    @property
    def ride_sfx(self) -> ToggleState:
        return self._state("ride_sfx")

    @property
    def tv_room_sound(self) -> ToggleState:
        return self._state("tv_room_sound")

    @property
    def bgm(self) -> ToggleState:
        return self._state("bgm")

    def toggle_attraction(self) -> bool:
        return self._press("attraction")

    def toggle_automatic_doors(self) -> bool:
        return self._press("automatic_doors")

    def toggle_daytime(self) -> bool:
        return self._press("daytime")

    def toggle_show_fullscreen(self) -> bool:
        return self._press("show_fullscreen")

    def toggle_ride_sfx(self) -> bool:
        return self._press("ride_sfx")

    def toggle_tv_room_sound(self) -> bool:
        return self._press("tv_room_sound")

    def toggle_bgm(self) -> bool:
        return self._press("bgm")
