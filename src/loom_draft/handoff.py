"""拆纬重织后的耗纱交班单（loom-handoff）。

本模块在 loom-batch 已选计划之上跟踪一个工位一卷布的实织耗纱：

* 计划纬序 0..P-1 完全由 ``batch.validate_batch`` / ``batch.production``
  选出的唯一方案展开（含相位与接缝），实织事件不改变该计划；
* 每织入一纬按整幅纬耗 ``ceil(宽 × (1000+takeup_permille)/1000)`` 从当时
  该型号的在用纱筒扣料，并逐纬记住（纱筒, 织入班号）归属；
* 拆纬把尾料改记报废（不退料、不改归属），签封把尾料移入成布；
* ``batch.stock`` 只是原计划预算，实物余量从 ``spools`` 单独扣。

交班 checkpoint 是自包含的紧凑对象（格式见 docs/contract-handoff.md）：
坐标、在用筒、按筒累计的 成布/尾料/废耗 三元组，以及尾窗内逐纬的
（纱筒索引, 织入班号）。历史只保留累计量，尾窗最多 2048 纬，因此任意
多班交接的序列化尺寸都有常数上界，远低于 65536 字节。checkpoint 用
规范 JSON（键排序、纱筒按 id 排序）的 SHA-256 绑定原 batch 与 spools。

非法输入抛 ``ValueError``，返回值是普通字典。
"""
import hashlib
import json
from collections import Counter

from .batch import validate_batch, production
from .identity import identifier
from .io import fields, integer, label, rows
from .materials import ceil_permille

TAIL_WINDOW = 2048
MAX_SHIFT = 2_147_483_647
MAX_EVENTS = 120_000
MAX_SPOOLS = 32
CHECKPOINT_LIMIT = 65_536

TOP_FIELDS = ("batch", "spools", "checkpoint", "shift")
CHECKPOINT_FIELDS = ("v", "base", "shift", "sealed", "next", "mounted",
                     "amounts", "tail_spool", "tail_shift")


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

def _validate_spools(value):
    spools = []
    seen = set()
    for row in rows(value, "spools", 1, MAX_SPOOLS):
        fields(row, ("id", "yarn", "length_mm"))
        sid = identifier(row["id"], "spool id")
        if sid in seen:
            raise ValueError(f"duplicate spool id: {sid}")
        seen.add(sid)
        spools.append({"id": sid,
                       "yarn": label(row["yarn"], "spool yarn"),
                       "length_mm": integer(row["length_mm"], "spool length_mm",
                                            0, 10 ** 12)})
    spools.sort(key=lambda spool: spool["id"])
    return spools


def _validate_shift(value, spool_index):
    fields(value, ("no", "events"))
    no = integer(value["no"], "shift no", 1, MAX_SHIFT)
    events = []
    for event in rows(value["events"], "shift events", 0, MAX_EVENTS):
        if not isinstance(event, dict):
            raise ValueError("shift event must be an object")
        op = event.get("op")
        if op == "mount":
            fields(event, ("op", "spool_id"))
            sid = identifier(event["spool_id"], "mount spool_id")
            if sid not in spool_index:
                raise ValueError(f"mount references unknown spool: {sid}")
            events.append(("mount", spool_index[sid]))
        elif op in ("weave", "unpick", "seal"):
            fields(event, ("op", "count"))
            events.append((op, integer(event["count"], f"{op} count", 1, 10 ** 12)))
        else:
            raise ValueError(f"unknown event op: {op!r}")
    return no, events


def _baseline(batch, spool_rows):
    """原 batch 与 spools 的绑定指纹：键序无关、纱筒可重排、数组保序。"""
    canonical_batch = json.dumps(batch, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False)
    ordered = sorted(spool_rows, key=lambda row: row["id"])
    canonical_spools = json.dumps(ordered, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False)
    payload = canonical_batch + "\n" + canonical_spools
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------

class _State:
    __slots__ = ("prev_shift", "sealed", "next_pick", "mounted", "amounts",
                 "tail_spool", "tail_shift")

    def __init__(self, prev_shift, sealed, next_pick, mounted, amounts,
                 tail_spool, tail_shift):
        self.prev_shift = prev_shift          # 最近一个已完成班的班号
        self.sealed = sealed                  # 已签封卷走的计划纬前缀长
        self.next_pick = next_pick            # 布尾下一纬的计划纬坐标
        self.mounted = mounted                # {yarn: 纱筒索引}
        self.amounts = amounts                # 每筒 [sealed_mm, tail_mm, scrap_mm]
        self.tail_spool = tail_spool          # 尾料逐纬纱筒索引
        self.tail_shift = tail_shift          # 尾料逐纬织入班号


def _fresh_state(spool_count):
    return _State(0, 0, 0, {}, [[0, 0, 0] for _ in range(spool_count)], [], [])


def _parse_checkpoint(value, base, spools, plan_yarns, unit):
    """严格验证上一班 checkpoint 并还原状态；任何自相矛盾都抛 ValueError。"""
    if not isinstance(value, dict):
        raise ValueError("checkpoint must be an object or null")
    fields(value, CHECKPOINT_FIELDS)
    encoded = json.dumps(value, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > CHECKPOINT_LIMIT:
        raise ValueError("checkpoint exceeds 65536 bytes")
    integer(value["v"], "checkpoint v", 1, 1)
    if value["base"] != base:
        raise ValueError("checkpoint does not match this batch and spools")
    prev_shift = integer(value["shift"], "checkpoint shift", 1, MAX_SHIFT)
    sealed = integer(value["sealed"], "checkpoint sealed", 0, 10 ** 12)
    next_pick = integer(value["next"], "checkpoint next", 0, 10 ** 12)
    total = len(plan_yarns)
    if not sealed <= next_pick <= total:
        raise ValueError("checkpoint coordinates out of range")
    if next_pick - sealed > TAIL_WINDOW:
        raise ValueError("checkpoint tail exceeds the 2048-pick window")
    count = len(spools)
    index = {spool["id"]: i for i, spool in enumerate(spools)}
    mounted = {}
    mounted_raw = value["mounted"]
    if not isinstance(mounted_raw, list) or len(mounted_raw) > count:
        raise ValueError("checkpoint mounted must be an array of spool ids")
    for sid in mounted_raw:
        if not isinstance(sid, str) or sid not in index:
            raise ValueError(f"checkpoint mounted unknown spool: {sid}")
        yarn = spools[index[sid]]["yarn"]
        if yarn in mounted:
            raise ValueError("checkpoint mounts two spools of one yarn")
        mounted[yarn] = index[sid]
    amounts_raw = value["amounts"]
    if not isinstance(amounts_raw, list) or len(amounts_raw) != count:
        raise ValueError("checkpoint amounts must align with the spool list")
    amounts = []
    for i, triple in enumerate(amounts_raw):
        if not isinstance(triple, list) or len(triple) != 3:
            raise ValueError("checkpoint amounts rows must be triples")
        row = [integer(item, "checkpoint amount", 0, 10 ** 12) for item in triple]
        if any(item % unit for item in row):
            raise ValueError("checkpoint amount is not a whole-pick multiple")
        if sum(row) > spools[i]["length_mm"]:
            raise ValueError("checkpoint spool amounts exceed its start length")
        amounts.append(row)
    if sum(row[0] for row in amounts) != sealed * unit:
        raise ValueError("checkpoint sealed total does not match coordinates")
    sealed_counts = Counter(plan_yarns[:sealed])
    sealed_by_yarn = {}
    for i, spool in enumerate(spools):
        yarn = spool["yarn"]
        sealed_by_yarn[yarn] = sealed_by_yarn.get(yarn, 0) + amounts[i][0]
    for yarn in set(sealed_by_yarn) | set(sealed_counts):
        if sealed_by_yarn.get(yarn, 0) != sealed_counts.get(yarn, 0) * unit:
            raise ValueError("checkpoint sealed amounts do not match the plan")
    tail_spool_raw = value["tail_spool"]
    tail_shift_raw = value["tail_shift"]
    if not isinstance(tail_spool_raw, list) or not isinstance(tail_shift_raw, list):
        raise ValueError("checkpoint tail attribution must be arrays")
    if len(tail_spool_raw) != next_pick - sealed \
            or len(tail_shift_raw) != next_pick - sealed:
        raise ValueError("checkpoint tail attribution does not match coordinates")
    tail_spool = [integer(item, "checkpoint tail spool", 0, count - 1)
                  for item in tail_spool_raw]
    tail_shift = [integer(item, "checkpoint tail shift", 1, prev_shift)
                  for item in tail_shift_raw]
    for offset, spool_idx in enumerate(tail_spool):
        if spools[spool_idx]["yarn"] != plan_yarns[sealed + offset]:
            raise ValueError("checkpoint tail attribution yarn mismatch")
    tail_counts = Counter(tail_spool)
    for i in range(count):
        if amounts[i][1] != tail_counts.get(i, 0) * unit:
            raise ValueError("checkpoint tail amount does not match attribution")
    return _State(prev_shift, sealed, next_pick, mounted, amounts,
                  tail_spool, tail_shift)


def _checkpoint(state, base, spools, shift_no):
    return {"v": 1,
            "base": base,
            "shift": shift_no,
            "sealed": state.sealed,
            "next": state.next_pick,
            "mounted": sorted(spools[idx]["id"] for idx in state.mounted.values()),
            "amounts": [list(triple) for triple in state.amounts],
            "tail_spool": list(state.tail_spool),
            "tail_shift": list(state.tail_shift)}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def _remaining(state, spools, idx):
    sealed, tail, scrap = state.amounts[idx]
    return spools[idx]["length_mm"] - sealed - tail - scrap


def _weave(state, count, plan_yarns, unit, shift_no, spools):
    start = state.next_pick
    if count > len(plan_yarns) - start:
        raise ValueError("weave exceeds the planned picks")
    if count > TAIL_WINDOW - (start - state.sealed):
        raise ValueError("weave exceeds the 2048-pick tail window")
    indices = []
    needed = Counter()
    for yarn in plan_yarns[start:start + count]:
        idx = state.mounted.get(yarn)
        if idx is None:
            raise ValueError(f"no mounted spool for yarn: {yarn}")
        indices.append(idx)
        needed[idx] += 1
    for idx, picks in needed.items():
        if picks * unit > _remaining(state, spools, idx):
            raise ValueError(f"spool {spools[idx]['id']} has insufficient yarn")
    for idx, picks in needed.items():
        state.amounts[idx][1] += picks * unit
    state.tail_spool.extend(indices)
    state.tail_shift.extend([shift_no] * count)
    state.next_pick = start + count


def _unpick(state, count, unit, scrap_log):
    if count > state.next_pick - state.sealed:
        raise ValueError("unpick crosses the sealed prefix")
    per_spool = Counter(state.tail_spool[-count:])
    per_source = Counter(zip(state.tail_shift[-count:], state.tail_spool[-count:]))
    del state.tail_spool[-count:]
    del state.tail_shift[-count:]
    for idx, picks in per_spool.items():
        state.amounts[idx][1] -= picks * unit
        state.amounts[idx][2] += picks * unit
    for source, picks in per_source.items():
        scrap_log[source] += picks * unit
    state.next_pick -= count


def _seal(state, count, unit):
    if count > state.next_pick - state.sealed:
        raise ValueError("seal exceeds the woven tail")
    per_spool = Counter(state.tail_spool[:count])
    del state.tail_spool[:count]
    del state.tail_shift[:count]
    for idx, picks in per_spool.items():
        state.amounts[idx][0] += picks * unit
        state.amounts[idx][1] -= picks * unit
    state.sealed += count


def _process(state, events, plan_yarns, unit, shift_no, spools, scrap_log):
    for op, arg in events:
        if op == "mount":
            state.mounted[spools[arg]["yarn"]] = arg
        elif op == "weave":
            _weave(state, arg, plan_yarns, unit, shift_no, spools)
        elif op == "unpick":
            _unpick(state, arg, unit, scrap_log)
        else:
            _seal(state, arg, unit)


# ---------------------------------------------------------------------------
# resume simulation (never touches real state)
# ---------------------------------------------------------------------------

def _resume(state, plan_yarns, unit, spools):
    total = len(plan_yarns)
    pick = state.next_pick
    virtual = {}
    while True:
        if pick >= total:
            return {"count": pick - state.next_pick, "stop_pick": total,
                    "reason": "plan_end", "yarn": None, "spool_id": None}
        if pick - state.sealed >= TAIL_WINDOW:
            return {"count": pick - state.next_pick, "stop_pick": pick,
                    "reason": "tail_full", "yarn": None, "spool_id": None}
        yarn = plan_yarns[pick]
        idx = state.mounted.get(yarn)
        if idx is None:
            return {"count": pick - state.next_pick, "stop_pick": pick,
                    "reason": "no_spool", "yarn": yarn, "spool_id": None}
        if idx not in virtual:
            virtual[idx] = _remaining(state, spools, idx)
        if virtual[idx] < unit:
            return {"count": pick - state.next_pick, "stop_pick": pick,
                    "reason": "insufficient", "yarn": yarn,
                    "spool_id": spools[idx]["id"]}
        virtual[idx] -= unit
        pick += 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _report(state, spools, plan_yarns, unit, shift_no, base, scrap_log):
    mounted_idx = set(state.mounted.values())
    spool_rows = []
    for i, spool in enumerate(spools):
        sealed, tail, scrap = state.amounts[i]
        spool_rows.append({"spool_id": spool["id"], "yarn": spool["yarn"],
                           "mounted": i in mounted_idx,
                           "remaining_mm": spool["length_mm"] - sealed - tail - scrap,
                           "sealed_mm": sealed, "tail_mm": tail, "scrap_mm": scrap})
    scrap_rows = [{"woven_shift": woven, "spool_id": spools[idx]["id"], "mm": mm}
                  for (woven, idx), mm in sorted(scrap_log.items())]
    resume = _resume(state, plan_yarns, unit, spools)
    summary = {"shift_no": shift_no, "sealed": state.sealed,
               "next_pick": state.next_pick,
               "scrap_this_shift_mm": sum(scrap_log.values()),
               "resume_count": resume["count"], "resume_reason": resume["reason"]}
    return {"schema_version": 1,
            "shift_no": shift_no,
            "sealed": state.sealed,
            "next_pick": state.next_pick,
            "total_picks": len(plan_yarns),
            "spools": spool_rows,
            "scrap_this_shift": scrap_rows,
            "resume": resume,
            "checkpoint": _checkpoint(state, base, spools, shift_no),
            "summary": summary}


def run_handoff(data):
    """验证一班输入并返回交班单（普通字典）；非法输入抛 ``ValueError``。"""
    if not isinstance(data, dict):
        raise ValueError("root must be an object")
    fields(data, TOP_FIELDS)
    spec = validate_batch(data["batch"])
    spools = _validate_spools(data["spools"])
    spool_index = {spool["id"]: i for i, spool in enumerate(spools)}
    base = _baseline(data["batch"], data["spools"])
    _, raw_segments = production(spec)
    plan_yarns = []
    for segment in raw_segments:
        plan_yarns.extend(segment["yarns"] * segment["repeat_count"])
    common = spec["common"]
    width = len(spec["warp"]) * common["repeat_x"] * common["warp_spacing_mm"]
    unit = ceil_permille(width, common["takeup_permille"])
    shift_no, events = _validate_shift(data["shift"], spool_index)
    if data["checkpoint"] is None:
        if shift_no != 1:
            raise ValueError("the first shift requires no == 1")
        state = _fresh_state(len(spools))
    else:
        state = _parse_checkpoint(data["checkpoint"], base, spools,
                                  plan_yarns, unit)
        if shift_no != state.prev_shift + 1:
            raise ValueError("shift no must be the checkpoint shift plus one")
    scrap_log = Counter()
    _process(state, events, plan_yarns, unit, shift_no, spools, scrap_log)
    return _report(state, spools, plan_yarns, unit, shift_no, base, scrap_log)
