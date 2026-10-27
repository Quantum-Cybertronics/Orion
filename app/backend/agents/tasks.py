"""The kinds of run ORION can perform.

A task is a named, fixed list of steps built from the run's parameters:

  * self-check: exercises the whole pipeline (supervisor, event log, AI engine)
  * ask about a folder: the reader looks through a folder and answers a question

Later tasks add steps that edit files and check the edits.
"""

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.backend.agents.changes import ChangeError, ChangeSet, backup_dir_for
from app.backend.agents.checks import has_high, redact_secrets, run_checks, summarize, to_json
from app.backend.agents.editor import Editor
from app.backend.agents.reader import Reader, reply_tokens_for
from app.backend.agents.supervisor import Limits, Step, StepContext, StepFailed
from app.backend.agents.workspace import Workspace, WorkspaceError
from app.backend.ai.base import AIProviderError
from app.backend.ai.runtime import get_runtime
from app.backend.config import DATA_DIR

MAX_QUESTION_CHARS = 1000


class ParamError(ValueError):
    """The run's parameters are not acceptable. The message is shown to the user."""


@dataclass(frozen=True)
class Field:
    """One input the page asks for before starting a run."""

    name: str
    label: str
    kind: str = "text"        # "text" or "textarea"
    placeholder: str = ""


def _no_params(params: Mapping) -> dict:
    return {}


def _same_title(task_title: str) -> Callable[[dict], str]:
    return lambda params: task_title


@dataclass(frozen=True)
class Task:
    kind: str
    title: str
    description: str
    build: Callable[[dict], list[Step]]
    limits: Limits = field(default_factory=Limits)
    fields: tuple[Field, ...] = ()
    validate: Callable[[Mapping], dict] = _no_params
    run_title: Callable[[dict], str] | None = None

    def title_for(self, params: dict) -> str:
        return self.run_title(params) if self.run_title else self.title


# -- self-check ----------------------------------------------------------------

def _check_engine(ctx: StepContext) -> str:
    status = get_runtime().status()

    if status["provider"] == "echo":
        ctx.warn("Echo mode: no AI model is loaded", status.get("detail"))
        return "Echo mode (replies are not from a real model)."

    if status["provider"] == "unavailable" or status["state"] == "error":
        raise StepFailed(status.get("detail") or "The local AI is not available.")

    model = status.get("model") or "unknown model"

    if status["state"] != "ready":
        ctx.info(f"{model} is still loading", "The last check waits for it.")
        return f"llama.cpp with {model} (still loading)"

    return f"llama.cpp with {model} (ready)"


def _check_models(ctx: StepContext) -> str:
    models = get_runtime().status().get("models") or []

    if not models:
        raise StepFailed("no .gguf models found in the models folder")

    names = ", ".join(model["name"] for model in models[:5])

    if len(models) > 5:
        names += f" and {len(models) - 5} more"

    return f"{len(models)} model{'' if len(models) == 1 else 's'} available: {names}"


def _ask_model(ctx: StepContext) -> str:
    """Send one tiny prompt and time it. Cancellable between pieces of text."""
    service = get_runtime().service
    prompt = [
        {"role": "user", "content": "Reply with one short sentence saying you are ready."}
    ]

    began = time.monotonic()
    first_text: float | None = None
    parts: list[str] = []

    # If the model is still loading, this waits for it: cancelling is noticed
    # as soon as the first text arrives.
    stream = service.stream_reply(prompt)

    try:
        for piece in stream:
            ctx.check_cancelled()

            if first_text is None:
                first_text = time.monotonic() - began

            parts.append(piece)

            if ctx.seconds_left() <= 0:
                raise StepFailed("the time limit was reached while waiting for the model")
    except AIProviderError as exc:
        raise StepFailed(str(exc)) from exc
    finally:
        close = getattr(stream, "close", None)

        if close is not None:
            close()

    reply = "".join(parts).strip()

    if not reply:
        raise StepFailed("the model sent an empty reply")

    total = time.monotonic() - began

    ctx.state["first_text_seconds"] = first_text or 0.0
    ctx.state["reply_seconds"] = total

    return (
        f"Reply: {reply[:300]}\n"
        f"First text after {ctx.state['first_text_seconds']:.1f}s, "
        f"complete after {total:.1f}s."
    )


def _report(ctx: StepContext) -> str:
    first = ctx.state.get("first_text_seconds", 0.0)
    total = ctx.state.get("reply_seconds", 0.0)

    return f"All checks passed. The AI answered in {total:.1f}s (first text after {first:.1f}s)."


def _self_check_steps(params: dict) -> list[Step]:
    return [
        Step("engine", "Check the AI engine", _check_engine),
        Step("models", "List the available models", _check_models, optional=True),
        Step("ask", "Ask the model a test question", _ask_model, role="reader"),
        Step("report", "Summarise the results", _report),
    ]


SELF_CHECK = Task(
    kind="self_check",
    title="Self-check",
    description=(
        "Checks the AI engine and models, sends the model a short test question, "
        "and reports how long it takes. A quick way to see the step log in action."
    ),
    build=_self_check_steps,
    limits=Limits(max_steps=10, max_seconds=600),
)


# -- ask about a folder ------------------------------------------------------------

def allowed_roots(env: Mapping[str, str] = os.environ) -> list[Path]:
    """Folders ORION may read, from ORION_AGENT_ROOTS (empty means anywhere)."""
    raw = env.get("ORION_AGENT_ROOTS", "")

    return [
        Path(part).expanduser().resolve()
        for part in raw.split(os.pathsep)
        if part.strip()
    ]


def validate_ask_folder(params: Mapping, env: Mapping[str, str] = os.environ) -> dict:
    folder = _clean_folder(params.get("folder"))
    question = " ".join(str(params.get("question") or "").split())

    if not folder:
        raise ParamError("Enter the folder to look at.")

    if not question:
        raise ParamError("Enter a question about the folder.")

    if len(question) > MAX_QUESTION_CHARS:
        raise ParamError(f"The question is too long (limit {MAX_QUESTION_CHARS} characters).")

    return {"folder": str(_check_folder(folder, env)), "question": question}


def _clean_folder(value) -> str:
    # People paste paths from Explorer's "Copy as path", which adds quotes.
    return str(value or "").strip().strip('"').strip("'").strip()


def _check_folder(folder: str, env: Mapping[str, str]) -> Path:
    """The folder as a resolved path, or ``ParamError`` saying what is wrong with it."""
    path = Path(folder).expanduser()

    if not path.is_absolute():
        raise ParamError("Enter the full path of the folder, like D:\\Projects\\myapp.")

    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ParamError("That folder does not exist.") from None

    if not resolved.is_dir():
        raise ParamError("That is a file, not a folder.")

    roots = allowed_roots(env)

    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        raise ParamError(
            "That folder is outside the places ORION is allowed to read "
            "(set by ORION_AGENT_ROOTS)."
        )

    data = DATA_DIR.resolve()

    if resolved == data or data in resolved.parents:
        raise ParamError("That folder is off limits (it holds ORION's own data).")

    try:
        next(os.scandir(resolved), None)
    except OSError:
        raise ParamError("ORION is not allowed to read that folder.") from None

    return resolved


MAX_INSTRUCTION_CHARS = 2000


def validate_edit_files(params: Mapping, env: Mapping[str, str] = os.environ) -> dict:
    folder = _clean_folder(params.get("folder"))
    instruction = " ".join(str(params.get("instruction") or "").split())

    if not folder:
        raise ParamError("Enter the folder to change.")

    if not instruction:
        raise ParamError("Describe the change you want.")

    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise ParamError(f"The description is too long (limit {MAX_INSTRUCTION_CHARS} characters).")

    resolved = _check_folder(folder, env)

    # Changing files is riskier than reading them: insist on a project folder.
    if resolved.parent == resolved:
        raise ParamError("Pick a specific project folder, not a whole drive.")

    return {"folder": str(resolved), "instruction": instruction}


def _require_model(ctx: StepContext) -> str:
    """Fail early, with a clear reason, if there is no real model to ask."""
    status = get_runtime().status()

    if status["provider"] == "echo":
        raise StepFailed("ORION is in echo mode (no AI model is loaded). Load a model first.")

    if status["provider"] == "unavailable" or status["state"] == "error":
        raise StepFailed(status.get("detail") or "The local AI is not available.")

    model = status.get("model") or "the model"

    if status["state"] != "ready":
        ctx.info(f"{model} is still loading", "The next steps wait for it.")

    return f"Using {model}."


def _open_folder(ctx: StepContext) -> str:
    folder = ctx.state["params"]["folder"]

    try:
        workspace = Workspace(folder, blocked=[DATA_DIR])
    except WorkspaceError as exc:
        raise StepFailed(str(exc)) from exc

    ctx.state["workspace"] = workspace

    return f"Read-only access to {workspace.root}\n\n{workspace.overview()}"


def _hide_secrets(detail: str | None) -> str | None:
    """Tool previews go into the run's log, so secret-looking values are masked first."""
    return redact_secrets(detail) if detail else detail


def _model_asker(ctx: StepContext, runtime, reply_tokens: int):
    """The function the reader/editor use to get one structured reply from the model."""
    warned = []

    def ask(messages, schema):
        try:
            reply = runtime.service.generate_structured(
                messages, schema, max_tokens=reply_tokens
            )
        except AIProviderError as exc:
            raise StepFailed(str(exc)) from exc

        # The server may refuse to constrain the model's output (some llama.cpp
        # builds do); say so once, since replies are then a little less reliable.
        if not warned and getattr(getattr(runtime, "provider", None), "structured_fallback", False):
            warned.append(True)
            ctx.warn(
                "Output format is not enforced for this model",
                "The AI server rejected ORION's output grammar, so replies are read "
                "leniently instead. This works, but is a little less reliable.",
            )

        return reply

    return ask


def backup_dir_of(run_id: str):
    """Where this run's backups live. Reads DATA_DIR when called, so tests can point it elsewhere."""
    return backup_dir_for(run_id, DATA_DIR)


def _explore(ctx: StepContext) -> str:
    runtime = get_runtime()
    ctx_size = runtime.settings.ctx_size
    reply_tokens = reply_tokens_for(ctx_size)
    ask = _model_asker(ctx, runtime, reply_tokens)

    reader = Reader(
        ctx.state["workspace"],
        ctx.state["params"]["question"],
        ask,
        notify=lambda title, detail, ms: ctx.tool(title, _hide_secrets(detail), ms),
        check_cancelled=ctx.check_cancelled,
        seconds_left=ctx.seconds_left,
        ctx_size=ctx_size,
        reply_tokens=reply_tokens,
    )

    result = reader.run()
    ctx.state["answer"] = result.answer
    ctx.state["sources"] = result.sources

    calls = result.tool_calls

    return f"Used {calls} tool call{'' if calls == 1 else 's'}."


def _write_answer(ctx: StepContext) -> str:
    answer = ctx.state["answer"]
    sources = ctx.state.get("sources")

    # Shown at the top of the run: the answer, then what it is based on.
    ctx.state["summary"] = f"{answer}\n\n{sources}" if sources else answer

    return f"Answer ready ({len(answer)} characters)."


def _ask_folder_steps(params: dict) -> list[Step]:
    def prime(ctx: StepContext) -> str:
        # Steps share what they need through ctx.state; start with the parameters.
        ctx.state["params"] = params

        return _require_model(ctx)

    return [
        Step("model", "Check the AI model", prime),
        Step("open", "Open the folder (read-only)", _open_folder),
        Step("read", "Explore the files and find the answer", _explore, role="reader"),
        Step("answer", "Write up the answer", _write_answer),
    ]


ASK_FOLDER = Task(
    kind="ask_folder",
    title="Ask about a folder",
    description=(
        "The reader looks through a folder of your files (read-only) and answers "
        "your question, naming the files it used."
    ),
    build=_ask_folder_steps,
    limits=Limits(max_steps=10, max_seconds=900),
    fields=(
        Field("folder", "Folder", "text", "Full path, e.g. D:\\Projects\\myapp"),
        Field("question", "Question", "textarea", "What would you like to know?"),
    ),
    validate=validate_ask_folder,
    run_title=lambda params: "Ask: " + (
        params["question"] if len(params["question"]) <= 60 else params["question"][:57] + "..."
    ),
)


# -- edit files ----------------------------------------------------------------------

def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _open_for_edit(ctx: StepContext) -> str:
    folder = ctx.state["params"]["folder"]

    try:
        workspace = Workspace(folder, blocked=[DATA_DIR])
    except WorkspaceError as exc:
        raise StepFailed(str(exc)) from exc

    ctx.state["workspace"] = workspace
    ctx.state["changes"] = ChangeSet(workspace)

    return (
        f"Working in {workspace.root}. Edits are only collected for now; "
        f"nothing is written until you approve.\n\n{workspace.overview()}"
    )


def _make_edits(ctx: StepContext) -> str:
    runtime = get_runtime()
    ctx_size = runtime.settings.ctx_size
    reply_tokens = reply_tokens_for(ctx_size)
    changes: ChangeSet = ctx.state["changes"]

    editor = Editor(
        changes,
        ctx.state["params"]["instruction"],
        _model_asker(ctx, runtime, reply_tokens),
        notify=lambda title, detail, ms: ctx.tool(title, _hide_secrets(detail), ms),
        check_cancelled=ctx.check_cancelled,
        seconds_left=ctx.seconds_left,
        ctx_size=ctx_size,
        reply_tokens=reply_tokens,
    )

    result = editor.run()
    ctx.state["editor_summary"] = result.summary

    if not changes:
        ctx.state["no_changes"] = True

        return "The editor proposed no changes."

    files, added, removed = changes.stats()

    return f"{_plural(files, 'file')} staged (+{added} \u2212{removed}). Nothing has been written yet."


def _security_checks(ctx: StepContext) -> str:
    """Plain-code checks of what is about to be written. No AI involved."""
    if ctx.state.get("no_changes"):
        return "Nothing to check."

    changes: ChangeSet = ctx.state["changes"]
    findings = run_checks(changes)
    ctx.state["findings"] = findings

    ctx.check(f"Security checks: {summarize(findings)}", to_json(findings))

    return f"Checked {_plural(len(changes.files()), 'file')}: {summarize(findings)}."


def _ask_approval(ctx: StepContext) -> str:
    if ctx.state.get("no_changes"):
        return "Nothing to approve."

    changes: ChangeSet = ctx.state["changes"]
    files, added, removed = changes.stats()

    # The diff is shown and logged, so secret-looking values are masked in this view
    # (the real text is what gets written).
    diff = changes.diff()
    shown = redact_secrets(diff, diff=True)

    if shown != diff:
        shown = (
            "# Secret-looking values are hidden in this view (shown as ****). "
            "The real text will be written.\n" + shown
        )

    ctx.proposal(
        f"Proposed changes: {_plural(files, 'file')} (+{added} \u2212{removed})",
        shown,
    )

    # High-risk findings mean "Apply" is not enough: the user must confirm them.
    decision = ctx.wait_for_approval(
        require_acknowledgement=has_high(ctx.state.get("findings", []))
    )
    ctx.state["decision"] = decision

    return "You approved the changes." if decision == "apply" else "You rejected the changes."


def _apply_changes(ctx: StepContext) -> str:
    said = ctx.state.get("editor_summary", "")

    if ctx.state.get("no_changes"):
        ctx.state["summary"] = f"No changes were proposed.\n\n{said}".strip()

        return "Nothing to apply."

    if ctx.state.get("decision") != "apply":
        ctx.state["summary"] = (
            f"You rejected the proposed changes; nothing was written.\n\n{said}".strip()
        )

        return "Rejected: nothing was changed."

    run_id = ctx.state.get("run_id")

    if not run_id:
        raise StepFailed("this run has no id to keep its backup under")

    changes: ChangeSet = ctx.state["changes"]
    files, added, removed = changes.stats()

    try:
        report = changes.apply(backup_dir_of(run_id))
    except ChangeError as exc:
        raise StepFailed(str(exc)) from exc

    names = ", ".join(f"`{name}`" for name in report.written + report.created)
    findings = ctx.state.get("findings", [])
    checked = f"\n\nSecurity checks: {summarize(findings)}." if findings else ""

    if has_high(findings):
        checked += " You confirmed the high-risk findings before applying."

    ctx.state["summary"] = (
        f"**Applied {_plural(files, 'file change')}** (+{added} \u2212{removed}): {names}.\n\n"
        f"{said}{checked}\n\nOriginals are backed up; use **Undo changes** on this page to put them back."
    ).strip()

    return f"Wrote {_plural(files, 'file')}. A backup of the originals was saved."


def _edit_files_steps(params: dict) -> list[Step]:
    def prime(ctx: StepContext) -> str:
        ctx.state["params"] = params

        return _require_model(ctx)

    return [
        Step("model", "Check the AI model", prime),
        Step("open", "Open the folder", _open_for_edit),
        Step("edit", "Plan and prepare the edits (nothing is written yet)", _make_edits, role="editor"),
        Step("check", "Run security checks (plain code, no AI)", _security_checks, role="security"),
        Step("approve", "Wait for your approval", _ask_approval),
        Step("apply", "Apply the changes", _apply_changes),
    ]


EDIT_FILES = Task(
    kind="edit_files",
    title="Edit files in a folder",
    description=(
        "The editor makes small changes to files in a folder, as you describe. "
        "You review a diff first; nothing is written until you click Apply, and you can undo afterwards."
    ),
    build=_edit_files_steps,
    limits=Limits(max_steps=10, max_seconds=3600),
    fields=(
        Field("folder", "Folder", "text", "Full path, e.g. D:\\Projects\\myapp"),
        Field("instruction", "What to change", "textarea", "e.g. In util.py, rename the function helper to compute"),
    ),
    validate=validate_edit_files,
    run_title=lambda params: "Edit: " + (
        params["instruction"] if len(params["instruction"]) <= 60 else params["instruction"][:57] + "..."
    ),
)


TASKS: dict[str, Task] = {task.kind: task for task in (SELF_CHECK, ASK_FOLDER, EDIT_FILES)}


def get_task(kind: str) -> Task | None:
    return TASKS.get(kind)


def list_tasks() -> list[Task]:
    return list(TASKS.values())
