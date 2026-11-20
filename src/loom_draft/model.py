from .io import fields, integer, records, rows, label


def validate(data):
    fields(data, ("shafts", "tie_up", "warp", "picks", "repeat_x", "repeat_y",
                  "warp_spacing_mm", "pick_spacing_mm", "takeup_permille", "warp_waste_mm", "float_limit"))
    shafts = integer(data["shafts"], "shafts", 1, 16)
    tie_up = records(data["tie_up"], "treadle", 1, 32)
    for treadle in tie_up.values():
        fields(treadle, ("id", "shafts"))
        values = rows(treadle["shafts"], "treadle shafts", 0, shafts)
        for value in values:
            integer(value, "shaft number", 1, shafts)
        if len(set(values)) != len(values):
            raise ValueError("duplicate shaft in treadle")
    warp = rows(data["warp"], "warp", 1, 128)
    for end in warp:
        fields(end, ("shaft", "yarn"))
        integer(end["shaft"], "warp shaft", 1, shafts)
        label(end["yarn"], "warp yarn")
    picks = rows(data["picks"], "picks", 1, 128)
    for pick in picks:
        fields(pick, ("treadles", "yarn"))
        values = rows(pick["treadles"], "pick treadles", 0, 32)
        if any(not isinstance(value, str) or value not in tie_up for value in values):
            raise ValueError("unknown treadle")
        if len(set(values)) != len(values):
            raise ValueError("duplicate treadle in pick")
        label(pick["yarn"], "weft yarn")
    integer(data["repeat_x"], "repeat_x", 1, 1000)
    integer(data["repeat_y"], "repeat_y", 1, 1000)
    integer(data["warp_spacing_mm"], "warp_spacing_mm", 1, 100)
    integer(data["pick_spacing_mm"], "pick_spacing_mm", 1, 100)
    integer(data["takeup_permille"], "takeup_permille", 0, 1000)
    integer(data["warp_waste_mm"], "warp_waste_mm", 0, 10000)
    integer(data["float_limit"], "float_limit", 1, 1000)
    return tie_up, warp, picks
