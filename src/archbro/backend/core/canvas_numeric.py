from __future__ import annotations

import math
from typing import Iterable


DECISION_SCALE = 1_000_000


def decision_key(value: int | float) -> int:
    """Canonical micro-unit key used only where a numeric value affects a decision.

    CPython round uses ties-to-even. Keeping the quantization at the comparison
    boundary avoids paying Decimal conversion cost inside the A* hot loop while
    still preventing sub-micro-unit accumulation drift from changing ordering.
    """
    return int(round(float(value) * DECISION_SCALE))


def stable_cost_sum(values: Iterable[int | float]) -> float:
    # fsum removes CPython's builtin-sum accumulation algorithm from the
    # contract. Quantization is deliberately deferred until a value decides
    # ordering, so pure measurement helpers do not add hot-loop conversion cost.
    return math.fsum(float(value) for value in values)


def stable_mean(values: Iterable[int | float]) -> float:
    materialized = tuple(float(value) for value in values)
    if not materialized:
        raise ValueError("stable_mean requires at least one value")
    return canonical_cost(math.fsum(materialized) / len(materialized))


def canonical_cost(value: int | float) -> float:
    return decision_key(value) / DECISION_SCALE


def cost_less(left: int | float, right: int | float) -> bool:
    return decision_key(left) < decision_key(right)
