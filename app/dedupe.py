"""Near-duplicate detection for posts that appear in several channels at once.

Uses a 64-bit SimHash over word bigrams. Running it *before* summarisation is
the point: a story carried by five channels is paid for once, not five times.
No external dependency, no model call.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

_URL = re.compile(r"https?://\S+|t\.me/\S+|@[A-Za-z0-9_]+")
_KEEP = re.compile(r"[^0-9a-zа-яё ]+")
_SPACE = re.compile(r"\s+")

T = TypeVar("T")

# Above this many posts the pairwise scan stops being free; exact-text matching
# still runs, which catches verbatim reposts (the common case at that volume).
MAX_PAIRWISE = 1500


def normalize(text: str) -> str:
    text = _URL.sub(" ", text.lower().replace("ё", "е"))
    return _SPACE.sub(" ", _KEEP.sub(" ", text)).strip()


def simhash(text: str) -> int:
    """64-bit SimHash of word bigrams (unigrams for very short texts)."""
    words = normalize(text).split()
    if not words:
        return 0
    grams = [" ".join(words[i : i + 2]) for i in range(len(words) - 1)] or words

    vector = [0] * 64
    for gram in grams:
        digest = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if digest >> bit & 1 else -1

    out = 0
    for bit in range(64):
        if vector[bit] > 0:
            out |= 1 << bit
    return out


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def dedupe(
    items: Sequence[T],
    text_of: Callable[[T], str],
    rank_of: Callable[[T], float],
    max_distance: int = 3,
) -> tuple[list[T], dict[int, list[int]]]:
    """Collapses near-identical items, keeping the highest-ranked of each cluster.

    Returns ``(kept, dropped_by_kept_index)`` where the mapping points from an
    index into ``kept`` to the indices (into ``items``) that it absorbed, so the
    renderer can say where else a story ran.
    """
    if len(items) < 2:
        return list(items), {}

    order = sorted(range(len(items)), key=lambda i: -rank_of(items[i]))
    pairwise = len(items) <= MAX_PAIRWISE

    hashes: dict[int, int] = {}
    exact: dict[str, int] = {}
    kept_indices: list[int] = []
    absorbed: dict[int, list[int]] = {}

    for idx in order:
        text = text_of(items[idx])
        norm = normalize(text)
        if not norm:
            # Nothing comparable (media-only post): keep it, never a duplicate.
            hashes[idx] = 0
            kept_indices.append(idx)
            continue

        winner = exact.get(norm)
        if winner is None and pairwise:
            sig = hashes[idx] = simhash(text)
            for candidate in kept_indices:
                other = hashes.get(candidate, 0)
                if other and distance(sig, other) <= max_distance:
                    winner = candidate
                    break

        if winner is None:
            exact.setdefault(norm, idx)
            kept_indices.append(idx)
        else:
            absorbed.setdefault(winner, []).append(idx)

    kept_indices.sort()
    position = {original: new for new, original in enumerate(kept_indices)}
    return (
        [items[i] for i in kept_indices],
        {position[k]: v for k, v in absorbed.items() if k in position},
    )


def any_match(text: str, needles: Iterable[str]) -> bool:
    haystack = normalize(text)
    return any(n for n in (normalize(x) for x in needles) if n and n in haystack)
