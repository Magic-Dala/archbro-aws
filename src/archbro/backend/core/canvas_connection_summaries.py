"""Lossless reading summaries over canonical, already-routed relationships.

Only same-boundary peers, a shared endpoint, one direction and one explicit
relationship category can share a visual trunk. Individual actions are retained.
The backend connects short source branches to an existing shared trunk as early
as obstacles allow, comparing compatible group coverage when a fixed trunk
misses peers. Group-search detours are capped at 25% of the original route plus
the existing port allowance and require at least 20% less total drawn ink.
Original routes and architecture entities remain unchanged;
no browser-generated geometry is introduced.
"""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json

from .canvas_budget import CanvasWorkBudget
from .canvas_numeric import stable_cost_sum


VERSION = "archbro.connection-summaries.v1"
ROUTING_POLICY = "coverage-shared-trunk.v2"


def _length(points):
    return stable_cost_sum(abs(a[0]-b[0])+abs(a[1]-b[1]) for a,b in zip(points,points[1:]))


def _points(points):
    result=[]
    for p in points:
        if not result or p != result[-1]:
            result.append(p)
    return result


def _intersections(a,b,c,d):
    horizontal = a[1] == b[1]
    other_horizontal = c[1] == d[1]
    if horizontal != other_horizontal:
        p=(c[0],a[1]) if horizontal else (a[0],c[1])
        if all(min(u,v) <= w <= max(u,v) for u,v,w in zip(a,b,p)) and all(min(u,v) <= w <= max(u,v) for u,v,w in zip(c,d,p)):
            return [p]
        return []
    fixed=1 if horizontal else 0
    along=1-fixed
    if a[fixed] != c[fixed]:
        return []
    low=max(min(a[along],b[along]),min(c[along],d[along]))
    high=min(max(a[along],b[along]),max(c[along],d[along]))
    if low > high:
        return []
    return [(value,a[1]) if horizontal else (a[0],value) for value in sorted({low,high})]


def _compact(points):
    result=[]
    for point in _points(points):
        while len(result)>1:
            a,b=result[-2:]
            if (b[0]-a[0])*(point[1]-b[1]) != (b[1]-a[1])*(point[0]-b[0]) or (b[0]-a[0])*(point[0]-b[0])+(b[1]-a[1])*(point[1]-b[1])<=0:
                break
            result.pop()
        result.append(point)
    return result


def _simple(points):
    for a,b,c in zip(points,points[1:],points[2:]):
        if (b[0]-a[0])*(c[0]-b[0])+(b[1]-a[1])*(c[1]-b[1])<0:
            return False
    segments=list(zip(points,points[1:]))
    return not any(_intersections(a,b,c,d) for i,(a,b) in enumerate(segments) for c,d in segments[i+2:])


def _clear(points,boxes,unrelated):
    for a,b in zip(points,points[1:]):
        horizontal=a[1]==b[1]
        for left,top,right,bottom in boxes:
            hit=(top<a[1]<bottom and max(min(a[0],b[0]),left)<min(max(a[0],b[0]),right)) if horizontal else (left<a[0]<right and max(min(a[1],b[1]),top)<min(max(a[1],b[1]),bottom))
            if hit:
                return False
        # A new branch may cross an unrelated connector, but cannot masquerade
        # as its shared trunk. The renderer keeps ordinary crossings cased.
        for route in unrelated:
            for c,d in zip(route,route[1:]):
                hits=_intersections(a,b,c,d)
                if len(hits)>1 and _length(hits)>4:
                    return False
    return True


def _contains(point, a, b):
    return (
        (a[0] == b[0] == point[0] and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))
        or (a[1] == b[1] == point[1] and min(a[0], b[0]) <= point[0] <= max(a[0], b[0]))
    )


def _ray(point, other):
    if other[0] < point[0]: return 'L'
    if other[0] > point[0]: return 'R'
    if other[1] < point[1]: return 'U'
    if other[1] > point[1]: return 'D'
    return None


def _branch_points(paths):
    """Return the real T/cross vertices of the final rendered path union.

    A branch can overlap the trunk after its logical join and diverge later.
    Marking the logical join makes a dot appear on an ordinary straight line.
    Derive markers from the final member geometry instead.
    """
    segments=[(a,b) for path in paths for a,b in zip(path,path[1:])]
    candidates={point for segment in segments for point in segment}
    for index,(a,b) in enumerate(segments):
        for c,d in segments[index+1:]:
            candidates.update(_intersections(a,b,c,d))
    result=[]
    for point in candidates:
        rays=set()
        for a,b in segments:
            if not _contains(point,a,b):
                continue
            if point != a:
                direction=_ray(point,a)
                if direction: rays.add(direction)
            if point != b:
                direction=_ray(point,b)
                if direction: rays.add(direction)
        if len(rays) >= 3:
            result.append(point)
    return sorted(set(result))


def _join(branch,trunk,boxes=(),unrelated=(),*,group_search=False,segment_clear=None):
    candidates=[]
    original_length=_length(branch)
    # Independent canonical routes can travel on opposite sides of an obstacle.
    # A group may trade a bounded member detour for substantially less total ink;
    # the ordinary pairwise search keeps its original strict budget.
    detour_budget=max(24,original_length*0.25) if group_search else 24
    port_shift=min(24,_length([branch[-1],trunk[-1]]))
    segment_clear=segment_clear or (lambda a,b:_clear([a,b],boxes,unrelated))

    def clear_bridge(points):
        return all(segment_clear(a,b) for a,b in zip(points,points[1:]))

    def consider(prefix,point,j):
        prefix=_compact(prefix)
        suffix=_points([point,*trunk[j+1:]])
        if _length(prefix)<24 or _length(suffix)<48 or _length(branch)-_length(prefix)<48:
            return
        member=_compact([*prefix,*suffix[1:]])
        if _length(member)>original_length+detour_budget+port_shift+1e-6 or not _simple(member):
            return
        # Earliest safe convergence inside the applicable detour budget:
        # minimize the independent branch before bends/total length.
        candidates.append((_length(prefix),len(prefix),_length(member),point,prefix,member))
        return True

    for i,(a,b) in enumerate(zip(branch,branch[1:])):
        for j,(c,d) in enumerate(zip(trunk,trunk[1:])):
            for point in _intersections(a,b,c,d):
                consider([*branch[:i+1],point],point,j)
    if boxes:
        # Keep the real source port and its outgoing direction. A 12-unit
        # escape plus original corners bounds the deterministic search.
        a,b=branch[:2]
        distance=abs(b[0]-a[0])+abs(b[1]-a[1])
        escape=(a[0]+(b[0]-a[0])*min(12,distance)/distance,a[1]+(b[1]-a[1])*min(12,distance)/distance)
        anchors=[([a,escape],escape)]+[(branch[:i+1],point) for i,point in enumerate(branch[1:-1],1)]
        for prefix,anchor in anchors:
            for j,(c,d) in enumerate(zip(trunk,trunk[1:])):
                horizontal=c[1]==d[1]
                axis=0 if horizontal else 1
                low,high=sorted((c[axis],d[axis]))
                coordinates={max(low,min(high,anchor[axis])),low,high}
                for box in boxes:
                    coordinates.update(value for value in (box[axis],box[axis+2]) if low<=value<=high)
                for coordinate in sorted(coordinates):
                    point=(coordinate,c[1]) if horizontal else (c[0],coordinate)
                    # Reject impossible bridges before the bounded obstacle-rail
                    # search. Every proposed bridge is monotone between anchors.
                    shortest_prefix=_length(prefix)+_length([anchor,point])
                    suffix=_length([point,*trunk[j+1:]])
                    if shortest_prefix+suffix>original_length+detour_budget+port_shift+1e-6:
                        continue
                    accepted=False
                    for corner in ((point[0],anchor[1]),(anchor[0],point[1])):
                        bridge=_compact([anchor,corner,point])
                        if clear_bridge(bridge):
                            accepted=bool(consider([*prefix,*bridge[1:]],point,j)) or accepted
                    if group_search and not accepted:
                        # A single elbow cannot thread the gap between two rows
                        # of cards. Try two elbows on actual obstacle boundaries,
                        # never an unbounded grid or a browser-generated shortcut.
                        for axis in (0,1):
                            rails=sorted({box[index] for box in boxes for index in (axis,axis+2)
                                          if min(anchor[axis],point[axis])<box[index]<max(anchor[axis],point[axis])})
                            for rail in rails:
                                corners=((rail,anchor[1]),(rail,point[1])) if axis==0 else ((anchor[0],rail),(point[0],rail))
                                bridge=_compact([anchor,*corners,point])
                                if clear_bridge(bridge):
                                    consider([*prefix,*bridge[1:]],point,j)
    if not candidates:
        return None
    _,_,_,point,prefix,member=min(candidates)
    return point,prefix,member


def _ink_length(paths):
    """Length of the drawn orthogonal union, counting overlapping ink once."""
    lines=defaultdict(list)
    for path in paths:
        for a,b in zip(path,path[1:]):
            horizontal=a[1]==b[1]
            axis=0 if horizontal else 1
            lines[horizontal,a[1-axis]].append(tuple(sorted((a[axis],b[axis]))))
    total=0.0
    for intervals in lines.values():
        end=None
        for low,high in sorted(intervals):
            total=stable_cost_sum((total,max(0,high-max(low,end)) if end is not None else high-low))
            end=max(high,end) if end is not None else high
    return total


def _shared_suffix(trunk,junctions):
    suffixes=[_points([point,*trunk[i+1:]])
              for point in junctions for i,(a,b) in enumerate(zip(trunk,trunk[1:]))
              if _contains(point,a,b)]
    return min(suffixes,key=_length) if suffixes else []


def canvas_connection_summaries(diagram,graph,*,work_budget=None):
    work_budget = work_budget or CanvasWorkBudget()
    nodes={node.id:node for node in diagram.nodes}
    routes={edge.edge_id:edge for edge in graph.edges}
    edges={edge.id:edge for edge in diagram.edges if edge.id in routes}
    boxes=[(n.x-12,n.y-12,n.x+n.width+12,n.y+n.height+12) for n in getattr(graph,"nodes",())]
    endpoint_pairs={(edge.source,edge.target) for edge in edges.values()}

    def domain(node_id):
        seen=set()
        while nodes[node_id].parent_id in nodes and node_id not in seen:
            seen.add(node_id);node_id=nodes[node_id].parent_id
        return node_id

    candidates=defaultdict(list)
    for edge in sorted(edges.values(),key=lambda e:e.id):
        # Reciprocal pairs remain explicit. Same-direction parallel actions are
        # canonical facts of one logical peer connection and may participate in
        # a higher-level shared trunk through that peer's representative route.
        if edge.source==edge.target or (edge.target,edge.source) in endpoint_pairs:
            continue
        category=str(edge.relationship_category)
        for direction,hub,peer in (('IN',edge.target,edge.source),('OUT',edge.source,edge.target)):
            root=domain(peer)
            # Sibling services inside one architecture boundary may share a
            # trunk as long as the root itself is not being used as a peer or
            # hub. The old same-domain exclusion forced dense internal hubs
            # (for example API -> Context/Governance/Layout/Lifecycle) into a
            # fan of visually independent lines.
            if root==peer or root==hub:
                continue
            candidates[direction,hub,root,category].append(edge)

    groups=[];used=set()
    for (direction,hub,root,category),members in sorted(candidates.items(),key=lambda item:(-len(item[1]),item[0])):
        work_budget.consume("summary_candidates", len(members))
        available=[edge for edge in members if edge.id not in used]
        peer_for=lambda edge:edge.source if direction=='IN' else edge.target
        peer_groups=defaultdict(list)
        for edge in available:
            peer_groups[peer_for(edge)].append(edge)
        if len(peer_groups)<2:
            continue
        oriented_all={edge.id:[(p.x,p.y) for p in routes[edge.id].points][::1 if direction=='IN' else -1] for edge in available}
        representatives=[]
        representative_members={}
        for peer,peer_edges in sorted(peer_groups.items()):
            work_budget.consume("summary_candidates", len(peer_edges))
            representative=max(peer_edges,key=lambda edge:(_length(oriented_all[edge.id]),edge.id))
            representatives.append(representative)
            representative_members[representative.id]=sorted(peer_edges,key=lambda edge:edge.id)
        oriented={edge.id:oriented_all[edge.id] for edge in representatives}
        primary=max(representatives,key=lambda edge:(_length(oriented[edge.id]),edge.id))
        available_ids={edge.id for edge in available}
        unrelated=[[(p.x,p.y) for p in route.points] for key,route in routes.items() if key not in available_ids]
        @lru_cache(maxsize=32768)
        def segment_clear(a,b):
            return _clear([a,b],boxes,unrelated)

        def plan(candidate,group_search=False):
            trunk=oriented[candidate.id]
            paths={candidate.id:trunk};branches=[];junctions=[]
            for edge in sorted(representatives,key=lambda edge:edge.id):
                if edge.id==candidate.id:
                    continue
                joined=_join(oriented[edge.id],trunk,boxes,unrelated,
                             group_search=group_search,segment_clear=segment_clear)
                if joined:
                    point,prefix,member=joined
                    paths[edge.id]=member;branches.append(prefix);junctions.append(point)
            return paths,branches,junctions

        paths,branches,junctions=plan(primary)
        if len(paths)<len(representatives):
            best_score=(-len(paths),-_length(_shared_suffix(oriented[primary.id],junctions)),_ink_length(paths.values()),primary.id)
            for candidate in sorted(representatives,key=lambda edge:edge.id):
                work_budget.consume("summary_candidates")
                proposed,new_branches,new_junctions=plan(candidate,group_search=True)
                ink=_ink_length(proposed.values())
                original_ink=stable_cost_sum(_length(oriented[edge_id]) for edge_id in proposed)
                # A relaxed per-member budget is legal only when the whole
                # representative group saves at least 20% of independent ink.
                # Full membership alone can still leave one long independent
                # line until just above the hub. Prefer a genuinely early common
                # suffix before optimizing ink among equally complete plans.
                score=(-len(proposed),-_length(_shared_suffix(oriented[candidate.id],new_junctions)),ink,candidate.id)
                if len(proposed)>=2 and ink<=original_ink*0.8 and score<best_score:
                    primary=candidate;paths=proposed;branches=new_branches;junctions=new_junctions
                    best_score=score
        trunk=oriented[primary.id]
        if len(paths)<2:
            continue
        shared=_shared_suffix(trunk,junctions)
        accepted_representatives=set(paths)
        member_ids=sorted(
            member.id
            for representative_id in accepted_representatives
            for member in representative_members[representative_id]
        )
        # A stable ID binds the exact member set, direction and accepted version.
        key=json.dumps([ROUTING_POLICY,graph.architecture_version,direction,hub,root,category,member_ids],separators=(',',':'))
        identifier='summary:'+hashlib.sha256(key.encode()).hexdigest()[:24]
        encode=lambda points:[dict(x=x,y=y) for x,y in (points if direction=='IN' else list(reversed(points)))]
        true_junctions=_branch_points(list(paths.values()))
        member_paths={}
        for representative_id,points in sorted(paths.items()):
            for member in representative_members[representative_id]:
                # The representative owns the grouped geometry. Parallel
                # canonical actions retain their exact original route for
                # individual inspection/drilldown.
                member_paths[member.id]=encode(points if member.id==representative_id else oriented_all[member.id])
        groups.append(dict(id=identifier,architecture_version=graph.architecture_version,direction=direction,hub_node_id=hub,domain_node_id=root,relationship_category=category,member_edge_ids=member_ids,primary_edge_id=primary.id,shared_path=encode(shared),paths=[encode(trunk),*(encode(branch) for branch in branches)],junctions=[dict(x=x,y=y) for x,y in true_junctions],member_paths=member_paths))
        used.update(member_ids)
    return dict(schema=VERSION,routing_policy=ROUTING_POLICY,architecture_version=graph.architecture_version,groups=sorted(groups,key=lambda group:group['id']))
