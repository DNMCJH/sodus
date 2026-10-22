"""Make the animations we already have feel intentional.

The characters have between 7 and 39 clips each. Nodus was using one per mood,
so a run that starts, waits, succeeds and fails all looked roughly the same:
one animation replaces another. What makes a desk pet feel alive is not more
clips, it is the *order* — celebrate then waved at you, sigh then turn away.

This module turns one mood into a short beat list. It is pure (`plan` returns
data), so the choreography is testable without a pet, a window or a clock.
"""

from __future__ import annotations

import threading
import time

from . import pet

#: mood -> [(anim name, seconds to hold before the next beat), ...]
#:
#: The first beat carries the mood; the tail is what makes it read as a
#: reaction rather than a slideshow. Kept short on purpose: a pet that performs
#: for six seconds is a pet you close.
DEFAULT_BEATS: dict[str, list[tuple[str, float]]] = {
    # per-character sequences live in config; these are the fallback shape
}

#: How long the last beat stays before the pet returns to its own chain.
TAIL = 2.0


def plan(mood: str, agent: str = "", beats: dict | None = None) -> list[tuple[str, float]]:
    """The beats to play for this mood, or [] when we have nothing to say.

    Character order: explicit `beats` argument, then the per-pet config in
    `pet_bridge.mood_beats[<pet id>][<mood>]`, then the single-animation
    behaviour that existed before (one beat, no tail).
    """
    table = beats if beats is not None else _config_beats()
    pet_id = pet.pet_for(agent)
    per_pet = (table or {}).get(pet_id) or {}
    chosen = per_pet.get(mood)
    if isinstance(chosen, list) and chosen:
        out: list[tuple[str, float]] = []
        for item in chosen:
            if isinstance(item, str) and item:
                out.append((item, TAIL))
            elif isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
                hold = float(item[1]) if len(item) > 1 and isinstance(item[1], (int, float)) else TAIL
                out.append((item[0], hold))
        return out
    single = pet.anim_for(mood, agent)
    return [(single, TAIL)] if single else []


def _config_beats() -> dict:
    cfg = pet.bridge()
    raw = cfg.get("mood_beats")
    return raw if isinstance(raw, dict) else {}


def play(mood: str, agent: str = "", background: bool = True) -> list[str]:
    """Run the beats. Returns the names it actually played.

    Each beat is a best-effort `animate`; a pet that is not running costs one
    failed HTTP call per beat and nothing else.
    """
    beats = plan(mood, agent)
    if not beats:
        return []

    def run() -> list[str]:
        played: list[str] = []
        for name, hold in beats:
            if pet.animate(name, agent):
                played.append(name)
            time.sleep(max(0.0, hold))
        return played

    if not background:
        return run()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return [name for name, _ in beats]
