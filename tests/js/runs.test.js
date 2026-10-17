// Run with:  node tests/js/runs.test.js
// Loads the real frontend/static/runs.js into a tiny fake DOM.
const fs = require("fs"), vm = require("vm"), path = require("path");
const read = f => fs.readFileSync(path.join(__dirname, "../../frontend/static", f), "utf8");

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.parent = null; this._text = ""; this._html = ""; this.className = ""; this.dataset = {}; this.style = {};
    this.hidden = false; this.disabled = false; this.title = ""; this.scrollTop = 0; this.scrollHeight = 1000; this.clientHeight = 500; this.listeners = {}; }
  set textContent(v) { this._text = String(v); this.children = []; this._html = ""; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set innerHTML(v) { this.children = []; this._text = ""; this._html = v; }
  get innerHTML() { return this._html; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
}
const ids = {}; const get = id => (ids[id] ||= new El("div"));
const document = { getElementById: get, createElement: t => new El(t) };
const alerts = [], confirms = []; let confirmAnswer = true;
let fetchImpl;
const realSetTimeout = setTimeout;
const copied = []; const store = {};
const ctx = { document, alert: m => alerts.push(m), confirm: m => { confirms.push(m); return confirmAnswer; }, console, setTimeout: realSetTimeout, clearTimeout, fetch: (...a) => fetchImpl(...a),
  window: { isSecureContext: true }, navigator: { clipboard: { writeText: async t => { copied.push(t); } } },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } } };
ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(read("markdown.js"), ctx);
vm.runInContext(read("runs.js").replace(/\ninitialize\(\);\s*$/, "\n"), ctx);
const run = c => vm.runInContext(c, ctx);
const settle = (ms = 30) => new Promise(r => realSetTimeout(r, ms));

let failures = 0;
const check = (name, ok, info = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  -> " + info)); if (!ok) failures++; };

// ---- a fake server -------------------------------------------------------
// Like a real server, hand back a fresh copy each time (never the page's own objects).
const json = (data, status = 200) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(data)) });
const mkRun = (id, status, extra = {}) => ({ id, kind: "self_check", title: "Self-check", status, active: ["queued", "running", "awaiting_approval"].includes(status),
  summary: null, error: null, created_at: "2026-10-10T10:00:00+00:00", started_at: null, finished_at: null, ...extra });
const ev = (seq, type, title, extra = {}) => ({ seq, type, role: "supervisor", title, detail: null, duration_ms: null, elapsed_ms: seq * 100, ...extra });
const KINDS = [
  { kind: "self_check", title: "Self-check", description: "Checks the engine.", fields: [] },
  { kind: "ask_folder", title: "Ask about a folder", description: "Reads a folder and answers.", fields: [
    { name: "folder", label: "Folder", kind: "text", placeholder: "Full path" },
    { name: "question", label: "Question", kind: "textarea", placeholder: "What would you like to know?" } ] },
];
const startBtn = (i = 0) => { const card = get("task-list").children[i]; return card.children[card.children.length - 1]; };
const fieldInput = (card, name) => get("task-list").children[card].children.find(c => c.tag === "label" && c.children[1].dataset.field === name).children[1];

let S;
function resetServer() {
  S = { runs: [], events: {}, calls: [], eventsHook: null,
    createReply: () => json(mkRun("new1", "running"), 201), cancelReply: () => json({}), deleteReply: () => json({ deleted: true }),
    decisionReply: () => json({}), undoReply: () => json({}) };
}
fetchImpl = async (url, o = {}) => {
  const method = o.method || "GET"; S.calls.push(method + " " + url);
  if (url === "/runs/kinds") return json(KINDS);
  if (url === "/runs/" && method === "GET") return json(S.runs);
  if (url === "/runs/" && method === "POST") return S.createReply(JSON.parse(o.body));
  let m;
  if ((m = url.match(/^\/runs\/([^/]+)\/events\?after=(\d+)$/))) {
    const found = S.runs.find(r => r.id === m[1]);
    if (!found) return json({ detail: "Run not found." }, 404);
    if (S.eventsHook) await S.eventsHook(found, +m[2]);
    return json({ run: found, events: (S.events[found.id] || []).filter(e => e.seq > +m[2]) });
  }
  if ((m = url.match(/^\/runs\/([^/]+)\/cancel$/))) return S.cancelReply(m[1]);
  if ((m = url.match(/^\/runs\/([^/]+)\/decision$/))) return S.decisionReply(JSON.parse(o.body), m[1]);
  if ((m = url.match(/^\/runs\/([^/]+)\/undo$/))) return S.undoReply(m[1]);
  if (method === "DELETE" && (m = url.match(/^\/runs\/([^/]+)$/))) return S.deleteReply(m[1]);
  throw new Error("unexpected request " + method + " " + url);
};

function resetPage() {
  run("clearTimeout(pollTimer); runs = []; selectedRunId = null; lastSeq = 0; POLL_MS = 10");
  for (const id of ["task-list", "run-list", "timeline", "run-params"]) { get(id).children = []; get(id)._text = ""; }
  get("runs-empty").hidden = false; get("run-header").hidden = true; get("run-summary").hidden = true; get("run-params").hidden = true; get("run-params").dataset.runId = ""; get("approval-bar").hidden = true; get("run-changes").hidden = true; get("undo-button").hidden = true; get("apply-button").disabled = false; get("reject-button").disabled = false;
  for (const k of Object.keys(store)) delete store[k]; copied.length = 0;
  alerts.length = 0; confirms.length = 0; confirmAnswer = true; resetServer();
}
async function boot(runs = [], events = {}) {
  resetPage(); S.runs = runs; S.events = events;
  await run("initialize()"); await settle();
}
const listItems = () => get("run-list").children.filter(c => c.tag === "button");
const rows = () => get("timeline").children;
const eventTitles = () => rows().map(r => r.children[1].children[0]._text);
const pollsAfter = () => S.calls.filter(c => c.includes("/events?after=")).map(c => +c.split("after=")[1]);

(async () => {
  // 1 a fresh install: buttons from the server, empty history, empty state
  await boot();
  const card0 = get("task-list").children[0];
  check("a card per kind of run, built from the server's list", get("task-list").children.length === 2 && card0.children[0].textContent === "Self-check" && card0.children[1].textContent === "Checks the engine." && startBtn(0).textContent === "Start" && startBtn(0).disabled === false);
  check("no runs yet: friendly note and empty state", get("run-list").children[0].textContent === "No runs yet." && get("runs-empty").hidden === false && get("run-header").hidden === true);

  // 2 history: newest run opens by itself and its log is drawn
  const done = mkRun("r2", "completed", { summary: "4 of 4 steps finished" });
  const old = mkRun("r1", "failed", { error: "Ask: boom", created_at: "2026-10-09T09:00:00+00:00" });
  await boot([done, old], { r2: [
    ev(1, "run_started", "Self-check", { detail: "4 steps planned" }),
    ev(2, "step_started", "Step 1/4: Check the AI engine"),
    ev(3, "step_finished", "Check the AI engine: done", { detail: "llama.cpp with qwen (ready)", duration_ms: 12 }),
    ev(4, "step_finished", "Ask: done", { role: "reader", duration_ms: 2500, elapsed_ms: 2600 }),
    ev(5, "run_finished", "Run completed after 2.7s", { elapsed_ms: 2700 }),
  ] });
  check("history lists every run, newest first, with status", listItems().length === 2 && listItems()[0].children[1].textContent === "Completed" && listItems()[1].children[1].textContent === "Failed");
  check("the newest run is opened and highlighted", get("run-title").textContent === "Self-check" && listItems()[0].className.includes("active") && !listItems()[1].className.includes("active"));
  check("every event is on the timeline in order", eventTitles().length === 5 && eventTitles()[0] === "Self-check" && eventTitles()[4] === "Run completed after 2.7s");
  check("event types get their own icon class", rows()[0].className === "event event-run_started" && rows()[2].className === "event event-step_finished" && rows()[2].children[0].textContent === "\u2713");
  check("details are shown under the title", rows()[2].children[1].children[1].textContent === "llama.cpp with qwen (ready)");
  check("non-supervisor roles get a chip; the supervisor does not", rows()[3].children[1].children[0].children[0].textContent === "reader" && rows()[0].children[1].children[0].children.length === 0);
  check("elapsed time and step duration are shown", rows()[3].children[2].children[0].textContent === "+2.6s" && rows()[3].children[2].children[1].textContent === "took 2.5 s" && rows()[2].children[2].children[1].textContent === "took 12 ms");
  check("a finished run shows its summary and Delete, not Cancel", get("run-summary").hidden === false && get("run-summary").innerHTML.includes("4 of 4 steps finished") && get("cancel-button").hidden === true && get("delete-button").hidden === false);
  check("a finished run does not keep polling", pollsAfter().length === 1);

  // 3 a failed run explains why, in the error style
  await run("selectRun('r1')"); await settle();
  check("a failed run shows its error styled as one", get("run-summary").textContent === "Ask: boom" && get("run-summary").className.includes("error") && get("run-status").className === "status-pill status-failed");

  // 4 a running run is polled for only what is new, until it ends
  const live = mkRun("r3", "running");
  S = null; resetPage(); S.runs = [live]; S.events = { r3: [ev(1, "run_started", "Self-check"), ev(2, "step_started", "Step 1/4: Check")] };
  S.eventsHook = async (r, after) => {
    if (after === 2) { S.events.r3.push(ev(3, "step_finished", "Check: done", { duration_ms: 5 })); }
    if (after === 3) { S.events.r3.push(ev(4, "run_finished", "Run completed after 1.0s")); r.status = "completed"; r.active = false; r.summary = "ok"; }
  };
  await run("initialize()"); await settle(150);
  check("polling asks only for newer events each time", JSON.stringify(pollsAfter().slice(0, 3)) === "[0,2,3]", JSON.stringify(pollsAfter()));
  check("no event is shown twice", rows().length === 4 && eventTitles().join("|") === "Self-check|Step 1/4: Check|Check: done|Run completed after 1.0s", eventTitles().join("|"));
  const callsBefore = S.calls.length; await settle(80);
  check("polling stops once the run has finished", S.calls.length === callsBefore && get("run-status").textContent === "Completed" && listItems()[0].children[1].textContent === "Completed");

  // 5 while running: Cancel is offered, the summary is hidden, Start is disabled
  const busy = mkRun("r4", "running");
  await boot([busy], { r4: [ev(1, "run_started", "Self-check")] });
  check("a running run offers Cancel and hides Delete and the summary", get("cancel-button").hidden === false && get("delete-button").hidden === true && get("run-summary").hidden === true);
  check("Start is disabled while a run is going", startBtn(0).disabled === true && startBtn(1).disabled === true);
  run("clearTimeout(pollTimer)");

  // 6 start a run
  await boot([]);
  let startBody = null;
  S.createReply = body => { startBody = body; const r = mkRun("new1", "running"); S.runs.unshift(r); S.events.new1 = [ev(1, "run_started", "Self-check")]; return json(r, 201); };
  await startBtn(0).listeners.click[0](); await settle();
  check("Start POSTs the kind (and its parameters)", startBody && startBody.kind === "self_check" && JSON.stringify(startBody.params) === "{}", JSON.stringify(startBody));
  check("the new run appears, is opened, and its log starts", listItems().length === 1 && listItems()[0].className.includes("active") && rows().length === 1 && get("run-status").textContent === "Running");
  check("Start is disabled while that run goes", startBtn(0).disabled === true);
  run("clearTimeout(pollTimer)");

  // 7 start refused: the reason is shown, nothing changes
  await boot([]);
  S.createReply = () => json({ detail: "A run is already in progress. Wait for it to finish or cancel it." }, 409);
  await startBtn(0).listeners.click[0](); await settle();
  check("a refused start shows the reason and changes nothing", alerts[0] === "A run is already in progress. Wait for it to finish or cancel it." && listItems().length === 0);

  // 8 cancel
  const going = mkRun("r5", "running");
  await boot([going], { r5: [ev(1, "run_started", "Self-check")] });
  S.cancelReply = () => { going.status = "cancelled"; going.active = false; S.events.r5.push(ev(2, "run_finished", "Run cancelled after 0.2s")); return json(going); };
  await get("cancel-button").listeners.click[0](); await settle();
  check("Cancel POSTs, then the run shows as cancelled", S.calls.includes("POST /runs/r5/cancel") && get("run-status").textContent === "Cancelled" && get("cancel-button").hidden === true && eventTitles().includes("Run cancelled after 0.2s"));
  check("the Cancel button is reset for the next run", get("cancel-button").disabled === false && get("cancel-button").textContent === "Cancel run");

  const stuck = mkRun("r6", "running");
  await boot([stuck], { r6: [] });
  S.cancelReply = () => json({ detail: "This run has already finished." }, 409);
  await get("cancel-button").listeners.click[0](); await settle();
  check("a refused cancel shows why and re-enables the button", alerts[0] === "This run has already finished." && get("cancel-button").disabled === false && get("cancel-button").textContent === "Cancel run");
  run("clearTimeout(pollTimer)");

  // 9 delete
  const gone = mkRun("r7", "completed");
  await boot([gone], { r7: [ev(1, "run_started", "Self-check")] });
  confirmAnswer = false; await get("delete-button").listeners.click[0]();
  check("declining the confirmation deletes nothing", confirms.length === 1 && !S.calls.some(c => c.startsWith("DELETE")));
  confirmAnswer = true; S.deleteReply = () => { S.runs = []; return json({ deleted: true }); };
  await get("delete-button").listeners.click[0](); await settle();
  check("confirming deletes it and clears the page", S.calls.includes("DELETE /runs/r7") && listItems().length === 0 && get("runs-empty").hidden === false && get("run-header").hidden === true && rows().length === 0);

  // 10 the user switches runs while a slow answer is on its way
  const a = mkRun("a", "completed"), b = mkRun("b", "completed", { title: "Other" });
  await boot([a, b], { a: [ev(1, "run_started", "A-log")], b: [ev(1, "run_started", "B-log")] });
  S.eventsHook = async (r) => { if (r.id === "a") await settle(60); };
  const slowA = run("selectRun('a')");
  await settle(5); await run("selectRun('b')"); await slowA; await settle(100);
  check("a late answer for another run never reaches the screen", eventTitles().join() === "B-log" && get("run-title").textContent === "Other", eventTitles().join());

  // two polls in flight at once (e.g. Cancel clicked during a timed poll) get the same new events
  const overlap = mkRun("o1", "running");
  await boot([overlap], { o1: [ev(1, "run_started", "Self-check")] });
  run("clearTimeout(pollTimer)");
  S.events.o1.push(ev(2, "info", "arrived once"));
  S.eventsHook = async () => settle(20);
  const first = run("pollEvents()"), second = run("pollEvents()");
  await first; await second; run("clearTimeout(pollTimer)");
  check("overlapping polls never show an event twice", eventTitles().join("|") === "Self-check|arrived once", eventTitles().join("|"));

  // 11 errors while polling
  const flaky = mkRun("f1", "running");
  await boot([flaky], { f1: [ev(1, "run_started", "Self-check")] });
  let fail = true; const realHook = S.eventsHook;
  const origFetch = fetchImpl;
  fetchImpl = async (url, o) => (fail && /events\?after=1$/.test(url) ? (() => { throw new Error("network down"); })() : origFetch(url, o));
  run("POLL_MS = 10"); await settle(120);
  check("a network error keeps the timeline and retries later", rows().length === 1 && pollsAfter().filter(n => n === 1).length >= 1);
  fail = false; fetchImpl = origFetch; run("clearTimeout(pollTimer)");

  // a run deleted elsewhere
  const vanishing = mkRun("v1", "running");
  await boot([vanishing], { v1: [ev(1, "run_started", "Self-check")] });
  run("clearTimeout(pollTimer)"); S.runs = [];
  await run("pollEvents()"); await settle();
  check("a run that no longer exists resets the page", get("runs-empty").hidden === false && rows().length === 0 && listItems().length === 0);

  // 12 text from the server is shown as text, never as HTML
  const evil = mkRun("x1", "completed", { summary: "<b>sum</b>" });
  await boot([evil], { x1: [ev(1, "info", "<img src=x onerror=alert(1)>", { detail: "<script>bad()</script>" })] });
  check("titles, details and summaries can never inject HTML", rows()[0].children[1].children[0]._text === "<img src=x onerror=alert(1)>" && rows()[0].children[1].children[1]._text === "<script>bad()</script>" && rows()[0].innerHTML === "" && get("run-summary").innerHTML.includes("&lt;b&gt;sum&lt;/b&gt;") && !get("run-summary").innerHTML.includes("<b>"), get("run-summary").innerHTML);

  // 13 forms, parameters, markdown answers ---------------------------------
  await boot();
  const folderField = fieldInput(1, "folder"), questionField = fieldInput(1, "question");
  check("a kind with fields gets a labelled input for each", folderField.tag === "input" && folderField.placeholder === "Full path" && questionField.tag === "textarea" && questionField.placeholder === "What would you like to know?" && get("task-list").children[1].children.filter(c => c.tag === "label").map(l => l.children[0].textContent).join() === "Folder,Question");
  check("a kind without fields has just a Start button", get("task-list").children[0].children.filter(c => c.tag === "label").length === 0);

  let body13 = null;
  S.createReply = b => { body13 = b; const r = mkRun("p1", "running", { kind: "ask_folder", title: "Ask: what?", params: b.params }); S.runs.unshift(r); S.events.p1 = [ev(1, "run_started", "Ask: what?")]; return json(r, 201); };
  folderField.value = "D:\\Projects\\app"; questionField.value = "What does it do?";
  const clickP = startBtn(1).listeners.click[0]();
  check("Start is locked while the request is out", startBtn(1).disabled === true);
  await clickP; await settle(); run("clearTimeout(pollTimer)");
  check("Start sends the typed values as parameters", body13 && body13.kind === "ask_folder" && body13.params.folder === "D:\\Projects\\app" && body13.params.question === "What does it do?", JSON.stringify(body13));
  check("the run's parameters are shown under its title, with the form's labels", get("run-params").hidden === false && get("run-params").children.map(l => l.textContent).join("|") === "Folder: D:\\Projects\\app|Question: What does it do?", get("run-params").children.map(l => l.textContent).join("|"));
  check("only the folder is remembered for next time", store["orion.runs.folder"] === "D:\\Projects\\app" && !Object.values(store).includes("What does it do?"));

  await boot();
  store["orion.runs.folder"] = "E:\\Notes"; run("renderTasks(" + JSON.stringify(KINDS) + ")");
  check("a remembered folder is pre-filled", fieldInput(1, "folder").value === "E:\\Notes" && fieldInput(1, "question").value === "");

  S.createReply = () => json({ detail: "That folder does not exist." }, 422);
  await startBtn(1).listeners.click[0](); await settle();
  check("a rejected form shows the reason and unlocks Start", alerts[0] === "That folder does not exist." && startBtn(1).disabled === false && listItems().length === 0, JSON.stringify(alerts));

  // parameters are rebuilt only when another run is opened
  const pa = mkRun("pa", "completed", { params: { folder: "D:\\A", question: "q1" } }), pb = mkRun("pb", "completed", { params: { folder: "", question: "q2" } });
  await boot([pa, pb], { pa: [ev(1, "run_started", "A")], pb: [ev(1, "run_started", "B")] });
  const paramLines = get("run-params").children;
  await run("pollEvents()"); await settle();
  check("parameters are not rebuilt on every poll", get("run-params").children === paramLines && paramLines.length === 2);
  await run("selectRun('pb')"); await settle();
  check("opening another run shows its own parameters (empty ones skipped)", get("run-params").children.length === 1 && get("run-params").children[0].textContent === "Question: q2");
  await boot([mkRun("pc", "completed")], { pc: [] });
  check("a run without parameters hides the line", get("run-params").hidden === true);

  // the answer is Markdown
  const answered = mkRun("m1", "completed", { summary: "It **prints** hello.\n\n```python\nprint(1)\n```\n\nSee `src/app.py:4`." });
  await boot([answered], { m1: [ev(1, "run_started", "x")] });
  const html = get("run-summary").innerHTML;
  check("the answer is rendered as Markdown", get("run-summary").hidden === false && get("run-summary").className === "run-summary markdown" && html.includes("<strong>prints</strong>") && html.includes("code-block") && html.includes("<code>src/app.py:4</code>"), html);
  check("an error stays plain text", (await boot([mkRun("m2", "failed", { error: "**not bold** <i>x</i>" })], { m2: [] }), get("run-summary").textContent === "**not bold** <i>x</i>" && get("run-summary").innerHTML === "" && get("run-summary").className === "run-summary error"));

  await boot([answered], { m1: [] });
  const codeEl = { textContent: "print(1)" }, block = { querySelector: () => codeEl };
  const copyBtn = { textContent: "Copy", closest: sel => (sel === ".code-block" ? block : null) };
  await get("run-summary").listeners.click[0]({ target: { closest: sel => (sel === ".code-copy" ? copyBtn : null) } });
  check("the Copy button on a code block copies its code", copied.join() === "print(1)" && copyBtn.textContent === "Copied", copied.join() + " / " + copyBtn.textContent);
  copied.length = 0; await get("run-summary").listeners.click[0]({ target: { closest: () => null } });
  check("clicking elsewhere in the answer copies nothing", copied.length === 0);

  // tool calls
  await boot([mkRun("t1", "completed")], { t1: [ev(1, "tool_call", "read_file src/app.py", { role: "reader", detail: "Why: look\n\n   1| import os", duration_ms: 1200 })] });
  check("a tool call has its own icon, role chip, multi-line detail and timing", rows()[0].className === "event event-tool_call" && rows()[0].children[0].textContent === "\u2699" && rows()[0].children[1].children[0].children[0].textContent === "reader" && rows()[0].children[1].children[1]._text === "Why: look\n\n   1| import os" && rows()[0].children[2].children[1].textContent === "took 1.2 s");

  // 14 reviewing, approving and undoing changes -------------------------------
  const DIFF = "--- a/src/app.py\n+++ b/src/app.py\n@@ -3,3 +3,3 @@\n def main():\n-    return 1\n+    return 2\n";
  const proposalEvents = () => [ev(1, "run_started", "Edit: x"), ev(2, "proposal", "Proposed changes: 1 file (+1 \u22121)", { detail: DIFF })];
  const CHANGES = { applied: true, undone: false, files: ["src/app.py", "src/util.py"], created: "2026-10-12T10:00:00+00:00" };

  const waitRun = mkRun("w1", "awaiting_approval", { kind: "edit_files", title: "Edit: x" });
  await boot([waitRun], { w1: proposalEvents() }); run("clearTimeout(pollTimer)");
  check("a run waiting for you is labelled and offers the decision", get("run-status").textContent === "Waiting for you" && get("run-status").className === "status-pill status-awaiting_approval" && listItems()[0].children[1].textContent === "Waiting for you");
  check("the approval bar shows; Cancel stays; Delete, Undo and the summary are hidden", get("approval-bar").hidden === false && get("cancel-button").hidden === false && get("delete-button").hidden === true && get("undo-button").hidden === true && get("run-summary").hidden === true);
  check("Start is disabled while a run waits for you", startBtn(0).disabled === true && startBtn(1).disabled === true);

  const diffBlock = rows()[1].children[1].children[1];
  check("the proposal is drawn as a coloured diff, one line per line", rows()[1].children[0].textContent === "\u00B1" && diffBlock.tag === "pre" && diffBlock.className === "event-diff" && diffBlock.children.map(l => l.className).join("|") === "diff-file|diff-file|diff-hunk||diff-del|diff-add" && diffBlock.children[4].textContent === "-    return 1", diffBlock.children.map(l => l.className).join("|"));
  check("--- and +++ lines are file headers, not removals and additions", diffBlock.children[0].className === "diff-file" && diffBlock.children[1].className === "diff-file");

  await boot([mkRun("x2", "awaiting_approval")], { x2: [ev(1, "proposal", "p", { detail: "+<img src=x onerror=alert(1)>\n-<b>old</b>\n\n+end" })] }); run("clearTimeout(pollTimer)");
  const hostile = rows()[0].children[1].children[1];
  check("diff lines are text, never HTML; blank lines survive", hostile.children[0].className === "diff-add" && hostile.children[0]._text === "+<img src=x onerror=alert(1)>" && hostile.children[0].innerHTML === "" && hostile.children[1].className === "diff-del" && hostile.children.length === 4 && hostile.children[2]._text === " ");

  const idle = mkRun("o1", "running");
  await boot([idle], { o1: [ev(1, "run_started", "x")] }); run("clearTimeout(pollTimer)");
  check("no approval bar when nothing is waiting", get("approval-bar").hidden === true);

  // Apply / Reject
  const w2 = mkRun("w2", "awaiting_approval");
  await boot([w2], { w2: proposalEvents() }); run("clearTimeout(pollTimer)");
  let decided = null, lockedDuring = null;
  S.decisionReply = b => { decided = b; lockedDuring = [get("apply-button").disabled, get("reject-button").disabled]; w2.status = "running"; return json(w2); };
  await get("apply-button").listeners.click[0](); run("clearTimeout(pollTimer)");
  check("Apply sends the decision and locks both buttons while it is out", JSON.stringify(decided) === '{"decision":"apply"}' && JSON.stringify(lockedDuring) === "[true,true]", JSON.stringify([decided, lockedDuring]));
  check("once the run resumes the approval bar goes away", get("approval-bar").hidden === true && get("run-status").textContent === "Running");

  const w3 = mkRun("w3", "awaiting_approval");
  await boot([w3], { w3: proposalEvents() }); run("clearTimeout(pollTimer)");
  S.decisionReply = b => { decided = b; w3.status = "running"; return json(w3); };
  await get("reject-button").listeners.click[0](); run("clearTimeout(pollTimer)");
  check("Reject sends 'reject'", JSON.stringify(decided) === '{"decision":"reject"}');

  const w4 = mkRun("w4", "awaiting_approval");
  await boot([w4], { w4: proposalEvents() }); run("clearTimeout(pollTimer)");
  S.decisionReply = () => json({ detail: "This run is not waiting for a decision." }, 409);
  await get("apply-button").listeners.click[0](); run("clearTimeout(pollTimer)");
  check("a refused decision shows why and unlocks the buttons", alerts[0] === "This run is not waiting for a decision." && get("apply-button").disabled === false && get("reject-button").disabled === false, JSON.stringify(alerts));

  // the changes note and Undo
  const finished = mkRun("d1", "completed", { kind: "edit_files", summary: "Applied.", changes: { ...CHANGES } });
  await boot([finished], { d1: [ev(1, "run_started", "x")] });
  check("a finished run with changes lists the files and offers Undo", get("run-changes").hidden === false && get("run-changes").textContent === "Changes written to: src/app.py, src/util.py" && get("undo-button").hidden === false && get("cancel-button").hidden === true && get("delete-button").hidden === false);

  confirmAnswer = false; await get("undo-button").listeners.click[0]();
  check("declining the confirmation undoes nothing", confirms.length === 1 && /Files you have edited since are left as they are/.test(confirms[0]) && !S.calls.some(c => c.includes("/undo")));

  confirmAnswer = true;
  S.undoReply = () => { finished.changes = { ...CHANGES, undone: true }; S.events.d1.push(ev(2, "info", "Undo: restored 1")); return json({ run: finished, undo: { restored: ["src/app.py"], deleted: [], skipped: [] } }); };
  await get("undo-button").listeners.click[0](); await settle();
  check("Undo POSTs, then the page shows the changes as undone", S.calls.includes("POST /runs/d1/undo") && get("undo-button").hidden === true && get("run-changes").textContent === "The changes this run made were undone." && alerts.length === 0, JSON.stringify(alerts));
  check("the undo event appears in the log", eventTitles().includes("Undo: restored 1"));

  const halfway = mkRun("d2", "completed", { changes: { ...CHANGES } });
  await boot([halfway], { d2: [ev(1, "run_started", "x")] });
  S.undoReply = () => { halfway.changes = { ...CHANGES, undone: true }; return json({ run: halfway, undo: { restored: [], deleted: [], skipped: [{ path: "src/app.py", reason: "was changed after ORION wrote it" }] } }); };
  await get("undo-button").listeners.click[0](); await settle();
  check("files left alone because you changed them are reported", /left alone because they changed/.test(alerts[0]) && alerts[0].includes("src/app.py"), alerts[0]);

  const refused = mkRun("d3", "completed", { changes: { ...CHANGES } });
  await boot([refused], { d3: [ev(1, "run_started", "x")] });
  S.undoReply = () => json({ detail: "These changes were already undone." }, 409);
  await get("undo-button").listeners.click[0](); await settle();
  check("a refused undo shows why and the button is usable again", alerts[0] === "These changes were already undone." && get("undo-button").disabled === false, JSON.stringify(alerts));

  for (const [label, r] of [["undone changes", mkRun("n1", "completed", { changes: { ...CHANGES, undone: true } })], ["no changes", mkRun("n2", "completed")], ["a running run", mkRun("n3", "running", { changes: { ...CHANGES } })]]) {
    await boot([r], { [r.id]: [] }); run("clearTimeout(pollTimer)");
    check("no Undo button for " + label, get("undo-button").hidden === true);
  }
  await boot([mkRun("n4", "completed")], { n4: [] });
  check("no changes note for a run that changed nothing", get("run-changes").hidden === true);

  // deleting a run that can still be undone says so
  const kept = mkRun("k1", "completed", { changes: { ...CHANGES } });
  await boot([kept], { k1: [] });
  await get("delete-button").listeners.click[0]();
  check("deleting warns that it removes the way to undo", /no longer be undone/.test(confirms[0]), confirms[0]);

  const calm = mkRun("k2", "completed", { changes: { ...CHANGES, undone: true } });
  await boot([calm], { k2: [] });
  await get("delete-button").listeners.click[0]();
  check("deleting after an undo needs no such warning", confirms[0] === "Delete this run and its log?", confirms[0]);

  console.log(failures ? failures + " FAILED" : "ALL PASSED");
  process.exit(failures ? 1 : 0);
})();
