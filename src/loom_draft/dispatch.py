"""实际裁片发货台账与召回交接（loom-dispatch）。

loom-trace 的 ``historical.allowed`` 只推断“当时是否具备放行条件”，本模块补上
仓库真正发生的事实：

* 一块布只能被实际交付一次，发货占用是矩形事实，召回/恢复/确认都不释放面积；
* 每次成功发货永久冻结当次 trace 决定（``admitted_decision``），后续导入的新证据
  只能改写 ``current_decision`` 并在 allowed 发生 true↔false 翻转时生成召回/恢复
  事件，绝不回头改写旧凭据；
* 所有状态变更（导入+复核+事件、整批发货、整批确认+回执）在一个本地文件提交内
  原子生效，``fcntl`` 互斥锁串行化多进程，进程被强杀后锁由内核释放、临时文件由
  下次打开清理，已提交未返回的请求靠永久回执原样重放。

质量判断完全复用 :mod:`loom_draft.trace` 的验证、几何、版本可见性与 ``decide``，
不复制另一套放行语义。仅使用 Python 标准库。
"""
import copy
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager

from .batch import production, validate_batch
from .identity import identifier
from .io import fields, integer
from .trace import (TOP_FIELDS, Shipment, Snapshot, decide,
                    validate_no_contention, _lot_rows,
                    _validate_allocations, _validate_certificates)
from .trace_geometry import Geometry
from .versions import MAX_TIME

SCHEMA_VERSION = 1
MAX_DELIVERED_CUTS = 128
MAX_CUTS_PER_REQUEST = 16
MAX_ACK_EVENTS = 128
LEDGER_NAME = "ledger.json"
LOCK_NAME = ".lock"
TMP_PREFIX = ".commit-"
FAULT_ENV = "LOOM_DISPATCH_FAULT"

INIT_FIELDS = ("op", "request_id", "source")
INGEST_FIELDS = ("op", "request_id", "expected_generation", "source")
DISPATCH_FIELDS = ("op", "request_id", "expected_generation", "cuts")
ACK_FIELDS = ("op", "request_id", "event_ids")
CUT_FIELDS = ("cut_id", "end_start", "end_stop", "pick_start", "pick_stop")
OPS = ("init", "ingest", "dispatch", "ack")


# ---------------------------------------------------------------------------
# JSON structural equality (object key order ignored, array order preserved)
# ---------------------------------------------------------------------------

def json_equal(left, right):
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if set(left) != set(right):
            return False
        return all(json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        return len(left) == len(right) and all(
            json_equal(a, b) for a, b in zip(left, right))
    return type(left) == type(right) and left == right


# ---------------------------------------------------------------------------
# validated source knowledge — every rule reused from loom-trace
# ---------------------------------------------------------------------------

class Knowledge:
    """A fully validated loom-trace input with shipments forced empty."""

    def __init__(self, source):
        if not isinstance(source, dict):
            raise ValueError("source must be an object")
        fields(source, TOP_FIELDS)
        if source["shipments"] != []:
            raise ValueError("dispatch source requires an empty shipments array")
        self.source = source
        self.as_of = integer(source["as_of"], "as_of", 0, MAX_TIME)
        self.spec = validate_batch(source["batch"])
        self.report, raw_segments = production(self.spec)
        self.geometry = Geometry(self.spec, raw_segments)
        self.lots = _lot_rows(source["lots"])
        self.empty_plan = self.geometry.P == 0 or self.geometry.E == 0
        if self.empty_plan and (source["allocations"] or source["certificates"]):
            raise ValueError(
                "empty plan requires empty allocations, certificates and shipments")
        self.allocations = _validate_allocations(
            source["allocations"], self.spec, self.geometry, self.lots)
        self.certificates = _validate_certificates(
            source["certificates"], self.geometry, self.lots)
        validate_no_contention(self.geometry, self.lots, self.allocations)
        for grouped in (self.allocations, self.certificates):
            for versions in grouped.values():
                for version in versions:
                    if version["known_at"] > self.as_of:
                        raise ValueError(
                            "dispatch source cannot contain versions known after as_of")

    def decide(self, rect, requested_at):
        """Exactly the trace decision for one cut rectangle at ``requested_at``."""
        snapshot = Snapshot(self.geometry, self.lots, self.allocations,
                            self.certificates, requested_at)
        shipment = Shipment(rect["cut_id"], requested_at, rect["end_start"],
                            rect["end_stop"], rect["pick_start"], rect["pick_stop"])
        return decide(snapshot, shipment)

    def production_view(self):
        return {"plan": self.report["plan"], "segments": self.report["segments"],
                "material_requirements": self.report["material_requirements"]}


# ---------------------------------------------------------------------------
# stateless command format validation (input errors never consume request_id)
# ---------------------------------------------------------------------------

def _strict_id(value, name):
    return identifier(value, name)


def _validate_cut_shapes(value):
    """Store-independent cut checks; geometry bounds are checked against the
    committed source once the store is open (still an input error)."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CUTS_PER_REQUEST:
        raise ValueError(f"cuts requires an array of 1..{MAX_CUTS_PER_REQUEST} items")
    cuts = []
    seen = set()
    for row in value:
        fields(row, CUT_FIELDS)
        cut_id = _strict_id(row["cut_id"], "cut_id")
        if cut_id in seen:
            raise ValueError(f"duplicate cut_id in request: {cut_id}")
        seen.add(cut_id)
        for name in row:
            if name != "cut_id" and type(row[name]) is not int:
                raise ValueError(f"cut {name} requires an integer")
        end_start, end_stop = row["end_start"], row["end_stop"]
        pick_start, pick_stop = row["pick_start"], row["pick_stop"]
        if not end_start < end_stop:
            raise ValueError("cut requires end_start < end_stop")
        if not pick_start < pick_stop:
            raise ValueError("cut requires pick_start < pick_stop")
        cuts.append({"cut_id": cut_id, "end_start": end_start, "end_stop": end_stop,
                     "pick_start": pick_start, "pick_stop": pick_stop})
    return cuts


def _check_cut_bounds(cuts, knowledge):
    E = knowledge.geometry.E
    P = knowledge.geometry.P
    for cut in cuts:
        integer(cut["end_start"], "end_start", 0, E - 1)
        integer(cut["end_stop"], "end_stop", 1, E)
        integer(cut["pick_start"], "pick_start", 0, P - 1)
        integer(cut["pick_stop"], "pick_stop", 1, P)


def validate_command(command):
    """Pure, store-independent format validation.

    Returns ``(op, request_id, payload)`` where payload carries the validated
    knowledge/cuts/event ids.  Raises ``ValueError`` on every input error.
    """
    if not isinstance(command, dict):
        raise ValueError("command must be an object")
    if "op" not in command:
        raise ValueError("command requires op")
    op = command["op"]
    if op not in OPS:
        raise ValueError("op must be one of init, ingest, dispatch, ack")
    request_id = _strict_id(command.get("request_id"), "request_id")
    if op == "init":
        fields(command, INIT_FIELDS)
        knowledge = Knowledge(command["source"])
        return op, request_id, {"knowledge": knowledge}
    if op == "ingest":
        fields(command, INGEST_FIELDS)
        expected = integer(command["expected_generation"], "expected_generation",
                           1, 10**12)
        knowledge = Knowledge(command["source"])
        return op, request_id, {"knowledge": knowledge, "expected_generation": expected}
    if op == "dispatch":
        fields(command, DISPATCH_FIELDS)
        expected = integer(command["expected_generation"], "expected_generation",
                           1, 10**12)
        cuts = _validate_cut_shapes(command["cuts"])
        return op, request_id, {"expected_generation": expected, "cuts": cuts}
    fields(command, ACK_FIELDS)
    event_ids = command["event_ids"]
    if not isinstance(event_ids, list) or not 1 <= len(event_ids) <= MAX_ACK_EVENTS:
        raise ValueError(f"event_ids requires an array of 1..{MAX_ACK_EVENTS} items")
    validated = []
    for event_id in event_ids:
        validated.append(integer(event_id, "event_id", 1, 10**12))
    if len(set(validated)) != len(validated):
        raise ValueError("duplicate event_id in ack request")
    return op, request_id, {"event_ids": validated}


# ---------------------------------------------------------------------------
# accumulation contract between two committed sources
# ---------------------------------------------------------------------------

def _version_index(source, name):
    index = {}
    for row in source[name]:
        index[(row["id"], row["revision"])] = row
    return index


def check_accumulation(old_source, new_source, old_as_of, new_as_of):
    """Raise ``ValueError`` when the new source rewrites history or jumps time."""
    if new_as_of <= old_as_of:
        raise ValueError("ingest requires new as_of strictly greater than current as_of")
    if not json_equal(old_source["batch"], new_source["batch"]):
        raise ValueError("batch is fixed after initialization")
    if not json_equal(old_source["lots"], new_source["lots"]):
        raise ValueError("lots are fixed after initialization")
    for name in ("allocations", "certificates"):
        old_index = _version_index(old_source, name)
        new_index = _version_index(new_source, name)
        for key, old_version in old_index.items():
            new_version = new_index.get(key)
            if new_version is None:
                raise ValueError(
                    f"ingest cannot remove existing {name} version {key[0]}@{key[1]}")
            if not json_equal(old_version, new_version):
                raise ValueError(
                    f"ingest cannot rewrite existing {name} version {key[0]}@{key[1]}")
        for key, new_version in new_index.items():
            if key not in old_index and new_version["known_at"] <= old_as_of:
                raise ValueError(
                    f"new {name} version {key[0]}@{key[1]} must be known after "
                    "the previous as_of")


# ---------------------------------------------------------------------------
# rectangle facts
# ---------------------------------------------------------------------------

def overlaps(left, right):
    return (max(left["end_start"], right["end_start"])
            < min(left["end_stop"], right["end_stop"])
            and max(left["pick_start"], right["pick_start"])
            < min(left["pick_stop"], right["pick_stop"]))


def _receipt(request_id, op, status, generation, result):
    return {"schema_version": SCHEMA_VERSION, "request_id": request_id, "op": op,
            "status": status, "generation": generation, "result": result}


# ---------------------------------------------------------------------------
# persistent store: one ledger file, one flock, atomic replace
# ---------------------------------------------------------------------------

class StoreError(ValueError):
    """Raised when an existing ledger is externally damaged.

    Subclasses ``ValueError`` so the CLI reports it concisely at exit 2 while
    callers can still match the specific ``StoreError`` type.
    """


class _Store:
    def __init__(self, store_path):
        self.dir = os.path.abspath(os.fspath(store_path))
        self.ledger_path = os.path.join(self.dir, LEDGER_NAME)
        self.lock_path = os.path.join(self.dir, LOCK_NAME)
        self.lock_file = None
        self.state = None

    def open_for_write(self):
        os.makedirs(self.dir, exist_ok=True)
        self.lock_file = open(self.lock_path, "a+b")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX)
        self._clean_temps()
        self.state = self._load()

    def open_for_read(self):
        if not os.path.isdir(self.dir):
            raise ValueError(f"store does not exist: {self.dir}")
        self.lock_file = open(self.lock_path, "a+b")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_SH)
        self.state = self._load()
        if self.state is None:
            raise ValueError(f"store is not initialized: {self.dir}")

    def close(self):
        if self.lock_file is not None:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None

    def _clean_temps(self):
        for name in os.listdir(self.dir):
            if name.startswith(TMP_PREFIX) and name.endswith(".tmp"):
                try:
                    os.unlink(os.path.join(self.dir, name))
                except FileNotFoundError:
                    pass

    def _load(self):
        if not os.path.exists(self.ledger_path):
            self._check_usable_empty_dir()
            return None
        try:
            with open(self.ledger_path, "rb") as handle:
                raw = handle.read()
            state = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"ledger is unreadable or corrupted: {exc}") from exc
        if not isinstance(state, dict):
            raise StoreError("ledger root must be an object")
        required = {"schema_version", "generation", "source", "initial_batch",
                    "initial_lots", "deliveries", "events", "next_event_id",
                    "requests"}
        if set(state) != required or state["schema_version"] != SCHEMA_VERSION:
            raise StoreError("ledger schema is damaged")
        if not isinstance(state["requests"], dict) or not state["requests"]:
            raise StoreError("ledger request log is damaged")
        return state

    def _check_usable_empty_dir(self):
        allowed = {LOCK_NAME}
        for name in os.listdir(self.dir):
            if name in allowed or (name.startswith(TMP_PREFIX) and name.endswith(".tmp")):
                continue
            raise StoreError(
                f"store directory contains foreign file without a ledger: {name}")

    def commit(self):
        fault = os.environ.get(FAULT_ENV)
        if fault == "before_commit":
            os._exit(3)
        encoded = (json.dumps(self.state, ensure_ascii=False, allow_nan=False,
                              indent=2) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=TMP_PREFIX, suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.ledger_path)
            directory = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        if fault == "after_commit":
            os._exit(4)


@contextmanager
def _locked_store(store_path, write):
    store = _Store(store_path)
    try:
        if write:
            store.open_for_write()
        else:
            store.open_for_read()
    except BaseException:
        if store.lock_file is not None:
            store.lock_file.close()
            store.lock_file = None
        raise
    try:
        yield store
    finally:
        store.close()


# ---------------------------------------------------------------------------
# op implementations
# ---------------------------------------------------------------------------

def _initial_state(source):
    return {"schema_version": SCHEMA_VERSION, "generation": 1,
            "source": copy.deepcopy(source),
            "initial_batch": copy.deepcopy(source["batch"]),
            "initial_lots": copy.deepcopy(source["lots"]),
            "deliveries": [], "events": [], "next_event_id": 1, "requests": {}}


def _stored_command(state, request_id):
    entry = state["requests"].get(request_id)
    return None if entry is None else entry["command"]


def _save_receipt(state, request_id, op, command, receipt):
    state["requests"][request_id] = {"command": copy.deepcopy(command),
                                     "receipt": receipt}


def _do_init(store, command, request_id, payload):
    state = store.state
    if state is not None:
        receipt = _receipt(request_id, "init", "rejected", state["generation"],
                           {"code": "already_initialized"})
        _save_receipt(state, request_id, "init", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    knowledge = payload["knowledge"]
    state = _initial_state(knowledge.source)
    store.state = state
    receipt = _receipt(request_id, "init", "accepted", 1,
                       {"as_of": knowledge.as_of, "transition_event_ids": []})
    _save_receipt(state, request_id, "init", command, receipt)
    store.commit()
    return copy.deepcopy(receipt)


def _recompute_current(state, knowledge):
    for delivery in state["deliveries"]:
        decision = knowledge.decide(delivery, knowledge.as_of)
        delivery["current_decision"] = decision
        delivery["state"] = "recall" if not decision["allowed"] else "normal"


def _do_ingest(store, command, request_id, payload):
    state = store.state
    knowledge = payload["knowledge"]
    # expected_generation mismatch precedes every other store-dependent check,
    # including the source accumulation contract.
    if payload["expected_generation"] != state["generation"]:
        receipt = _receipt(request_id, "ingest", "rejected", state["generation"],
                           {"code": "stale_generation"})
        _save_receipt(state, request_id, "ingest", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    check_accumulation(state["source"], knowledge.source,
                       state["source"]["as_of"], knowledge.as_of)
    previous_allowed = {d["cut_id"]: d["current_decision"]["allowed"]
                        for d in state["deliveries"]}
    _recompute_current(state, knowledge)
    new_events = []
    for delivery in sorted(state["deliveries"], key=lambda item: item["cut_id"]):
        cut_id = delivery["cut_id"]
        was_allowed = previous_allowed[cut_id]
        now_allowed = delivery["current_decision"]["allowed"]
        if was_allowed and not now_allowed:
            kind = "recall"
        elif not was_allowed and now_allowed:
            kind = "clear"
        else:
            continue
        event = {"event_id": state["next_event_id"], "kind": kind, "cut_id": cut_id,
                 "generation": state["generation"] + 1,
                 "knowledge_at": knowledge.as_of,
                 "decision": copy.deepcopy(delivery["current_decision"]),
                 "acknowledged": False}
        state["next_event_id"] += 1
        new_events.append(event)
    state["events"].extend(new_events)
    state["generation"] += 1
    state["source"] = copy.deepcopy(knowledge.source)
    transition_ids = [event["event_id"] for event in new_events]
    receipt = _receipt(request_id, "ingest", "accepted", state["generation"],
                       {"as_of": knowledge.as_of,
                        "transition_event_ids": transition_ids})
    _save_receipt(state, request_id, "ingest", command, receipt)
    store.commit()
    return copy.deepcopy(receipt)


def _dispatch_rejection(request_id, generation, code, rejected, conflicting,
                        decisions=None):
    result = {"code": code, "rejected_cut_ids": sorted(set(rejected)),
              "conflicting_cut_ids": sorted(set(conflicting))}
    if decisions is not None:
        result["decisions"] = decisions
    return _receipt(request_id, "dispatch", "rejected", generation, result)


def _do_dispatch(store, command, request_id, payload, knowledge):
    state = store.state
    cuts = payload["cuts"]
    proposed_ids = sorted(cut["cut_id"] for cut in cuts)
    if payload["expected_generation"] != state["generation"]:
        receipt = _dispatch_rejection(request_id, state["generation"],
                                      "stale_generation", proposed_ids, [])
        _save_receipt(state, request_id, "dispatch", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    if len(state["deliveries"]) + len(cuts) > MAX_DELIVERED_CUTS:
        receipt = _dispatch_rejection(request_id, state["generation"], "cut_limit",
                                      proposed_ids, [])
        _save_receipt(state, request_id, "dispatch", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    used_ids = {delivery["cut_id"] for delivery in state["deliveries"]}
    id_collisions = sorted(used_ids.intersection(proposed_ids))
    if id_collisions:
        receipt = _dispatch_rejection(request_id, state["generation"], "cut_id_used",
                                      id_collisions, [])
        _save_receipt(state, request_id, "dispatch", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    overlap_rejected = set()
    conflicting = set()
    for index, cut in enumerate(cuts):
        for other in cuts[index + 1:]:
            if overlaps(cut, other):
                overlap_rejected.add(cut["cut_id"])
                overlap_rejected.add(other["cut_id"])
        for delivery in state["deliveries"]:
            if overlaps(cut, delivery):
                overlap_rejected.add(cut["cut_id"])
                conflicting.add(delivery["cut_id"])
    if overlap_rejected:
        receipt = _dispatch_rejection(request_id, state["generation"], "cloth_overlap",
                                      overlap_rejected, conflicting)
        _save_receipt(state, request_id, "dispatch", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    decisions = {cut["cut_id"]: knowledge.decide(cut, knowledge.as_of)
                 for cut in cuts}
    blocked = sorted(cut_id for cut_id in proposed_ids
                     if not decisions[cut_id]["allowed"])
    if blocked:
        ordered_decisions = [{"cut_id": cut_id, "decision": decisions[cut_id]}
                             for cut_id in proposed_ids]
        receipt = _dispatch_rejection(request_id, state["generation"], "trace_blocked",
                                      blocked, [], ordered_decisions)
        _save_receipt(state, request_id, "dispatch", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    deliveries = []
    for cut in cuts:
        decision = decisions[cut["cut_id"]]
        delivery = {
            "cut_id": cut["cut_id"],
            "end_start": cut["end_start"], "end_stop": cut["end_stop"],
            "pick_start": cut["pick_start"], "pick_stop": cut["pick_stop"],
            "request_id": request_id,
            "admitted_generation": state["generation"],
            "admitted_at": knowledge.as_of,
            "admitted_decision": copy.deepcopy(decision),
            "current_decision": copy.deepcopy(decision),
            "state": "normal",
        }
        deliveries.append(delivery)
    state["deliveries"].extend(deliveries)
    state["deliveries"].sort(key=lambda item: item["cut_id"])
    credentials = []
    for cut_id in proposed_ids:
        delivery = next(item for item in state["deliveries"]
                        if item["cut_id"] == cut_id)
        credentials.append({key: copy.deepcopy(delivery[key]) for key in (
            "cut_id", "end_start", "end_stop", "pick_start", "pick_stop",
            "request_id", "admitted_generation", "admitted_at", "admitted_decision")})
    receipt = _receipt(request_id, "dispatch", "accepted", state["generation"],
                       {"deliveries": credentials})
    _save_receipt(state, request_id, "dispatch", command, receipt)
    store.commit()
    return copy.deepcopy(receipt)


def _do_ack(store, command, request_id, payload):
    state = store.state
    event_ids = payload["event_ids"]
    existing = {event["event_id"]: event for event in state["events"]}
    unknown = sorted(set(event_ids) - set(existing))
    if unknown:
        receipt = _receipt(request_id, "ack", "rejected", state["generation"],
                           {"code": "unknown_event", "unknown_event_ids": unknown})
        _save_receipt(state, request_id, "ack", command, receipt)
        store.commit()
        return copy.deepcopy(receipt)
    for event_id in event_ids:
        existing[event_id]["acknowledged"] = True
    receipt = _receipt(request_id, "ack", "accepted", state["generation"],
                       {"acknowledged_event_ids": sorted(event_ids)})
    _save_receipt(state, request_id, "ack", command, receipt)
    store.commit()
    return copy.deepcopy(receipt)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def apply(store_path, command):
    """Apply one strict command against the local ledger; return a plain dict.

    Input errors and same-request-id/different-command conflicts raise
    ``ValueError`` and never consume the request id.  Business conclusions are
    returned as permanent receipts (including rejections) and committed
    atomically together with their effects.
    """
    op, request_id, payload = validate_command(command)
    with _locked_store(store_path, write=True) as store:
        # Geometry-bound input checks still precede replay lookup, so an
        # out-of-cloth cut is an input error even on a previously seen id.
        current_knowledge = None
        if op == "dispatch" and store.state is not None:
            current_knowledge = Knowledge(store.state["source"])
            _check_cut_bounds(payload["cuts"], current_knowledge)
        if store.state is not None:
            stored = _stored_command(store.state, request_id)
            if stored is not None:
                if not json_equal(stored, command):
                    raise ValueError(
                        f"request_id {request_id} was already used with a different command")
                return copy.deepcopy(store.state["requests"][request_id]["receipt"])
        if op == "init":
            return _do_init(store, command, request_id, payload)
        if store.state is None:
            raise ValueError(f"store is not initialized: {store.dir}")
        if op == "ingest":
            return _do_ingest(store, command, request_id, payload)
        if op == "dispatch":
            if current_knowledge is None:
                current_knowledge = Knowledge(store.state["source"])
            return _do_dispatch(store, command, request_id, payload,
                                current_knowledge)
        return _do_ack(store, command, request_id, payload)


def read_store(store_path):
    """Return a full plain-dict ledger view without creating or modifying it."""
    with _locked_store(store_path, write=False) as store:
        state = store.state
        knowledge = Knowledge(state["source"])
        deliveries = []
        for delivery in sorted(state["deliveries"], key=lambda item: item["cut_id"]):
            view_item = {key: copy.deepcopy(delivery[key]) for key in (
                "cut_id", "end_start", "end_stop", "pick_start", "pick_stop",
                "request_id", "admitted_generation", "admitted_at",
                "admitted_decision", "current_decision", "state")}
            deliveries.append(view_item)
        events = [copy.deepcopy(event) for event in
                  sorted(state["events"], key=lambda item: item["event_id"])]
        pending = [event["event_id"] for event in events if not event["acknowledged"]]
        normal = sum(1 for delivery in deliveries if delivery["state"] == "normal")
        recall = sum(1 for delivery in deliveries if delivery["state"] == "recall")
        return {
            "schema_version": SCHEMA_VERSION,
            "generation": state["generation"],
            "as_of": state["source"]["as_of"],
            "production": knowledge.production_view(),
            "deliveries": deliveries,
            "events": events,
            "pending_event_ids": pending,
            "summary": {"delivered_cuts": len(deliveries), "normal": normal,
                        "recall": recall, "events": len(events),
                        "pending_events": len(pending)},
        }
