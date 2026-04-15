"""Value coercion helpers shared across the three MCP servers.

The deliberation engine persists LLM-produced values (confidence, likelihood,
etc.) into numeric Postgres columns. LLMs occasionally emit natural-language
categoricals like ``"high"`` or ``"moderate"`` instead of a float, even when
the prompt asks for a number — those writes raise ``asyncpg.DataError`` and
are silently dropped by per-row ``try/except`` guards.

``coerce_confidence`` is the single coercion point. Every INSERT that binds
an LLM-produced numeric value MUST route through it so that a model
regression anywhere in the deliberation pipeline cannot silently drop rows.
"""

from __future__ import annotations

# Ordered roughly high → low. When new strings show up in the wild, add them
# here (and add a test in
# ``server/deliberation/tests/test_coerce_confidence.py``).
_CONFIDENCE_MAP: dict[str, float] = {
    "critical":  0.95,
    "very high": 0.90,
    "high":      0.80,
    "moderate":  0.60,
    "medium":    0.60,
    "low":       0.35,
    "very low":  0.20,
    "none":      0.05,
}


def coerce_confidence(raw: object) -> float | None:
    """Coerce any LLM-produced confidence value to a Python float.

    Returns a float in ``[0.0, 1.0]`` safe for a Postgres ``REAL``/``FLOAT4``
    column, or ``None`` if ``raw`` is ``None`` or unresolvable.

    Accepts:
        - ``float`` — clamped to ``[0, 1]``.
        - ``int`` — ``0``/``1`` literal, otherwise treated as a percentage
          (e.g. ``85`` → ``0.85``) and clamped.
        - numeric ``str`` (``"0.85"``) — parsed; integer-valued strings > 1
          treated as percentages (``"85"`` → ``0.85``); others clamped.
        - categorical ``str`` (``"high"``, ``"moderate"``, …) — looked up in
          ``_CONFIDENCE_MAP``. Case- and whitespace-insensitive.
        - ``None`` — returned as ``None`` (means "no value" in the DB).
        - anything else — returned as ``None``.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        # ``bool`` is a subclass of ``int`` — intercept before int path so
        # that ``True``/``False`` cannot become 1.0/0.0 by accident.
        return None
    if isinstance(raw, float):
        return max(0.0, min(1.0, raw))
    if isinstance(raw, int):
        if raw <= 1:
            return max(0.0, float(raw))
        return max(0.0, min(1.0, float(raw) / 100.0))
    if isinstance(raw, str):
        normalised = raw.strip().lower()
        if not normalised:
            return None
        if normalised in _CONFIDENCE_MAP:
            return _CONFIDENCE_MAP[normalised]
        try:
            val = float(normalised)
            if val > 1.0 and val == int(val):
                val = val / 100.0
            return max(0.0, min(1.0, val))
        except ValueError:
            return None
    return None
