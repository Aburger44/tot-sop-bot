# Tower of Terror — SOP bot

A bot that plays the [Tower of Terror simulation](https://www.themagical.nl/content/plugins/flash-emulator/flash-player.php?game=tower-of-terror)
so that its behaviour can be turned into a standard operating procedure a person
can follow.

The bot is the instrument, not the product. It reads the RideControl panel the way
a player does — button colours, and whether a counter says 0 or 21 — and holds
**no strategy at all**. Every decision lives in `main.py`, hand-written, so each
branch maps to one instruction a human can carry out.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Use

```bash
python main.py                       # play as fast as the bot can
python main.py --click-interval 0.4  # the same rules at roughly human speed
python main.py --url <arcade url>    # the timed, scored game
```

It opens the page and then **waits**. You load the game and pick the mode; it
touches nothing. The moment the RideControl panel appears on screen the game has
begun, and it starts playing.

```python
from tot import Game

game = Game.launch()      # opens Chromium. Clicks nothing.
game.wait_for_panel()     # blocks while you pick a game mode

while game.refresh():                       # one screenshot per tick
    if game.tv_room1.preshow_ready:         # Start Preshow has turned orange
        game.tv_room1.start_preshow()
    for lift in game.elevators.values():
        if lift.can_dispatch and not game.track.is_locked:
            lift.dispatch()
```

### The strategy

[`main.py`](main.py) plays by four rules, built on one observation: only one
elevator can be on the track at a time, so the track sets the throughput and
everything upstream exists to keep it fed.

1. **Never leave the track idle.** When it reads Track Ready, dispatch the car
   that has been armed longest.
2. **Always have the next car ready.** Load elevators while another is out.
3. **Send full cars.** Hold for 21 waiting, unless holding would starve the next
   stage, in which case go with whoever is there. Load only takes the people
   waiting at that moment: 21 closes the doors by itself, fewer leaves them open,
   so the bot closes them by hand (Entrance for a TV room, Doors for an elevator).
   An elevator's doors also have to be opened with Doors before Load works.
4. **Never keep a cue waiting.** Start Preshow and Unload are pressed the moment
   they turn orange.

Every decision is printed with its reason, so the run log reads as a draft of the
procedure:

```
   41.3s  Elevator 2       load           21 waiting: a full car
   44.0s  Elevator 1       dispatch       armed, doors closed, track ready
```

The patience timings are constants at the top of `main.py`. A full game is about
12 real minutes, so try one change per run and compare the scores.

## How it works

| | |
| --- | --- |
| **Sees** | Reads **only the RideControl panel** — about 930×290 px, an eighth of the canvas. No hardcoded coordinates: the panel is found from the pixels once, at startup, and that rectangle is all that gets decoded from then on. |
| **Stays out of the way** | Never clicks, scrolls or resizes on its own, and never asks the browser for a screenshot. Your view of the game does not move or flicker while it plays — see below. |
| **Classifies** | Eight exact flat fills. All 32 buttons are read in one vectorised majority vote over 288 probe pixels — 0.05 ms. |
| **Counts** | Only **0** and **21**. Every other value is `OTHER`, because every other value means the same thing: wait. Counters are read lazily, so a tick that only looks at colours never touches a digit. |
| **Acts** | Clicks and nothing else — no precondition checks, no waiting for animations. That is your job. The one thing it will not do is press a button the panel has not answered yet: see below. |

### What it costs

Measured with `python -m tools.benchmark`:

| | |
| --- | --- |
| locate the panel, full canvas | 77 ms — **once**, at startup |
| wait for a painted frame | ~14 ms — the compositor's cadence |
| decode it | ~27 ms |
| read all 32 buttons | 0.05 ms |
| read one counter | 0.3 ms |
| **a whole tick** | **~41 ms → ~24 Hz** |

The screenshot is 90% of that and is entirely the browser's. See
[`docs/design-decisions.md`](docs/design-decisions.md) §21 for why the bot is
still in Python.

### It never disturbs your view

The window is opened maximised and then left completely alone. It is not just
that the bot does not scroll — **it never asks the browser for a screenshot at
all**, because every way of asking disturbs something:

| | |
| --- | --- |
| `page.screenshot(clip=…)` | refuses a clip outside the window, so reaching a panel below the fold means **scrolling the page to it** |
| CDP `captureBeyondViewport` | reaches it without scrolling, but fires a **`resize` on the page for every capture** |
| CDP clipped capture | in a headed window this makes the page **visibly flash, resized to the clip** |

Instead the compositor pushes frames it has already painted, over
`Page.startScreencast` — the same channel DevTools uses for its device preview.
There is no capture request, so there is nothing that could resize anything, and
the panel crop happens in numpy where it costs nothing.

The bill for that is real: a whole-window frame has to be decoded rather than a
small clip, which is ~27 ms instead of ~2. A tick is 41 ms instead of 19. Worth
it — 24 Hz is still twenty times faster than the game asks for, and the game is
what you are trying to watch.

If the panel is off the bottom of the window it simply is not found, the bot keeps
waiting, and it tells you to enlarge the window or scroll it into view yourself.

### It presses once per cue, not once per frame

A frame is a picture of the past — it was painted before your last click reached
the game. So a loop that re-decides every 40 ms keeps acting on a reading that
predates its own last action, and on a toggle that means opening and closing the
ride on alternate frames, forever.

Two things stop it, and **neither is sufficient alone**. Measured against a fake
panel with a 250 ms response and a rollover on the button, over four seconds:

| | ride toggled |
| --- | --- |
| neither | 63 times (once per tick) |
| pointer parked off the panel | 36 times |
| press guard only | 4 times |
| **both** | **once** |

* **The pointer is parked off the panel after every click.** A button under the
  cursor may render its rollover state, and the bot would be reading a colour it
  caused itself. `Attraction` is the one that cannot survive that: it is the only
  button whose two states differ by brightness alone — `#ffffff` active against
  `#cccccc` closed — rather than by hue, so anything that dims it reads as
  "closed", and the answer to "closed" is to press it again.
* **A button is not pressed again while it looks exactly as it did when it was
  last pressed.** The moment its colour moves, pressing is allowed again.
  `reclick_after` (default 1 s) bounds the wait so a click the game dropped is
  retried rather than deadlocking that button; set it to `0` to disable.

Every action returns `True` if it pressed and `False` if it was suppressed.

### One property, one observable

Nothing combines two readings. `elevator1.can_dispatch` is exactly "the Dispatch
button is bright green" — not "green *and* the doors are shut *and* the track is
clear". A compound property would hide the decision the written procedure is
supposed to make explicit.

```python
game.elevator1.dispatch_button   # DispatchState.ARMED
game.elevator1.can_dispatch      # True
game.track.is_locked             # False
game.tv_room1.waiting_is_full    # True  — the box reads 21
game.tv_room1.loaded             # Count.ZERO / Count.FULL / Count.OTHER
```

### The two numbers

A counter says one of three things, and never a number:

| | |
| --- | --- |
| `Count.ZERO` | the box reads `0` — nothing to move |
| `Count.FULL` | the box reads `21` — a full load |
| `Count.OTHER` | anything else, including a box it could not read |

`OTHER` is the safe direction: a glyph the reader is not confident about lands
there, and the bot waits instead of acting on a misread.

## Verify the calibration

```bash
python -m tools.verify_layout          # annotate the calibration frames
python -m tools.verify_layout --live   # annotate a frame from the running game
python -m tools.benchmark              # what perception costs on your machine
python -m pytest                       # perception and strategy, against the screenshots
```

The test suite runs the whole stack against PNGs with no browser. That is what
makes the palette and geometry *verified* rather than guessed — the game was never
reachable from the machine this was written on.

If the panel is never found, run the probe and send me its output:

```bash
python -m tools.probe_page
```

## Docs

- [`docs/findings.md`](docs/findings.md) — the measured palette, geometry and state semantics
- [`docs/design-decisions.md`](docs/design-decisions.md) — the decisions and why, including the ones since reversed
- [`docs/game-mechanics.md`](docs/game-mechanics.md) — open questions only you can answer
- [`TODO.md`](TODO.md) — what is still unverified
