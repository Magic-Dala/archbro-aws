"""Bounded deterministic channel separation after shortest-path routing.

Routes are optimized together, including ports on a shared card side. This is
geometry only: no relationship is removed and no junction is introduced.
"""
from itertools import combinations

from .canvas_budget import CanvasWorkBudget
from .canvas_numeric import cost_less, stable_cost_sum


def route_conflicts(a, b, spacing=12.0):
    """Return (crossing points, parallel crowding area) for two polylines."""
    return _segment_conflicts(_route_segments(a), _route_segments(b), spacing)


def _route_segments(points):
    """Immutable spans reusable across all pair comparisons for one route."""
    return tuple(
        (a[1] == b[1], a[0] == b[0], a[0], a[1],
         min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]))
        for a, b in zip(points, points[1:])
    )


def _segment_conflicts(a, b, spacing):
    crossings, crowding_terms = set(), []
    for horizontal, _vertical, x, y, left, right, top, bottom in a:
        if horizontal:
            for other_horizontal, _, ox, oy, ol, oright, ot, ob in b:
                if other_horizontal:
                    distance = abs(y-oy)
                    if distance < spacing:
                        overlap = min(right, oright)-max(left, ol)
                        if overlap > 0:
                            crowding_terms.append((spacing-distance)*overlap)
                elif left <= ox <= right and ot <= y <= ob:
                    crossings.add((ox, y))
        else:
            for _, other_vertical, ox, oy, ol, oright, ot, ob in b:
                if other_vertical:
                    distance = abs(x-ox)
                    if distance < spacing:
                        overlap = min(bottom, ob)-max(top, ot)
                        if overlap > 0:
                            crowding_terms.append((spacing-distance)*overlap)
                elif top <= oy <= bottom and ol <= x <= oright:
                    crossings.add((x, oy))
    return crossings, stable_cost_sum(crowding_terms)


def separate_canvas_routes(nodes, edges, routes, *, clearance=12.0, spacing=12.0, work_budget=None):
    """Separate existing lanes, without adding bends or moving any card.

    Every accepted step strictly improves a joint objective. Displacements are
    bounded to 48 units of the initial route and length growth to 48 units.
    Clear two-point connectors are fixed, preserving straight-first behavior.
    """
    work_budget = work_budget or CanvasWorkBudget()
    by_id = {n.node_id: n for n in nodes}
    by_edge = {e.edge_id: e for e in edges}
    degree = {node_id: sum(node_id in (e.source,e.target) for e in edges) for node_id in by_id}
    result = dict(routes)
    initial = dict(routes)
    boxes = [(n.x-clearance, n.y-clearance, n.x+n.width+clearance, n.y+n.height+clearance) for n in nodes]
    lengths = {key: _length(points) for key, points in routes.items()}
    current_lengths = dict(lengths)

    def valid(key, points):
        original = initial[key]
        if _length(points) > lengths[key]+48+1e-6:
            return False
        if any(abs(x-u)+abs(y-v) > 48+1e-6 for (x,y),(u,v) in zip(points, original)):
            return False
        for i, (a,b) in enumerate(zip(points,points[1:])):
            # Keep segment order and signs. No hairpins, disappearing segments,
            # new bends, or reverse terminal directions can be introduced.
            old_a, old_b = original[i:i+2]
            axis = 0 if old_a[1] == old_b[1] else 1
            if a[1-axis] != b[1-axis] or (b[axis]-a[axis])*(old_b[axis]-old_a[axis]) <= 0:
                return False
            for j, box in enumerate(boxes):
                node = nodes[j]
                terminal = (i == 0 and node.node_id == by_edge[key].source) or (i == len(points)-2 and node.node_id == by_edge[key].target)
                left, top, right, bottom = (node.x,node.y,node.x+node.width,node.y+node.height) if terminal else box
                if (left < a[0] < right and max(min(a[1],b[1]),top) < min(max(a[1],b[1]),bottom)) if axis == 1 else (top < a[1] < bottom and max(min(a[0],b[0]),left) < min(max(a[0],b[0]),right)):
                    return False
        return True

    def pair_cost(a_id, a, b_id, b):
        work_budget.consume("pair_evaluations")
        crossings, crowding = _segment_conflicts(
            pending_segments.get(a_id, route_segments[a_id]),
            pending_segments.get(b_id, route_segments[b_id]), spacing,
        )
        # A crossing between unrelated routes is readable with a casing. A
        # crossing between routes of the same hub is much harder to follow.
        crossing_cost = crossing_weights[a_id, b_id]
        corners = a[1:-1]+b[1:-1]
        near_corner = sum(any(abs(p[0]-q[0])+abs(p[1]-q[1]) < spacing for q in corners) for p in crossings)
        return crowding*.25 + len(crossings)*crossing_cost + near_corner*36

    # Pair costs are updated only for changed routes; this makes evaluating a
    # local move independent of all the unrelated pairs in the drawing.
    pair_scores = {}
    keys = sorted(result)
    pair_keys_by_edge = {key: [] for key in keys}
    route_bounds = {key: _bounds(points) for key, points in result.items()}
    route_segments = {key: _route_segments(points) for key, points in result.items()}
    pending_segments = {}
    crossing_weights = {}
    for a,b in combinations(keys,2):
        pair_key = (a,b)
        common = {by_edge[a].source, by_edge[a].target} & {by_edge[b].source, by_edge[b].target}
        crossing_weights[pair_key] = 40*max(degree[node] for node in common) if common else 12
        pair_scores[pair_key] = pair_cost(a,result[a],b,result[b])
        pair_keys_by_edge[a].append(pair_key)
        pair_keys_by_edge[b].append(pair_key)

    def affected_pairs(changes):
        pairs = set()
        for key in changes:
            pairs.update(pair_keys_by_edge[key])
        return sorted(pairs)

    def candidate_pair_cost(a, a_points, b, b_points):
        a_bounds = pending_bounds.get(a, route_bounds[a])
        b_bounds = pending_bounds.get(b, route_bounds[b])
        if not _bounds_may_conflict(a_bounds, b_bounds, spacing):
            return 0.0
        return pair_cost(a, a_points, b, b_points)

    def improvement(changes):
        nonlocal pending_bounds, pending_segments
        # Each proposed route participates in many pairs. Its bounds and the
        # current route's length do not need recomputing for every comparison.
        pending_bounds = {key: _bounds(points) for key, points in changes.items()}
        pending_segments = {key: _route_segments(points) for key, points in changes.items()}
        terms = [_length(points)-current_lengths[key] for key,points in sorted(changes.items())]
        for a,b in affected_pairs(changes):
            a_points = changes.get(a,result[a])
            b_points = changes.get(b,result[b])
            terms.append(candidate_pair_cost(a,a_points,b,b_points)-pair_scores[a,b])
        return stable_cost_sum(terms)

    def apply(changes):
        nonlocal pending_bounds, pending_segments
        pending_bounds = {key: _bounds(points) for key, points in changes.items()}
        pending_segments = {key: _route_segments(points) for key, points in changes.items()}
        result.update(changes)
        for a,b in affected_pairs(changes):
            pair_scores[a,b] = candidate_pair_cost(a,result[a],b,result[b])
        route_bounds.update(pending_bounds)
        route_segments.update(pending_segments)
        current_lengths.update({key: _length(points) for key, points in changes.items()})

    pending_bounds = {}

    # Port swaps let channels retain their approach order at the destination.
    # They preserve the same occupied ports and their existing separation.
    port_groups = {}
    for key,points in result.items():
        if len(points) == 2:
            continue
        edge = by_edge[key]
        for end, node_id in ((0,edge.source),(-1,edge.target)):
            node, p = by_id[node_id], points[end]
            side = 'left' if p[0] == node.x else 'right' if p[0] == node.x+node.width else 'top' if p[1] == node.y else 'bottom'
            port_groups.setdefault((node_id,side),[]).append((key,end))

    max_passes = 1 if len(keys) > 64 else 2 if len(keys) > 48 else 4
    for _ in range(max_passes):
        changed = False
        for (_,side), members in sorted(port_groups.items()):
            axis = 1 if side in ('left','right') else 0
            for (a,ae),(b,be) in combinations(sorted(members),2):
                if a == b:
                    continue
                proposals = {}
                for key,end,coordinate in ((a,ae,result[b][be][axis]),(b,be,result[a][ae][axis])):
                    points = [list(p) for p in result[key]]
                    for index in ((0,1) if end == 0 else (-1,-2)):
                        points[index][axis] = coordinate
                    proposals[key] = tuple(map(tuple,points))
                if all(valid(key,points) for key,points in proposals.items()) and cost_less(improvement(proposals), -1e-6):
                    apply(proposals); changed = True

        # Two routes may need to exchange lanes together: either individual
        # move would temporarily overlap the other, trapping a greedy nudge.
        for a,b in combinations(keys,2):
            ea,eb = by_edge[a],by_edge[b]
            if not ({ea.source,ea.target} & {eb.source,eb.target}):
                continue
            if not route_conflicts(result[a],result[b],spacing)[0]:
                continue
            for ai in range(1,len(result[a])-2):
                for bi in range(1,len(result[b])-2):
                    ap,aq = result[a][ai:ai+2]
                    bp,bq = result[b][bi:bi+2]
                    fixed = 1 if ap[1] == aq[1] else 0
                    if bp[fixed] != bq[fixed] or not 0 < abs(ap[fixed]-bp[fixed]) <= 48:
                        continue
                    along = 1-fixed
                    if min(max(ap[along],aq[along]),max(bp[along],bq[along])) <= max(min(ap[along],aq[along]),min(bp[along],bq[along])):
                        continue
                    proposals = {}
                    for key,index,coordinate in ((a,ai,bp[fixed]),(b,bi,ap[fixed])):
                        points = [list(p) for p in result[key]]
                        points[index][fixed] = points[index+1][fixed] = coordinate
                        proposals[key] = tuple(map(tuple,points))
                    if all(valid(key,points) for key,points in proposals.items()) and cost_less(improvement(proposals), -1e-6):
                        apply(proposals); changed = True

        # Slide entire inner segments, retaining orthogonal neighboring legs.
        # Candidate lanes come from surrounding routes and obstacle boundaries.
        for key in keys:
            if len(result[key]) == 2:
                continue
            for index in range(1,len(result[key])-2):
                points = result[key]
                a,b = points[index:index+2]
                fixed = 1 if a[1] == b[1] else 0
                low, high = sorted((a[1-fixed],b[1-fixed]))
                candidates = {a[fixed]+step for step in (-24,-12,12,24)}
                for other,route in result.items():
                    if other == key:
                        continue
                    for p,q in zip(route,route[1:]):
                        if p[fixed] == q[fixed] and min(high,max(p[1-fixed],q[1-fixed])) > max(low,min(p[1-fixed],q[1-fixed])):
                            candidates.update((p[fixed]-spacing,p[fixed]+spacing))
                best, best_delta = None, -1e-6
                for coordinate in sorted(candidates):
                    if abs(coordinate-a[fixed]) > 24+1e-6:
                        continue
                    proposal = [list(p) for p in points]
                    proposal[index][fixed] = proposal[index+1][fixed] = coordinate
                    proposal = tuple(map(tuple,proposal))
                    if not valid(key,proposal):
                        continue
                    delta = improvement({key:proposal})
                    if cost_less(delta, best_delta):
                        best, best_delta = proposal, delta
                if best is not None:
                    apply({key:best}); changed = True
        if not changed:
            break
    return result


def _length(points):
    return stable_cost_sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in zip(points,points[1:]))


def _bounds(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bounds_may_conflict(a, b, spacing):
    return not (
        a[2] + spacing <= b[0]
        or b[2] + spacing <= a[0]
        or a[3] + spacing <= b[1]
        or b[3] + spacing <= a[1]
    )
