"""Versioned, time-aware logical records.

A logical record is identified by its ``id`` inside one namespace.  Each version
carries ``revision``, ``known_at`` and a full-replacement ``payload``; a
``null`` payload withdraws the whole record.  At knowledge time T only the
greatest revision with ``known_at <= T`` is visible, and a withdrawn winner
means the record does not exist at T.

All versions are validated -- including versions known after ``as_of`` -- and a
late-arriving version may describe an earlier production position without ever
leaking into a historical decision.
"""
from bisect import bisect_right

from .identity import identifier
from .io import fields, integer, rows

MAX_REVISION = 1024
MAX_ROWS = 1024
MAX_TIME = 10 ** 12


def read_versions(value, name, validate_payload):
    """Validate one version array and group it by logical id.

    ``validate_payload`` receives each non-null payload object and returns the
    normalised payload (raising ``ValueError`` on bad content).  The result is
    ``{id: [version, ...]}`` sorted by revision, where each version is a dict
    with ``revision``, ``known_at`` and ``payload`` (``None`` for withdrawn).
    """
    grouped = {}
    for row in rows(value, name, 0, MAX_ROWS):
        fields(row, ("id", "revision", "known_at", "payload"))
        rid = identifier(row["id"], f"{name} id")
        revision = integer(row["revision"], f"{name} revision", 1, MAX_REVISION)
        known_at = integer(row["known_at"], f"{name} known_at", 0, MAX_TIME)
        payload = row["payload"]
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValueError(f"{name} payload must be an object or null")
            payload = validate_payload(payload)
        grouped.setdefault(rid, []).append(
            {"revision": revision, "known_at": known_at, "payload": payload})
    for rid, versions in grouped.items():
        versions.sort(key=lambda item: item["revision"])
        expected = 1
        previous_known = None
        for version in versions:
            if version["revision"] != expected:
                raise ValueError(f"{name} {rid}: revisions must be 1..N without gaps")
            if version["payload"] is None and version["revision"] == 1:
                raise ValueError(f"{name} {rid}: revision 1 cannot be a withdrawal")
            if previous_known is not None and version["known_at"] <= previous_known:
                raise ValueError(f"{name} {rid}: known_at must strictly increase")
            previous_known = version["known_at"]
            expected += 1
    return grouped


def visible(grouped, at):
    """Return ``[(id, version), ...]`` alive at knowledge time ``at``, sorted by id."""
    result = []
    for rid in sorted(grouped):
        versions = grouped[rid]
        # known_at strictly increases with revision, so it is already sorted.
        index = bisect_right([version["known_at"] for version in versions], at)
        if index:
            winner = versions[index - 1]
            if winner["payload"] is not None:
                result.append((rid, winner))
    return result
