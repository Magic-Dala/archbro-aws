from __future__ import annotations

from dataclasses import dataclass


class CanvasComplexityError(RuntimeError):
    def __init__(self, dimension: str, observed: int, limit: int) -> None:
        self.dimension = dimension
        self.observed = observed
        self.limit = limit
        super().__init__(f"Canvas complexity budget exceeded for {dimension}: {observed} > {limit}")

    def detail(self) -> dict[str, object]:
        return {
            "code": "canvas_complexity_exceeded",
            "dimension": self.dimension,
            "observed": self.observed,
            "limit": self.limit,
        }


@dataclass(slots=True)
class CanvasWorkBudget:
    max_nodes: int = 40
    max_edges: int = 120
    max_route_expansions: int = 400_000
    max_route_candidates: int = 1_600_000
    max_pair_evaluations: int = 260_000
    max_summary_candidates: int = 45_000
    route_expansions: int = 0
    route_candidates: int = 0
    pair_evaluations: int = 0
    summary_candidates: int = 0

    def require_input(self, *, nodes: int, edges: int) -> None:
        if nodes > self.max_nodes:
            raise CanvasComplexityError("nodes", nodes, self.max_nodes)
        if edges > self.max_edges:
            raise CanvasComplexityError("edges", edges, self.max_edges)

    def consume(self, dimension: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("Canvas work amount cannot be negative")
        if dimension == "route_expansions":
            observed = self.route_expansions + amount
            self.route_expansions = observed
            limit = self.max_route_expansions
        elif dimension == "route_candidates":
            observed = self.route_candidates + amount
            self.route_candidates = observed
            limit = self.max_route_candidates
        elif dimension == "pair_evaluations":
            observed = self.pair_evaluations + amount
            self.pair_evaluations = observed
            limit = self.max_pair_evaluations
        elif dimension == "summary_candidates":
            observed = self.summary_candidates + amount
            self.summary_candidates = observed
            limit = self.max_summary_candidates
        else:
            raise ValueError(f"Unknown Canvas work dimension: {dimension}")
        if observed > limit:
            raise CanvasComplexityError(dimension, observed, limit)

    def snapshot(self) -> dict[str, int]:
        return {
            key: value
            for key, value in (
                ("pair_evaluations", self.pair_evaluations),
                ("route_candidates", self.route_candidates),
                ("route_expansions", self.route_expansions),
                ("summary_candidates", self.summary_candidates),
            )
            if value
        }
