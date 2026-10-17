const taskList = document.getElementById("task-list");
const runListElement = document.getElementById("run-list");
const runsMain = document.getElementById("runs-main");
const runsEmpty = document.getElementById("runs-empty");
const runHeader = document.getElementById("run-header");
const runTitle = document.getElementById("run-title");
const runStatus = document.getElementById("run-status");
const undoButton = document.getElementById("undo-button");
const approvalBar = document.getElementById("approval-bar");
const applyButton = document.getElementById("apply-button");
const rejectButton = document.getElementById("reject-button");
const runChanges = document.getElementById("run-changes");
const cancelButton = document.getElementById("cancel-button");
const deleteButton = document.getElementById("delete-button");
const runParams = document.getElementById("run-params");
const runSummary = document.getElementById("run-summary");
const timeline = document.getElementById("timeline");

let POLL_MS = 1000; // how often a running run is checked for new steps

let runs = [];              // newest first
let selectedRunId = null;
let lastSeq = 0;            // newest event already on screen
let pollTimer = null;
let taskCards = [];         // [{kind, button, inputs}] for each kind of run
let fieldLabels = {};       // field name -> label, for showing a run's parameters

const STATUS_LABELS = {
    queued: "Queued",
    running: "Running",
    awaiting_approval: "Waiting for you",
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
};

const EVENT_ICONS = {
    run_started: "\u25B6",    // ▶
    step_started: "\u25CB",   // ○
    step_finished: "\u2713",  // ✓
    step_failed: "\u2717",    // ✗
    step_skipped: "\u26A0",   // ⚠
    tool_call: "\u2699",     // ⚙
    proposal: "\u00B1",      // ±
    info: "\u2022",           // •
    warning: "\u26A0",        // ⚠
    run_finished: "\u25A0",   // ■
};


async function apiRequest(url, options = {}) {
    const response = await fetch(url, {
        credentials: "same-origin",
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {}),
        },
    });

    if (!response.ok) {
        let detail = "Request failed.";

        try {
            const data = await response.json();

            if (typeof data.detail === "string") {
                detail = data.detail;
            }
        } catch {
            // Keep the default message.
        }

        throw new Error(detail);
    }

    return response.json();
}


function formatDuration(ms) {
    return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}


function formatDate(iso) {
    if (!iso) {
        return "";
    }

    return new Date(iso).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
    });
}


// ---- Run list -------------------------------------------------------------

function makePill(status) {
    const pill = document.createElement("span");

    pill.className = `status-pill status-${status}`;
    pill.textContent = STATUS_LABELS[status] || status;

    return pill;
}


function renderRunList() {
    runListElement.innerHTML = "";

    if (runs.length === 0) {
        const empty = document.createElement("div");

        empty.className = "list-empty";
        empty.textContent = "No runs yet.";
        runListElement.appendChild(empty);

        updateStartButtons();

        return;
    }

    for (const run of runs) {
        const item = document.createElement("button");

        item.type = "button";
        item.className = "run-item" + (run.id === selectedRunId ? " active" : "");
        item.dataset.runId = run.id;

        const text = document.createElement("span");
        const title = document.createElement("span");
        const date = document.createElement("span");

        text.className = "run-item-text";
        title.className = "run-item-title";
        title.textContent = run.title;
        date.className = "run-item-date";
        date.textContent = formatDate(run.created_at);

        text.appendChild(title);
        text.appendChild(date);

        item.appendChild(text);
        item.appendChild(makePill(run.status));

        item.addEventListener("click", () => selectRun(run.id));

        runListElement.appendChild(item);
    }

    updateStartButtons();
}


function updateRunInList(run) {
    const index = runs.findIndex((item) => item.id === run.id);

    if (index === -1) {
        return;
    }

    const changed = runs[index].status !== run.status;

    runs[index] = run;

    if (changed) {
        renderRunList();
    }
}


// ---- Starting runs ---------------------------------------------------------

// The folder is worth remembering between visits; a question is not.
function recallField(name) {
    try {
        return name === "folder" && typeof localStorage !== "undefined"
            ? localStorage.getItem("orion.runs.folder") || ""
            : "";
    } catch {
        return "";
    }
}


function rememberField(name, value) {
    try {
        if (name === "folder" && typeof localStorage !== "undefined") {
            localStorage.setItem("orion.runs.folder", value);
        }
    } catch {
        // Storage can be blocked; remembering is only a convenience.
    }
}


function renderTasks(kinds) {
    taskList.innerHTML = "";
    taskCards = [];
    fieldLabels = {};

    for (const kind of kinds) {
        const card = document.createElement("div");
        const title = document.createElement("strong");
        const description = document.createElement("span");
        const inputs = {};

        card.className = "task-card";
        card.dataset.kind = kind.kind;

        title.textContent = kind.title;
        description.textContent = kind.description;

        card.appendChild(title);
        card.appendChild(description);

        for (const field of kind.fields || []) {
            const label = document.createElement("label");
            const caption = document.createElement("span");
            const input = document.createElement(field.kind === "textarea" ? "textarea" : "input");

            fieldLabels[field.name] = field.label;

            label.className = "task-field";
            caption.textContent = field.label;

            if (field.kind === "textarea") {
                input.rows = 3;
            } else {
                input.type = "text";
            }

            input.placeholder = field.placeholder || "";
            input.value = recallField(field.name);
            input.dataset.field = field.name;

            label.appendChild(caption);
            label.appendChild(input);
            card.appendChild(label);

            inputs[field.name] = input;
        }

        const button = document.createElement("button");

        button.type = "button";
        button.className = "task-start";
        button.textContent = "Start";
        button.addEventListener("click", () => startRun(kind.kind));

        card.appendChild(button);
        taskList.appendChild(card);

        taskCards.push({ kind: kind.kind, button, inputs });
    }

    updateStartButtons();
}


// Only one run at a time (there is one AI model), so starting is disabled
// while any run is still going.
function updateStartButtons() {
    const busy = runs.some((run) => run.active);

    for (const card of taskCards) {
        card.button.disabled = busy;
        card.button.title = busy ? "A run is already in progress." : "";
    }
}


async function startRun(kind) {
    const card = taskCards.find((item) => item.kind === kind);
    const params = {};

    for (const [name, input] of Object.entries(card ? card.inputs : {})) {
        params[name] = input.value;
    }

    if (card) {
        card.button.disabled = true; // no double-clicks while the request is out
    }

    try {
        const run = await apiRequest("/runs/", {
            method: "POST",
            body: JSON.stringify({ kind, params }),
        });

        for (const [name, value] of Object.entries(params)) {
            rememberField(name, value);
        }

        runs.unshift(run);
        renderRunList();
        selectRun(run.id);
    } catch (error) {
        console.error(error);

        updateStartButtons();

        alert(
            error instanceof Error
                ? error.message
                : "Unable to start the run."
        );
    }
}


// ---- The selected run -----------------------------------------------------

// A unified diff, one coloured line per line. Built from text, never HTML.
function renderDiff(text) {
    const block = document.createElement("pre");

    block.className = "event-diff";

    // A trailing newline would otherwise draw one empty line at the end.
    for (const line of text.replace(/\n$/, "").split("\n")) {
        const row = document.createElement("div");

        if (line.startsWith("+++") || line.startsWith("---")) {
            row.className = "diff-file";
        } else if (line.startsWith("@@")) {
            row.className = "diff-hunk";
        } else if (line.startsWith("+")) {
            row.className = "diff-add";
        } else if (line.startsWith("-")) {
            row.className = "diff-del";
        }

        row.textContent = line === "" ? " " : line;
        block.appendChild(row);
    }

    return block;
}


function renderEvent(event) {
    const item = document.createElement("li");
    const icon = document.createElement("span");
    const body = document.createElement("div");
    const title = document.createElement("div");
    const meta = document.createElement("div");
    const time = document.createElement("span");

    item.className = `event event-${event.type}`;

    icon.className = "event-icon";
    icon.textContent = EVENT_ICONS[event.type] || "\u2022";

    body.className = "event-body";
    title.className = "event-title";
    title.textContent = event.title;
    body.appendChild(title);

    if (event.role && event.role !== "supervisor") {
        const role = document.createElement("span");

        role.className = "event-role";
        role.textContent = event.role;
        title.appendChild(role);
    }

    if (event.detail && event.type === "proposal") {
        body.appendChild(renderDiff(event.detail));
    } else if (event.detail) {
        const detail = document.createElement("div");

        detail.className = "event-detail";
        detail.textContent = event.detail;
        body.appendChild(detail);
    }

    meta.className = "event-meta";
    time.className = "event-time";
    time.textContent = `+${(event.elapsed_ms / 1000).toFixed(1)}s`;
    meta.appendChild(time);

    if (event.duration_ms !== null && event.duration_ms !== undefined) {
        const duration = document.createElement("span");

        duration.className = "event-duration";
        duration.textContent = `took ${formatDuration(event.duration_ms)}`;
        meta.appendChild(duration);
    }

    item.appendChild(icon);
    item.appendChild(body);
    item.appendChild(meta);

    return item;
}


// "Folder: ...  Question: ..." under the title; rebuilt only when the run changes.
function renderParams(run) {
    if (runParams.dataset.runId === run.id) {
        return;
    }

    runParams.dataset.runId = run.id;
    runParams.innerHTML = "";

    const entries = Object.entries(run.params || {}).filter(
        ([, value]) => value !== "" && value !== null && value !== undefined
    );

    runParams.hidden = entries.length === 0;

    for (const [name, value] of entries) {
        const line = document.createElement("div");
        const label = document.createElement("strong");
        const text = document.createElement("span");

        label.textContent = `${fieldLabels[name] || name}: `;
        text.textContent = String(value);

        line.appendChild(label);
        line.appendChild(text);
        runParams.appendChild(line);
    }
}


function renderRunHeader(run) {
    runsEmpty.hidden = true;
    runHeader.hidden = false;

    renderParams(run);

    runTitle.textContent = run.title;
    runStatus.className = `status-pill status-${run.status}`;
    runStatus.textContent = STATUS_LABELS[run.status] || run.status;

    cancelButton.hidden = !run.active;
    deleteButton.hidden = run.active;

    const waiting = run.status === "awaiting_approval";
    const changes = run.changes || null;

    approvalBar.hidden = !waiting;

    if (!waiting) {
        applyButton.disabled = false;
        rejectButton.disabled = false;
    }

    undoButton.hidden = run.active || !changes || !changes.applied || changes.undone;

    runChanges.hidden = !changes;

    if (changes) {
        runChanges.textContent = changes.undone
            ? "The changes this run made were undone."
            : `Changes written to: ${changes.files.join(", ")}`;
    }

    // A cancel already requested keeps its label until the run really stops.
    if (!run.active) {
        cancelButton.disabled = false;
        cancelButton.textContent = "Cancel run";
    }

    const note = run.error || run.summary;

    runSummary.hidden = run.active || !note;

    if (run.error) {
        runSummary.className = "run-summary error";
        runSummary.textContent = run.error;
    } else if (run.summary) {
        // The summary can be a Markdown answer; the renderer escapes any HTML.
        runSummary.className = "run-summary markdown";
        runSummary.innerHTML = renderMarkdown(run.summary);
    } else {
        runSummary.className = "run-summary";
        runSummary.textContent = "";
    }
}


function showNoRunSelected() {
    selectedRunId = null;
    lastSeq = 0;

    runParams.hidden = true;
    runParams.dataset.runId = "";
    approvalBar.hidden = true;
    runChanges.hidden = true;

    clearTimeout(pollTimer);

    timeline.innerHTML = "";
    runHeader.hidden = true;
    runSummary.hidden = true;
    runsEmpty.hidden = false;
}


function selectRun(runId) {
    clearTimeout(pollTimer);

    selectedRunId = runId;
    lastSeq = 0;

    timeline.innerHTML = "";
    runSummary.hidden = true;

    const known = runs.find((run) => run.id === runId);

    if (known) {
        renderRunHeader(known);
    }

    renderRunList();

    return pollEvents();
}


// Asks for the events newer than `lastSeq`, shows them, and asks again a
// moment later while the run is still going.
async function pollEvents() {
    clearTimeout(pollTimer);

    const runId = selectedRunId;

    if (!runId) {
        return;
    }

    let data;

    try {
        data = await apiRequest(`/runs/${runId}/events?after=${lastSeq}`);
    } catch (error) {
        if (runId !== selectedRunId) {
            return;
        }

        console.error(error);

        if (error.message === "Run not found.") {
            // Deleted from somewhere else: drop it from the page.
            showNoRunSelected();
            await loadRuns();
            return;
        }

        pollTimer = setTimeout(pollEvents, POLL_MS * 3);
        return;
    }

    // The user picked another run while this request was in flight.
    if (runId !== selectedRunId) {
        return;
    }

    const wasAtBottom =
        runsMain.scrollTop + runsMain.clientHeight >= runsMain.scrollHeight - 40;

    for (const event of data.events) {
        if (event.seq > lastSeq) {
            timeline.appendChild(renderEvent(event));
            lastSeq = event.seq;
        }
    }

    renderRunHeader(data.run);
    updateRunInList(data.run);

    if (data.events.length > 0 && wasAtBottom) {
        runsMain.scrollTop = runsMain.scrollHeight;
    }

    if (data.run.active) {
        pollTimer = setTimeout(pollEvents, POLL_MS);
    } else {
        updateStartButtons();
    }
}


async function cancelRun() {
    const runId = selectedRunId;

    if (!runId) {
        return;
    }

    cancelButton.disabled = true;
    cancelButton.textContent = "Cancelling...";

    try {
        await apiRequest(`/runs/${runId}/cancel`, { method: "POST" });
    } catch (error) {
        console.error(error);

        cancelButton.disabled = false;
        cancelButton.textContent = "Cancel run";

        alert(
            error instanceof Error
                ? error.message
                : "Unable to cancel the run."
        );
    }

    await pollEvents();
}


async function decide(decision) {
    const runId = selectedRunId;

    if (!runId) {
        return;
    }

    applyButton.disabled = true;
    rejectButton.disabled = true;

    try {
        await apiRequest(`/runs/${runId}/decision`, {
            method: "POST",
            body: JSON.stringify({ decision }),
        });
    } catch (error) {
        console.error(error);

        applyButton.disabled = false;
        rejectButton.disabled = false;

        alert(
            error instanceof Error
                ? error.message
                : "Unable to send your decision."
        );
    }

    await pollEvents();
}


async function undoChanges() {
    const runId = selectedRunId;

    if (!runId || !confirm(
        "Undo the changes this run made? Files you have edited since are left as they are."
    )) {
        return;
    }

    undoButton.disabled = true;

    try {
        const data = await apiRequest(`/runs/${runId}/undo`, { method: "POST" });

        if (runId === selectedRunId) {
            updateRunInList(data.run);
            renderRunHeader(data.run);
        }

        const skipped = (data.undo && data.undo.skipped) || [];

        if (skipped.length > 0) {
            alert(
                "Some files were left alone because they changed after ORION wrote them:\n" +
                skipped.map((item) => `${item.path}`).join("\n")
            );
        }
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to undo the changes."
        );
    } finally {
        undoButton.disabled = false;
    }

    await pollEvents();
}


async function deleteRun() {
    const runId = selectedRunId;
    const known = runs.find((run) => run.id === runId);
    const keepsBackup = known && known.changes && known.changes.applied && !known.changes.undone;

    const message = keepsBackup
        ? "Delete this run and its log? This also deletes the backup, so its changes can no longer be undone."
        : "Delete this run and its log?";

    if (!runId || !confirm(message)) {
        return;
    }

    try {
        await apiRequest(`/runs/${runId}`, { method: "DELETE" });
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to delete the run."
        );

        return;
    }

    showNoRunSelected();
    await loadRuns();
}


async function loadRuns() {
    runs = await apiRequest("/runs/");

    renderRunList();

    return runs;
}


async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
    }

    // Fallback for non-secure contexts (e.g. opened by IP address).
    const area = document.createElement("textarea");

    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";

    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
}


// One listener serves every code block in the answer.
runSummary.addEventListener("click", async (event) => {
    const button = event.target.closest(".code-copy");

    if (!button) {
        return;
    }

    const code = button.closest(".code-block").querySelector("code");

    try {
        await copyText(code.textContent);
        button.textContent = "Copied";
    } catch (error) {
        console.error(error);
        button.textContent = "Copy failed";
    }

    setTimeout(() => {
        button.textContent = "Copy";
    }, 1500);
});


applyButton.addEventListener("click", () => decide("apply"));
rejectButton.addEventListener("click", () => decide("reject"));
undoButton.addEventListener("click", undoChanges);
cancelButton.addEventListener("click", cancelRun);
deleteButton.addEventListener("click", deleteRun);


async function initialize() {
    try {
        renderTasks(await apiRequest("/runs/kinds"));
        await loadRuns();

        if (runs.length > 0) {
            await selectRun(runs[0].id);
        }
    } catch (error) {
        console.error(error);
    }
}

initialize();
