"""Independent naive reference for small trace cases.

Deliberately expands the cloth into (end, pick) points and per-pick yarn
identities; it shares no code with loom_draft.trace beyond the batch plan the
test supplies explicitly.
"""
from collections import Counter


def expand_plan(batch, plan_segments):
    """Full list of (drawdown row, yarn) for the production string."""
    rows = []
    for segment in plan_segments:
        period = list(zip(segment["repeat_drawdown_warp_above"],
                          segment["repeat_yarns"]))
        for _ in range(segment["repeat_count"]):
            rows.extend(period)
    return rows


def set_live(version_rows, at):
    """Pick winning version per id at time ``at`` (independent re-implementation)."""
    grouped = {}
    for row in version_rows:
        grouped.setdefault(row["id"], []).append(row)
    live = {}
    for rid, versions in grouped.items():
        ordered = sorted(versions, key=lambda v: v["revision"])
        winner = None
        for version in ordered:
            if version["known_at"] <= at:
                winner = version
        if winner is not None and winner["payload"] is not None:
            live[rid] = winner
    return live


def check(version_rows):
    grouped = {}
    for row in version_rows:
        grouped.setdefault(row["id"], []).append(row)
    for rid, versions in grouped.items():
        ordered = sorted(versions, key=lambda v: v["revision"])
        assert [v["revision"] for v in ordered] == list(range(1, len(ordered) + 1))
        times = [v["known_at"] for v in ordered]
        assert all(x < y for x, y in zip(times, times[1:])), times
        assert ordered[0]["payload"] is not None


def evaluate(data, plan_segments, at, shipment):
    """Independent point-by-point decision.  Returns (allowed, problems, lots, missing)."""
    common = data["batch"]["common"]
    warp = common["warp"]
    M = len(warp)
    repeat_x = common["repeat_x"]
    P = len(plan_segments_rows(plan_segments))
    rows = plan_segments_rows(plan_segments)
    lots = {lot["id"]: lot for lot in data["lots"]}
    allocations = set_live(data["allocations"], at)
    certificates = set_live(data["certificates"], at)
    s = common["pick_spacing_mm"]
    d = common["warp_spacing_mm"]
    k = 1000 + common["takeup_permille"]
    w = common["warp_waste_mm"]

    def F(t):
        return (t * s * k + 999) // 1000

    def H(x):
        return (x * d * k + 999) // 1000

    used = {}   # lot -> {"warp": set points, "weft": set picks, mass}
    for lot_id in lots:
        used[lot_id] = {"warp": set(), "weft": set()}
    gap_warp = set()
    gap_weft = set()
    for alloc in allocations.values():
        payload = alloc["payload"]
        if payload["orientation"] == "warp":
            for base in range(M):
                if base != payload["base_end"]:
                    continue
                for rep in range(payload["repeat_start"], payload["repeat_stop"]):
                    end = base + M * rep
                    for p in range(payload["pick_start"], payload["pick_stop"]):
                        used[payload["lot_id"]]["warp"].add((end, p))
        else:
            yarn = lots[payload["lot_id"]]["yarn"]
            for p in range(payload["pick_start"], payload["pick_stop"]):
                if rows[p][1] == yarn:
                    used[payload["lot_id"]]["weft"].add(p)
    es, ee = shipment["end_start"], shipment["end_stop"]
    ps, pe = shipment["pick_start"], shipment["pick_stop"]
    point_owner = {}
    for lot_id in lots:
        for (end, p) in used[lot_id]["warp"]:
            point_owner[(end, p)] = lot_id
    weft_owner = {}
    for lot_id in lots:
        for p in used[lot_id]["weft"]:
            weft_owner[p] = lot_id
    miss_pts = 0
    miss_mm = 0
    miss_first = None
    for end in range(es, ee):
        for p in range(ps, pe):
            if (end, p) not in point_owner:
                miss_pts += 1
                miss_mm += 0  # per-point warp mm handled below by interval diff; use F diff
    # material via exact interval-difference per allocation actually covering points
    warp_mm = Counter()
    for lot_id in lots:
        for alloc in allocations.values():
            payload = alloc["payload"]
            if payload["orientation"] != "warp" or payload["lot_id"] != lot_id:
                continue
            if payload["base_end"] >= M:
                continue
            for rep in range(payload["repeat_start"], payload["repeat_stop"]):
                end = payload["base_end"] + M * rep
                if not es <= end < ee:
                    continue
                lo = max(payload["pick_start"], ps)
                hi = min(payload["pick_stop"], pe)
                if lo < hi:
                    warp_mm[lot_id] += F(hi) - F(lo)
    weft_mm = Counter()
    width = H(ee) - H(es)
    for lot_id in lots:
        count = sum(1 for p in used[lot_id]["weft"] if ps <= p < pe)
        weft_mm[lot_id] += count * width
    # gap material from points
    for p in range(ps, pe):
        owned = 0
        for end in range(es, ee):
            if (end, p) in point_owner:
                owned += 1
        if owned < (ee - es):
            missing = (ee - es) - owned
            miss_mm += (F(p + 1) - F(p)) * missing
            if miss_first is None:
                for end in range(es, ee):
                    if (end, p) not in point_owner:
                        miss_first = {"orientation": "warp", "end_index": end, "pick_index": p}
                        break
    used_picks = {p for p in range(ps, pe) if p in weft_owner}
    missing_picks = (pe - ps) - len(used_picks)
    miss_mm += missing_picks * width
    if miss_first is None and missing_picks:
        for p in range(ps, pe):
            if p not in weft_owner:
                miss_first = {"orientation": "weft", "end_index": es, "pick_index": p}
                break
    # evidence
    certs = list(certificates.values())
    problems = set()
    firsts = {}
    if miss_pts or missing_picks:
        problems.add(("source_gap", None))
        candidates = []
        if miss_pts:
            candidates.append(miss_first["pick_index"])
        if missing_picks:
            candidates.append(next(p for p in range(ps, pe) if p not in weft_owner))
        firsts[("source_gap", None)] = min(candidates)
    lot_out = {}
    for lot_id, lot in lots.items():
        warp_points = {(e, p) for (e, p) in used[lot_id]["warp"] if es <= e < ee and ps <= p < pe}
        weft_points = {p for p in used[lot_id]["weft"] if ps <= p < pe}
        if not warp_points and not weft_points:
            continue
        # capacity over whole plan
        whole = 0
        for alloc in allocations.values():
            payload = alloc["payload"]
            if payload["lot_id"] != lot_id:
                continue
            if payload["orientation"] == "warp":
                reps = payload["repeat_stop"] - payload["repeat_start"]
                whole += (F(payload["pick_stop"]) - F(payload["pick_start"])) * reps
                if payload["pick_start"] == 0:
                    whole += w * reps
            else:
                count = 0
                for p in range(payload["pick_start"], payload["pick_stop"]):
                    if rows[p][1] == lot["yarn"]:
                        count += 1
                whole += count * H(len(warp) * repeat_x)
        overdrawn = whole > lot["capacity_mm"]
        failed = False
        uncertified = False
        first_fail = None
        first_unc = None
        referenced = set()
        for cert in certs:
            payload = cert["payload"]
            if payload["lot_id"] != lot_id:
                continue
            hit = False
            for (e, p) in warp_points:
                if payload["pick_start"] <= p < payload["pick_stop"]:
                    hit = True
                    break
            if not hit:
                for p in weft_points:
                    if payload["pick_start"] <= p < payload["pick_stop"]:
                        hit = True
                        break
            if hit:
                referenced.add((cert["id"], cert["revision"]))
        for (e, p) in warp_points:
            relevant = [c for c in certs if c["payload"]["lot_id"] == lot_id
                        and c["payload"]["pick_start"] <= p < c["payload"]["pick_stop"]]
            if any(c["payload"]["verdict"] == "fail" for c in relevant):
                failed = True
                first_fail = p if first_fail is None else min(first_fail, p)
            if not relevant:
                uncertified = True
                first_unc = p if first_unc is None else min(first_unc, p)
        for p in weft_points:
            relevant = [c for c in certs if c["payload"]["lot_id"] == lot_id
                        and c["payload"]["pick_start"] <= p < c["payload"]["pick_stop"]]
            if any(c["payload"]["verdict"] == "fail" for c in relevant):
                failed = True
                first_fail = p if first_fail is None else min(first_fail, p)
            if not relevant:
                uncertified = True
                first_unc = p if first_unc is None else min(first_unc, p)
        if overdrawn:
            problems.add(("overdrawn", lot_id))
            starts = []
            if warp_points:
                starts.append(min(p for (_, p) in warp_points))
            if weft_points:
                starts.append(min(weft_points))
            firsts[("overdrawn", lot_id)] = min(starts) if starts else None
        if failed:
            problems.add(("failed", lot_id))
            firsts[("failed", lot_id)] = first_fail
        if uncertified:
            problems.add(("uncertified", lot_id))
            firsts[("uncertified", lot_id)] = first_unc
        lot_out[lot_id] = {
            "warp_mm": warp_mm[lot_id], "weft_mm": weft_mm[lot_id],
            "whole_attributed_mm": whole, "overdrawn": overdrawn,
            "allocation_versions": sorted({(a["id"], a["revision"])
                                          for a in allocations.values()
                                          if a["payload"]["lot_id"] == lot_id
                                          and (a["payload"]["orientation"] == "warp"
                                               and any(a["payload"]["base_end"] + M * r == e
                                                       and a["payload"]["pick_start"] <= p < a["payload"]["pick_stop"]
                                                       for (e, p) in warp_points
                                                       for r in range(a["payload"]["repeat_start"],
                                                                      a["payload"]["repeat_stop"]))
                                               or a["payload"]["orientation"] == "weft"
                                               and any(a["payload"]["pick_start"] <= p < a["payload"]["pick_stop"]
                                                       for p in weft_points))}),
            "certificate_versions": sorted(referenced),
        }
    missing = {"warp_points": miss_pts, "weft_picks": missing_picks,
               "material_mm": miss_mm, "first": miss_first}
    return not problems, sorted(problems, key=lambda x: (x[0], x[1] or "")), lot_out, missing, firsts


def plan_segments_rows(plan_segments):
    rows = []
    for segment in plan_segments:
        period = list(zip(segment["repeat_drawdown_warp_above"], segment["repeat_yarns"]))
        for _ in range(segment["repeat_count"]):
            rows.extend(period)
    return rows
