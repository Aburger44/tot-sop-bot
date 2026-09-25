"""Reading the counter boxes — as little of them as possible.

Only two numbers guide a decision: **0** (this queue or car is empty) and **21**
(it is full). Everything in between is the same instruction — wait. So a counter
is classified three ways and nothing else is decoded.

That is cheaper than it sounds, because the short-circuits do most of the work:

* three or more glyphs in the box  -> ``OTHER``, no matching at all
* two glyphs whose first is not 2  -> ``OTHER``, the second is never looked at
* zero glyphs                      -> ``OTHER``

so a counter reading 148 costs one segmentation pass and no correlation.

Glyphs are *not* pixel-identical between renders — the same digit picks up a pixel
or two of anti-aliasing depending on its sub-pixel position — so the matching that
does happen is normalised grayscale correlation against the exemplars mined from
the calibration frames, not bitmap equality. A match that is not confidently ``0``,
``2`` or ``1`` falls through to ``OTHER``, which is the safe direction: the bot
waits rather than acting on a misread.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import numpy as np

from .geometry import Rect

#: Every glyph is resampled to this (width, height) before comparison.
CANON = (12, 16)

#: Correlation floor, and how far the winner must beat the best other character.
#: Across the calibration glyphs the worst correct match scored 0.753 with a worst
#: margin of 0.144, so both leave headroom without being reckless.
MIN_SCORE = 0.65
MIN_MARGIN = 0.05

#: A full load. The only number besides zero that changes what the bot does.
FULL = 21

TEMPLATE_FILE = Path(__file__).with_name("templates.npz")


class Count(Enum):
    """All a counter box is allowed to say."""

    ZERO = "zero"
    FULL = "full"
    OTHER = "other"

    @property
    def is_zero(self) -> bool:
        return self is Count.ZERO

    @property
    def is_full(self) -> bool:
        return self is Count.FULL


def _spans(flags: np.ndarray) -> list[tuple[int, int]]:
    if not flags.any():
        return []
    d = np.diff(np.concatenate(([0], flags.view(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def cut_glyphs(frame: np.ndarray, rect: Rect, max_glyphs: int = 4) -> list[np.ndarray]:
    """Grayscale patches for each glyph inside ``rect``, left to right.

    Stops early once ``max_glyphs`` columns have been found: the caller only ever
    cares whether there are one, two, or more than two.
    """
    patch = frame[rect.y : rect.bottom, rect.x : rect.right]
    if patch.size == 0:
        return []
    hi = patch.max(axis=2).astype(np.int16)
    lo = patch.min(axis=2)
    # Bright, near-neutral pixels: the white text, not the box or the game art.
    mask = (hi - lo < 45) & (hi > 120)
    gray = patch.mean(axis=2)

    out: list[np.ndarray] = []
    for c0, c1 in _spans(mask.any(axis=0)):
        if c1 - c0 < 2:
            continue
        rows = np.flatnonzero(mask[:, c0:c1].any(axis=1))
        if len(rows) < 3:
            continue
        out.append(gray[rows[0] : rows[-1] + 1, c0:c1])
        if len(out) > max_glyphs:
            break
    return out


def normalise(patch: np.ndarray) -> np.ndarray:
    """A glyph reduced to a zero-mean unit-norm vector at a canonical size."""
    from PIL import Image

    im = Image.fromarray(np.clip(patch, 0, 255).astype(np.uint8)).resize(CANON, Image.BILINEAR)
    v = np.asarray(im, dtype=float)
    v -= v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


@dataclass
class Templates:
    """Labelled glyph exemplars, several variants per character."""

    chars: list[str]
    vectors: np.ndarray  # (n, H, W), each normalised

    @classmethod
    def load(cls, path: Path = TEMPLATE_FILE) -> Templates:
        if not path.exists():
            raise FileNotFoundError(
                f"no digit templates at {path}. Run `python -m tools.extract_templates` "
                f"to build them from the calibration screenshots."
            )
        d = np.load(path, allow_pickle=False)
        return cls([str(c) for c in d["chars"]], d["vectors"])

    def save(self, path: Path = TEMPLATE_FILE) -> None:
        np.savez_compressed(path, chars=np.array(self.chars), vectors=self.vectors)

    def is_char(self, patch: np.ndarray, want: str) -> bool:
        """Is this glyph confidently ``want``?"""
        scores = (self.vectors * normalise(patch)).sum(axis=(1, 2))
        order = np.argsort(scores)[::-1]
        top = self.chars[order[0]]
        if top != want or scores[order[0]] < MIN_SCORE:
            return False
        runner = next((scores[i] for i in order if self.chars[i] != top), 0.0)
        return bool(scores[order[0]] - runner >= MIN_MARGIN)


def looks_like_zero(patch: np.ndarray) -> bool:
    """A closed ring with nothing in the middle — a ``0`` and no other digit.

    The correlation match alone is fragile here: rendered at a different scale or
    sub-pixel offset than the calibration frames, a live ``0`` can score closer
    to ``6``. The shape is not ambiguous, though. ``6``, ``8`` and ``9`` all put
    ink across the middle; ``0`` is the only one whose centre column is empty
    from a quarter of the way down to three quarters, with ink down both sides.
    """
    h, w = patch.shape
    if h < 7 or w < 4 or w > h:
        return False
    ink = patch > (float(patch.min()) + float(patch.max())) / 2
    rows = slice(int(h * 0.25), int(np.ceil(h * 0.75)))
    mid = w // 2
    centre = ink[rows, max(1, mid - w // 6) : min(w - 1, mid + w // 6 + 1)]
    left = ink[rows, : max(1, w // 3)].any(axis=1)
    right = ink[rows, w - max(1, w // 3) :].any(axis=1)
    top, bottom = ink[: max(1, h // 4), 1:-1].any(), ink[-max(1, h // 4) :, 1:-1].any()
    return bool(not centre.any() and left.all() and right.all() and top and bottom)


class CounterReader:
    """Classifies a counter box. Never raises — an unreadable box is ``OTHER``."""

    def __init__(self, templates: Templates | None = None):
        self.templates = templates or Templates.load()

    def read(self, frame: np.ndarray, rect: Rect) -> Count:
        glyphs = cut_glyphs(frame, rect, max_glyphs=2)
        if len(glyphs) == 1:
            g = glyphs[0]
            return Count.ZERO if looks_like_zero(g) or self.templates.is_char(g, "0") else Count.OTHER
        if len(glyphs) == 2 and self.templates.is_char(glyphs[0], "2"):
            return Count.FULL if self.templates.is_char(glyphs[1], "1") else Count.OTHER
        return Count.OTHER
