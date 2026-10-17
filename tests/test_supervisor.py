import threading

import pytest

from app.backend.agents.events import ListSink
import time

from app.backend.agents.supervisor import (
    ApprovalGate,
    Limits,
    RunCancelled,
    Step,
    StepFailed,
    Supervisor,
)


class Clock:
    """A clock the tests move by hand, so timing is exact and instant."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make(limits=None):
    sink, cancel, clock = ListSink(), threading.Event(), Clock()
    supervisor = Supervisor(sink, cancel, limits or Limits(), clock=clock)
    return supervisor, sink, cancel, clock


def types_of(sink):
    return [event.type for event in sink.events]


def step(name, fn=None, **kwargs):
    return Step(name, name.title(), fn or (lambda ctx: None), **kwargs)


# ------------------------------------------------------------- happy path

def test_runs_every_step_in_order_and_logs_it():
    supervisor, sink, _, _ = make()
    order = []

    result = supervisor.run(
        [step("a", lambda ctx: order.append("a")), step("b", lambda ctx: order.append("b"))],
        title="Demo",
    )

    assert order == ["a", "b"]
    assert result.status == "completed"
    assert result.summary == "2 of 2 steps finished"
    assert types_of(sink) == [
        "run_started",
        "step_started", "step_finished",
        "step_started", "step_finished",
        "run_finished",
    ]
    assert sink.events[0].title == "Demo"
    assert sink.events[0].detail == "2 steps planned"
    assert sink.events[1].title == "Step 1/2: A"


def test_a_step_result_is_recorded_as_detail():
    supervisor, sink, _, _ = make()

    supervisor.run([step("a", lambda ctx: "all good")])

    finished = [e for e in sink.events if e.type == "step_finished"][0]

    assert finished.detail == "all good"


def test_roles_are_recorded():
    supervisor, sink, _, _ = make()

    supervisor.run([step("read", role="reader")])

    started = [e for e in sink.events if e.type == "step_started"][0]

    assert started.role == "reader"
    assert sink.events[0].role == "supervisor"


def test_steps_can_log_notes_and_share_state():
    supervisor, sink, _, _ = make()
    seen = {}

    def first(ctx):
        ctx.info("hello", "details")
        ctx.warn("careful")
        ctx.state["answer"] = 42

    def second(ctx):
        seen["answer"] = ctx.state["answer"]

    supervisor.run([step("first", first, role="reader"), step("second", second)])

    notes = [(e.type, e.title, e.detail, e.role) for e in sink.events if e.type in ("info", "warning")]

    assert notes == [("info", "hello", "details", "reader"), ("warning", "careful", None, "reader")]
    assert seen == {"answer": 42}


def test_timing_is_measured():
    supervisor, sink, _, clock = make()

    supervisor.run([step("slow", lambda ctx: clock.advance(2.5)), step("quick")])

    finished = [e for e in sink.events if e.type == "step_finished"]

    assert finished[0].duration_ms == 2500
    assert finished[0].elapsed_ms == 2500
    assert finished[1].duration_ms == 0
    assert sink.events[-1].title == "Run completed after 2.5s"


def test_an_empty_plan_completes():
    supervisor, sink, _, _ = make()

    result = supervisor.run([])

    assert result.status == "completed"
    assert types_of(sink) == ["run_started", "run_finished"]


# --------------------------------------------------------------- failures

def test_a_failing_step_stops_the_run():
    supervisor, sink, _, _ = make()
    ran = []

    def boom(ctx):
        raise StepFailed("the model is down")

    result = supervisor.run([step("one", boom), step("two", lambda ctx: ran.append("two"))])

    assert ran == []
    assert result.status == "failed"
    assert result.error == "One: the model is down"
    assert types_of(sink) == ["run_started", "step_started", "step_failed", "run_finished"]
    assert sink.events[-1].detail == "One: the model is down"


def test_an_unexpected_exception_is_contained_and_reported():
    supervisor, sink, _, _ = make()

    def crash(ctx):
        raise KeyError("oops")

    result = supervisor.run([step("one", crash)])

    assert result.status == "failed"
    assert "KeyError" in result.error
    assert types_of(sink)[-1] == "run_finished"


def test_an_optional_step_that_fails_is_skipped():
    supervisor, sink, _, _ = make()
    ran = []

    def boom(ctx):
        raise StepFailed("no models folder")

    result = supervisor.run(
        [step("maybe", boom, optional=True), step("next", lambda ctx: ran.append("next"))]
    )

    assert ran == ["next"]
    assert result.status == "completed"
    assert result.summary == "1 of 2 steps finished, 1 skipped"
    skipped = [e for e in sink.events if e.type == "step_skipped"]
    assert skipped[0].title == "Maybe: no models folder (carrying on)"


# ------------------------------------------------------------ cancellation

def test_cancelled_before_it_starts_runs_nothing():
    supervisor, sink, cancel, _ = make()
    ran = []
    cancel.set()

    result = supervisor.run([step("a", lambda ctx: ran.append("a"))])

    assert ran == []
    assert result.status == "cancelled"
    assert types_of(sink) == ["run_started", "run_finished"]


def test_cancel_between_steps_stops_before_the_next_one():
    supervisor, sink, cancel, _ = make()
    ran = []

    result = supervisor.run(
        [step("a", lambda ctx: cancel.set()), step("b", lambda ctx: ran.append("b"))]
    )

    assert ran == []
    assert result.status == "cancelled"
    assert types_of(sink) == ["run_started", "step_started", "step_finished", "run_finished"]


def test_cancel_inside_a_step_is_noticed():
    supervisor, sink, cancel, _ = make()
    ran = []

    def long_step(ctx):
        for _ in range(100):
            ctx.check_cancelled()
            cancel.set()          # the user clicks Cancel part way through
            ran.append("tick")

    result = supervisor.run([step("long", long_step), step("after", lambda ctx: ran.append("after"))])

    assert ran == ["tick"]         # stopped at the next check
    assert result.status == "cancelled"
    assert "step_failed" in types_of(sink)
    assert [e.title for e in sink.events if e.type == "step_failed"] == ["Long: cancelled"]


def test_a_step_may_raise_run_cancelled_itself():
    supervisor, sink, _, _ = make()

    def stop(ctx):
        raise RunCancelled

    assert supervisor.run([step("x", stop)]).status == "cancelled"


# ----------------------------------------------------------------- limits

def test_time_limit_stops_the_run_between_steps():
    supervisor, sink, _, clock = make(Limits(max_seconds=10))
    ran = []

    result = supervisor.run(
        [step("slow", lambda ctx: clock.advance(11)), step("late", lambda ctx: ran.append("late"))]
    )

    assert ran == []
    assert result.status == "failed"
    assert "time limit of 10 seconds" in result.error


def test_steps_can_see_how_much_time_is_left():
    supervisor, _, _, clock = make(Limits(max_seconds=60))
    left = []

    supervisor.run([step("a", lambda ctx: (clock.advance(15), left.append(ctx.seconds_left())))])

    assert left == [45.0]


def test_too_many_steps_is_refused_before_anything_runs():
    supervisor, sink, _, _ = make(Limits(max_steps=2))
    ran = []

    result = supervisor.run([step(str(i), lambda ctx: ran.append(1)) for i in range(3)])

    assert ran == []
    assert result.status == "failed"
    assert "3 steps" in result.error and "limit of 2" in result.error
    assert types_of(sink) == ["run_started", "run_finished"]


# ----------------------------------------------------------- invariants

@pytest.mark.parametrize("outcome", ["ok", "fail", "crash", "cancel"])
def test_exactly_one_run_finished_event_and_it_is_last(outcome):
    supervisor, sink, cancel, _ = make()

    def work(ctx):
        if outcome == "fail":
            raise StepFailed("no")
        if outcome == "crash":
            raise ValueError("bad")
        if outcome == "cancel":
            raise RunCancelled

    supervisor.run([step("a", work), step("b")])

    assert types_of(sink).count("run_finished") == 1
    assert types_of(sink)[-1] == "run_finished"
    assert types_of(sink)[0] == "run_started"


def test_elapsed_time_never_goes_backwards():
    supervisor, sink, _, clock = make()

    supervisor.run([step("a", lambda ctx: clock.advance(1)), step("b", lambda ctx: clock.advance(2))])

    times = [e.elapsed_ms for e in sink.events]

    assert times == sorted(times)


# --------------------------------------------------------------- approvals

def make_with_gate(limits=None, clock=None, hook=None):
    sink, cancel = ListSink(), threading.Event()
    gate = ApprovalGate()
    calls = []
    supervisor = Supervisor(
        sink, cancel, limits or Limits(), clock=clock or time.monotonic, gate=gate,
        status_hook=hook or calls.append,
    )

    return supervisor, sink, cancel, gate, calls


def decide_soon(gate, decision, delay=0.05, before=None):
    def later():
        time.sleep(delay)

        if before:
            before()

        # wait until the run is actually waiting, as a real click would be
        for _ in range(200):
            if gate.decide(decision):
                return

            time.sleep(0.01)

    thread = threading.Thread(target=later, daemon=True)
    thread.start()
    return thread


def test_approve_resumes_the_run_and_reports_the_waiting_status():
    supervisor, sink, _, gate, statuses = make_with_gate()
    seen = []
    decide_soon(gate, "apply")

    result = supervisor.run([step("ask", lambda ctx: seen.append(ctx.wait_for_approval())), step("after")])

    assert result.status == "completed"
    assert seen == ["apply"]
    assert statuses == ["awaiting_approval", "running"]
    assert not gate.waiting


def test_reject_is_returned_to_the_step():
    supervisor, _, _, gate, _ = make_with_gate()
    seen = []
    decide_soon(gate, "reject")

    supervisor.run([step("ask", lambda ctx: seen.append(ctx.wait_for_approval()))])

    assert seen == ["reject"]


def test_cancelling_while_waiting_stops_the_run_and_resumes_the_status():
    supervisor, sink, cancel, gate, statuses = make_with_gate()
    ran = []
    threading.Timer(0.1, cancel.set).start()

    result = supervisor.run(
        [step("ask", lambda ctx: ctx.wait_for_approval()), step("after", lambda ctx: ran.append(1))]
    )

    assert result.status == "cancelled" and ran == []
    assert statuses == ["awaiting_approval", "running"]


def test_nobody_answering_ends_the_run_without_a_decision():
    supervisor, sink, _, gate, statuses = make_with_gate()

    result = supervisor.run([step("ask", lambda ctx: ctx.wait_for_approval(timeout_seconds=0.3))])

    assert result.status == "failed"
    assert "no decision was made within 0 minutes" in result.error and "nothing was changed" in result.error
    assert statuses == ["awaiting_approval", "running"]


def test_a_run_without_a_gate_cannot_ask():
    supervisor, _, _, _ = make()

    result = supervisor.run([step("ask", lambda ctx: ctx.wait_for_approval())])

    assert result.status == "failed" and "cannot ask for approval" in result.error


def test_time_spent_waiting_is_not_counted_against_the_limit():
    clock = Clock()
    supervisor, _, _, gate, _ = make_with_gate(Limits(max_seconds=100), clock=clock)
    left = []
    decide_soon(gate, "apply", before=lambda: clock.advance(500))      # the user takes "500 seconds"

    result = supervisor.run(
        [
            step("ask", lambda ctx: ctx.wait_for_approval()),
            step("after", lambda ctx: left.append(ctx.seconds_left())),
        ]
    )

    assert result.status == "completed"                                # not "time limit reached"
    assert left == [100.0]
    assert sink_elapsed(supervisor) >= 500_000                          # the timeline still shows real time


def sink_elapsed(supervisor):
    return supervisor.elapsed_ms()


def test_the_gate_only_accepts_one_decision_while_open():
    gate = ApprovalGate()

    assert gate.decide("apply") is False                # nothing is waiting yet
    gate.open()
    assert gate.waiting is True
    assert gate.decide("apply") is True
    assert gate.decide("reject") is False               # too late
    assert gate.decision == "apply"
    gate.close()
    assert gate.decide("apply") is False


def test_the_gate_can_be_used_again_for_a_second_question():
    supervisor, _, _, gate, statuses = make_with_gate()
    answers = []
    decide_soon(gate, "apply")

    def first(ctx):
        answers.append(ctx.wait_for_approval())
        decide_soon(gate, "reject")

    supervisor.run([step("one", first), step("two", lambda ctx: answers.append(ctx.wait_for_approval()))])

    assert answers == ["apply", "reject"]


def test_a_proposal_is_logged_with_its_diff():
    supervisor, sink, _, _ = make()

    supervisor.run([step("show", lambda ctx: ctx.proposal("Proposed changes: 1 file", "--- a/x\n+++ b/x"), role="editor")])

    proposal = [e for e in sink.events if e.type == "proposal"][0]

    assert proposal.title == "Proposed changes: 1 file"
    assert proposal.detail == "--- a/x\n+++ b/x" and proposal.role == "editor"


def test_a_failing_status_hook_does_not_break_the_run():
    def broken(status):
        raise RuntimeError("database is down")

    supervisor, _, _, gate, _ = make_with_gate(hook=broken)
    decide_soon(gate, "apply")

    result = supervisor.run([step("ask", lambda ctx: ctx.wait_for_approval())])

    assert result.status == "completed"

