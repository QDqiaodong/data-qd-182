"""Compressed production geometry shared by trace validation and decisions.

The selected production string is at most eleven compressed segments (six jobs
plus five optional bridges).  Every question the trace layer asks -- which yarn
sits at production pick *p*, how many occurrences of a yarn lie in a pick range,
where the k-th occurrence is, which yarns appear in a range -- is answered by
period arithmetic on those segments.  Neither ``repeat_x`` nor ``repeat_y`` is
ever expanded into a cloth grid, so 128000 x 128000 virtual intersections stay
cheap in memory.
"""
from bisect import bisect_left
from collections import Counter

from .materials import ceil_permille


class Segment:
    __slots__ = ("index", "n", "repeat_count", "start", "yarns", "_positions", "_counts")

    def __init__(self, index, raw, start):
        self.index = index
        self.n = raw["n"]
        self.repeat_count = raw["repeat_count"]
        self.start = start
        self.yarns = raw["yarns"]
        self._positions = {}
        self._counts = Counter(self.yarns)

    @property
    def length(self):
        return self.n * self.repeat_count

    def positions(self, yarn):
        value = self._positions.get(yarn)
        if value is None:
            value = [i for i, item in enumerate(self.yarns) if item == yarn]
            self._positions[yarn] = value
        return value

    def per_cycle(self, yarn):
        return len(self.positions(yarn))


class Geometry:
    """Base-end identity plus compressed weft map of the selected plan."""

    def __init__(self, spec, raw_segments):
        self.spec = spec
        self.warp = spec["warp"]
        self.M = len(self.warp)
        self.repeat_x = spec["common"]["repeat_x"]
        self.E = self.M * self.repeat_x
        self.spacing = spec["common"]["pick_spacing_mm"]
        self.warp_spacing = spec["common"]["warp_spacing_mm"]
        self.takeup = spec["common"]["takeup_permille"]
        self.waste = spec["common"]["warp_waste_mm"]
        self.segments = []
        cursor = 0
        for index, raw in enumerate(raw_segments):
            segment = Segment(index, raw, cursor)
            self.segments.append(segment)
            cursor += segment.length
        self.P = cursor
        self._occ_total = {}
        self._prefix = {}

    # -- rounding ----------------------------------------------------------
    def F(self, picks):
        """Cloth length of one warp end over ``picks`` production picks."""
        return ceil_permille(picks * self.spacing, self.takeup)

    def H(self, ends):
        """Cloth length of one whole weft pick over ``ends`` real ends."""
        return ceil_permille(ends * self.warp_spacing, self.takeup)

    # -- segment lookup ----------------------------------------------------
    def segment_index(self, pick):
        lo, hi = 0, len(self.segments) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            segment = self.segments[mid]
            if pick < segment.start:
                hi = mid - 1
            elif pick >= segment.start + segment.length:
                lo = mid + 1
            else:
                return mid
        raise IndexError(pick)

    # -- occurrence arithmetic --------------------------------------------
    def _segment_offsets(self, segment, begin, end, yarn):
        """Occurrences of ``yarn`` in compressed offsets [begin, end)."""
        if begin >= end:
            return 0
        positions = segment.positions(yarn)
        per = len(positions)
        if per == 0:
            return 0
        n = segment.n
        q1, r1 = divmod(begin, n)
        q2, r2 = divmod(end, n)
        if q1 == q2:
            return bisect_left(positions, r2) - bisect_left(positions, r1)
        return (per - bisect_left(positions, r1)) + bisect_left(positions, r2) \
            + (q2 - q1 - 1) * per

    def occ_prefix(self, yarn, pick):
        """Occurrences of ``yarn`` in production picks [0, pick)."""
        if pick <= 0:
            return 0
        if pick > self.P:
            pick = self.P
        key = (yarn, pick)
        cached = self._prefix.get(key)
        if cached is not None:
            return cached
        total = 0
        for segment in self.segments:
            stop = segment.start + segment.length
            if pick >= stop:
                total += segment.per_cycle(yarn) * segment.repeat_count
            else:
                if pick > segment.start:
                    total += self._segment_offsets(segment, 0, pick - segment.start, yarn)
                break
        if len(self._prefix) < 100000:
            self._prefix[key] = total
        return total

    def occ_count(self, yarn, begin, end):
        return self.occ_prefix(yarn, end) - self.occ_prefix(yarn, begin)

    def occ_total(self, yarn):
        value = self._occ_total.get(yarn)
        if value is None:
            value = sum(segment.per_cycle(yarn) * segment.repeat_count
                        for segment in self.segments)
            self._occ_total[yarn] = value
        return value

    def kth_occurrence(self, yarn, index):
        """Production pick of the 0-based ``index``-th occurrence of ``yarn``."""
        for segment in self.segments:
            per = segment.per_cycle(yarn)
            full = per * segment.repeat_count
            if index >= full:
                index -= full
                continue
            positions = segment.positions(yarn)
            cycle, rest = divmod(index, per)
            return segment.start + cycle * segment.n + positions[rest]
        raise IndexError("occurrence index out of range")

    def next_occurrence(self, yarn, pick, bound=None):
        """First occurrence of ``yarn`` at or after ``pick`` and before ``bound``."""
        if bound is not None and pick >= bound:
            return None
        if pick <= 0:
            index = 0
        else:
            index = self.occ_prefix(yarn, pick)
        if index >= self.occ_total(yarn):
            return None
        position = self.kth_occurrence(yarn, index)
        if bound is not None and position >= bound:
            return None
        return position
