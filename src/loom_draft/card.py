"""共用绑综的连续织造工艺卡（loom-card）。

同一套踏板绑综供所有款使用；每款在开织前另行选定常态踩法。本模块对
“改绑最少的可行绑综、各款常态表、以及每个单拍故障的应急踩法”做全局
精确搜索，按契约规定的分层字典序选出唯一工艺卡。

组织编译复用 v1 语义：同一拍升起的综框是该拍所踩踏板所绑综框的并集，
``1`` 表示该经端在纬纱上方。踏板绑综与踩法都沿用旧织机可表达的格式。
"""
from .identity import identifier
from .io import fields, integer, label, rows
from .model import warp_ends

MAX_SHAFTS = 16
MAX_WARP = 128
MAX_PEDALS = 8
MAX_OPTIONS = 3
MAX_PATTERNS = 6
MAX_ROWS = 48
MAX_BLOCKS = 28
MAX_FAULTS = 96
MAX_REJOIN = 3


# ---------------------------------------------------------------------------
# 输入验证
# ---------------------------------------------------------------------------

def _shaft_set(value, shafts, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array of shafts")
    values = [integer(item, name, 1, shafts) for item in value]
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate shaft in {name}")
    return tuple(sorted(values))


def validate(data):
    """严格验证工艺卡输入；返回规范化后的内部规格（非报告结构）。"""
    fields(data, ("shafts", "warp", "pedals", "max_pressed", "blocked_pairs",
                  "patterns", "rejoin_limit"))
    shafts = integer(data["shafts"], "shafts", 1, MAX_SHAFTS)
    warp = warp_ends(data, shafts)
    if not 1 <= len(warp) <= MAX_WARP:
        raise ValueError("warp out of range")
    max_pressed = integer(data["max_pressed"], "max_pressed", 1, 3)

    pedals = []
    seen_ids = set()
    for row in rows(data["pedals"], "pedals", 1, MAX_PEDALS):
        fields(row, ("id", "current", "options"))
        pid = label(row["id"], "pedal id")
        if pid in seen_ids:
            raise ValueError(f"duplicate pedal ID: {pid}")
        seen_ids.add(pid)
        options = []
        seen_options = set()
        for option in rows(row["options"], "pedal options", 1, MAX_OPTIONS):
            key = _shaft_set(option, shafts, "pedal option")
            if key in seen_options:
                raise ValueError("duplicate pedal option")
            seen_options.add(key)
            options.append(key)
        current = _shaft_set(row["current"], shafts, "pedal current")
        if current not in seen_options:
            raise ValueError("pedal current must be one of its options")
        pedals.append({"id": pid, "current": current, "options": options})
    pedal_ids = [pedal["id"] for pedal in pedals]
    id_to_bit = {pid: index for index, pid in enumerate(pedal_ids)}

    blocked = set()
    for pair in rows(data["blocked_pairs"], "blocked_pairs", 0, MAX_BLOCKS):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("blocked pair must be an array of two pedal ids")
        left = label(pair[0], "blocked pedal id")
        right = label(pair[1], "blocked pedal id")
        if left not in id_to_bit or right not in id_to_bit:
            raise ValueError("blocked pair references unknown pedal")
        if left == right:
            raise ValueError("blocked pair must reference distinct pedals")
        key = (min(id_to_bit[left], id_to_bit[right]),
               max(id_to_bit[left], id_to_bit[right]))
        if key in blocked:
            raise ValueError("duplicate blocked pair")
        blocked.add(key)

    patterns = []
    seen_patterns = set()
    for row in rows(data["patterns"], "patterns", 1, MAX_PATTERNS):
        fields(row, ("id", "rows", "faults"))
        pid = identifier(row["id"], "pattern id")
        if pid in seen_patterns:
            raise ValueError(f"duplicate pattern ID: {pid}")
        seen_patterns.add(pid)
        grid = []
        for text in rows(row["rows"], "pattern rows", 1, MAX_ROWS):
            if not isinstance(text, str) or len(text) != len(warp):
                raise ValueError("pattern row length must equal warp length")
            if any(ch not in "01" for ch in text):
                raise ValueError("pattern row may only contain 0 and 1")
            grid.append(tuple(int(ch) for ch in text))
        faults = []
        seen_faults = set()
        for fault in rows(row["faults"], "pattern faults", 0, MAX_FAULTS):
            if not isinstance(fault, list) or len(fault) != 2:
                raise ValueError("fault must be a [row, pedal_id] pair")
            index = integer(fault[0], "fault row", 0, len(grid) - 1)
            pedal = label(fault[1], "fault pedal id")
            if pedal not in id_to_bit:
                raise ValueError("fault references unknown pedal")
            if (index, pedal) in seen_faults:
                raise ValueError("duplicate fault")
            seen_faults.add((index, pedal))
            faults.append((index, pedal))
        faults.sort()
        patterns.append({"id": pid, "rows": grid, "faults": faults})
    patterns.sort(key=lambda item: item["id"])

    rejoin_limit = integer(data["rejoin_limit"], "rejoin_limit", 0, MAX_REJOIN)
    return {"shafts": shafts, "warp": warp, "pedals": pedals,
            "pedal_ids": pedal_ids, "id_to_bit": id_to_bit,
            "max_pressed": max_pressed, "blocked": blocked,
            "patterns": patterns, "rejoin_limit": rejoin_limit}


# ---------------------------------------------------------------------------
# 组织目标：逐纬必须升起/落下的综框
# ---------------------------------------------------------------------------

def row_targets(warp, grid):
    """返回 ``[(up, down), ...]`` 位掩码；同综框经端要求相反时返回 ``None``。

    没有经端穿过的综框不进入掩码，可以任意升降。
    """
    by_shaft = {}
    for position, end in enumerate(warp):
        by_shaft.setdefault(end["shaft"], []).append(position)
    targets = []
    for index in range(len(grid)):
        up = down = 0
        for shaft, positions in by_shaft.items():
            bit = grid[index][positions[0]]
            for position in positions[1:]:
                if grid[index][position] != bit:
                    return None
            if bit:
                up |= 1 << (shaft - 1)
            else:
                down |= 1 << (shaft - 1)
        targets.append((up, down))
    return targets


def all_masks(pedal_count, max_pressed, blocked):
    """全部单拍踏板集合位掩码（不含禁配对、不超同时踩上限）。"""
    pairs = [(1 << a) | (1 << b) for a, b in blocked]
    result = []
    for mask in range(1 << pedal_count):
        if bin(mask).count("1") > max_pressed:
            continue
        if any(mask & pair == pair for pair in pairs):
            continue
        result.append(mask)
    return result


def mask_shafts(bound, mask):
    lift = 0
    for bit, shafts in enumerate(bound):
        if mask >> bit & 1:
            lift |= shafts
    return lift


def mask_pick(bound_ids, mask):
    """按踏板 ID 的 ASCII 升序给出该拍踩法（字符串元组）。"""
    return tuple(sorted(bound_ids[bit] for bit in range(len(bound_ids))
                        if mask >> bit & 1))


class Card:
    """固定绑综下的编译上下文：缓存每纬可选踩法。"""

    def __init__(self, spec, bound, valid_masks):
        self.spec = spec
        self.bound = bound
        self.valid_masks = valid_masks
        self.pedal_ids = spec["pedal_ids"]
        self.pedal_count = len(bound)
        self._cache = {}
        self.pick = {mask: mask_pick(self.pedal_ids, mask) for mask in valid_masks}

    def allowed(self, up, down):
        """该纬仍能织出目标位串的全部踩法，按踩法 ID 序列升序。"""
        key = (up, down)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        safe = 0
        for bit, shafts in enumerate(self.bound):
            if shafts & down == 0:
                safe |= 1 << bit
        result = []
        for mask in self.valid_masks:
            if mask & ~safe:
                continue
            if mask_shafts(self.bound, mask) & up == up:
                result.append(mask)
        result.sort(key=lambda mask: self.pick[mask])
        self._cache[key] = result
        return result


# ---------------------------------------------------------------------------
# 应急踩法
# ---------------------------------------------------------------------------

def emergency(card, allowed_rows, normal, length, start, fault_bit, window):
    """故障拍起的应急踩法：最短优先，再踩法序列字典序最小。

    ``normal`` 可以是部分填充的常态表（长度不足 ``length``），只读取已赋值
    的前一拍与接回拍；返回 ``(k, (mask, ...))`` 或 ``None``。
    """
    previous = normal[(start - 1) % length]
    for count in range(1, window + 1):
        resume = normal[(start + count) % length]
        dead = set()

        def search(step, prev):
            if step == count:
                return ()
            state = (step, prev)
            if state in dead:
                return None
            row = (start + step) % length
            for mask in allowed_rows[row]:
                if step == 0 and mask >> fault_bit & 1:
                    continue
                if mask & prev:
                    continue
                if step == count - 1 and mask & resume:
                    continue
                suffix = search(step + 1, mask)
                if suffix is not None:
                    return (mask,) + suffix
            dead.add(state)
            return None

        result = search(0, previous)
        if result is not None:
            return count, result
    return None


# ---------------------------------------------------------------------------
# 常态表：字典序最小且全部故障可处置
# ---------------------------------------------------------------------------

def _dependencies(length, index, window):
    deps = {(index - 1) % length}
    for step in range(1, window + 1):
        deps.add((index + step) % length)
    return deps


def normal_sequence(card, allowed_rows, faults, fault_bit_of, window):
    """字典序最小的周期常态表；任一故障无应急串则无解返回 ``None``。

    ``faults`` 为 ``[(row, pedal_id), ...]``。返回按 0 起纬序的踩法位掩码列表。
    先用相邻不重叠约束做前向/后向可达性传播，再按逐纬字典序回溯。
    """
    length = len(allowed_rows)
    if any(not options for options in allowed_rows):
        return None

    checks = []
    by_last = {}
    for index, pedal in faults:
        deps = _dependencies(length, index, window)
        entry = (index, fault_bit_of[pedal], deps)
        checks.append(entry)
        by_last.setdefault(max(deps), []).append(entry)

    # 尚未校验的故障会读取的已赋值纬号：对其依赖集取并。只保留这些已赋值
    # 纬号可让等价历史合并；多余的远处纬号无法影响后续任何校验。
    relevant_at = [()] * length
    for t in range(length):
        past = set()
        for _, _, deps in checks:
            if max(deps) >= t:
                past.update(dep for dep in deps if dep < t)
        relevant_at[t] = tuple(sorted(past))

    cache = {}

    def fault_ok(index, bit, deps, assigned):
        # 应急串在拍内只依赖“前一拍”和“各候选接回拍”，其余按应急自身链式
        # 冷却；这些纬号在 dependencies 中已完整列出，按实际值缓存是可靠的。
        key = (index, bit, tuple(assigned[dep] for dep in sorted(deps)))
        if key not in cache:
            cache[key] = emergency(card, allowed_rows, assigned, length, index,
                                   bit, window) is not None
        return cache[key]

    def passes(assigned, t):
        for index, bit, deps in by_last.get(t, ()):
            if not fault_ok(index, bit, deps, assigned):
                return False
        return True

    def start_possible(index, bit, previous):
        """故障拍首拍的必要条件：存在不含故障踏板、且与前一拍不重叠的踩法。

        前一拍未知时只检查“存在不含故障踏板的踩法”。作为必要剪枝，不影响
        最终由 ``passes`` 做出的完整判定。
        """
        candidates = [mask for mask in allowed_rows[index]
                      if mask >> bit & 1 == 0]
        if not candidates:
            return False
        if previous is None:
            return True
        return any(mask & previous == 0 for mask in candidates)

    # 故障按起始纬分组；当该纬与前驱纬都已知时先做必要剪枝。
    by_start = {}
    for index, bit, _ in checks:
        by_start.setdefault(index, []).append((index, bit))

    def reachable_domains(row0):
        """相邻不重叠约束下的前向/后向可达域；空域即该 row0 无解。"""
        forward = [set()] * length
        forward[0] = {row0}
        for t in range(1, length):
            previous = forward[t - 1]
            forward[t] = {mask for mask in allowed_rows[t]
                          if any(mask & prev == 0 for prev in previous)}
            if not forward[t]:
                return None
        backward = [set()] * length
        backward[length - 1] = {mask for mask in allowed_rows[length - 1]
                                if mask & row0 == 0}
        if not backward[length - 1]:
            return None
        for t in range(length - 2, -1, -1):
            following = backward[t + 1]
            backward[t] = {mask for mask in allowed_rows[t]
                           if any(mask & nxt == 0 for nxt in following)}
            if not backward[t]:
                return None
        domains = []
        for t in range(length):
            domain = [mask for mask in allowed_rows[t]
                      if mask in forward[t] and mask in backward[t]]
            if t == 0:
                domain = [mask for mask in domain if mask == row0]
            if not domain:
                return None
            domains.append(domain)
        return domains

    def solve_first(row0):
        if not all(start_possible(index, bit, None)
                   for index, bit in by_start.get(0, ())):
            return None
        domains = reachable_domains(row0)
        if domains is None:
            return None
        if not passes([row0], 0):
            return None
        dead = set()

        def dfs(t, assigned):
            if t == length:
                return () if assigned[-1] & row0 == 0 else None
            key = (t, assigned[-1],
                   tuple(assigned[d] for d in relevant_at[t]))
            if key in dead:
                return None
            previous = assigned[-1]
            # 该纬是故障起始纬时，首拍是否可能只取决于真实前一拍，与本节候选
            # 无关；不可能则整条分支剪掉。
            if any(not start_possible(index, bit, previous)
                   for index, bit in by_start.get(t, ())):
                dead.add(key)
                return None
            for mask in domains[t]:
                if mask & previous:
                    continue
                assigned.append(mask)
                if passes(assigned, t):
                    suffix = dfs(t + 1, assigned)
                    if suffix is not None:
                        assigned.pop()
                        return (mask,) + suffix
                assigned.pop()
            dead.add(key)
            return None

        suffix = dfs(1, [row0])
        return None if suffix is None else [row0] + list(suffix)

    for row0 in allowed_rows[0]:
        result = solve_first(row0)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# 绑综枚举与全局选卡
# ---------------------------------------------------------------------------

def _combinations(values, size):
    if size == 0:
        yield ()
        return
    for index in range(len(values) - size + 1):
        for rest in _combinations(values[index + 1:], size - 1):
            yield (values[index],) + rest


def _product(pools):
    if not pools:
        yield ()
        return
    for value in pools[0]:
        for rest in _product(pools[1:]):
            yield (value,) + rest


def binding_orders(pedals):
    """按改动踏板数升序生成全部绑综选择（每项为各踏板 option 下标）。"""
    current = [pedal["options"].index(pedal["current"]) for pedal in pedals]
    changeable = [index for index in range(len(pedals))
                  if len(pedals[index]["options"]) > 1]
    for changed in range(len(changeable) + 1):
        for subset in _combinations(changeable, changed):
            chosen = set(subset)
            pools = []
            for index in range(len(pedals)):
                if index in chosen:
                    pools.append([option for option in range(len(pedals[index]["options"]))
                                  if option != current[index]])
                else:
                    pools.append([current[index]])
            for selection in _product(pools):
                yield changed, selection


def _evaluate(spec, selection, valid_masks, targets_by_pattern):
    bound = []
    tie_up = []
    for pedal, option_index in zip(spec["pedals"], selection):
        shafts = pedal["options"][option_index]
        bound.append(sum(1 << (shaft - 1) for shaft in shafts))
        tie_up.append((pedal["id"], shafts))
    tie_up.sort(key=lambda item: item[0])

    card = Card(spec, bound, valid_masks)
    window = 1 + spec["rejoin_limit"]
    # 先验证约束最紧的款（行数多、故障多、每纬可选踩法少），以便尽早失败。
    order = sorted(range(len(spec["patterns"])),
                   key=lambda j: (-len(spec["patterns"][j]["faults"]),
                                  -len(targets_by_pattern[j]),
                                  j))
    normals = {}
    for j in order:
        pattern = spec["patterns"][j]
        allowed_rows = [card.allowed(up, down) for up, down in targets_by_pattern[j]]
        sequence = normal_sequence(card, allowed_rows, pattern["faults"],
                                   spec["id_to_bit"], window)
        if sequence is None:
            return None
        normals[pattern["id"]] = sequence
    normal_key = tuple(tuple(card.pick[mask] for mask in normals[pattern["id"]])
                       for pattern in spec["patterns"])
    return {"selection": selection,
            "tie_up_key": tuple(shafts for _, shafts in tie_up),
            "normal_key": normal_key,
            "card": card, "normals": normals}


def select(spec, valid_masks, targets_by_pattern):
    """按全局分层规则选卡；无可用卡返回 ``None``。"""
    best = None
    best_changed = None
    for changed, selection in binding_orders(spec["pedals"]):
        if best_changed is not None and changed > best_changed:
            break
        evaluated = _evaluate(spec, selection, valid_masks, targets_by_pattern)
        if evaluated is None:
            continue
        key = (evaluated["tie_up_key"], evaluated["normal_key"])
        if best is None or key < best[0]:
            best = (key, changed, evaluated)
            best_changed = changed
    if best is None:
        return None
    return best[1], best[2]


# ---------------------------------------------------------------------------
# 报告与入口
# ---------------------------------------------------------------------------

def infeasible_report():
    return {"schema_version": 1, "status": "infeasible", "changed_pedals": None,
            "tie_up": [], "patterns": []}


def _build(spec, changed, evaluated):
    card = evaluated["card"]
    window = 1 + spec["rejoin_limit"]
    patterns = []
    for pattern in spec["patterns"]:
        normal = evaluated["normals"][pattern["id"]]
        targets = row_targets(spec["warp"], pattern["rows"])
        allowed_rows = [card.allowed(up, down) for up, down in targets]
        playbook = []
        for index, pedal in pattern["faults"]:
            count, picks = emergency(card, allowed_rows, normal, len(normal),
                                     index, spec["id_to_bit"][pedal], window)
            playbook.append({"row": index, "pedal_id": pedal,
                             "picks": [list(card.pick[mask]) for mask in picks],
                             "resume_row": (index + count) % len(normal)})
        patterns.append({"id": pattern["id"],
                         "picks": [list(card.pick[mask]) for mask in normal],
                         "fault_playbook": playbook})
    tie_up = []
    for pedal, option_index in zip(spec["pedals"], evaluated["selection"]):
        tie_up.append({"id": pedal["id"],
                       "shafts": list(pedal["options"][option_index])})
    tie_up.sort(key=lambda item: item["id"])
    return {"schema_version": 1, "status": "ready", "changed_pedals": changed,
            "tie_up": tie_up, "patterns": patterns}


def run_card(data):
    """验证输入并返回工艺卡报告（普通字典）；非法输入抛 ``ValueError``。"""
    spec = validate(data)
    targets_by_pattern = []
    for pattern in spec["patterns"]:
        targets = row_targets(spec["warp"], pattern["rows"])
        if targets is None:
            return infeasible_report()
        targets_by_pattern.append(targets)
    valid_masks = all_masks(len(spec["pedals"]), spec["max_pressed"],
                            spec["blocked"])
    selected = select(spec, valid_masks, targets_by_pattern)
    if selected is None:
        return infeasible_report()
    changed, evaluated = selected
    return _build(spec, changed, evaluated)
