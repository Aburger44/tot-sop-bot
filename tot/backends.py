"""Where frames come from and where clicks go.

``PlaywrightBackend``  the real thing — drives its own Chromium and reads frames
                       out of it without ever touching the page.
``ImageBackend``       serves a PNG as "the current frame", so the whole stack runs
                       with no browser. That is how the fixture tests work.

The live backend's one hard rule: **nothing it does may change what you see.** You
are watching the game while it plays, so it never clicks on its own, never
scrolls, never resizes, and never asks the browser for anything that might.

That rule is what picks the capture method. Asking Chromium for a *clipped*
screenshot is the obvious way to read only the panel, and it is 30 ms a tick
cheaper — but in a headed window a clipped capture makes the page visibly flash,
resized to the clip. So frames are not requested at all: ``Page.startScreencast``
has the compositor push whatever it has already painted, the same channel
DevTools uses for its device preview. There is no capture request, so there is
nothing that can resize anything, and the panel crop happens here in numpy where
it costs nothing.

Screencast frames are PNG, so the flat button fills come back bit-exact — which
the whole colour-reading approach depends on. JPEG would be faster and smaller and
is not an option for that reason.
"""

from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .errors import BrowserError, RuffleCrashed
from .geometry import Rect

DEFAULT_URL = (
    "https://www.themagical.nl/content/plugins/flash-emulator/"
    "flash-player.php?game=tower-of-terror"
)


class Backend(Protocol):
    """Everything the game needs from the outside world."""

    def grab(self) -> np.ndarray:
        """The current frame as an (H, W, 3) uint8 RGB array."""

    def set_region(self, region: Rect | None) -> None:
        """Restrict later grabs to ``region`` of the full frame, or undo that."""

    def click(self, x: int, y: int) -> None:
        """Click at a point in the *current* frame's coordinates."""

    def close(self) -> None: ...


def _to_array(im: Any, region: Rect | None) -> np.ndarray:
    """A decoded image as RGB, cropped, without a redundant conversion pass."""
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if region is not None:
        im = im.crop((region.x, region.y, region.right, region.bottom))
    return np.asarray(im)[..., :3]


class ImageBackend:
    """A still frame. Clicks are recorded rather than delivered."""

    def __init__(self, path: str | Path):
        from PIL import Image

        self.path = Path(path)
        if not self.path.exists():
            raise BrowserError(f"no such fixture image: {self.path}")
        self._full = np.array(Image.open(self.path).convert("RGB"))
        self._region: Rect | None = None
        self.clicks: list[tuple[int, int]] = []

    def grab(self) -> np.ndarray:
        r = self._region
        if r is None:
            return self._full
        return self._full[r.y : r.bottom, r.x : r.right]

    def set_region(self, region: Rect | None) -> None:
        self._region = region

    def click(self, x: int, y: int) -> None:
        r = self._region
        self.clicks.append((x, y) if r is None else (x + r.x, y + r.y))

    def close(self) -> None:  # nothing to release
        pass


#: Ruffle reads this before it boots. Everything here either removes an overlay
#: that would sit on top of the panel, or removes a reason for Ruffle to give up
#: mid-run — ``maxExecutionDuration`` in particular defaults to 15 seconds, after
#: which a slow ActionScript frame is treated as a hang and the player panics.
RUFFLE_CONFIG: dict[str, Any] = {
    "autoplay": "on",
    "unmuteOverlay": "hidden",
    "splashScreen": False,
    "warnOnUnsupportedContent": False,
    "contextMenu": False,
    "showSwfDownload": False,
    "maxExecutionDuration": 3600,
    "logLevel": "error",
}

#: Flags that matter, and why:
#:   backgrounding  a window that loses focus otherwise gets throttled to 1 Hz,
#:                  and the screencast stops with it
#:   autoplay       lets the game's sound play without a click first; the unmute
#:                  overlay is hidden in RUFFLE_CONFIG for the same reason
#:
#: --mute-audio is added only when ``mute=True``: the sound costs the bot nothing
#: it needs, but it is part of watching the ride.
#:
#: Deliberately absent: --disable-frame-rate-limit and --disable-gpu-vsync. They
#: bought a few ms back when every tick asked for a screenshot, and uncapping a
#: compositor is a plausible way to make a real display flicker. The screencast
#: takes frames at whatever rate the page paints, so neither is needed.
_CHROME_ARGS = (
    "--autoplay-policy=no-user-gesture-required",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
)


class PlaywrightBackend:
    """Drives Chromium and reads the frames it is already painting."""

    #: Walks open shadow roots, because Ruffle mounts its canvas inside a
    #: <ruffle-player> custom element rather than in the light DOM. Diagnostics
    #: only — capture does not care where the canvas is.
    _INVENTORY_JS = r"""
    () => {
      const inv = {};
      let canvas = null;
      const visit = (root) => {
        let els;
        try { els = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of els) {
          const tag = el.tagName.toLowerCase();
          if (['canvas','embed','object','iframe'].includes(tag) || tag.startsWith('ruffle-')) {
            const r = el.getBoundingClientRect();
            (inv[tag] = inv[tag] || []).push(Math.round(r.width) + 'x' + Math.round(r.height));
            if (tag === 'canvas' && (!canvas || r.width * r.height > canvas.w * canvas.h))
              canvas = {x: r.left, y: r.top, w: r.width, h: r.height};
          }
          if (el.shadowRoot) visit(el.shadowRoot);
        }
      };
      visit(document);
      return {
        title: document.title,
        elements: inv,
        canvas: canvas,
        view: {w: window.innerWidth, h: window.innerHeight},
        text: document.body ? document.body.innerText.replace(/\s+/g, ' ').slice(0, 300) : ''
      };
    }
    """

    #: Ruffle renders its failures into its own shadow root: #panic for a hard
    #: crash, #message-overlay for the softer "this content is not supported"
    #: banner. Both cover the panel, so both end the run rather than being read
    #: as a strange-looking game.
    _TROUBLE_JS = r"""
    () => {
      let hit = null;
      const visit = (root) => {
        let els;
        try { els = root.querySelectorAll('*'); } catch (e) { return; }
        for (const el of els) {
          const id = (el.id || '').toLowerCase();
          if (!hit && (id === 'panic' || id === 'message-overlay' ||
                       id === 'panic-body' || id === 'error')) {
            hit = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 400) || id;
          }
          if (el.shadowRoot) visit(el.shadowRoot);
        }
      };
      visit(document);
      return hit;
    }
    """

    _VIEW_JS = "() => [window.innerWidth, window.innerHeight]"

    def __init__(
        self,
        url: str = DEFAULT_URL,
        headless: bool = False,
        viewport: tuple[int, int] | None = None,
        timeout_ms: int = 90_000,
        frame_wait_ms: int = 60,
        ruffle_config: dict[str, Any] | None = None,
        debug_dir: str | Path | None = "debug_output",
        mute: bool = False,
    ):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on the host
            raise BrowserError(
                "playwright is not installed. Run:\n"
                "    pip install playwright && playwright install chromium"
            ) from exc

        self.url = url
        self.frame_wait_ms = frame_wait_ms
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self._region: Rect | None = None
        self._frame_b64: str | None = None
        self._frame_seq = 0
        self._taken_seq = -1
        self._frame_size: tuple[int, int] | None = None
        self._scale = 1.0

        args = list(_CHROME_ARGS)
        if mute:
            args.append("--mute-audio")
        # A maximised window is the one way to be sure the whole game is on screen
        # without the bot ever needing to move the page to see part of it.
        if viewport is None and not headless:
            args.append("--start-maximized")

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=headless, args=args)
            self._page = self._browser.new_page(
                no_viewport=viewport is None,
                **({} if viewport is None else
                   {"viewport": {"width": viewport[0], "height": viewport[1]}}),
            )
            cfg = json.dumps({**RUFFLE_CONFIG, **(ruffle_config or {})})
            self._page.add_init_script(
                "window.RufflePlayer = window.RufflePlayer || {};"
                "window.RufflePlayer.config = Object.assign("
                f"{{}}, window.RufflePlayer.config || {{}}, {cfg});"
            )
            self._page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            self._cdp: Any = self._page.context.new_cdp_session(self._page)
            self._start_screencast()
        except Exception:
            self.close()
            raise

    # -- frames ---------------------------------------------------------------
    def _start_screencast(self) -> None:
        """Ask the compositor to push what it paints. Nothing is requested per
        frame, so there is no per-frame call that could disturb the page."""
        self._cdp.on("Page.screencastFrame", self._on_frame)
        self._cdp.send(
            "Page.startScreencast",
            # maxWidth/maxHeight are set past any real window on purpose: a frame
            # scaled to fit them would resample the flat fills, and reading them
            # exactly is the whole basis of the colour classification.
            {"format": "png", "maxWidth": 8000, "maxHeight": 8000, "everyNthFrame": 1},
        )

    def _on_frame(self, event: dict) -> None:
        self._frame_b64 = event.get("data")
        self._frame_seq += 1
        # Chromium holds the next frame until this one is acknowledged, so the ack
        # is what paces the stream — and it goes out on arrival, not when the
        # frame is taken. Waiting until then measured 59 ms a tick against 41 ms,
        # because it costs a whole paint cycle; the frames that go unread in
        # exchange cost only bandwidth down a local socket, never a decode.
        try:
            self._cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        except Exception:
            pass

    def _latest_frame(self) -> str:
        """The newest painted frame, waiting briefly for one if it is due.

        A page that is not animating — a mode chooser, a paused game — simply
        stops producing frames, so this settles for the last one rather than
        blocking. Only if none has ever arrived does it fall back to asking.
        """
        deadline = time.monotonic() + self.frame_wait_ms / 1000
        while self._frame_seq == self._taken_seq:
            if time.monotonic() >= deadline:
                break
            self._page.wait_for_timeout(2)
        self._taken_seq = self._frame_seq
        if self._frame_b64 is None:
            # No screencast at all. A whole-viewport capture takes the same path
            # through the compositor and, crucially, carries no clip.
            reply = self._cdp.send(
                "Page.captureScreenshot",
                {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            )
            self._frame_b64 = reply["data"]
        return self._frame_b64

    def grab(self) -> np.ndarray:
        from PIL import Image

        im = Image.open(io.BytesIO(base64.b64decode(self._latest_frame())))
        self._frame_size = im.size
        return _to_array(im, self._region)

    def set_region(self, region: Rect | None) -> None:
        """Record the panel crop. Applied here in numpy, never in the browser."""
        self._region = region
        if region is not None:
            self._measure_scale()

    def _measure_scale(self) -> None:
        """Frame pixels per CSS pixel.

        A frame comes back at the display's real resolution, so on a HiDPI screen
        it is twice the size the mouse works in. Detection does not care — it
        finds the panel at whatever scale it is drawn — but clicking does.
        """
        if self._frame_size is None:
            return
        try:
            w, _h = self._page.evaluate(self._VIEW_JS)
        except Exception:
            return
        if w:
            self._scale = self._frame_size[0] / float(w)

    # -- acting ---------------------------------------------------------------
    def click(self, x: int, y: int) -> None:
        """Click a point in the current frame, then get out of the way.

        The pointer is moved off the panel afterwards because a button under the
        cursor may render its rollover state, and the bot would then be reading a
        colour it caused itself. ``Attraction`` is the one that cannot survive
        that: it is the only button whose two states differ by brightness alone
        (#ffffff active against #cccccc closed) rather than by hue, so anything
        that dims it reads as "closed" — and the answer to "closed" is to press
        it again.
        """
        fx, fy = float(x), float(y)
        if self._region is not None:
            fx += self._region.x
            fy += self._region.y
        self._page.mouse.click(fx / self._scale, fy / self._scale)
        park = self._park_point()
        if park is not None:
            self._page.mouse.move(park[0] / self._scale, park[1] / self._scale)

    def _park_point(self) -> tuple[float, float] | None:
        """Somewhere in the frame that is not the panel, in frame coordinates."""
        r = self._region
        if r is None:
            return None
        w, h = self._frame_size or (r.right + 64, r.bottom + 64)
        margin = 12.0
        if r.y - margin > 0:
            return (r.cx, r.y - margin)
        if r.bottom + margin < h:
            return (r.cx, r.bottom + margin)
        if r.x - margin > 0:
            return (r.x - margin, r.cy)
        if r.right + margin < w:
            return (r.right + margin, r.cy)
        return None

    # -- the emulator ---------------------------------------------------------
    def trouble(self) -> str | None:
        """Ruffle's own error text, if it is showing an error screen."""
        try:
            return self._page.evaluate(self._TROUBLE_JS)
        except Exception:
            return None

    def check(self) -> None:
        """Raise if the emulator has died. Cheap enough to call between ticks."""
        msg = self.trouble()
        if msg:
            raise RuffleCrashed(f"the Flash emulator gave up: {msg}")

    def reload(self, timeout_ms: int = 90_000) -> None:
        """Start the page over. The game restarts, so you pick the mode again."""
        self._region = None
        self._frame_b64, self._frame_seq, self._taken_seq = None, 0, -1
        self._page.goto(self.url, timeout=timeout_ms, wait_until="domcontentloaded")
        self._cdp.send("Page.startScreencast",
                       {"format": "png", "maxWidth": 8000, "maxHeight": 8000, "everyNthFrame": 1})

    # -- diagnosis ------------------------------------------------------------
    @property
    def canvas_size(self) -> tuple[int, int] | None:
        """The game canvas, if one can be found. Diagnostics only."""
        try:
            info = self._page.evaluate(self._INVENTORY_JS)
        except Exception:
            return None
        c = info.get("canvas")
        return None if c is None else (int(c["w"]), int(c["h"]))

    def describe_page(self) -> str:
        """What is actually on the page. Used when the panel never turns up."""
        lines = [f"the RideControl panel was not found at {self.url}"]
        try:
            info = self._page.evaluate(self._INVENTORY_JS)
            lines.append(f"page title: {info.get('title')!r}")
            elements = info.get("elements") or {}
            lines.append(
                "elements found: "
                + (", ".join(f"{k}={v}" for k, v in elements.items()) if elements else "none")
            )
            if info.get("text"):
                lines.append(f"page text: {info['text'][:200]!r}")
            c, view = info.get("canvas"), info.get("view") or {}
            if c and view:
                below = c["y"] + c["h"] - view.get("h", 0)
                right = c["x"] + c["w"] - view.get("w", 0)
                lines.append(
                    f"canvas is {int(c['w'])}x{int(c['h'])} at ({int(c['x'])}, {int(c['y'])}) "
                    f"in a {int(view.get('w', 0))}x{int(view.get('h', 0))} window"
                )
                if below > 1 or right > 1:
                    lines.append(
                        "part of the game is outside the window, and this bot will not "
                        "scroll or resize your page to reach it. Enlarge the window, or "
                        "scroll the RideControl panel into view yourself."
                    )
        except Exception as exc:
            lines.append(f"(could not inspect the page: {exc})")
        lines.append(f"frames: {[f.url for f in self._page.frames]}")
        lines.append(f"screencast frames received: {self._frame_seq}")
        if self.debug_dir and self._frame_b64:
            try:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                shot = self.debug_dir / f"page-{int(time.time())}.png"
                shot.write_bytes(base64.b64decode(self._frame_b64))
                lines.append(f"last frame saved to: {shot}")
            except Exception:
                pass
        lines.append("Run `python -m tools.probe_page` to watch the page load.")
        return "\n  ".join(lines)

    def close(self) -> None:
        cdp = getattr(self, "_cdp", None)
        if cdp is not None:
            try:
                cdp.send("Page.stopScreencast")
            except Exception:
                pass
        for attr in ("_browser", "_pw"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.close() if attr == "_browser" else obj.stop()
            except Exception:
                pass
