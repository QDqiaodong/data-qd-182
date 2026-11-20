"""Lifts are the union of active treadles; 1 means warp above weft."""


def draw(tie_up, warp, picks):
    lifts = []
    grid = []
    for pick in picks:
        active = set()
        for treadle in pick["treadles"]:
            active.update(tie_up[treadle]["shafts"])
        lifts.append(sorted(active))
        grid.append([int(end["shaft"] in active) for end in warp])
    return grid, lifts


def circular_float(sequence, target):
    if all(value == target for value in sequence):
        return {"unbounded": True, "max_crossings": None}
    best, current = 0, 0
    for value in sequence + sequence:
        current = current + 1 if value == target else 0
        best = max(best, current)
    return {"unbounded": False, "max_crossings": best}


def inspect(grid, limit):
    warp = [circular_float([row[x] for row in grid], 1) for x in range(len(grid[0]))]
    weft = [circular_float(row, 0) for row in grid]
    violations = []
    for orientation, values in (("warp", warp), ("weft", weft)):
        for index, value in enumerate(values):
            if value["unbounded"] or value["max_crossings"] > limit:
                violations.append({"orientation": orientation, "repeat_index": index,
                                   **value})
    return {"warp_floats": warp, "weft_floats": weft, "violations": violations,
            "passes_float_limit": not violations}
