"""纱线批次追溯与历史放行复核（loom-trace）。

本模块只增加追溯语义，不改变 loom-batch 的选单目标、可行性或材料契约：

* 批次计划、压缩段、整轴取整全部复用 ``batch.validate_batch`` /
  ``batch.production`` 与 ``materials.ceil_permille``；
* allocation / certificate 是带 ``known_at`` 的版本化逻辑记录，任一知识时刻只能
  看到当时已知的最大修订版，未来版本不会泄漏到历史决定；
* 归账按整批压缩段计算，不展开 ``repeat_x`` × ``repeat_y`` 的交织网格；
* 每块裁片分别用 ``requested_at`` 与 ``as_of`` 两个知识快照独立决定放行。

非法输入抛 ``ValueError``，返回值是普通字典。
"""
from collections import defaultdict

from .batch import validate_batch, production
from .identity import identifier
from .io import fields, integer, label, rows
from .trace_geometry import Geometry
from .versions import MAX_TIME, read_versions, visible

TOP_FIELDS = ("batch", "lots", "allocations", "certificates", "shipments", "as_of")
SHIPMENT_FIELDS = ("id", "requested_at", "end_start", "end_stop", "pick_start", "pick_stop")


# ---------------------------------------------------------------------------
# small interval helpers (integer, half-open)
# ---------------------------------------------------------------------------

def merge(intervals):
    items = sorted((a, b) for a, b in intervals if a < b)
    result = []
    for a, b in items:
        if result and a <= result[-1][1]:
            if b > result[-1][1]:
                result[-1][1] = b
        else:
            result.append([a, b])
    return [(a, b) for a, b in result]


def _ceil_div(value, divisor):
    return -((-value) // divisor)


# ---------------------------------------------------------------------------
# validated logical records
# ---------------------------------------------------------------------------

class Lot:
    __slots__ = ("id", "yarn", "capacity_mm")

    def __init__(self, rid, yarn, capacity_mm):
        self.id = rid
        self.yarn = yarn
        self.capacity_mm = capacity_mm


class Allocation:
    __slots__ = ("id", "revision", "known_at", "orientation", "lot_id",
                 "base_end", "repeat_start", "repeat_stop", "pick_start", "pick_stop")

    def __init__(self, rid, version):
        payload = version["payload"]
        self.id = rid
        self.revision = version["revision"]
        self.known_at = version["known_at"]
        self.orientation = payload["orientation"]
        self.lot_id = payload["lot_id"]
        self.pick_start = payload["pick_start"]
        self.pick_stop = payload["pick_stop"]
        warp = self.orientation == "warp"
        self.base_end = payload["base_end"] if warp else None
        self.repeat_start = payload["repeat_start"] if warp else None
        self.repeat_stop = payload["repeat_stop"] if warp else None


class Certificate:
    __slots__ = ("id", "revision", "known_at", "lot_id",
                 "pick_start", "pick_stop", "verdict")

    def __init__(self, rid, version):
        payload = version["payload"]
        self.id = rid
        self.revision = version["revision"]
        self.known_at = version["known_at"]
        self.lot_id = payload["lot_id"]
        self.pick_start = payload["pick_start"]
        self.pick_stop = payload["pick_stop"]
        self.verdict = payload["verdict"]


class Shipment:
    __slots__ = ("id", "requested_at", "end_start", "end_stop", "pick_start", "pick_stop")

    def __init__(self, rid, requested_at, end_start, end_stop, pick_start, pick_stop):
        self.id = rid
        self.requested_at = requested_at
        self.end_start = end_start
        self.end_stop = end_stop
        self.pick_start = pick_start
        self.pick_stop = pick_stop


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

def _lot_rows(value):
    lots = {}
    for row in rows(value, "lots", 0, 256):
        fields(row, ("id", "yarn", "capacity_mm"))
        rid = identifier(row["id"], "lot id")
        if rid in lots:
            raise ValueError(f"duplicate lot id: {rid}")
        yarn = label(row["yarn"], "lot yarn")
        capacity = integer(row["capacity_mm"], "lot capacity_mm", 0, 10**12)
        lots[rid] = Lot(rid, yarn, capacity)
    return lots


def _validate_allocations(value, spec, geometry, lots):
    M = geometry.M
    P = geometry.P
    repeat_x = geometry.repeat_x

    def validate(payload):
        orientation = payload.get("orientation")
        if orientation == "warp":
            fields(payload, ("orientation", "base_end", "repeat_start",
                             "repeat_stop", "pick_start", "pick_stop", "lot_id"))
            base_end = integer(payload["base_end"], "allocation base_end", 0, M - 1)
            repeat_start = integer(payload["repeat_start"], "allocation repeat_start",
                                   0, repeat_x - 1)
            repeat_stop = integer(payload["repeat_stop"], "allocation repeat_stop", 1, repeat_x)
            if not repeat_start < repeat_stop:
                raise ValueError("warp allocation requires repeat_start < repeat_stop")
            pick_start = integer(payload["pick_start"], "allocation pick_start", 0, P - 1)
            pick_stop = integer(payload["pick_stop"], "allocation pick_stop", 1, P)
            if not pick_start < pick_stop:
                raise ValueError("allocation requires pick_start < pick_stop")
            lot_id = identifier(payload["lot_id"], "allocation lot_id")
            if lot_id not in lots:
                raise ValueError(f"allocation references unknown lot: {lot_id}")
            if spec["warp"][base_end]["yarn"] != lots[lot_id].yarn:
                raise ValueError("warp allocation lot yarn must match base end yarn")
            return {"orientation": "warp", "base_end": base_end,
                    "repeat_start": repeat_start, "repeat_stop": repeat_stop,
                    "pick_start": pick_start, "pick_stop": pick_stop, "lot_id": lot_id}
        if orientation == "weft":
            fields(payload, ("orientation", "pick_start", "pick_stop", "lot_id"))
            pick_start = integer(payload["pick_start"], "allocation pick_start", 0, P - 1)
            pick_stop = integer(payload["pick_stop"], "allocation pick_stop", 1, P)
            if not pick_start < pick_stop:
                raise ValueError("allocation requires pick_start < pick_stop")
            lot_id = identifier(payload["lot_id"], "allocation lot_id")
            if lot_id not in lots:
                raise ValueError(f"allocation references unknown lot: {lot_id}")
            if geometry.occ_count(lots[lot_id].yarn, pick_start, pick_stop) == 0:
                raise ValueError("weft allocation must match at least one real weft")
            return {"orientation": "weft", "pick_start": pick_start,
                    "pick_stop": pick_stop, "lot_id": lot_id}
        raise ValueError("allocation orientation must be warp or weft")

    grouped = read_versions(value, "allocations", validate)
    for rid, versions in grouped.items():
        seen = None
        for version in versions:
            payload = version["payload"]
            if payload is None:
                continue
            if seen is None:
                seen = payload["orientation"]
            elif payload["orientation"] != seen:
                raise ValueError(f"allocation {rid}: orientation cannot change between versions")
    return grouped


def _validate_certificates(value, geometry, lots):
    P = geometry.P

    def validate(payload):
        fields(payload, ("lot_id", "pick_start", "pick_stop", "verdict"))
        lot_id = identifier(payload["lot_id"], "certificate lot_id")
        if lot_id not in lots:
            raise ValueError(f"certificate references unknown lot: {lot_id}")
        pick_start = integer(payload["pick_start"], "certificate pick_start", 0, P - 1)
        pick_stop = integer(payload["pick_stop"], "certificate pick_stop", 1, P)
        if not pick_start < pick_stop:
            raise ValueError("certificate requires pick_start < pick_stop")
        if payload["verdict"] not in ("pass", "fail"):
            raise ValueError("certificate verdict must be pass or fail")
        return {"lot_id": lot_id, "pick_start": pick_start,
                "pick_stop": pick_stop, "verdict": payload["verdict"]}

    return read_versions(value, "certificates", validate)


def _validate_shipments(value, geometry, as_of):
    E = geometry.E
    P = geometry.P
    shipments = []
    seen = set()
    for row in rows(value, "shipments", 0, 128):
        fields(row, SHIPMENT_FIELDS)
        rid = identifier(row["id"], "shipment id")
        if rid in seen:
            raise ValueError(f"duplicate shipment id: {rid}")
        seen.add(rid)
        requested_at = integer(row["requested_at"], "shipment requested_at", 0, as_of)
        end_start = integer(row["end_start"], "shipment end_start", 0, E - 1)
        end_stop = integer(row["end_stop"], "shipment end_stop", 1, E)
        pick_start = integer(row["pick_start"], "shipment pick_start", 0, P - 1)
        pick_stop = integer(row["pick_stop"], "shipment pick_stop", 1, P)
        if not end_start < end_stop:
            raise ValueError("shipment requires end_start < end_stop")
        if not pick_start < pick_stop:
            raise ValueError("shipment requires pick_start < pick_stop")
        shipments.append(Shipment(rid, requested_at, end_start, end_stop,
                                  pick_start, pick_stop))
    ordered = sorted(shipments, key=lambda s: (s.end_start, s.end_stop, s.id))
    active = []
    for ship in ordered:
        active = [other for other in active if other.end_stop > ship.end_start]
        for other in active:
            if max(ship.pick_start, other.pick_start) < min(ship.pick_stop, other.pick_stop):
                raise ValueError(f"shipments {ship.id} and {other.id} overlap on cloth")
        active.append(ship)
    return shipments


# ---------------------------------------------------------------------------
# cross-time contention
# ---------------------------------------------------------------------------

def validate_no_contention(geometry, lots, grouped):
    """No two allocations may claim the same real yarn at any knowledge time.

    Visible sets only change at ``known_at`` boundaries, so sweeping those
    times sees every simultaneous combination -- including combinations after
    ``as_of``.  Each changed record is compared against the same-index bucket
    (same base end for warp, same yarn for weft) after the boundary, so
    versions that never coexist never conflict and disjoint families are never
    compared.
    """
    events = defaultdict(list)
    for rid, versions in grouped.items():
        for version in versions:
            events[version["known_at"]].append((rid, version))
    current = {}
    warp_index = defaultdict(dict)     # base_end -> {id: alloc}
    weft_index = defaultdict(dict)     # yarn -> {id: alloc}

    def bucket_of(alloc):
        if alloc.orientation == "warp":
            return warp_index[alloc.base_end]
        return weft_index[lots[alloc.lot_id].yarn]

    def drop(alloc):
        bucket = bucket_of(alloc)
        if bucket.get(alloc.id) is alloc:
            del bucket[alloc.id]

    for when in sorted(events):
        changed = []
        for rid, version in sorted(events[when], key=lambda item: item[0]):
            previous = current.pop(rid, None)
            if previous is not None:
                drop(previous)
            if version["payload"] is not None:
                alloc = Allocation(rid, version)
                current[rid] = alloc
                bucket_of(alloc)[rid] = alloc
                changed.append(alloc)
        for alloc in changed:
            for other in bucket_of(alloc).values():
                if other is alloc:
                    continue
                if _contend(geometry, lots, alloc, other):
                    raise ValueError(
                        f"allocations {alloc.id} and {other.id} contend for the same yarn")


def _contend(geometry, lots, left, right):
    if left.orientation != right.orientation:
        return False
    if left.pick_start >= right.pick_stop or right.pick_start >= left.pick_stop:
        return False
    if left.orientation == "warp":
        if left.base_end != right.base_end:
            return False
        return left.repeat_start < right.repeat_stop and right.repeat_start < left.repeat_stop
    yarn = lots[left.lot_id].yarn
    if yarn != lots[right.lot_id].yarn:
        return False
    lo = max(left.pick_start, right.pick_start)
    hi = min(left.pick_stop, right.pick_stop)
    return lo < hi and geometry.occ_count(yarn, lo, hi) > 0


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class Snapshot:
    """Visible yarn ownership and evidence at one knowledge time."""

    def __init__(self, geometry, lots, allocation_groups, certificate_groups, at):
        self.geometry = geometry
        self.lots = lots
        self.at = at
        self.allocations = [Allocation(rid, version)
                            for rid, version in visible(allocation_groups, at)]
        self.certificates = [Certificate(rid, version)
                             for rid, version in visible(certificate_groups, at)]
        self.warp_by_base = defaultdict(list)
        self.weft_by_yarn = defaultdict(list)
        for alloc in self.allocations:
            if alloc.orientation == "warp":
                self.warp_by_base[alloc.base_end].append(alloc)
            else:
                self.weft_by_yarn[lots[alloc.lot_id].yarn].append(alloc)
        self._warp_cover = {}
        self._weft_cover = {}
        self._certs = defaultdict(list)
        for cert in self.certificates:
            self._certs[cert.lot_id].append(cert)
        self._balance = None

    def weft_cover(self, yarn):
        value = self._weft_cover.get(yarn)
        if value is None:
            value = merge((a.pick_start, a.pick_stop)
                          for a in self.weft_by_yarn.get(yarn, ()))
            self._weft_cover[yarn] = value
        return value

    def certificates_for(self, lot_id):
        return self._certs.get(lot_id, ())

    def balances(self):
        if self._balance is not None:
            return self._balance
        geometry = self.geometry
        warp = {lot_id: 0 for lot_id in self.lots}
        weft = {lot_id: 0 for lot_id in self.lots}
        for alloc in self.allocations:
            lot_id = alloc.lot_id
            if alloc.orientation == "warp":
                repeats = alloc.repeat_stop - alloc.repeat_start
                warp[lot_id] += (geometry.F(alloc.pick_stop)
                                 - geometry.F(alloc.pick_start)) * repeats
                if alloc.pick_start == 0:
                    warp[lot_id] += geometry.waste * repeats
            else:
                yarn = self.lots[lot_id].yarn
                strips = geometry.occ_count(yarn, alloc.pick_start, alloc.pick_stop)
                weft[lot_id] += strips * geometry.H(geometry.E)
        result = {}
        for lot_id, lot in self.lots.items():
            attributed = warp[lot_id] + weft[lot_id]
            result[lot_id] = {
                "lot_id": lot_id, "yarn": lot.yarn, "capacity_mm": lot.capacity_mm,
                "warp_mm": warp[lot_id], "weft_mm": weft[lot_id],
                "attributed_mm": attributed,
                "remaining_mm": lot.capacity_mm - attributed,
                "overdrawn": attributed > lot.capacity_mm,
            }
        self._balance = result
        return result

    def plan_total(self):
        geometry = self.geometry
        if geometry.P == 0 or geometry.E == 0:
            return 0
        return (geometry.F(geometry.P) + geometry.waste) * geometry.E \
            + geometry.P * geometry.H(geometry.E)

    def whole_unattributed(self):
        attributed = sum(item["attributed_mm"] for item in self.balances().values())
        return self.plan_total() - attributed


# ---------------------------------------------------------------------------
# geometry queries over a shipment rectangle
# ---------------------------------------------------------------------------

def _end_span(geometry, base, repeat_start, repeat_stop, end_start, end_stop):
    """Count real ends ``base + M*rep`` with rep in [repeat_start, repeat_stop)."""
    M = geometry.M
    low = max(repeat_start, _ceil_div(end_start - base, M), 0)
    high = min(repeat_stop, _ceil_div(end_stop - base, M))
    return max(0, high - low)


def _intersect(intervals, lo, hi):
    return [(max(a, lo), min(b, hi)) for a, b in intervals if max(a, lo) < min(b, hi)]


def _subtract(intervals, covers):
    """Points of ``intervals`` not covered by ``covers`` (both merged)."""
    result = []
    j = 0
    covers = covers or []
    for a, b in intervals:
        cursor = a
        while j < len(covers) and covers[j][1] <= cursor:
            j += 1
        k = j
        while k < len(covers) and covers[k][0] < b:
            c, d = covers[k]
            if c > cursor:
                result.append((cursor, min(c, b)))
            cursor = max(cursor, d)
            if cursor >= b:
                break
            k += 1
        if cursor < b:
            result.append((cursor, b))
    return [(a, b) for a, b in result if a < b]


def _occurrences(geometry, yarn, intervals):
    return sum(geometry.occ_count(yarn, a, b) for a, b in intervals)


def _first_uncovered_end(geometry, base, repeat_start, repeat_stop,
                         end_start, end_stop, pick, allocations):
    M = geometry.M
    low = max(repeat_start, _ceil_div(end_start - base, M), 0)
    high = min(repeat_stop, _ceil_div(end_stop - base, M))
    if high <= low:
        return None
    covered = merge((alloc.repeat_start, alloc.repeat_stop) for alloc in allocations
                    if alloc.pick_start <= pick < alloc.pick_stop)
    cursor = low
    for a, b in covered:
        if a > cursor:
            return base + M * cursor
        cursor = max(cursor, b)
        if cursor >= high:
            return None
    return base + M * cursor if cursor < high else None


class PieceWarp:
    def __init__(self):
        self.missing_points = 0
        self.missing_material = 0
        self.first_pick = None
        self.first_end = None
        self.material = defaultdict(int)
        self.used = defaultdict(list)       # lot -> merged pick intervals
        self.allocs = defaultdict(set)


def analyze_warp(snapshot, shipment):
    geometry = snapshot.geometry
    es, ee = shipment.end_start, shipment.end_stop
    ps, pe = shipment.pick_start, shipment.pick_stop
    result = PieceWarp()
    for base in range(geometry.M):
        present = _end_span(geometry, base, 0, geometry.repeat_x, es, ee)
        if present == 0:
            continue
        allocations = snapshot.warp_by_base.get(base, ())
        clips = []
        events = {}
        for alloc in allocations:
            ends = _end_span(geometry, base, alloc.repeat_start, alloc.repeat_stop, es, ee)
            if not ends:
                continue
            lo = max(alloc.pick_start, ps)
            hi = min(alloc.pick_stop, pe)
            if lo >= hi:
                continue
            clips.append((lo, hi, ends, alloc))
            events[lo] = events.get(lo, 0) + ends
            events[hi] = events.get(hi, 0) - ends
        if not clips:
            # every present (end, pick) of this base is a gap
            result.missing_points += present * (pe - ps)
            result.missing_material += present * (geometry.F(pe) - geometry.F(ps))
            end = _first_uncovered_end(geometry, base, 0, geometry.repeat_x,
                                       es, ee, ps, allocations)
            if end is not None and (result.first_pick is None
                                    or (ps, end) < (result.first_pick, result.first_end)):
                result.first_pick, result.first_end = ps, end
            continue
        covered = 0
        previous = ps
        first_gap_pick = None
        for point in sorted(events):
            if point > previous and covered < present:
                gap = present - covered
                result.missing_points += (point - previous) * gap
                result.missing_material += (geometry.F(point) - geometry.F(previous)) * gap
                if first_gap_pick is None:
                    first_gap_pick = previous
            covered += events[point]
            previous = point
        if previous < pe and covered < present:
            gap = present - covered
            result.missing_points += (pe - previous) * gap
            result.missing_material += (geometry.F(pe) - geometry.F(previous)) * gap
            if first_gap_pick is None:
                first_gap_pick = previous
        if first_gap_pick is not None:
            end = _first_uncovered_end(geometry, base, 0, geometry.repeat_x,
                                       es, ee, first_gap_pick, allocations)
            if end is not None and (result.first_pick is None
                                    or (first_gap_pick, end) < (result.first_pick,
                                                                result.first_end)):
                result.first_pick, result.first_end = first_gap_pick, end
        for lo, hi, ends, alloc in clips:
            result.material[alloc.lot_id] += (geometry.F(hi) - geometry.F(lo)) * ends
            result.used[alloc.lot_id].append((lo, hi))
            result.allocs[alloc.lot_id].add((alloc.id, alloc.revision))
    for lot_id in result.used:
        result.used[lot_id] = merge(result.used[lot_id])
    return result


class PieceWeft:
    def __init__(self):
        self.missing_picks = 0
        self.missing_material = 0
        self.first_pick = None
        self.material = defaultdict(int)
        self.used = defaultdict(list)
        self.yarn = {}
        self.allocs = defaultdict(set)


def analyze_weft(snapshot, shipment):
    geometry = snapshot.geometry
    es, ee = shipment.end_start, shipment.end_stop
    ps, pe = shipment.pick_start, shipment.pick_stop
    width = geometry.H(ee) - geometry.H(es)
    result = PieceWeft()
    covered = 0
    for yarn in snapshot.weft_by_yarn:
        clipped = _intersect(snapshot.weft_cover(yarn), ps, pe)
        covered += _occurrences(geometry, yarn, clipped)
    total = pe - ps
    result.missing_picks = total - covered
    result.missing_material = result.missing_picks * width
    if result.missing_picks:
        result.first_pick = _first_missing_weft(snapshot, ps, pe)
    for yarn, allocs in snapshot.weft_by_yarn.items():
        for alloc in allocs:
            lo = max(alloc.pick_start, ps)
            hi = min(alloc.pick_stop, pe)
            if lo >= hi:
                continue
            strips = geometry.occ_count(yarn, lo, hi)
            if strips == 0:
                continue
            lot_id = alloc.lot_id
            result.material[lot_id] += strips * width
            result.used[lot_id].append((lo, hi))
            result.yarn[lot_id] = yarn
            result.allocs[lot_id].add((alloc.id, alloc.revision))
    for lot_id in result.used:
        result.used[lot_id] = merge(result.used[lot_id])
    return result


def _covered_strips(snapshot, lo, hi):
    if lo >= hi:
        return 0
    geometry = snapshot.geometry
    total = 0
    for yarn in snapshot.weft_by_yarn:
        clipped = _intersect(snapshot.weft_cover(yarn), lo, hi)
        total += _occurrences(geometry, yarn, clipped)
    return total


def _first_missing_weft(snapshot, lo, hi):
    if lo >= hi:
        return None
    if (hi - lo) - _covered_strips(snapshot, lo, hi) <= 0:
        return None
    low, high = lo + 1, hi
    while low < high:
        mid = (low + high) // 2
        if (mid - lo) - _covered_strips(snapshot, lo, mid) > 0:
            high = mid
        else:
            low = mid + 1
    return low - 1


# ---------------------------------------------------------------------------
# certificate evidence
# ---------------------------------------------------------------------------

class Evidence:
    def __init__(self):
        self.failed = False
        self.uncertified = False
        self.first_failed_pick = None
        self.first_uncertified_pick = None
        self.referenced = set()


def _covered_intervals(certificates, verdicts, lo, hi):
    return merge((max(cert.pick_start, lo), min(cert.pick_stop, hi))
                 for cert in certificates if cert.verdict in verdicts)


def _first_occurrence_in(geometry, yarn, intervals):
    for lo, hi in intervals:
        position = geometry.next_occurrence(yarn, lo, hi)
        if position is not None:
            return position
    return None


def _count_in(geometry, yarn, intervals):
    return sum(geometry.occ_count(yarn, lo, hi) for lo, hi in intervals)


def _warp_evidence(certificates, intervals):
    """Warp use: every pick in a warp use interval is used by >=1 real end."""
    evidence = Evidence()
    for lo, hi in intervals:
        fail = _covered_intervals(certificates, ("fail",), lo, hi)
        if fail:
            evidence.failed = True
            evidence.first_failed_pick = _min_none(evidence.first_failed_pick, fail[0][0])
        gap = _subtract([(lo, hi)], _covered_intervals(certificates, ("pass", "fail"), lo, hi))
        if gap:
            evidence.uncertified = True
            evidence.first_uncertified_pick = _min_none(evidence.first_uncertified_pick,
                                                        gap[0][0])
    return evidence


def _weft_evidence(geometry, yarn, certificates, intervals):
    """Weft use: only actual occurrences of the lot yarn are used positions."""
    evidence = Evidence()
    for lo, hi in intervals:
        fail = _covered_intervals(certificates, ("fail",), lo, hi)
        if _count_in(geometry, yarn, fail):
            evidence.failed = True
            evidence.first_failed_pick = _min_none(
                evidence.first_failed_pick, _first_occurrence_in(geometry, yarn, fail))
        gap = _subtract([(lo, hi)], _covered_intervals(certificates, ("pass", "fail"), lo, hi))
        if _count_in(geometry, yarn, gap):
            evidence.uncertified = True
            evidence.first_uncertified_pick = _min_none(
                evidence.first_uncertified_pick, _first_occurrence_in(geometry, yarn, gap))
    return evidence


def _referenced_certificates(snapshot, lot_id, yarn, warp_intervals, weft_intervals):
    geometry = snapshot.geometry
    referenced = set()
    for cert in snapshot.certificates_for(lot_id):
        hit = any(max(cert.pick_start, a) < min(cert.pick_stop, b) for a, b in warp_intervals)
        if not hit:
            clipped = [(max(cert.pick_start, a), min(cert.pick_stop, b))
                       for a, b in weft_intervals
                       if max(cert.pick_start, a) < min(cert.pick_stop, b)]
            hit = bool(clipped) and _count_in(geometry, yarn, clipped) > 0
        if hit:
            referenced.add((cert.id, cert.revision))
    return referenced


# ---------------------------------------------------------------------------
# one snapshot's decision for one shipment
# ---------------------------------------------------------------------------

def _refs(items):
    return [{"id": rid, "revision": revision}
            for rid, revision in sorted(items, key=lambda item: (item[0], item[1]))]


def _min_none(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return left if left <= right else right


def _first_interval_start(intervals):
    best = None
    for a, b in intervals:
        if a < b and (best is None or a < best):
            best = a
    return best


def _first_missing(warp, weft, shipment):
    """Earliest missing witness: warp takes precedence, then pick, then end."""
    if warp.missing_points:
        return {"orientation": "warp", "end_index": warp.first_end,
                "pick_index": warp.first_pick}
    if weft.missing_picks:
        return {"orientation": "weft", "end_index": shipment.end_start,
                "pick_index": weft.first_pick}
    return None


def decide(snapshot, shipment):
    warp = analyze_warp(snapshot, shipment)
    weft = analyze_weft(snapshot, shipment)
    balances = snapshot.balances()
    geometry = snapshot.geometry
    problems = {}

    def add(kind, lot_id, pick):
        key = (kind, lot_id if lot_id is not None else "")
        if key in problems:
            problems[key]["first_pick"] = _min_none(problems[key]["first_pick"], pick)
        else:
            problems[key] = {"kind": kind, "lot_id": lot_id, "first_pick": pick}

    if warp.missing_points or weft.missing_picks:
        add("source_gap", None, _min_none(warp.first_pick, weft.first_pick))

    lots = []
    for lot_id in sorted(set(warp.material) | set(weft.material)):
        warp_mm = warp.material.get(lot_id, 0)
        weft_mm = weft.material.get(lot_id, 0)
        whole = balances[lot_id]
        yarn = whole["yarn"]
        warp_intervals = warp.used.get(lot_id, [])
        weft_intervals = weft.used.get(lot_id, [])
        certificates = snapshot.certificates_for(lot_id)
        warp_ev = _warp_evidence(certificates, warp_intervals)
        weft_ev = _weft_evidence(geometry, yarn, certificates, weft_intervals)
        used_first = _min_none(
            _first_interval_start(warp_intervals),
            _first_occurrence_in(geometry, yarn, weft_intervals))
        if whole["overdrawn"]:
            add("overdrawn", lot_id, used_first)
        if warp_ev.failed or weft_ev.failed:
            add("failed", lot_id, _min_none(warp_ev.first_failed_pick,
                                            weft_ev.first_failed_pick))
        if warp_ev.uncertified or weft_ev.uncertified:
            add("uncertified", lot_id, _min_none(warp_ev.first_uncertified_pick,
                                                 weft_ev.first_uncertified_pick))
        lots.append({
            "lot_id": lot_id,
            "yarn": yarn,
            "warp_mm": warp_mm,
            "weft_mm": weft_mm,
            "total_mm": warp_mm + weft_mm,
            "whole_attributed_mm": whole["attributed_mm"],
            "overdrawn": whole["overdrawn"],
            "allocation_versions": _refs(warp.allocs.get(lot_id, set())
                                         | weft.allocs.get(lot_id, set())),
            "certificate_versions": _refs(
                _referenced_certificates(snapshot, lot_id, yarn, warp_intervals, weft_intervals)),
        })
    ordered = [problems[key] for key in sorted(problems)]
    return {"knowledge_at": snapshot.at, "allowed": not ordered, "lots": lots,
            "missing": {"warp_points": warp.missing_points,
                        "weft_picks": weft.missing_picks,
                        "material_mm": warp.missing_material + weft.missing_material,
                        "first": _first_missing(warp, weft, shipment)},
            "problems": ordered}


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------

DISPOSITION = {(True, True): "released", (True, False): "recall",
               (False, True): "ready", (False, False): "hold"}


def _lot_balances(snapshot):
    return [snapshot.balances()[lot_id] for lot_id in sorted(snapshot.lots)]


def _shipment_report(shipment, historical, current):
    return {"id": shipment.id,
            "disposition": DISPOSITION[(historical["allowed"], current["allowed"])],
            "historical": historical, "current": current}


def run_trace(data):
    """Validate and compute a trace report; illegal input raises ``ValueError``."""
    if not isinstance(data, dict):
        raise ValueError("root must be an object")
    fields(data, TOP_FIELDS)
    as_of = integer(data["as_of"], "as_of", 0, MAX_TIME)
    spec = validate_batch(data["batch"])
    report, raw_segments = production(spec)
    geometry = Geometry(spec, raw_segments)
    lots = _lot_rows(data["lots"])
    empty_plan = geometry.P == 0 or geometry.E == 0
    if empty_plan:
        for name in ("allocations", "certificates", "shipments"):
            if data[name]:
                raise ValueError(
                    "empty plan requires empty allocations, certificates and shipments")
    allocations = _validate_allocations(data["allocations"], spec, geometry, lots)
    certificates = _validate_certificates(data["certificates"], geometry, lots)
    shipments = _validate_shipments(data["shipments"], geometry, as_of)
    validate_no_contention(geometry, lots, allocations)

    final = Snapshot(geometry, lots, allocations, certificates, as_of)
    snapshots = {}
    decisions = []
    for shipment in shipments:
        if shipment.requested_at not in snapshots:
            snapshots[shipment.requested_at] = Snapshot(
                geometry, lots, allocations, certificates, shipment.requested_at)
        historical = decide(snapshots[shipment.requested_at], shipment)
        current = decide(final, shipment)
        decisions.append(_shipment_report(shipment, historical, current))

    decisions.sort(key=lambda item: item["id"])
    summary = {"shipments": len(decisions), "released": 0, "recall": 0,
               "ready": 0, "hold": 0,
               "whole_unattributed_mm": final.whole_unattributed()}
    for item in decisions:
        summary[item["disposition"]] += 1
    whole_unattributed = final.whole_unattributed()
    return {"schema_version": 1,
            "production": {"plan": report["plan"], "segments": report["segments"],
                           "material_requirements": report["material_requirements"]},
            "lot_balances": _lot_balances(final),
            "whole_unattributed_mm": whole_unattributed,
            "shipments": decisions,
            "summary": summary}
