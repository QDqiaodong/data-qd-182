"""多工单共用一根经轴的连续织造全局最优计划。

复用 v1 的组织编译（weave.draw）、无限重复纬向审查（weave.circular_float）
与千分比取整（materials.ceil_permille），在此基础上扩展：
有限生产串的跨段经向浮线审查、整轴一次取整的材料汇总、共享库存检查，
以及“交付价值最大、总用纱最小、身份键字典序最小”的全局精确搜索。
"""
import re
from collections import Counter
from itertools import combinations

from .io import fields, integer, rows, label
from .model import tie_up_map, warp_ends, pick_rows, gauge_numbers
from .weave import draw, circular_float
from .materials import ceil_permille

ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _identity(value, name):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{name} requires id matching [A-Za-z0-9_-]{{1,40}}")
    return value


def validate_batch(data):
    """严格验证批次输入；所有声明（含永不生产的工单/接缝）都必须合法。"""
    fields(data, ("common", "jobs", "bridges", "stock", "max_length_mm"))
    common = data["common"]
    fields(common, ("shafts", "tie_up", "warp", "repeat_x", "warp_spacing_mm",
                    "pick_spacing_mm", "takeup_permille", "warp_waste_mm", "float_limit"))
    shafts, tie_up = tie_up_map(common)
    warp = warp_ends(common, shafts)
    gauge_numbers(common)
    jobs = []
    seen = set()
    for job in rows(data["jobs"], "jobs", 1, 6):
        fields(job, ("id", "value", "picks", "repeat_y", "phases"))
        jid = _identity(job["id"], "job id")
        if jid in seen:
            raise ValueError(f"duplicate job ID: {jid}")
        seen.add(jid)
        integer(job["value"], "job value", 1, 10**6)
        picks = pick_rows(job["picks"], tie_up, "job picks", 128)
        integer(job["repeat_y"], "repeat_y", 1, 1000)
        phases = rows(job["phases"], "phases", 1, 2)
        for phase in phases:
            integer(phase, "phase", 0, len(picks) - 1)
        if len(set(phases)) != len(phases):
            raise ValueError("duplicate phase")
        jobs.append({"id": jid, "value": job["value"], "picks": picks,
                     "repeat_y": job["repeat_y"], "phases": sorted(set(phases))})
    bridges = []
    seen = set()
    for bridge in rows(data["bridges"], "bridges", 0, 2):
        fields(bridge, ("id", "picks"))
        bid = _identity(bridge["id"], "bridge id")
        if bid in seen:
            raise ValueError(f"duplicate bridge ID: {bid}")
        seen.add(bid)
        bridges.append({"id": bid, "picks": pick_rows(bridge["picks"], tie_up, "bridge picks", 8)})
    stock = {}
    for row in rows(data["stock"], "stock", 0, 10**6):
        fields(row, ("yarn", "length_mm"))
        yarn = label(row["yarn"], "stock yarn")
        if yarn in stock:
            raise ValueError(f"duplicate stock yarn: {yarn}")
        stock[yarn] = integer(row["length_mm"], "stock length_mm", 0, 10**12)
    integer(data["max_length_mm"], "max_length_mm", 1, 10**12)
    return {"common": common, "warp": warp, "tie_up": tie_up, "jobs": jobs,
            "bridges": bridges, "stock": stock, "max_length_mm": data["max_length_mm"]}


def _column_stats(column):
    """一个基础经端在一周期内的 1 的极大段：返回(全1, 前缀长, 后缀长, [(起点,长度)])。段按起点升序。"""
    n = len(column)
    runs = []
    start = None
    for index, value in enumerate(column):
        if value and start is None:
            start = index
        elif not value and start is not None:
            runs.append((start, index - start))
            start = None
    if start is not None:
        runs.append((start, n - start))
    all_ones = len(runs) == 1 and runs[0] == (0, n)
    pre = runs[0][1] if runs and runs[0][0] == 0 else 0
    suf = runs[-1][1] if runs and runs[-1][0] + runs[-1][1] == n else 0
    return all_ones, pre, suf, runs


def _segment(tie_up, warp, picks, repeat_count):
    """编译一段（工单某相位旋转后的周期或一条接缝）为压缩描述，不展开 repeat_count。"""
    grid, lifts = draw(tie_up, warp, picks)
    n = len(picks)
    stats = [_column_stats([row[x] for row in grid]) for x in range(len(warp))]
    desc = []
    for all_ones, pre, suf, runs in stats:
        if all_ones:
            desc.append((1, 0, 0, 0, n * repeat_count))
        else:
            best = max((length for _, length in runs), default=0)
            if repeat_count > 1:
                best = max(best, pre + suf)
            desc.append((0, pre, suf, best, n * repeat_count))
    weft_rows = [circular_float(row, 0) for row in grid]
    weft_counts = Counter()
    for pick in picks:
        weft_counts[pick["yarn"]] += repeat_count
    return {"grid": grid, "lifts": lifts, "yarns": [pick["yarn"] for pick in picks],
            "n": n, "repeat_count": repeat_count, "picks": n * repeat_count,
            "stats": stats, "desc": desc, "weft_rows": weft_rows,
            "weft_counts": weft_counts}


def _weft_ok(segment, limit):
    return all(not row["unbounded"] and row["max_crossings"] <= limit
               for row in segment["weft_rows"])


def _prepare(spec):
    common = spec["common"]
    warp = spec["warp"]
    ends = len(warp)
    limit = common["float_limit"]
    job_segs = {}
    job_picks = {}
    job_weft = {}
    usable_jobs = []
    for j, job in enumerate(spec["jobs"]):
        for phase in job["phases"]:
            rotated = job["picks"][phase:] + job["picks"][:phase]
            job_segs[(j, phase)] = _segment(spec["tie_up"], warp, rotated, job["repeat_y"])
        first = job_segs[(j, job["phases"][0])]
        job_picks[j] = first["picks"]
        job_weft[j] = first["weft_counts"]
        if _weft_ok(first, limit):
            usable_jobs.append(j)
    bridge_segs = []
    usable_bridges = []
    for b, bridge in enumerate(spec["bridges"]):
        segment = _segment(spec["tie_up"], warp, bridge["picks"], 1)
        bridge_segs.append(segment)
        if _weft_ok(segment, limit):
            usable_bridges.append(b)
    usable_bridges.sort(key=lambda b: (bridge_segs[b]["picks"], spec["bridges"][b]["id"]))
    width = ends * common["repeat_x"] * common["warp_spacing_mm"]
    return {"job_segs": job_segs, "job_picks": job_picks, "job_weft": job_weft,
            "usable_jobs": usable_jobs, "bridge_segs": bridge_segs,
            "gaps": [None] + usable_bridges, "ends": ends, "limit": limit,
            "spacing": common["pick_spacing_mm"], "takeup": common["takeup_permille"],
            "waste": common["warp_waste_mm"], "repeat_x": common["repeat_x"],
            "warp_yarns": Counter(end["yarn"] for end in warp),
            "weft_unit": ceil_permille(width, common["takeup_permille"])}


def _merge(desc, carry, best, limit, ends):
    """把一段接到当前生产串尾部，返回新的(尾部连续1长, 历史最长1长)数组；越限返回 None。"""
    new_carry = [0] * ends
    new_best = [0] * ends
    for x in range(ends):
        kind, pre, suf, seg_best, length = desc[x]
        b = best[x]
        if kind:
            c = carry[x] + length
            if c > b:
                b = c
        else:
            first = carry[x] + pre
            if first > b:
                b = first
            if seg_best > b:
                b = seg_best
            c = suf
        if b > limit:
            return None
        new_carry[x] = c
        new_best[x] = b
    return new_carry, new_best


def _search_subset(spec, ctx, combo, yarn_bound):
    """在一个固定工单子集内精确枚举全部排列/相位/接缝，返回(总用纱, 身份键, 段序列)或 None。"""
    jobs = spec["jobs"]
    stock = spec["stock"]
    max_length = spec["max_length_mm"]
    ends = ctx["ends"]
    limit = ctx["limit"]
    spacing = ctx["spacing"]
    weft_unit = ctx["weft_unit"]
    repeat_x = ctx["repeat_x"]
    warp_yarns = ctx["warp_yarns"]
    job_segs = ctx["job_segs"]
    bridge_segs = ctx["bridge_segs"]
    gaps = ctx["gaps"]
    ordered = tuple(sorted(combo, key=lambda j: jobs[j]["id"]))
    min_picks = sum(ctx["job_picks"][j] for j in combo)
    if min_picks * spacing > max_length:
        return None
    warp_unit = ceil_permille(min_picks * spacing, ctx["takeup"]) + ctx["waste"]
    usage = Counter()
    for yarn, count in warp_yarns.items():
        usage[yarn] += count * repeat_x * warp_unit
    for j in combo:
        for yarn, count in ctx["job_weft"][j].items():
            usage[yarn] += count * weft_unit
    if any(amount > stock.get(yarn, 0) for yarn, amount in usage.items()):
        return None
    best = [None]
    steps = []
    segs = []

    def recurse(remaining, picks, weft_counts, carry, best_end):
        total_picks = picks + sum(ctx["job_picks"][j] for j in remaining)
        if total_picks * spacing > max_length:
            return
        warp_unit = ceil_permille(total_picks * spacing, ctx["takeup"]) + ctx["waste"]
        lower = Counter()
        for yarn, count in warp_yarns.items():
            lower[yarn] += count * repeat_x * warp_unit
        for yarn, count in weft_counts.items():
            lower[yarn] += count * weft_unit
        for j in remaining:
            for yarn, count in ctx["job_weft"][j].items():
                lower[yarn] += count * weft_unit
        total = 0
        for yarn, amount in lower.items():
            if amount > stock.get(yarn, 0):
                return
            total += amount
        bound = yarn_bound[0]
        if bound is not None and total > bound:
            return
        local = best[0]
        if local is not None:
            if total > local[0]:
                return
            # 仅当本子串纱量不可能更优（下界已等于最优）时，身份键前缀才可用于剪枝
            if total == local[0]:
                identity = local[1]
                for i, step in enumerate(steps):
                    if step > identity[i]:
                        return
                    if step < identity[i]:
                        break
        if not remaining:
            if local is None or total < local[0] or (total == local[0] and steps < local[1]):
                best[0] = (total, list(steps), list(segs))
                if yarn_bound[0] is None or total < yarn_bound[0]:
                    yarn_bound[0] = total
            return
        for i, j in enumerate(remaining):
            rest = remaining[:i] + remaining[i + 1:]
            job = jobs[j]
            for phase in job["phases"]:
                jseg = job_segs[(j, phase)]
                for gap in gaps if steps else (None,):
                    cur_carry, cur_best = carry, best_end
                    cur_picks = picks
                    cur_weft = weft_counts
                    if gap is not None:
                        bseg = bridge_segs[gap]
                        merged = _merge(bseg["desc"], cur_carry, cur_best, limit, ends)
                        if merged is None:
                            continue
                        cur_carry, cur_best = merged
                        cur_picks += bseg["picks"]
                        cur_weft = cur_weft + bseg["weft_counts"]
                    merged = _merge(jseg["desc"], cur_carry, cur_best, limit, ends)
                    if merged is None:
                        continue
                    cur_carry, cur_best = merged
                    cur_picks += jseg["picks"]
                    cur_weft = cur_weft + jseg["weft_counts"]
                    steps.append((job["id"], phase,
                                  "" if gap is None else spec["bridges"][gap]["id"]))
                    if gap is not None:
                        segs.append(("b", gap))
                    segs.append(("j", j, phase))
                    recurse(rest, cur_picks, cur_weft, cur_carry, cur_best)
                    if gap is not None:
                        segs.pop()
                    segs.pop()
                    steps.pop()

    recurse(ordered, 0, Counter(), [0] * ends, [0] * ends)
    return best[0]


def _search(spec, ctx):
    """按交付价值降序逐层精确搜索；同价值层内取总用纱最小、再取身份键最小。"""
    jobs = spec["jobs"]
    by_value = {}
    for size in range(1, len(ctx["usable_jobs"]) + 1):
        for combo in combinations(ctx["usable_jobs"], size):
            value = sum(jobs[j]["value"] for j in combo)
            by_value.setdefault(value, []).append(combo)
    for value in sorted(by_value, reverse=True):
        yarn_bound = [None]
        group_best = None
        for combo in by_value[value]:
            result = _search_subset(spec, ctx, combo, yarn_bound)
            if result is not None and (group_best is None or result[0] < group_best[0]
                                       or (result[0] == group_best[0] and result[1] < group_best[1])):
                group_best = result
        if group_best is not None:
            return group_best
    return None


def _end_max_run(seg_infos, x):
    """基础经端 x 在整条有限生产串上的最长连续 1 段及其最早起点（压缩扫描，不展开重复）。"""
    best = 0
    best_start = None
    carry = 0
    carry_start = 0
    for stats, n, repeat_count, length, pos in seg_infos:
        all_ones, pre, suf, runs = stats[x]
        if all_ones:
            if carry == 0:
                carry_start = pos
            carry += length
            continue
        first = carry + pre
        if first:
            start = carry_start if carry else pos
            if first > best:
                best, best_start = first, start
        for run_start, run_length in runs:
            if run_start == 0 or run_start + run_length == n:
                continue
            if run_length > best:
                best, best_start = run_length, pos + run_start
        if repeat_count > 1 and suf:
            junction = suf + pre
            if junction > best:
                best, best_start = junction, pos + n - suf
        if suf:
            carry, carry_start = suf, pos + length - suf
        else:
            carry = 0
    if carry > best:
        best, best_start = carry, carry_start
    return best, best_start


def _materials(spec, ctx, total_picks, weft_counts):
    common = spec["common"]
    stock = spec["stock"]
    if total_picks == 0:
        balance = [{"yarn": yarn, "available_mm": stock[yarn], "used_mm": 0,
                    "remaining_mm": stock[yarn]} for yarn in sorted(stock)]
        return {"width_mm": 0, "length_mm": 0, "total_ends": 0, "total_picks": 0,
                "yarn": [], "stock_balance": balance}
    total_ends = len(spec["warp"]) * common["repeat_x"]
    width = total_ends * common["warp_spacing_mm"]
    length = total_picks * common["pick_spacing_mm"]
    warp_unit = ceil_permille(length, common["takeup_permille"]) + common["warp_waste_mm"]
    weft_unit = ceil_permille(width, common["takeup_permille"])
    amounts = Counter()
    for end in spec["warp"]:
        amounts[("warp", end["yarn"])] += warp_unit * common["repeat_x"]
    for yarn, count in weft_counts.items():
        amounts[("weft", yarn)] += count * weft_unit
    yarn_rows = [{"orientation": side, "yarn": yarn, "length_mm": amount}
                 for (side, yarn), amount in sorted(amounts.items()) if amount > 0]
    used = Counter()
    for (side, yarn), amount in amounts.items():
        used[yarn] += amount
    balance = [{"yarn": yarn, "available_mm": stock.get(yarn, 0), "used_mm": used[yarn],
                "remaining_mm": stock.get(yarn, 0) - used[yarn]}
               for yarn in sorted(set(stock) | set(used))]
    return {"width_mm": width, "length_mm": length, "total_ends": total_ends,
            "total_picks": total_picks, "yarn": yarn_rows, "stock_balance": balance}


def _report(spec, ctx, best):
    jobs = spec["jobs"]
    bridges = spec["bridges"]
    identity, segs = ([], []) if best is None else (best[1], best[2])
    segments = []
    seg_objects = []
    seg_infos = []
    weft_counts = Counter()
    delivered_value = 0
    bridge_picks = 0
    start = 0
    for entry in segs:
        if entry[0] == "b":
            segment = ctx["bridge_segs"][entry[1]]
            kind, sid, phase = "bridge", bridges[entry[1]]["id"], 0
            bridge_picks += segment["picks"]
        else:
            _, j, chosen = entry
            segment = ctx["job_segs"][(j, chosen)]
            kind, sid, phase = "job", jobs[j]["id"], chosen
            delivered_value += jobs[j]["value"]
        segments.append({"kind": kind, "id": sid, "phase": phase,
                         "repeat_count": segment["repeat_count"], "start_pick": start,
                         "pick_count": segment["picks"],
                         "repeat_drawdown_warp_above": segment["grid"],
                         "repeat_pick_lifts": segment["lifts"],
                         "repeat_yarns": segment["yarns"]})
        seg_objects.append(segment)
        seg_infos.append((segment["stats"], segment["n"], segment["repeat_count"],
                          segment["picks"], start))
        weft_counts += segment["weft_counts"]
        start += segment["picks"]
    total_picks = start
    warp_floats = []
    if total_picks:
        for x in range(ctx["ends"]):
            top, first = _end_max_run(seg_infos, x)
            warp_floats.append({"repeat_index": x, "max_crossings": top,
                                "first_start_pick": first if top else None})
    weft_floats = []
    for index, segment in enumerate(seg_objects):
        for pick_index, row in enumerate(segment["weft_rows"]):
            weft_floats.append({"segment_index": index, "repeat_pick_index": pick_index,
                                "unbounded": row["unbounded"],
                                "max_crossings": row["max_crossings"]})
    materials = _materials(spec, ctx, total_picks, weft_counts)
    selected = {step[0] for step in identity}
    return {"schema_version": 1,
            "plan": [{"job_id": jid, "phase": phase, "bridge_before": bridge}
                     for jid, phase, bridge in identity],
            "unselected_job_ids": sorted(job["id"] for job in jobs if job["id"] not in selected),
            "segments": segments,
            "float_audit": {"warp_floats": warp_floats, "weft_floats": weft_floats,
                            "passes_float_limit": True},
            "material_requirements": materials,
            "summary": {"delivered_value": delivered_value, "delivered_jobs": len(identity),
                        "bridge_picks": bridge_picks,
                        "total_yarn_mm": sum(row["length_mm"] for row in materials["yarn"])}}


def run_plan(data):
    """验证批次输入并返回全局最优的批次报告（普通字典）；非法输入抛 ValueError。"""
    spec = validate_batch(data)
    ctx = _prepare(spec)
    best = _search(spec, ctx)
    return _report(spec, ctx, best)
