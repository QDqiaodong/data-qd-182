from .model import validate
from .weave import draw, inspect
from .materials import requirements


def run(data):
    tie_up, warp, picks = validate(data)
    grid, lifts = draw(tie_up, warp, picks)
    quality = inspect(grid, data["float_limit"])
    material = requirements(data, warp, picks)
    return {"schema_version": 1, "drawdown_warp_above": grid,
            "pick_lifts": [{"pick_index": i, "shafts": lift} for i, lift in enumerate(lifts)],
            "float_audit": quality, "material_requirements": material,
            "summary": {"repeat_ends": len(warp), "repeat_picks": len(picks),
                        "float_violations": len(quality["violations"]),
                        "total_yarn_mm": sum(row["length_mm"] for row in material["yarn"])}}
