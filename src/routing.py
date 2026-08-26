"""
Risk-aware routing: A* search over a *local* risk grid from `risk_surface`,
to the nearest genuinely-safer zone, minimizing distance *and* risk exposure
along the way - not just straight-line nearest-exit, and not full
street-level turn-by-turn (that needs a real road graph, out of scope for
now).

Deliberately builds its own fine-grained grid around the query point rather
than searching the national risk_surface grid: routing needs city-block
resolution to be useful ("which street", not "which province"), and a
"nearest safe zone" search shouldn't be able to wander hundreds of km away
on a coarse national grid just because that's the grid that happened to be
built for a different purpose (general risk lookup / broadcast targeting).
"""
import heapq
import itertools

import numpy as np

from src.risk_surface import KM_PER_DEG_LAT, KM_PER_DEG_LON, build_risk_grid

# Local grid window around the query point: ~0.15 deg of lat is roughly a
# 33km-wide box at this resolution/size -> ~800m cells, fine enough for a
# real "walk this way" route without paying national-grid build cost.
LOCAL_HALF_WINDOW_DEG = 0.15
LOCAL_GRID_SIZE = 40


def _nearest_cell(lat, lon, lat_centers, lon_centers):
    i = int(np.argmin(np.abs(lat_centers - lat)))
    j = int(np.argmin(np.abs(lon_centers - lon)))
    return i, j


def _pick_safe_zone(grid, start, size, min_cell_dist=2, search_radius=25):
    """Lowest-risk cell reachable within search_radius, preferring closer among ties."""
    si, sj = start
    best_key, best_cell = None, None
    for i in range(max(0, si - search_radius), min(size, si + search_radius + 1)):
        for j in range(max(0, sj - search_radius), min(size, sj + search_radius + 1)):
            dist = ((i - si) ** 2 + (j - sj) ** 2) ** 0.5
            if dist < min_cell_dist:
                continue
            key = (round(float(grid[i, j]), 4), dist)
            if best_key is None or key < best_key:
                best_key, best_cell = key, (i, j)
    return best_cell


def _astar(grid, start, goal, risk_penalty, size):
    counter = itertools.count()

    def h(node):
        return ((node[0] - goal[0]) ** 2 + (node[1] - goal[1]) ** 2) ** 0.5

    open_heap = [(h(start), 0.0, next(counter), start)]
    g_score = {start: 0.0}
    came_from = {}
    closed = set()

    while open_heap:
        _, g, _, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        closed.add(current)
        if current == goal:
            break
        ci, cj = current
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if di == 0 and dj == 0:
                    continue
                ni, nj = ci + di, cj + dj
                if not (0 <= ni < size and 0 <= nj < size):
                    continue
                neighbor = (ni, nj)
                if neighbor in closed:
                    continue
                step = (di ** 2 + dj ** 2) ** 0.5
                avg_risk = (grid[ci, cj] + grid[ni, nj]) / 2
                tentative_g = g + step * (1 + risk_penalty * avg_risk)
                if tentative_g < g_score.get(neighbor, float("inf")):
                    g_score[neighbor] = tentative_g
                    came_from[neighbor] = current
                    heapq.heappush(open_heap, (tentative_g + h(neighbor), tentative_g, next(counter), neighbor))

    if goal != start and goal not in came_from:
        return None

    path = [goal]
    node = goal
    while node != start:
        node = came_from[node]
        path.append(node)
    path.reverse()
    return path


def find_safe_route(start_lat: float, start_lon: float, risk_penalty: float = 4.0):
    """
    risk_penalty controls how strongly the route avoids risky cells vs.
    taking the shortest path - 0 = ignore risk entirely, higher = detour
    more readily to stay in safer cells.
    """
    bbox = (
        start_lat - LOCAL_HALF_WINDOW_DEG, start_lon - LOCAL_HALF_WINDOW_DEG,
        start_lat + LOCAL_HALF_WINDOW_DEG, start_lon + LOCAL_HALF_WINDOW_DEG,
    )
    # Tighter bandwidth than the national default (6km) - this grid's cells
    # are ~800m, so risk needs to vary over a similarly local scale to mean
    # anything for routing.
    grid, lat_centers, lon_centers = build_risk_grid(bbox=bbox, grid_size=LOCAL_GRID_SIZE, spatial_bandwidth_km=1.2)
    size = grid.shape[0]

    start = _nearest_cell(start_lat, start_lon, lat_centers, lon_centers)
    goal = _pick_safe_zone(grid, start, size)
    if goal is None:
        return None

    path = _astar(grid, start, goal, risk_penalty, size)
    if path is None:
        return None

    waypoints = [
        {"lat": float(lat_centers[i]), "lon": float(lon_centers[j]), "risk": round(float(grid[i, j]), 3)}
        for i, j in path
    ]

    dist_km = 0.0
    for a, b in zip(waypoints, waypoints[1:]):
        dlat = (a["lat"] - b["lat"]) * KM_PER_DEG_LAT
        dlon = (a["lon"] - b["lon"]) * KM_PER_DEG_LON
        dist_km += (dlat ** 2 + dlon ** 2) ** 0.5

    si, sj = start
    return {
        "start": {"lat": start_lat, "lon": start_lon, "risk": round(float(grid[si, sj]), 3)},
        "safe_zone": {"lat": waypoints[-1]["lat"], "lon": waypoints[-1]["lon"], "risk": waypoints[-1]["risk"]},
        "waypoints": waypoints,
        "distance_km": round(dist_km, 2),
    }
