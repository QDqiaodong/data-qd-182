from collections import Counter


def ceil_permille(length, permille):
    return (length * (1000 + permille) + 999) // 1000


def requirements(data, warp, picks):
    total_ends = len(warp) * data["repeat_x"]
    total_picks = len(picks) * data["repeat_y"]
    width = total_ends * data["warp_spacing_mm"]
    length = total_picks * data["pick_spacing_mm"]
    warp_unit = ceil_permille(length, data["takeup_permille"]) + data["warp_waste_mm"]
    weft_unit = ceil_permille(width, data["takeup_permille"])
    amounts = Counter()
    for end in warp:
        amounts[("warp", end["yarn"])] += warp_unit * data["repeat_x"]
    for pick in picks:
        amounts[("weft", pick["yarn"])] += weft_unit * data["repeat_y"]
    return {"width_mm": width, "length_mm": length, "total_ends": total_ends,
            "total_picks": total_picks,
            "yarn": [{"orientation": side, "yarn": yarn, "length_mm": amount}
                     for (side, yarn), amount in sorted(amounts.items())]}
