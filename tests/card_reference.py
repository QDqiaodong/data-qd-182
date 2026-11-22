"""独立于实现语义的极简模型，供测试与独立遍历核对使用。

只使用契约文本与 ``tie_up``/``treadles`` 的旧织机语义，不导入 card 的
搜索或缓存代码。所有核对以展开的实际拍为准。
"""
import itertools


def compile_tie_up(tie_up):
    return {row["id"]: list(row["shafts"]) for row in tie_up}


def lift_of(tie_up, pick):
    active = set()
    for pedal in pick:
        active.update(tie_up[pedal])
    return active


def draw_row(ends, tie_up, pick):
    active = lift_of(tie_up, pick)
    return tuple(int(shaft in active) for shaft in ends)


def allowed_picks(shafts, tie_up, ends, target, max_pressed, blocked):
    """该纬能织出 target、且不超同时踩上限、不含禁配对的全部踩法。"""
    ids = sorted(tie_up)
    blocked = {frozenset(pair) for pair in blocked}
    result = []
    for size in range(max_pressed + 1):
        for combo in itertools.combinations(ids, size):
            if any(frozenset(pair) in blocked
                   for pair in itertools.combinations(combo, 2)):
                continue
            if draw_row(ends, tie_up, combo) == target:
                result.append(combo)
    result.sort(key=lambda combo: tuple(sorted(combo)))
    return result


def emergency_sequences(allowed_rows, normal, start, fault_pedal, window,
                        start_prev=None):
    """按契约生成 (k, 应急踩法序列) 的可行集合；只使用给定常态表。"""
    length = len(normal)
    previous = normal[(start - 1) % length] if start_prev is None else start_prev
    found = []
    for count in range(1, window + 1):
        resume = normal[(start + count) % length]
        sequences = []
        rows = [allowed_rows[(start + step) % length] for step in range(count)]
        for candidate in itertools.product(*rows):
            if count >= 1 and fault_pedal in candidate[0]:
                continue
            ok = True
            prev = previous
            for step, pick in enumerate(candidate):
                if set(prev) & set(pick):
                    ok = False
                    break
                if step == count - 1 and set(pick) & set(resume):
                    ok = False
                    break
                prev = pick
            if ok:
                sequences.append(tuple(tuple(sorted(pick)) for pick in candidate))
        if sequences:
            sequences.sort()
            found.append((count, sequences[0]))
            break
    return found[0] if found else None


def validate_normal(ends, tie_up, blocked, target_rows, normal, max_pressed):
    """核对常态表：逐拍组织、同时踩、禁配、冷却、原样循环首尾。"""
    length = len(normal)
    if length != len(target_rows):
        return False
    blocked = {frozenset(pair) for pair in blocked}
    for index, pick in enumerate(normal):
        if draw_row(ends, tie_up, pick) != target_rows[index]:
            return False
        if len(pick) > max_pressed:
            return False
        if any(frozenset(pair) in blocked
               for pair in itertools.combinations(pick, 2)):
            return False
    for index in range(length):
        if set(normal[index]) & set(normal[(index + 1) % length]):
            return False
    return True
