"""Real multi-process concurrency and crash-injection tests for loom-dispatch.

Every concurrency scenario here spawns independent OS processes through the
``loom-dispatch`` CLI (never threads or in-process exception simulation):

* identical concurrent commands with one request id produce one receipt;
* the same request id with different payloads leaves one committed effect and
  one exit-2 conflict, never two effects;
* different request ids racing for one cloth area have at most one winner;
* first-initialisation races produce exactly one initialised ledger;
* ingest/dispatch races serialise onto one committed generation;
* ``LOOM_DISPATCH_FAULT=before_commit`` kills the process before the atomic
  replace, while ``after_commit`` kills it after the replace + fsync but before
  the JSON receipt is returned: reopening must show a clean pre/post state and
  retrying must return the first committed receipt.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from loom_draft import dispatch

SRC = Path(__file__).resolve().parent.parent / "src"
REPO = SRC.parent


def env(**extra):
    base = dict(os.environ)
    # Subprocesses run without -I so PYTHONPATH can locate the in-tree package;
    # only the standard library is imported regardless.
    base["PYTHONPATH"] = str(SRC)
    base.update(extra)
    return base


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


class ProcessBase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="loom-proc-"))
        self.store = self.work / "store"
        self.cmds = self.work / "commands"
        self.addCleanup(lambda: shutil.rmtree(self.work, ignore_errors=True))

    def cli(self, command_path, store=None, fault=None, view=False):
        args = [sys.executable, "-m", "loom_draft.dispatch_cli",
                "--store", str(store or self.store)]
        if view:
            args.append("--view")
        else:
            args += ["--source", str(command_path)]
        return subprocess.run(args, capture_output=True, text=True,
                              env=env(**({dispatch.FAULT_ENV: fault} if fault else {})),
                              timeout=120)

    def run_parallel(self, runners):
        """Start ``runners`` (callables returning Popen) together, then wait."""
        processes = [runner() for runner in runners]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate()
            outcomes.append((process.returncode, stdout, stderr))
            process.stdout.close()
            process.stderr.close()
        return outcomes

    def popen(self, command_path, fault=None, store=None):
        args = [sys.executable, "-m", "loom_draft.dispatch_cli",
                "--store", str(store or self.store), "--source", str(command_path)]
        return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True,
                                env=env(**({dispatch.FAULT_ENV: fault}
                                           if fault else {})))

    # -- canonical cloth, identical to the unit-test acceptance cloth -------
    def base_source(self, as_of=3):
        batch = {"common": {"shafts": 2,
                 "tie_up": [{"id": "T1", "shafts": [1]},
                            {"id": "T2", "shafts": [2]}],
                 "warp": [{"shaft": 1, "yarn": "w"},
                          {"shaft": 2, "yarn": "w"}],
                 "repeat_x": 1, "warp_spacing_mm": 1, "pick_spacing_mm": 1,
                 "takeup_permille": 100, "warp_waste_mm": 1, "float_limit": 3},
                 "jobs": [{"id": "A", "value": 1, "repeat_y": 1, "phases": [0],
                           "picks": [
                               {"treadles": ["T1"], "yarn": "f"},
                               {"treadles": ["T2"], "yarn": "f"},
                               {"treadles": ["T1"], "yarn": "f"},
                               {"treadles": ["T2"], "yarn": "f"}]}],
                 "bridges": [],
                 "stock": [{"yarn": "w", "length_mm": 10**6},
                           {"yarn": "f", "length_mm": 10**6}],
                 "max_length_mm": 10**6}
        return {"batch": batch,
                "lots": [{"id": "W", "yarn": "w", "capacity_mm": 12},
                         {"id": "F", "yarn": "f", "capacity_mm": 12}],
                "allocations": [
                    {"id": "aw0", "revision": 1, "known_at": 1, "payload": {
                        "orientation": "warp", "base_end": 0, "repeat_start": 0,
                        "repeat_stop": 1, "pick_start": 0, "pick_stop": 4,
                        "lot_id": "W"}},
                    {"id": "aw1", "revision": 1, "known_at": 1, "payload": {
                        "orientation": "warp", "base_end": 1, "repeat_start": 0,
                        "repeat_stop": 1, "pick_start": 0, "pick_stop": 4,
                        "lot_id": "W"}},
                    {"id": "wf", "revision": 1, "known_at": 1, "payload": {
                        "orientation": "weft", "pick_start": 0, "pick_stop": 4,
                        "lot_id": "F"}}],
                "certificates": [
                    {"id": "cw", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "W", "pick_start": 0, "pick_stop": 4,
                                 "verdict": "pass"}},
                    {"id": "cf", "revision": 1, "known_at": 1,
                     "payload": {"lot_id": "F", "pick_start": 0, "pick_stop": 4,
                                 "verdict": "pass"}}],
                "shipments": [], "as_of": as_of}

    def init_command(self, request_id="init-1", as_of=3):
        return {"op": "init", "request_id": request_id,
                "source": self.base_source(as_of)}

    RECTS = {
        "S": (0, 1, 0, 2),
        "R": (1, 2, 0, 2),
        "U": (0, 2, 2, 4),
    }

    def dispatch_command(self, request_id, cut_id="S", expected=1, rect=None):
        if rect is None:
            rect = self.RECTS[cut_id]
        end_start, end_stop, pick_start, pick_stop = rect
        return {"op": "dispatch", "request_id": request_id,
                "expected_generation": expected,
                "cuts": [{"cut_id": cut_id, "end_start": end_start,
                          "end_stop": end_stop, "pick_start": pick_start,
                          "pick_stop": pick_stop}]}

    def ingest_command(self, request_id, expected, as_of, fail_range=None,
                       fail_known=None):
        src = self.base_source(as_of=3)
        if fail_range:
            src["certificates"].append({
                "id": "ff", "revision": 1, "known_at": fail_known or as_of - 1,
                "payload": {"lot_id": "F", "pick_start": fail_range[0],
                            "pick_stop": fail_range[1], "verdict": "fail"}})
        src["as_of"] = as_of
        return {"op": "ingest", "request_id": request_id,
                "expected_generation": expected, "source": src}

    def assert_clean_view(self):
        view = dispatch.read_store(self.store)
        self.assertNotIn("tmp", "".join(os.listdir(self.store)))
        return view


class SameRequestConcurrencyTests(ProcessBase):
    def test_identical_concurrent_commands_yield_one_receipt_and_effect(self):
        write_json(self.cmds / "init.json", self.init_command())
        init = self.cli(self.cmds / "init.json")
        self.assertEqual(init.returncode, 0, init.stderr)
        write_json(self.cmds / "d.json", self.dispatch_command("D1"))
        outcomes = self.run_parallel([
            lambda: self.popen(self.cmds / "d.json"),
            lambda: self.popen(self.cmds / "d.json"),
            lambda: self.popen(self.cmds / "d.json"),
            lambda: self.popen(self.cmds / "d.json")])
        self.assertEqual([code for code, _, _ in outcomes], [0, 0, 0, 0])
        receipts = [json.loads(stdout) for _, stdout, _ in outcomes]
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(receipts[0]["status"], "accepted")
        view = self.assert_clean_view()
        self.assertEqual(len(view["deliveries"]), 1)
        self.assertEqual(view["deliveries"][0]["request_id"], "D1")
        self.assertEqual(len(view["summary"]) and view["summary"]["delivered_cuts"], 1)

    def test_same_id_different_payload_one_effect_one_conflict(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "a.json", self.dispatch_command("DUP", "S"))
        write_json(self.cmds / "b.json", self.dispatch_command("DUP", "R"))
        outcomes = self.run_parallel([
            lambda: self.popen(self.cmds / "a.json"),
            lambda: self.popen(self.cmds / "b.json"),
            lambda: self.popen(self.cmds / "a.json"),
            lambda: self.popen(self.cmds / "b.json")])
        # Serialised under the flock: whichever payload commits first wins.
        # Its duplicates replay with exit 0; every conflicting payload exits 2.
        self.assertIn(sorted(code for code, _, _ in outcomes),
                      [[0, 0, 2, 2], [0, 2, 2, 2]])
        zero_receipts = [json.loads(stdout) for code, stdout, _ in outcomes
                         if code == 0]
        self.assertTrue(all(r == zero_receipts[0] for r in zero_receipts))
        self.assertEqual(zero_receipts[0]["status"], "accepted")
        joined_stderr = " ".join(err for _, _, err in outcomes)
        self.assertIn("different command", joined_stderr)
        view = self.assert_clean_view()
        self.assertEqual(len(view["deliveries"]), 1)
        winner = view["deliveries"][0]
        self.assertEqual(winner["request_id"], "DUP")
        self.assertIn(winner["cut_id"], ("S", "R"))
        # the winning command now replays identically from every process.
        winning_path = self.cmds / ("a.json" if winner["cut_id"] == "S" else "b.json")
        again = self.cli(winning_path)
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout)["status"], "accepted")
        self.assertEqual(len(dispatch.read_store(self.store)["deliveries"]), 1)


class AreaRaceTests(ProcessBase):
    def test_different_request_ids_racing_one_area_have_one_winner(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        # distinct request and cut ids, all claiming the identical S rectangle.
        rect = self.RECTS["S"]
        for index in range(6):
            write_json(self.cmds / f"r{index}.json",
                       self.dispatch_command(f"race-{index}",
                                             cut_id=f"cut-{index}", rect=rect))
        outcomes = self.run_parallel(
            [lambda i=i: self.popen(self.cmds / f"r{i}.json") for i in range(6)])
        statuses = [json.loads(stdout) for _, stdout, _ in outcomes]
        accepted = [r for r in statuses if r["status"] == "accepted"]
        rejected = [r for r in statuses if r["status"] == "rejected"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 5)
        self.assertTrue(all(r["result"]["code"] == "cloth_overlap" for r in rejected))
        winner_id = accepted[0]["result"]["deliveries"][0]["cut_id"]
        for index, r in enumerate(rejected):
            # receipts may arrive in any order; each names its own cut once.
            self.assertEqual(len(r["result"]["rejected_cut_ids"]), 1)
            self.assertTrue(r["result"]["rejected_cut_ids"][0].startswith("cut-"))
            self.assertEqual(r["result"]["conflicting_cut_ids"], [winner_id])
        rejected_cuts = {r["result"]["rejected_cut_ids"][0] for r in rejected}
        self.assertEqual(rejected_cuts,
                         {f"cut-{i}" for i in range(6)} - {winner_id})
        view = self.assert_clean_view()
        self.assertEqual(len(view["deliveries"]), 1)
        self.assertEqual(view["deliveries"][0]["cut_id"], winner_id)


class InitRaceTests(ProcessBase):
    def test_concurrent_initialisation_initialises_exactly_once(self):
        # Five distinct init requests race: exactly one initialises, the rest
        # get a permanent already_initialized business rejection.
        for index in range(5):
            write_json(self.cmds / f"init{index}.json",
                       self.init_command(f"init-race-{index}"))
        outcomes = self.run_parallel(
            [lambda i=i: self.popen(self.cmds / f"init{i}.json") for i in range(5)])
        self.assertTrue(all(code == 0 for code, _, _ in outcomes))
        receipts = [json.loads(stdout) for _, stdout, _ in outcomes]
        accepted = [r for r in receipts if r["status"] == "accepted"]
        rejected = [r for r in receipts if r["status"] == "rejected"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 4)
        self.assertTrue(all(r["result"]["code"] == "already_initialized"
                            for r in rejected))
        self.assertTrue(all(r["generation"] == 1 for r in receipts))
        view = self.assert_clean_view()
        self.assertEqual(view["generation"], 1)
        # a losing request still replays its stored rejection.
        losing_path = next(self.cmds / f"init{i}.json" for i in range(5)
                           if f"init-race-{i}" != accepted[0]["request_id"])
        again = self.cli(losing_path)
        self.assertEqual(json.loads(again.stdout)["result"]["code"],
                         "already_initialized")

    def test_identical_init_request_replays_one_accepted_receipt(self):
        # Same request id raced concurrently: one effect, identical receipts.
        write_json(self.cmds / "init.json", self.init_command("init-same"))
        outcomes = self.run_parallel(
            [lambda: self.popen(self.cmds / "init.json") for _ in range(5)])
        self.assertTrue(all(code == 0 for code, _, _ in outcomes))
        receipts = [json.loads(stdout) for _, stdout, _ in outcomes]
        self.assertTrue(all(r == receipts[0] for r in receipts))
        self.assertEqual(receipts[0]["status"], "accepted")
        self.assertEqual(self.assert_clean_view()["generation"], 1)


class IngestDispatchRaceTests(ProcessBase):
    def test_ingest_and_dispatch_serialise_to_one_generation(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "dispatch.json",
                   self.dispatch_command("D1", expected=1))
        write_json(self.cmds / "ingest.json",
                   self.ingest_command("ING1", expected=1, as_of=6,
                                       fail_range=(0, 1), fail_known=5))
        outcomes = self.run_parallel([
            lambda: self.popen(self.cmds / "dispatch.json"),
            lambda: self.popen(self.cmds / "ingest.json"),
            lambda: self.popen(self.cmds / "dispatch.json"),
            lambda: self.popen(self.cmds / "ingest.json")])
        receipts = [json.loads(stdout) for _, stdout, _ in outcomes]
        view = dispatch.read_store(self.store)
        # the ingest always commits exactly once: generation is 2.
        self.assertEqual(view["generation"], 2)
        ingests = [r for r in receipts if r["op"] == "ingest"]
        dispatches = [r for r in receipts if r["op"] == "dispatch"]
        accepted_ingest = next(r for r in ingests if r["status"] == "accepted")
        self.assertEqual(accepted_ingest["generation"], 2)
        # the losing ingest is a stored stale rejection and replays as such.
        stale = [r for r in ingests if r["status"] == "rejected"]
        self.assertTrue(all(r["result"]["code"] == "stale_generation"
                            for r in stale))
        # every dispatch receipt is the same first receipt. Two serial orders
        # are both legal:
        #  * dispatch commits first at generation 1 (one delivery, healthy
        #    frozen credential), then ingest recalls it;
        #  * ingest commits first, so the generation-1 dispatch is stored as a
        #    stale_generation rejection and nothing is delivered.
        self.assertTrue(all(r == dispatches[0] for r in dispatches))
        first_dispatch = dispatches[0]
        if first_dispatch["status"] == "accepted":
            self.assertEqual(first_dispatch["generation"], 1)
            self.assertEqual(len(view["deliveries"]), 1)
            credential = view["deliveries"][0]
            self.assertEqual(credential["admitted_generation"], 1)
            self.assertTrue(credential["admitted_decision"]["allowed"])
            # post-ingest the delivered cut is now recalled, but its frozen
            # admission credential is untouched.
            self.assertEqual(credential["state"], "recall")
            self.assertEqual(view["pending_event_ids"], [1])
        else:
            self.assertEqual(first_dispatch["result"]["code"], "stale_generation")
            self.assertEqual(view["deliveries"], [])
        # retrying both commands returns their stored receipts unchanged.
        replay_d = self.cli(self.cmds / "dispatch.json")
        replay_i = self.cli(self.cmds / "ingest.json")
        self.assertEqual(json.loads(replay_d.stdout), first_dispatch)
        self.assertIn(json.loads(replay_i.stdout)["status"], ("accepted", "rejected"))

    def test_view_always_reads_a_complete_committed_state(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D1"))
        writers = []
        for i in range(4):
            write_json(self.cmds / f"d{i}.json", self.dispatch_command(f"D{i}"))
            writers.append(self.popen(self.cmds / f"d{i}.json"))
        views = []
        for _ in range(3):
            result = self.cli(None, view=True)
            if result.returncode == 0:
                views.append(json.loads(result.stdout))
        for process in writers:
            process.communicate()
            process.stdout.close()
            process.stderr.close()
        # every observed view is internally consistent.
        for view in views:
            self.assertEqual(view["summary"]["delivered_cuts"],
                             len(view["deliveries"]))
            self.assertEqual(view["summary"]["events"], len(view["events"]))
            self.assertEqual(view["summary"]["pending_events"],
                             len(view["pending_event_ids"]))
        final = dispatch.read_store(self.store)
        self.assertEqual(len(final["deliveries"]), 1)


class CrashInjectionTests(ProcessBase):
    def _await_death(self, process, expected_code):
        process.wait(timeout=120)
        self.assertEqual(process.returncode, expected_code)

    def test_kill_before_commit_leaves_previous_state_and_request_reusable(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D-kill"))
        killed = self.popen(self.cmds / "d.json", fault="before_commit")
        self._await_death(killed, 3)
        # no half-committed delivery, no leftover temp files (the .lock stays).
        view = self.assert_clean_view()
        self.assertEqual(view["deliveries"], [])
        leftovers = [n for n in os.listdir(self.store)
                     if n.startswith(dispatch.TMP_PREFIX)]
        self.assertEqual(leftovers, [])
        # the request was never committed: it now executes normally, once.
        ok = self.cli(self.cmds / "d.json")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        receipt = json.loads(ok.stdout)
        self.assertEqual(receipt["status"], "accepted")
        again = self.cli(self.cmds / "d.json")
        self.assertEqual(json.loads(again.stdout), receipt)
        self.assertEqual(len(dispatch.read_store(self.store)["deliveries"]), 1)

    def test_kill_after_commit_returns_first_receipt_on_retry(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D-post"))
        killed = self.popen(self.cmds / "d.json", fault="after_commit")
        self._await_death(killed, 4)
        # the commit landed even though the caller never got the receipt.
        view = self.assert_clean_view()
        self.assertEqual(len(view["deliveries"]), 1)
        self.assertEqual(view["deliveries"][0]["request_id"], "D-post")
        retry = self.cli(self.cmds / "d.json")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        receipt = json.loads(retry.stdout)
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["result"]["deliveries"][0]["cut_id"], "S")
        # retrying again is still the identical receipt, still one delivery.
        third = self.cli(self.cmds / "d.json")
        self.assertEqual(json.loads(third.stdout), receipt)
        self.assertEqual(len(dispatch.read_store(self.store)["deliveries"]), 1)

    def test_kill_around_ingest_keeps_events_and_source_atomic(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D1"))
        self.assertEqual(self.cli(self.cmds / "d.json").returncode, 0)
        write_json(self.cmds / "ing.json",
                   self.ingest_command("ING", expected=1, as_of=6,
                                       fail_range=(0, 1), fail_known=5))
        # before-commit: generation stays 1, no events.
        killed = self.popen(self.cmds / "ing.json", fault="before_commit")
        self._await_death(killed, 3)
        view = self.assert_clean_view()
        self.assertEqual(view["generation"], 1)
        self.assertEqual(view["events"], [])
        self.assertEqual(view["as_of"], 3)
        # after-commit on the same retry: whole ingest + both recall events land.
        killed2 = self.popen(self.cmds / "ing.json", fault="after_commit")
        self._await_death(killed2, 4)
        view = self.assert_clean_view()
        self.assertEqual(view["generation"], 2)
        self.assertEqual(view["as_of"], 6)
        self.assertEqual(len(view["events"]), 1)
        self.assertEqual(view["events"][0]["kind"], "recall")
        self.assertEqual(view["pending_event_ids"], [1])
        retry = self.cli(self.cmds / "ing.json")
        receipt = json.loads(retry.stdout)
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["result"]["transition_event_ids"], [1])
        # no duplicate events from the retries.
        self.assertEqual(len(dispatch.read_store(self.store)["events"]), 1)

    def test_kill_around_ack_is_all_or_nothing(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D1"))
        self.assertEqual(self.cli(self.cmds / "d.json").returncode, 0)
        write_json(self.cmds / "ing.json",
                   self.ingest_command("ING", expected=1, as_of=6,
                                       fail_range=(0, 1), fail_known=5))
        self.assertEqual(self.cli(self.cmds / "ing.json").returncode, 0)
        ack_path = self.cmds / "ack.json"
        write_json(ack_path, {"op": "ack", "request_id": "ACK1", "event_ids": [1]})
        killed = self.popen(ack_path, fault="before_commit")
        self._await_death(killed, 3)
        self.assertEqual(dispatch.read_store(self.store)["pending_event_ids"], [1])
        killed2 = self.popen(ack_path, fault="after_commit")
        self._await_death(killed2, 4)
        view = self.assert_clean_view()
        self.assertEqual(view["pending_event_ids"], [])
        self.assertTrue(view["events"][0]["acknowledged"])
        retry = self.cli(ack_path)
        self.assertEqual(json.loads(retry.stdout)["status"], "accepted")
        self.assertEqual(dispatch.read_store(self.store)["pending_event_ids"], [])

    def test_lock_released_after_kill_lets_other_processes_continue(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "d.json", self.dispatch_command("D1"))
        killed = self.popen(self.cmds / "d.json", fault="after_commit")
        self._await_death(killed, 4)
        # no manual lock cleanup required: a fresh process proceeds normally.
        view_result = self.cli(None, view=True)
        self.assertEqual(view_result.returncode, 0, view_result.stderr)
        write_json(self.cmds / "d2.json", self.dispatch_command("D2", "R"))
        follow = self.cli(self.cmds / "d2.json")
        self.assertEqual(follow.returncode, 0, follow.stderr)
        self.assertEqual(json.loads(follow.stdout)["status"], "accepted")
        self.assertEqual(len(dispatch.read_store(self.store)["deliveries"]), 2)

    def test_reopen_keeps_ledger_replay_occupancy_and_notifications_consistent(self):
        # build a full lifecycle entirely through independent CLI processes.
        write_json(self.cmds / "init.json", self.init_command())
        init = self.cli(self.cmds / "init.json")
        init_receipt = json.loads(init.stdout)
        write_json(self.cmds / "d1.json", self.dispatch_command("D1", "S"))
        d1 = self.cli(self.cmds / "d1.json")
        d1_receipt = json.loads(d1.stdout)
        write_json(self.cmds / "ing.json",
                   self.ingest_command("ING", expected=1, as_of=6,
                                       fail_range=(0, 1), fail_known=5))
        ing = self.cli(self.cmds / "ing.json")
        ing_receipt = json.loads(ing.stdout)
        # reopen in a brand-new process via --view and the Python API.
        view_cli = json.loads(self.cli(None, view=True).stdout)
        view_py = dispatch.read_store(self.store)
        self.assertEqual(view_cli, view_py)
        self.assertEqual(view_py["generation"], 2)
        self.assertEqual(view_py["deliveries"][0]["state"], "recall")
        self.assertEqual([e["kind"] for e in view_py["events"]], ["recall"])
        self.assertEqual(view_py["pending_event_ids"], [1])
        # replay each historical request: byte-identical receipts, no effects.
        self.assertEqual(json.loads(self.cli(self.cmds / "init.json").stdout),
                         init_receipt)
        self.assertEqual(json.loads(self.cli(self.cmds / "d1.json").stdout),
                         d1_receipt)
        self.assertEqual(json.loads(self.cli(self.cmds / "ing.json").stdout),
                         ing_receipt)
        # occupancy still holds: a same-area new request is rejected.
        write_json(self.cmds / "again.json",
                   self.dispatch_command("D2", "S2",
                                         rect=self.RECTS["S"], expected=2))
        again = json.loads(self.cli(self.cmds / "again.json").stdout)
        self.assertEqual(again["result"]["code"], "cloth_overlap")
        # acknowledging in another process clears the pending notification only.
        write_json(self.cmds / "ack.json",
                   {"op": "ack", "request_id": "ACK", "event_ids": [1]})
        self.assertEqual(self.cli(self.cmds / "ack.json").returncode, 0)
        final = dispatch.read_store(self.store)
        self.assertEqual(final["pending_event_ids"], [])
        self.assertTrue(final["events"][0]["acknowledged"])
        self.assertEqual(len(final["deliveries"]), 1)


class CliContractTests(ProcessBase):
    def test_modes_are_mutually_exclusive_and_view_is_full_json(self):
        write_json(self.cmds / "init.json", self.init_command())
        result = subprocess.run(
            [sys.executable, "-m", "loom_draft.dispatch_cli",
             "--store", str(self.store), "--source",
             str(self.cmds / "init.json"), "--view"],
            capture_output=True, text=True, env=env(), timeout=60)
        self.assertEqual(result.returncode, 2)
        missing = subprocess.run(
            [sys.executable, "-m", "loom_draft.dispatch_cli",
             "--store", str(self.work / "never")],
            capture_output=True, text=True, env=env(), timeout=60)
        self.assertEqual(missing.returncode, 2)
        self.assertNotIn("Traceback", missing.stderr)
        self.assertFalse((self.work / "never").exists())

    def test_corrupt_ledger_is_a_concise_exit_two_without_rebuild(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        (self.store / dispatch.LEDGER_NAME).write_text("{broken")
        view = self.cli(None, view=True)
        self.assertEqual(view.returncode, 2)
        self.assertNotIn("Traceback", view.stderr)
        self.assertIn("corrupt", view.stderr)
        # the damaged ledger is not silently replaced by an empty one.
        self.assertEqual((self.store / dispatch.LEDGER_NAME).read_text(), "{broken")

    def test_business_rejection_exit_zero_input_error_exit_two(self):
        write_json(self.cmds / "init.json", self.init_command())
        self.assertEqual(self.cli(self.cmds / "init.json").returncode, 0)
        write_json(self.cmds / "bad.json",
                   {"op": "dispatch", "request_id": "bad",
                    "expected_generation": 1, "cuts": [
                        {"cut_id": "S", "end_start": 9, "end_stop": 10,
                         "pick_start": 0, "pick_stop": 1}]})
        bad = self.cli(self.cmds / "bad.json")
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn("Traceback", bad.stderr)
        write_json(self.cmds / "ok.json", self.dispatch_command("ok"))
        ok = self.cli(self.cmds / "ok.json")
        self.assertEqual(ok.returncode, 0)
        # oversized command file (>8MB) is rejected.
        huge = self.cmds / "huge.json"
        huge.write_text('{"op":"ack","request_id":"x","event_ids":[1]}' + " " * (8 * 10**6))
        oversized = self.cli(huge)
        self.assertEqual(oversized.returncode, 2)
        self.assertIn("8 MB", oversized.stderr)

    def test_duplicate_json_key_rejected_at_cli(self):
        path = self.cmds / "dup.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"op": "ack", "op": "ingest", "request_id": "x",'
                        ' "event_ids": [1]}')
        result = self.cli(path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate", result.stderr)


if __name__ == "__main__":
    unittest.main()
