"""Deterministic orthogonal canvas routes around component cards and headers.

The visibility grid contains obstacle boundaries and independently allocated
endpoint ports. Relationships keep their authored direction, including cycles;
route cost discourages bends, shared trunks and crossings without dropping edges.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from functools import lru_cache
import heapq
from math import inf

from .canvas_route_separation import separate_canvas_routes
from .canvas_budget import CanvasWorkBudget
from .canvas_numeric import canonical_cost, stable_cost_sum


def reciprocal_canvas_pairs(edges):
    """One edge each way may share geometry, while keeping both directed IDs."""
    groups = defaultdict(list)
    for edge in edges:
        if edge.source != edge.target:
            groups[tuple(sorted((edge.source, edge.target)))].append(edge)
    pairs = {}
    for members in groups.values():
        if len(members) == 2:
            a, b = members
            if a.source == b.target and a.target == b.source:
                pairs[a.edge_id], pairs[b.edge_id] = b.edge_id, a.edge_id
    return pairs


def route_canvas_connections(nodes, edges, *, clearance=12.0, stub=12.0, reading_edge_ids=(), work_budget=None):
    work_budget = work_budget or CanvasWorkBudget()
    work_budget.require_input(nodes=len(nodes), edges=len(edges))
    by_id = {node.node_id: node for node in nodes}
    edges = sorted(edges, key=lambda edge: (edge.edge_id not in reading_edge_ids, getattr(edge, "layout_role", "BACKBONE") != "BACKBONE", edge.edge_id))
    pairs = reciprocal_canvas_pairs(edges)
    priority = {edge.edge_id: i for i, edge in enumerate(edges)}
    reverse_of = {edge_id: partner for edge_id, partner in pairs.items() if priority[edge_id] > priority[partner]}
    edges = [edge for edge in edges if edge.edge_id not in reverse_of]
    obstacles = [(n.x-clearance, n.y-clearance, n.x+n.width+clearance, n.y+n.height+clearance) for n in nodes]
    ports = defaultdict(list)
    endpoint_records = defaultdict(list)
    min_port_spacing = 16.0

    def side_toward(node, peer):
        if node.node_id == peer.node_id:
            return 'right'
        horizontal_gap = max(peer.x-node.x-node.width, node.x-peer.x-peer.width)
        vertical_gap = max(peer.y-node.y-node.height, node.y-peer.y-peer.height)
        # A distant upper/lower peer should enter from above/below rather than
        # forcing every diagonal relationship into the hub's short left side.
        if horizontal_gap >= 0 and vertical_gap >= 0 and vertical_gap > horizontal_gap:
            return 'bottom' if peer.y > node.y else 'top'
        if node.x + node.width <= peer.x:
            return 'right'
        if peer.x + peer.width <= node.x:
            return 'left'
        return 'bottom' if peer.y >= node.y + node.height else 'top'

    for edge in edges:
        for endpoint, node_id, peer_id in [('source', edge.source, edge.target), ('target', edge.target, edge.source)]:
            node, peer = by_id[node_id], by_id[peer_id]
            side = side_toward(node, peer)
            if edge.source == edge.target and endpoint == 'target':
                side = 'top'
            endpoint_records[node_id].append((peer_id, edge.edge_id, endpoint, side))

    def side_capacity(node, side):
        length = node.height if side in ('left', 'right') else node.width
        return max(1, int(max(0.0, length - 28.0) // min_port_spacing) + 1)

    def side_scores(node, peer):
        cx, cy = node.x + node.width/2, node.y + node.height/2
        px, py = peer.x + peer.width/2, peer.y + peer.height/2
        dx, dy = px-cx, py-cy
        return {'right': dx, 'left': -dx, 'bottom': dy, 'top': -dy}

    # A card side has finite visual capacity. When a dense hub exceeds it,
    # spill the least geometrically committed endpoints onto adjacent sides
    # instead of shrinking spacing until ports become indistinguishable.
    for node_id, records in sorted(endpoint_records.items()):
        node = by_id[node_id]
        capacities = {side: side_capacity(node, side) for side in ('left','right','top','bottom')}
        if len(records) > sum(capacities.values()):
            raise ValueError(
                f'Canvas node port capacity exceeded: {node_id} needs {len(records)} endpoints '
                f'but geometry safely supports {sum(capacities.values())}'
            )
        loads = defaultdict(int)
        ranked=[]
        for peer_id, edge_id, endpoint, preferred in records:
            peer=by_id[peer_id]
            scores=side_scores(node,peer)
            ordered=sorted(scores, key=lambda side:(-scores[side],side))
            strength=scores[ordered[0]]-scores[ordered[1]] if len(ordered)>1 else 0.0
            ranked.append((-strength,edge_id,endpoint,peer_id,preferred,scores))
        for _strength, edge_id, endpoint, peer_id, preferred, scores in sorted(ranked):
            candidates=[side for side in ('left','right','top','bottom') if loads[side] < capacities[side]]
            side=min(candidates,key=lambda candidate:(candidate!=preferred,-scores[candidate],loads[candidate]/capacities[candidate],candidate))
            loads[side]+=1
            peer=by_id[peer_id]
            peer_order = peer.y + peer.height/2 if side in ('left', 'right') else peer.x + peer.width/2
            ports[node_id, side].append((peer_order, peer_id, edge_id, endpoint))

    endpoints = {}
    port_load = {}
    xs, ys = set(), set()
    for left, top, right, bottom in obstacles:
        xs.update((left, right))
        ys.update((top, bottom))
    for (node_id, side), members in sorted(ports.items()):
        node = by_id[node_id]
        length = node.height if side in ('left', 'right') else node.width
        spacing = min(24.0, (length - 28) / max(1, len(members)-1))
        for index, (_, _, edge_id, endpoint) in enumerate(sorted(members)):
            offset = (index-(len(members)-1)/2) * spacing
            if side == 'right':
                port = (node.x+node.width, node.y+node.height/2+offset)
                escape = (port[0]+stub, port[1])
            elif side == 'left':
                port = (node.x, node.y+node.height/2+offset)
                escape = (port[0]-stub, port[1])
            elif side == 'bottom':
                port = (node.x+node.width/2+offset, node.y+node.height)
                escape = (port[0], port[1]+stub)
            else:
                port = (node.x+node.width/2+offset, node.y)
                escape = (port[0], port[1]-stub)
            endpoints[edge_id, endpoint] = (port, escape)
            port_load[edge_id, endpoint] = len(members)
            xs.add(escape[0])
            ys.add(escape[1])
    def segment_clear(a, b, excluded=()):
        if a[0] != b[0] and a[1] != b[1]:
            return False
        for node, (left, top, right, bottom) in zip(nodes, obstacles):
            if node.node_id in excluded:
                continue
            hit = (left < a[0] < right and max(min(a[1], b[1]), top) < min(max(a[1], b[1]), bottom)) if a[0] == b[0] else (top < a[1] < bottom and max(min(a[0], b[0]), left) < min(max(a[0], b[0]), right))
            if hit:
                return False
        return True

    # Align facing ports for every clear direct connection, not only reciprocal
    # ones. Prefer the busier endpoint's port, then a free common coordinate.
    for edge in edges:
        source, source_escape = endpoints[edge.edge_id, 'source']
        target, target_escape = endpoints[edge.edge_id, 'target']
        vertical = source[0] == source_escape[0] and target[0] == target_escape[0]
        horizontal = source[1] == source_escape[1] and target[1] == target_escape[1]
        if not (vertical or horizontal):
            continue
        axis = 0 if vertical else 1
        normal = 1-axis
        if (source_escape[normal]-source[normal])*(target_escape[normal]-target[normal]) >= 0:
            continue
        a, b = by_id[edge.source], by_id[edge.target]
        low = max(a.x, b.x)+14 if vertical else max(a.y, b.y)+14
        high = min(a.x+a.width, b.x+b.width)-14 if vertical else min(a.y+a.height, b.y+b.height)-14
        if low > high:
            continue
        busy, free = (source, target) if port_load[edge.edge_id,'source'] >= port_load[edge.edge_id,'target'] else (target, source)
        candidates = [busy[axis], free[axis], (low+high)/2]
        candidates += sorted({max(low,min(high,point+delta)) for point in candidates for delta in (-12,-6,6,12)}, key=lambda value:(abs(value-busy[axis]),value))
        own_keys = {(edge.edge_id, 'source'), (edge.edge_id, 'target')}
        for coordinate in candidates:
            if not low <= coordinate <= high:
                continue
            start, end = list(source), list(target)
            start[axis] = end[axis] = coordinate
            if any(port in (tuple(start),tuple(end)) for key,(port,_) in endpoints.items() if key not in own_keys):
                continue
            if not segment_clear(start,end,excluded=(edge.source,edge.target)):
                continue
            for name,port,escape in [('source',start,source_escape),('target',end,target_escape)]:
                adjusted = list(escape); adjusted[axis] = coordinate
                endpoints[edge.edge_id,name] = tuple(port),tuple(adjusted)
                xs.add(adjusted[0]); ys.add(adjusted[1])
            break
    if not edges:
        return {}
    xs.update((min(xs)-stub, max(xs)+stub))
    ys.update((min(ys)-stub, max(ys)+stub))
    # Offer separate parallel tracks instead of forcing a shared branching bus.
    # Terminal runs stay inside the padded card exclusion zone.
    def tracks(values):
        ordered = sorted(values)
        return sorted(set(ordered) | {track for a, b in zip(ordered, ordered[1:])
                                      for track in (a+6, a+12, b-12, b-6) if a+5 < track < b-5})

    xs, ys = tracks(xs), tracks(ys)
    width = len(xs)
    x_index, y_index = {x:i for i,x in enumerate(xs)}, {y:i for i,y in enumerate(ys)}
    blocked = set()
    for left, top, right, bottom in obstacles:
        for y in range(bisect_right(ys, top), bisect_left(ys, bottom)):
            blocked.update(y*width+x for x in range(bisect_right(xs, left), bisect_left(xs, right)))

    # Obstacle geometry is fixed for this layout. Index spans once rather than
    # testing every card for every visited grid segment on every connection.
    # Strict inequalities retain travel along obstacle boundaries unchanged.
    row_spans = [
        [(left, right) for left, top, right, bottom in obstacles if top < y < bottom]
        for y in ys
    ]
    column_spans = [
        [(top, bottom) for left, top, right, bottom in obstacles if left < x < right]
        for x in xs
    ]

    def index(point):
        return y_index[point[1]]*width + x_index[point[0]]

    def point(vertex):
        return xs[vertex % width], ys[vertex // width]

    @lru_cache(maxsize=None)
    def neighbors(vertex):
        x, y = vertex % width, vertex // width
        result = []
        for dx, dy, direction in [(1,0,1),(-1,0,1),(0,1,2),(0,-1,2)]:
            nx, ny = x+dx, y+dy
            other = ny*width+nx
            if not (0 <= nx < len(xs) and 0 <= ny < len(ys)) or other in blocked:
                continue
            # Adjacent valid vertices can straddle an obstacle that contains no
            # interior coordinate. Check segments as well as vertex occupancy.
            ax, ay, bx, by = xs[x], ys[y], xs[nx], ys[ny]
            low, high = (min(ay, by), max(ay, by)) if direction == 2 else (min(ax, bx), max(ax, bx))
            spans = column_spans[x] if direction == 2 else row_spans[y]
            hits = any(low < end and start < high for start, end in spans)
            if not hits:
                result.append((other, direction, abs(bx-ax)+abs(by-ay)))
        return result

    used_directions = defaultdict(set)
    parallel_tracks = {1: defaultdict(list), 2: defaultdict(list)}
    routes = {}
    for edge in edges:
        start_port, start_escape = endpoints[edge.edge_id, 'source']
        end_port, end_escape = endpoints[edge.edge_id, 'target']
        start, end = index(start_escape), index(end_escape)
        if start in blocked or end in blocked:
            raise ValueError(f'Canvas endpoint has no clear escape: {edge.edge_id}')
        start_direction = 1 if start_port[1] == start_escape[1] else 2
        end_direction = 1 if end_port[1] == end_escape[1] else 2
        prefer_straight = start_direction == end_direction and segment_clear(start_escape,end_escape)
        track_coordinates = {axis: sorted(tracks) for axis,tracks in parallel_tracks.items()}

        @lru_cache(maxsize=None)
        def proximity_cost(vertex, other, axis):
            a, b = point(vertex), point(other)
            fixed, low, high = (a[1], min(a[0],b[0]), max(a[0],b[0])) if axis == 1 else (a[0], min(a[1],b[1]), max(a[1],b[1]))
            coordinates = track_coordinates[axis]
            contributions = []
            for track in coordinates[bisect_right(coordinates,fixed-12):bisect_left(coordinates,fixed+12)]:
                for start_at,end_at in parallel_tracks[axis][track]:
                    overlap = max(0.0, min(high,end_at)-max(low,start_at))
                    contributions.append(overlap * (0.5 if track == fixed else 0.3*(1-abs(track-fixed)/12)))
            return stable_cost_sum(contributions)
        initial = start, start_direction
        distance, previous = {initial:0.0}, {}
        heap = [(abs(start_escape[0]-end_escape[0])+abs(start_escape[1]-end_escape[1]),0.0,start,start_direction)]
        final = None
        while heap:
            work_budget.consume("route_expansions")
            _, cost, vertex, direction = heapq.heappop(heap)
            current = vertex, direction
            if cost != distance.get(current):
                continue
            if vertex == end:
                final = current
                break
            for other, next_direction, length in neighbors(vertex):
                work_budget.consume("route_candidates")
                turn = 8.0 if next_direction != direction else 0.0
                crossing = 2.0 if used_directions[other] - {next_direction} else 0.0
                # Congestion is a small preference, not permission to send a
                # nearby dependency around the perimeter of the architecture.
                shared = proximity_cost(vertex,other,next_direction)
                if prefer_straight:
                    # A clear straight route wins over crossing avoidance. The
                    # renderer distinguishes crossings without inventing bends.
                    crossing = shared = 0.0
                end_turn = 8.0 if other == end and next_direction != end_direction else 0.0
                # The term order is an explicit part of the numeric policy.
                # This is a fixed six-term expression, so avoid the variable-
                # length fsum path in the A* hot loop and quantize once at the
                # actual ordering boundary.
                candidate = canonical_cost(cost + length + turn + crossing + shared + end_turn)
                target_state = other, next_direction
                if candidate >= distance.get(target_state, inf):
                    continue
                distance[target_state] = candidate
                previous[target_state] = current
                x,y = point(other)
                estimate = abs(x-end_escape[0])+abs(y-end_escape[1])
                heapq.heappush(heap, (candidate+estimate,candidate,other,next_direction))
        if final is None:
            raise ValueError(f'No obstacle-free canvas route: {edge.edge_id}')
        vertices = []
        while True:
            vertices.append(final[0])
            if final == initial:
                break
            final = previous[final]
        vertices.reverse()
        for a,b in zip(vertices,vertices[1:]):
            direction = 1 if a//width == b//width else 2
            used_directions[a].add(direction)
            used_directions[b].add(direction)
        raw = [start_port, start_escape, *(point(v) for v in vertices), end_escape, end_port]
        compressed = []
        for p in raw:
            if compressed and p == compressed[-1]:
                continue
            if len(compressed) >= 2:
                a,b = compressed[-2:]
                if (a[0] == b[0] == p[0] and min(a[1],p[1]) <= b[1] <= max(a[1],p[1])) or (a[1] == b[1] == p[1] and min(a[0],p[0]) <= b[0] <= max(a[0],p[0])):
                    compressed[-1] = p
                    continue
            compressed.append(p)
        routes[edge.edge_id] = tuple(compressed)
        for a,b in zip(compressed,compressed[1:]):
            if a[1] == b[1]:
                parallel_tracks[1][a[1]].append((min(a[0],b[0]),max(a[0],b[0])))
            else:
                parallel_tracks[2][a[0]].append((min(a[1],b[1]),max(a[1],b[1])))
    routes = separate_canvas_routes(
        nodes,
        edges,
        routes,
        clearance=clearance,
        work_budget=work_budget,
    )
    routes.update({edge_id: tuple(reversed(routes[primary])) for edge_id, primary in reverse_of.items()})
    return routes
