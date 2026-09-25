"""The game object: one frozen frame, and everything readable off it.

``refresh()`` takes exactly one screenshot and reads all 32 buttons from it. Every
property then reads that frame until the next refresh, so a condition like
``lift.can_dispatch and not game.track.is_locked`` is guaranteed to describe a
single instant rather than two moments a few milliseconds apart.

A refresh measured end to end is about 52 ms, and almost none of it is here:

    wait for a painted frame    ~25 ms   the compositor's cadence
    decode the PNG              ~27 ms   a full window, because clipping the
                                         capture makes a headed page flash
    read all 32 buttons          0.05 ms  288 probe pixels, one vectorised vote
    a counter                    0.3 ms   lazy — only the ones you ask for

The counters are lazy on purpose. A tick that only looks at button colours never
segments a single digit.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import numpy as np

from .backends import DEFAULT_URL, Backend, ImageBackend, PlaywrightBackend
from .colors import PALETTE, Color, modal_rgb, vote
from .components import Elevator, RideControl, Track, TVRoom
from .counters import Count, CounterReader
from .errors import PanelError, UnknownColorError, UnmappedStateError
from .geometry import Layout, build_layout
from .states import meanings

#: A probe grid is 9 points. Fewer than this many agreeing means the panel has
#: moved out from under them, not that the button is an odd colour.
_MIN_VOTES = 3


class Game:
    """The whole panel, as of the last ``refresh()``."""

    def __init__(
        self,
        backend: Backend,
        on_unknown: str = "raise",
        min_click_interval: float = 0.0,
        reclick_after: float = 1.0,
        reader: CounterReader | None = None,
    ):
        if on_unknown not in ("raise", "ignore"):
            raise ValueError("on_unknown must be 'raise' or 'ignore'")
        self.backend = backend
        self.on_unknown = on_unknown
        self.min_click_interval = min_click_interval
        self.reclick_after = reclick_after
        self._reader = reader
        self._acted: dict[str, float] = {}
        self._layout: Layout | None = None
        self._frame: np.ndarray | None = None
        self._states: dict[str, Any] = {}
        self._counts: dict[str, Count] = {}
        self._last_click = 0.0

        self.tv_rooms = {i: TVRoom(self, i) for i in (1, 2)}
        self.elevators = {i: Elevator(self, i) for i in (1, 2, 3)}
        self.tv_room1, self.tv_room2 = self.tv_rooms[1], self.tv_rooms[2]
        self.elevator1, self.elevator2, self.elevator3 = (self.elevators[i] for i in (1, 2, 3))
        self.control = RideControl(self)
        self.track = Track(self)

    # -- construction ---------------------------------------------------------
    @classmethod
    def launch(cls, url: str = DEFAULT_URL, headless: bool = False, **kw: Any) -> Game:
        """Open Chromium and load the page. Nothing is clicked.

        The game is yours to start: this returns as soon as the page is open, and
        ``wait_for_panel()`` is what blocks until you have picked a mode.
        """
        backend_kw = {
            k: kw.pop(k)
            for k in ("viewport", "timeout_ms", "frame_wait_ms", "ruffle_config", "debug_dir", "mute")
            if k in kw
        }
        return cls(PlaywrightBackend(url=url, headless=headless, **backend_kw), **kw)

    @classmethod
    def from_image(cls, path: str, **kw: Any) -> Game:
        """A game backed by a still PNG. Used by the fixture tests."""
        g = cls(ImageBackend(path), **kw)
        g.refresh()
        return g

    @property
    def reader(self) -> CounterReader:
        """Built on first use, so a run that never reads a counter never loads
        the template set."""
        if self._reader is None:
            self._reader = CounterReader()
        return self._reader

    # -- the frame ------------------------------------------------------------
    @property
    def panel_visible(self) -> bool:
        """Was the RideControl panel there as of the last refresh?"""
        return self._layout is not None

    @property
    def frame(self) -> np.ndarray:
        if self._frame is None:
            self.refresh()
        assert self._frame is not None
        return self._frame

    @property
    def layout(self) -> Layout:
        if self._layout is None:
            raise PanelError("the panel has not been located; call refresh() first")
        return self._layout

    def refresh(self) -> bool:
        """Take one frame and read the panel from it.

        Returns True if the panel was there. Locating it happens at most once:
        after that the frame arrives already cropped to the panel and the read is
        288 pixel lookups.
        """
        self._counts.clear()
        if self._layout is not None:
            self._frame = self.backend.grab()
            if self._read_buttons(strict=False):
                return True
            # The probes stopped landing on palette colours. That is either drift
            # — the window was resized under them — or the game has left the
            # playing field. Both are answered by looking for the panel again.
            self._layout = None
            self.backend.set_region(None)
        full = self.backend.grab()
        try:
            layout = build_layout(full)
        except PanelError:
            self._frame = full
            return False
        self.backend.set_region(layout.region)
        self._layout = layout
        # Crop the frame already in hand rather than asking for another: a frame
        # costs a PNG decode, and this one is as good as its successor.
        r = layout.region
        self._frame = full[r.y : r.bottom, r.x : r.right]
        # The panel is where this frame says it is, so a probe that still reads
        # nothing is a fill the calibration has never seen — not drift.
        return self._read_buttons(strict=True)

    def _read_buttons(self, strict: bool) -> bool:
        """Read all 32 colours in one shot.

        Returns False if a probe grid did not settle on a palette colour. When
        ``strict``, the layout was just rebuilt from this very frame, so that can
        only mean an unknown fill — which raises rather than being read as "the
        panel is gone".
        """
        assert self._layout is not None and self._frame is not None
        lay = self._layout
        try:
            idx, votes = vote(self._frame, lay.probe_y, lay.probe_x)
        except IndexError:  # the crop came back the wrong size
            return False
        if int(votes.min()) < _MIN_VOTES:
            if strict and self.on_unknown == "raise":
                bad = int(np.argmin(votes))
                rgb = modal_rgb(self._frame, lay.probe_y[bad], lay.probe_x[bad])
                raise UnknownColorError(lay.keys[bad], rgb, int(votes[bad]))
            return False

        for key, i in zip(lay.keys, idx.tolist()):
            color = PALETTE[i]
            state = meanings(key).get(color)
            if state is None:
                raise UnmappedStateError(
                    key, color.name, [c.name for c in meanings(key)]
                )
            if self._states.get(key) is not state:
                # The panel has answered for this button, so a press is allowed
                # again — see _click.
                self._acted.pop(key, None)
            self._states[key] = state
        return True

    def wait_for_panel(self, timeout: float = 600.0, poll: float = 1.0) -> bool:
        """Block until the RideControl panel is on screen.

        This is the bot's whole startup sequence. Load the page, pick your game
        mode, and the moment the panel appears the game has begun and this
        returns. Nothing is clicked while waiting.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.refresh():
                return True
            check = getattr(self.backend, "check", None)
            if check is not None:
                check()
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    # -- reading --------------------------------------------------------------
    def _require_panel(self) -> None:
        if self._layout is None:
            raise PanelError(
                "the RideControl panel is not on screen, so panel readings would be "
                "meaningless. Check game.panel_visible before reading."
            )

    def _state(self, key: str) -> Any:
        self._require_panel()
        return self._states[key]

    def _count(self, key: str) -> Count:
        """A counter box, read on demand and cached until the next refresh."""
        self._require_panel()
        hit = self._counts.get(key)
        if hit is None:
            rect = self.layout.counters.get(key)
            if rect is None:
                raise PanelError(f"no counter box was found for {key}")
            hit = self._counts[key] = self.reader.read(self.frame, rect)
        return hit

    def color(self, key: str) -> Color:
        """The fill of one button, for debugging a surprising state."""
        self._require_panel()
        lay = self.layout
        i = lay.keys.index(key)
        return Color(modal_rgb(self.frame, lay.probe_y[i], lay.probe_x[i]))

    # -- acting ---------------------------------------------------------------
    def _click(self, key: str) -> bool:
        """Press a button, unless it has not yet answered the last press.

        Returns False when the press was suppressed. A frame is a picture of the
        past — it was painted before the last click reached the game — so a loop
        that re-decides every 40 ms will keep pressing on the strength of a
        reading that predates its own last action. On a toggle like Attraction
        that means opening and closing the ride on alternate frames forever.

        So a button is not pressed again while it still looks exactly as it did
        when it was last pressed. The moment its colour moves, the press is
        allowed again. ``reclick_after`` bounds the wait, so a click the game
        simply dropped is retried rather than deadlocking that button; set it to
        0 to turn the guard off entirely.
        """
        self._require_panel()
        if self.reclick_after and key in self._acted:
            if time.monotonic() - self._acted[key] < self.reclick_after:
                return False
        rect = self.layout.buttons[key]
        wait = self.min_click_interval - (time.monotonic() - self._last_click)
        if wait > 0:
            time.sleep(wait)
        self.backend.click(int(rect.cx), int(rect.cy))
        self._last_click = time.monotonic()
        self._acted[key] = self._last_click
        return True

    # -- waiting --------------------------------------------------------------
    def wait_until(
        self, predicate: Callable[[], bool], timeout: float = 30.0, poll: float = 0.0
    ) -> bool:
        """Refresh and poll until ``predicate()`` is true, or ``timeout`` elapses.

        Returns True if it came true, False on timeout — it never raises, so a
        miscalibrated colour surfaces as a False you can branch on rather than
        hanging the run forever. The default poll is 0: a screenshot already takes
        ~20 ms, which is a fast enough loop that sleeping on top of it only adds
        latency to the cue you are waiting for.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.refresh() and predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            if poll:
                time.sleep(poll)

    # -- inspection -----------------------------------------------------------
    def describe(self) -> list[str]:
        """Every button state from the current frame, one line each."""
        self._require_panel()
        return [f"{key} = {state.name}" for key, state in self._states.items()]

    def close(self) -> None:
        self.backend.close()

    def __enter__(self) -> Game:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
