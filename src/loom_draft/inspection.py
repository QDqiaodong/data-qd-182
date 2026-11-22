"""验布机读数对齐与复验清单（loom-inspect）。

织造计划由 loom-batch 的规则唯一确定，验布证据不参与选方案。验布机按采集
顺序输出帧：编码器给出每帧可能所在纬的闭区间（回跳会重拍、掉帧会跳拍），
探针给出若干实际经端的组织读数，每帧自带布面缺陷检出。本模块把全部帧与
计划的压缩段对齐，并在**全部**完整对齐路线上做保守判定：

* 一条纬只有在每条完整对齐中都至少被一帧拍到，才算确定验过；
* 一块裁区在至少一条完整对齐中被缺陷帧（横向正宽相交）命中即可能缺陷；
* 每条完整对齐都命中时裁区确定缺陷（damaged），否则有可能缺陷或覆盖不
  确定的纬为 recheck，全部确定验过且无可能缺陷才为 clear。

不枚举路线、不展开成布网格：用前向/后向可达位集（每帧窗口至多 512 纬、
步长至多 4，用 Python 大整数位运算）在压缩计划上精确计算同样的集合。
非法输入抛 ``ValueError``；返回普通字典。
"""
from .batch import validate_batch, production
from .identity import identifier
from .io import fields, integer, rows

TOP_FIELDS = ("batch", "probe_ends", "frames", "cuts")
CUT_FIELDS = ("id", "end_start", "end_stop", "pick_start", "pick_stop")
MAX_FRAMES = 120000
MAX_WINDOW = 512          # 帧窗口闭区间至多覆盖 512 条纬
MAX_STEP = 4
BLOCK = 512               # 全局位集分块大小（与窗口上限一致）
BLOCK_MASK = (1 << BLOCK) - 1


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

def _validate_probes(value, ends):
    probes = rows(value, "probe_ends", 1, 8)
    result = []
    seen = set()
    for item in probes:
        end = integer(item, "probe end", 0, ends - 1)
        if end in seen:
            raise ValueError(f"duplicate probe end: {end}")
        seen.add(end)
        result.append(end)
    return result


def _validate_frames(value, picks, ends, probe_count):
    frames = rows(value, "frames", 1, MAX_FRAMES)
    lo, hi, mn, mx, samples, defects = [], [], [], [], [], []
    for index, row in enumerate(frames):
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError("frame must be [lo, hi, min_step, max_step, sample, defect]")
        f_lo = integer(row[0], "frame lo", 0, picks - 1)
        f_hi = integer(row[1], "frame hi", 0, picks - 1)
        if f_lo > f_hi:
            raise ValueError("frame requires lo <= hi")
        if f_hi - f_lo >= MAX_WINDOW:
            raise ValueError("frame window exceeds 512 picks")
        f_min = integer(row[2], "frame min_step", 0, MAX_STEP)
        f_max = integer(row[3], "frame max_step", 0, MAX_STEP)
        if f_min > f_max:
            raise ValueError("frame requires min_step <= max_step")
        if index == 0 and (f_min or f_max):
            raise ValueError("first frame requires min_step=max_step=0")
        sample = row[4]
        if not isinstance(sample, str) or len(sample) != probe_count \
                or any(ch not in "01?" for ch in sample):
            raise ValueError("frame sample must be a 0/1/? string matching probe_ends")
        defect = row[5]
        if defect is not None:
            if not isinstance(defect, list) or len(defect) != 2:
                raise ValueError("frame defect must be null or [end_start, end_stop]")
            ds = integer(defect[0], "defect end_start", 0, ends - 1)
            de = integer(defect[1], "defect end_stop", 1, ends)
            if not ds < de:
                raise ValueError("defect requires end_start < end_stop")
            defect = (ds, de)
        lo.append(f_lo)
        hi.append(f_hi)
        mn.append(f_min)
        mx.append(f_max)
        samples.append(sample)
        defects.append(defect)
    return {"lo": lo, "hi": hi, "min": mn, "max": mx,
            "sample": samples, "defect": defects}


def _validate_cuts(value, picks, ends):
    result = []
    seen = set()
    for row in rows(value, "cuts", 0, 32):
        fields(row, CUT_FIELDS)
        cid = identifier(row["id"], "cut id")
        if cid in seen:
            raise ValueError(f"duplicate cut id: {cid}")
        seen.add(cid)
        es = integer(row["end_start"], "cut end_start", 0, ends - 1)
        ee = integer(row["end_stop"], "cut end_stop", 1, ends)
        ps = integer(row["pick_start"], "cut pick_start", 0, picks - 1)
        pe = integer(row["pick_stop"], "cut pick_stop", 1, picks)
        if not es < ee:
            raise ValueError("cut requires end_start < end_stop")
        if not ps < pe:
            raise ValueError("cut requires pick_start < pick_stop")
        result.append({"id": cid, "end_start": es, "end_stop": ee,
                       "pick_start": ps, "pick_stop": pe})
    return result


# ---------------------------------------------------------------------------
# planned weave as per-probe bitsets over real picks
# ---------------------------------------------------------------------------

class _Blocked:
    """一条实际经端基础位置在全部实际纬上的组织值位集，按 512 位分块。"""

    __slots__ = ("blocks",)

    def __init__(self, value, picks):
        self.blocks = []
        for _ in range((picks + BLOCK - 1) // BLOCK):
            self.blocks.append(value & BLOCK_MASK)
            value >>= BLOCK

    def window(self, lo, width):
        """取出 [lo, lo+width) 的位（width <= BLOCK），返回 width 位整数。"""
        index, off = divmod(lo, BLOCK)
        piece = self.blocks[index] >> off
        if off and index + 1 < len(self.blocks):
            piece |= self.blocks[index + 1] << (BLOCK - off)
        return piece & ((1 << width) - 1)


def _probe_columns(raw_segments, starts, probes, base, picks, samples):
    """为采样中真正用到的基础位置构建组织位集；全问号扫描不构建。"""
    needed = set()
    for sample in samples:
        for j, ch in enumerate(sample):
            if ch != "?":
                needed.add(probes[j] % base)
    columns = {}
    for pos in needed:
        bits = 0
        for segment, start in zip(raw_segments, starts):
            grid = segment["grid"]
            n = segment["n"]
            total = segment["picks"]
            period = 0
            for r in range(n):
                if grid[r][pos]:
                    period |= 1 << r
            rep = period
            length = n
            while length < total:          # 倍增复制 repeat_count 个周期
                rep |= rep << length
                length <<= 1
            rep &= (1 << total) - 1
            bits |= rep << start
        columns[pos] = _Blocked(bits, picks)
    return columns


# ---------------------------------------------------------------------------
# alignment reachability (bitsets relative to each frame's window origin)
# ---------------------------------------------------------------------------

def _dilate(bits, lo_step, hi_step):
    """{p + d : p in bits, lo_step <= d <= hi_step}。"""
    result = bits << lo_step
    for d in range(lo_step + 1, hi_step + 1):
        result |= bits << d
    return result


def _erode(bits, lo_step, hi_step):
    """{p : p + d in bits for some lo_step <= d <= hi_step}。"""
    result = bits >> lo_step
    for d in range(lo_step + 1, hi_step + 1):
        result |= bits >> d
    return result


def _shift(bits, amount):
    if amount >= 0:
        return bits << amount
    return bits >> -amount


def _frame_masks(frames, probes, base, columns):
    """每帧窗口内同时满足组织读数的候选纬位集（相对各自 lo）。"""
    lo, hi, samples = frames["lo"], frames["hi"], frames["sample"]
    full_by_width = {}
    masks = []
    for i in range(len(lo)):
        width = hi[i] - lo[i] + 1
        full = full_by_width.get(width)
        if full is None:
            full = (1 << width) - 1
            full_by_width[width] = full
        sample = samples[i]
        if set(sample) == {"?"}:
            masks.append(full)
            continue
        mask = full
        for j, ch in enumerate(sample):
            if ch == "?":
                continue
            bits = columns[probes[j] % base].window(lo[i], width)
            mask &= bits if ch == "1" else full & ~bits
            if not mask:
                break
        masks.append(mask)
    return masks


def _reachable_sets(frames, masks):
    """前向/后向可达位集；任一前向集合为空即不存在完整对齐。"""
    lo, mn, mx = frames["lo"], frames["min"], frames["max"]
    count = len(lo)
    forward = [0] * count
    cur = masks[0]
    if not cur:
        raise ValueError("frames admit no complete alignment")
    forward[0] = cur
    for i in range(1, count):
        reach = _shift(_dilate(cur, mn[i], mx[i]), lo[i - 1] - lo[i])
        cur = masks[i] & reach
        if not cur:
            raise ValueError("frames admit no complete alignment")
        forward[i] = cur
    backward = [0] * count
    cur = masks[count - 1]
    backward[count - 1] = cur
    for i in range(count - 2, -1, -1):
        # 先平移到第 i 帧原点再取前驱：前驱可能落在第 i+1 帧窗口原点左侧
        reach = _erode(_shift(cur, lo[i + 1] - lo[i]), mn[i + 1], mx[i + 1])
        cur = masks[i] & reach
        backward[i] = cur
    return forward, backward


# ---------------------------------------------------------------------------
# certainly-observed picks = picks no complete alignment can avoid
# ---------------------------------------------------------------------------

def _add_bits(blocks, value, origin):
    """把相对 origin 的位集并入按 BLOCK 分块的全局纬位集。"""
    index, off = divmod(origin, BLOCK)
    piece = value << off
    while piece:
        blocks[index] = blocks.get(index, 0) | (piece & BLOCK_MASK)
        piece >>= BLOCK
        index += 1


def _avoidable_picks(frames, forward, backward, picks):
    """可被某条完整对齐避开的全部实际纬。

    对齐是非降序列：避开纬 q 当且仅当整体在 q 之下、整体在 q 之上，或存在
    相邻可行边 a -> b（a 前向可达、b 后向可达、步长合法）满足 a < q < b。
    """
    lo, mn, mx = frames["lo"], frames["min"], frames["max"]
    count = len(lo)
    blocks = {}
    first = forward[0] & backward[0]
    top = lo[0] + first.bit_length() - 1
    if top > 0:                                   # 全部小于首帧最大可行纬
        _add_bits(blocks, (1 << top) - 1, 0)
    last = forward[count - 1] & backward[count - 1]
    bottom = lo[count - 1] + (last & -last).bit_length() - 1
    if bottom + 1 < picks:                        # 全部大于末帧最小可行纬
        _add_bits(blocks, (1 << (picks - bottom - 1)) - 1, bottom + 1)
    for k in range(count - 1):
        step_lo, step_hi = mn[k + 1], mx[k + 1]
        if step_hi < 2:                           # 步长不足 2 跳不过任何纬
            continue
        succ = _shift(backward[k + 1], lo[k + 1] - lo[k])
        here = forward[k]
        rel = 0
        for d in range(max(step_lo, 2), step_hi + 1):
            edge = here & (succ >> d)             # a 可行且 a+d 可行
            if edge:
                for j in range(1, d):             # 被跳过的纬 a+1 .. a+d-1
                    rel |= edge << j
        if rel:
            _add_bits(blocks, rel, lo[k])
    skipped = 0
    for index, value in blocks.items():
        skipped |= value << (index * BLOCK)
    return skipped


# ---------------------------------------------------------------------------
# defect evidence over all complete alignments
# ---------------------------------------------------------------------------

def _range_mask(origin, start, stop):
    """[start, stop) 相对 origin 的位掩码。"""
    width = stop - start
    if start >= origin:
        return ((1 << width) - 1) << (start - origin)
    return ((1 << width) - 1) >> (origin - start)


def _possible_defect(cut, defect_infos):
    """全部可命中位置中先最小真实纬、再最小帧下标的见证；无为 None。"""
    es, ee = cut["end_start"], cut["end_stop"]
    ps, pe = cut["pick_start"], cut["pick_stop"]
    best = None
    for index, ds, de, viable, f_lo, f_hi in defect_infos:
        if de <= es or ds >= ee or f_hi < ps or f_lo >= pe:
            continue
        a = ps - f_lo if ps > f_lo else 0
        b = pe - 1 - f_lo if pe - 1 < f_hi else f_hi - f_lo
        bits = (viable >> a) & ((1 << (b - a + 1)) - 1)
        if not bits:
            continue
        pick = f_lo + a + (bits & -bits).bit_length() - 1
        if best is None or pick < best[0]:
            best = (pick, index, ds if ds > es else es, de if de < ee else ee)
    if best is None:
        return None
    return {"pick_index": best[0], "frame_index": best[1],
            "end_start": best[2], "end_stop": best[3]}


def _certain_defect(cut, defect_infos, frames, masks, forward, backward):
    """每条完整对齐都命中裁区 <=> 不存在让全部命中帧避开裁区纬区间的对齐。"""
    es, ee = cut["end_start"], cut["end_stop"]
    ps, pe = cut["pick_start"], cut["pick_stop"]
    hits = [info for info in defect_infos
            if info[2] > es and info[1] < ee and info[5] >= ps and info[4] < pe]
    if not hits:
        return False
    forbidden = {info[0] for info in hits}
    first, last = hits[0][0], hits[-1][0]
    lo, mn, mx = frames["lo"], frames["min"], frames["max"]
    reach = forward[first - 1] if first else None
    for i in range(first, last + 1):
        if reach is None:
            cur = masks[i]
        else:
            step = _shift(_dilate(reach, mn[i], mx[i]), lo[i - 1] - lo[i])
            cur = masks[i] & step
        if i in forbidden:
            cur &= ~_range_mask(lo[i], ps, pe)
        if not cur:
            return True
        reach = cur
    return not (reach & backward[last])


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _empty_report(report):
    return {"schema_version": 1,
            "plan": report["plan"],
            "segments": report["segments"],
            "cuts": [],
            "summary": {"cuts": 0, "clear": 0, "recheck": 0, "damaged": 0}}


def run_inspection(data):
    """验证验布输入并返回复验清单报告（普通字典）；非法输入抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("root must be an object")
    fields(data, TOP_FIELDS)
    spec = validate_batch(data["batch"])
    report, raw_segments = production(spec)
    ends = len(spec["warp"]) * spec["common"]["repeat_x"]
    base = len(spec["warp"])
    starts = []
    cursor = 0
    for segment in raw_segments:
        starts.append(cursor)
        cursor += segment["picks"]
    picks = cursor
    if picks == 0:
        if data["probe_ends"] != [] or data["frames"] != [] or data["cuts"] != []:
            raise ValueError("empty plan requires empty probe_ends, frames and cuts")
        return _empty_report(report)
    probes = _validate_probes(data["probe_ends"], ends)
    frames = _validate_frames(data["frames"], picks, ends, len(probes))
    cuts = _validate_cuts(data["cuts"], picks, ends)

    columns = _probe_columns(raw_segments, starts, probes, base, picks, frames["sample"])
    masks = _frame_masks(frames, probes, base, columns)
    forward, backward = _reachable_sets(frames, masks)
    skipped = _avoidable_picks(frames, forward, backward, picks)

    defect_infos = []
    for i, defect in enumerate(frames["defect"]):
        if defect is not None:
            defect_infos.append((i, defect[0], defect[1],
                                 forward[i] & backward[i],
                                 frames["lo"][i], frames["hi"][i]))

    counts = {"clear": 0, "recheck": 0, "damaged": 0}
    decisions = []
    for cut in sorted(cuts, key=lambda item: item["id"]):
        ps, pe = cut["pick_start"], cut["pick_stop"]
        unconfirmed_bits = (skipped >> ps) & ((1 << (pe - ps)) - 1)
        forced = (pe - ps) - unconfirmed_bits.bit_count()
        first_unconfirmed = None
        if unconfirmed_bits:
            first_unconfirmed = ps + (unconfirmed_bits & -unconfirmed_bits).bit_length() - 1
        possible = _possible_defect(cut, defect_infos)
        certain = _certain_defect(cut, defect_infos, frames, masks, forward, backward)
        if certain:
            status = "damaged"
        elif possible is not None or first_unconfirmed is not None:
            status = "recheck"
        else:
            status = "clear"
        counts[status] += 1
        decisions.append({"id": cut["id"], "status": status,
                          "certain_defect": certain,
                          "possible_defect": possible,
                          "forced_observed_picks": forced,
                          "first_unconfirmed_pick": first_unconfirmed})
    return {"schema_version": 1,
            "plan": report["plan"],
            "segments": report["segments"],
            "cuts": decisions,
            "summary": {"cuts": len(decisions), "clear": counts["clear"],
                        "recheck": counts["recheck"], "damaged": counts["damaged"]}}
