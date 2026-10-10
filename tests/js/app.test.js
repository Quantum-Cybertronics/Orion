// Run with:  node tests/js/app.test.js
// Loads the real frontend/static/{markdown,app}.js into a tiny fake DOM.
const fs = require("fs"), vm = require("vm"), path = require("path");
const read = f => fs.readFileSync(path.join(__dirname, "../../frontend/static", f), "utf8");

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.parent = null; this._text = ""; this._html = ""; this.className = ""; this.dataset = {}; this.style = {};
    this.hidden = false; this.disabled = false; this.scrollTop = 0; this.scrollHeight = 1000; this.clientHeight = 500; this.value = ""; this.placeholder = ""; this.listeners = {};
    const self = this;
    this.classList = { add: c => { if (!self.className.split(" ").includes(c)) self.className = (self.className + " " + c).trim(); },
      toggle: (c, on) => { const has = self.className.split(" ").includes(c); if (on && !has) self.classList.add(c); if (!on && has) self.className = self.className.split(" ").filter(x => x !== c).join(" "); },
      remove: c => { self.className = self.className.split(" ").filter(x => x !== c).join(" "); },
      contains: c => self.className.split(" ").includes(c) }; }
  set textContent(v) { this._text = String(v); this.children = []; this._html = ""; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set innerHTML(v) { this.children = []; this._text = ""; this._html = v; }
  get innerHTML() { return this._html; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
  querySelector(sel) { const cls = sel.slice(1); const walk = n => { for (const c of n.children) { if (c.classList.contains(cls)) return c; const r = walk(c); if (r) return r; } return null; }; return walk(this); }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  focus() {} select() {}
  setAttribute(k, v) { this.attrs = this.attrs || {}; this.attrs[k] = v; }
}
const ids = {}; const get = id => (ids[id] ||= new El("div"));
const bodyEl = new El("body");
const document = { getElementById: get, createElement: t => new El(t), createTextNode: t => { const e = new El("#text"); e._text = t; return e; }, querySelectorAll: () => [], addEventListener() {}, body: bodyEl, execCommand() {} };
const alerts = [], copied = []; const loc = { href: "" };
let fetchImpl;
const realSetTimeout = setTimeout;
const ctx = { document, alert: m => alerts.push(m), console, TextDecoder, AbortController, setTimeout: realSetTimeout, clearTimeout,
  window: { location: loc, isSecureContext: true, requestAnimationFrame: cb => realSetTimeout(cb, 0) },
  navigator: { clipboard: { writeText: async t => { copied.push(t); } } },
  fetch: (...a) => fetchImpl(...a) };
ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(read("markdown.js"), ctx);
let renders = 0; const realRender = ctx.renderMarkdown; ctx.renderMarkdown = t => { renders++; return realRender(t); };
vm.runInContext(read("app.js").replace(/\ninitialize\(\);\s*$/, "\n"), ctx);
const run = c => vm.runInContext(c, ctx);

const enc = new TextEncoder();
function sse(events, splitAt = 7) {
  const bytes = enc.encode(events.map(e => "data: " + JSON.stringify(e) + "\n\n").join(""));
  return new ReadableStream({ start(c) { for (let i = 0; i < bytes.length; i += splitAt) c.enqueue(bytes.slice(i, i + splitAt)); c.close(); } });
}
const okResponse = body => ({ ok: true, status: 200, body });
const history = async () => ({ ok: true, status: 200, json: async () => [] });
function reset(impl) { get("messages").children = []; get("messages")._text = ""; bodyEl.className = ""; fetchImpl = impl; alerts.length = 0; copied.length = 0; renders = 0; get("message-input").value = ""; run("activeConversationId = 'c1'"); }
const msg = i => get("messages").children[i];
const content = i => msg(i).querySelector(".message-content");
const body = i => content(i).querySelector(".markdown");
let failures = 0; const check = (name, ok, info = "") => { console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "  -> " + info)); if (!ok) failures++; };
const streamRoute = events => async url => url.endsWith("/stream") ? okResponse(events) : history();

(async () => {
  // 1 happy path: markdown rendered; user text stays plain text
  reset(streamRoute(sse([{ type: "user_message" }, { type: "delta", content: "Hello **wor" }, { type: "delta", content: "ld** and `x<y`" }, { type: "done" }])));
  await run("streamReply('<b>hi</b> there')");
  check("user bubble: plain text, not interpreted as HTML", msg(0).className === "message user" && content(0).textContent === "<b>hi</b> there" && content(0).innerHTML === "");
  check("assistant reply rendered as markdown", body(1).innerHTML === "<p>Hello <strong>world</strong> and <code>x&lt;y</code></p>", body(1).innerHTML);
  check("controls restored", get("send-button").textContent === "Send" && !get("message-input").disabled && !bodyEl.classList.contains("streaming"));

  // 2 many fast deltas are batched into few renders, final result complete
  const deltas = Array.from({ length: 300 }, (_, i) => ({ type: "delta", content: `w${i} ` }));
  reset(streamRoute(new ReadableStream({ start(c) { c.enqueue(enc.encode([{ type: "user_message" }, ...deltas, { type: "done" }].map(e => "data: " + JSON.stringify(e) + "\n\n").join(""))); c.close(); } })));
  await run("streamReply('go')");
  check("300 deltas in one burst -> few renders", renders <= 5, "renders=" + renders);
  check("final render has every word", body(1).innerHTML.includes("w0 ") && body(1).innerHTML.includes("w299"));

  // 3 half-finished code block still renders as a block when the stream errors
  reset(streamRoute(sse([{ type: "user_message" }, { type: "delta", content: "Try:\n```py\nx = 1" }, { type: "error", detail: "model crashed" }])));
  await run("streamReply('x')");
  check("open code fence rendered as a code block", body(1).innerHTML.includes('<pre><code>x = 1</code></pre>') && body(1).innerHTML.includes("<p>Try:</p>"), body(1).innerHTML);
  check("error note kept after body, bubble marked failed", content(1).children.length === 2 && content(1).children[1].textContent === "model crashed" && content(1).className.includes("failed"));

  // 4 history: assistant markdown rendered, HTML in a reply is neutralised
  reset(history);
  run("renderMessage({role:'assistant', content:'# Hi\\n<img src=x onerror=alert(1)>\\n```\\ncode\\n```'})");
  check("loaded assistant message rendered + escaped", body(0).innerHTML.includes("<h1>Hi</h1>") && !body(0).innerHTML.includes("<img") && body(0).innerHTML.includes("&lt;img"), body(0).innerHTML);
  run("renderMessage({role:'user', content:'**not bold**'})");
  check("loaded user message not markdown-rendered", content(1).textContent === "**not bold**");

  // 5 copy button
  const codeEl = new El("code"); codeEl._text = "print('hi')\nprint(2)";
  const block = new El("div"); block.querySelector = s => (s === "code" ? codeEl : null);
  const button = new El("button"); button._text = "Copy";
  button.closest = s => (s === ".code-copy" ? button : s === ".code-block" ? block : null);
  await get("messages").listeners.click[0]({ target: button });
  check("copy button copies the code text", copied[0] === "print('hi')\nprint(2)" && button.textContent === "Copied", JSON.stringify([copied, button.textContent]));
  await get("messages").listeners.click[0]({ target: { closest: () => null } });
  check("clicking elsewhere does nothing", copied.length === 1);

  // 6 waiting state: user prompt visible + Stop button before the reply arrives
  let seen;
  reset(async url => { if (url.endsWith("/stream")) { seen = [get("messages").children.length, bodyEl.classList.contains("streaming"), get("send-button").textContent, body(1).innerHTML.includes("typing")]; return okResponse(sse([{ type: "user_message" }, { type: "done" }])); } return history(); });
  await run("streamReply('first!')");
  check("prompt visible, typing dots, Stop button while waiting", JSON.stringify(seen) === '[2,true,"Stop",true]', JSON.stringify(seen));
  check("empty reply -> note instead of blank bubble", content(1).children[1].textContent === "No response was produced.");

  // 7 request rejected before saving
  reset(async () => ({ ok: false, status: 404, json: async () => ({ detail: "Conversation not found." }) }));
  await run("streamReply('keep me')");
  check("404: bubbles removed, input restored, alert", get("messages").children.length === 0 && get("message-input").value === "keep me" && alerts[0] === "Conversation not found.");
  reset(async () => ({ ok: false, status: 401, json: async () => ({}) }));
  await run("streamReply('x')"); check("401 -> /login", loc.href === "/login");

  // 8 Stop mid-reply keeps the partial (rendered) text
  reset(async (url, opts) => {
    if (!url.endsWith("/stream")) return history();
    return okResponse(new ReadableStream({ start(c) { c.enqueue(enc.encode('data: {"type":"user_message"}\n\ndata: {"type":"delta","content":"**abc"}\n\n'));
      opts.signal.addEventListener("abort", () => c.error(Object.assign(new Error("aborted"), { name: "AbortError" }))); } }));
  });
  const pending = run("streamReply('long one')");
  await new Promise(r => realSetTimeout(r, 30));
  await get("message-form").listeners.submit[0]({ preventDefault() {} });
  await pending;
  check("Stop keeps partial text and adds 'Stopped.'", body(1).innerHTML === "<p>**abc</p>" && content(1).children[1].textContent === "Stopped.", body(1).innerHTML);
  check("Stop restores controls", get("send-button").textContent === "Send" && !get("message-input").disabled);

  // 9 dropped connection
  reset(async url => url.endsWith("/stream") ? okResponse(new ReadableStream({ start(c) { c.enqueue(enc.encode('data: {"type":"user_message"}\n\ndata: {"type":"delta","content":"hi"}\n\n')); realSetTimeout(() => c.error(new TypeError("network")), 20); } })) : history());
  await run("streamReply('x')");
  check("dropped connection: user msg kept, note shown", get("messages").children.length === 2 && content(1).children[1].textContent.includes("Connection lost") && body(1).innerHTML === "<p>hi</p>");


  // 11 attached files ---------------------------------------------------
  const jsonRes = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  const calls = [];
  const strip = () => get("file-strip");
  const chip = i => strip().children[i];
  const chipPart = (i, cls) => chip(i).querySelector("." + cls);
  let serverFiles = [];
  const fileApi = async (url, opts = {}) => {
    const method = opts.method || "GET"; calls.push([method, url, opts.body, opts.headers]);
    if (url === "/conversations/" && method === "POST") return jsonRes({ id: "new1" });
    if (url === "/conversations/") return jsonRes([]);
    if (method === "POST") return jsonRes({ id: "f" + (serverFiles.length + 1) }, 201);
    if (method === "DELETE") { serverFiles = []; return { ok: true, status: 204 }; }
    return jsonRes(serverFiles);
  };
  const fileObj = name => ({ name, size: 10 });
  const give = (name, o) => { ctx[name] = o; };

  reset(fileApi); calls.length = 0; serverFiles = [{ id: "f1", filename: "story one.txt", token_estimate: 1500 }]; run("renderFiles([])");
  const f1 = fileObj("story one.txt"); give("__f1", f1);
  await run("uploadFiles([__f1])");
  check("upload: raw body POST with filename in the URL",
    calls[0][0] === "POST" && calls[0][1] === "/conversations/c1/attachments/?filename=story%20one.txt" && calls[0][2] === f1 && calls[0][3]["Content-Type"] === "application/octet-stream", JSON.stringify(calls[0].slice(0, 2)));
  check("upload: list reloaded afterwards", calls[1][0] === "GET" && calls[1][1] === "/conversations/c1/attachments/");
  check("chip shows name and token estimate, strip visible",
    !strip().hidden && chipPart(0, "file-name").textContent === "story one.txt" && chipPart(0, "file-meta").textContent === "~1.5k tokens" && get("chat").classList.contains("has-files"));
  check("attach button re-enabled after upload", !get("attach-button").disabled && !get("attach-button").classList.contains("busy"));

  // server rejects the file (too big for the model's context)
  reset(async (url, opts = {}) => opts.method === "POST" ? jsonRes({ detail: "This file is about 9,000 tokens, but only about 2,000 more fit." }, 413) : jsonRes([]));
  run("renderFiles([])"); give("__f2", fileObj("big.txt"));
  await run("uploadFiles([__f2])");
  check("413: server's explanation shown with the file name, strip unchanged", alerts[0] === "big.txt: This file is about 9,000 tokens, but only about 2,000 more fit." && strip().hidden);

  // no conversation yet: one is created first
  reset(fileApi); calls.length = 0; run("activeConversationId = null"); serverFiles = [];
  give("__f3", fileObj("a.txt")); await run("uploadFiles([__f3])");
  check("no active conversation: creates one, then uploads to it",
    calls[0][0] === "POST" && calls[0][1] === "/conversations/" && calls.some(c => c[0] === "POST" && c[1] === "/conversations/new1/attachments/?filename=a.txt"), JSON.stringify(calls.map(c => c[0] + " " + c[1])));

  // remove a file
  reset(fileApi); calls.length = 0; serverFiles = [{ id: "f1", filename: "s.txt", token_estimate: 40 }];
  run("renderFiles([{id:'f1',filename:'s.txt',token_estimate:40}])");
  chipPart(0, "file-remove").listeners.click[0]();            // fire-and-forget, like a real click
  await new Promise(r => realSetTimeout(r, 30));
  check("remove: DELETE sent, strip hidden when empty",
    calls[0][0] === "DELETE" && calls[0][1] === "/conversations/c1/attachments/f1" && strip().hidden && !get("chat").classList.contains("has-files"), JSON.stringify(calls.map(c => c[0] + " " + c[1])));

  // 401 redirects
  reset(async () => jsonRes({}, 401)); loc.href = ""; give("__f4", fileObj("x.txt"));
  await run("uploadFiles([__f4])"); check("upload 401 -> /login", loc.href === "/login");

  // drag and drop: several files, uploaded in order
  reset(fileApi); calls.length = 0; serverFiles = [];
  let prevented = 0; await get("chat").listeners.dragover[0]({ preventDefault() { prevented++; } });
  check("dragover allows the drop and shows the overlay", prevented === 1 && get("chat").classList.contains("dragging"));
  await get("chat").listeners.drop[0]({ preventDefault() { prevented++; }, dataTransfer: { files: [fileObj("one.md"), fileObj("two.pdf")] } });
  const posts = calls.filter(c => c[0] === "POST").map(c => c[1]);
  check("drop: files uploaded in order, overlay hidden",
    JSON.stringify(posts) === JSON.stringify(["/conversations/c1/attachments/?filename=one.md", "/conversations/c1/attachments/?filename=two.pdf"]) && !get("chat").classList.contains("dragging"), JSON.stringify(posts));

  // not while a reply is streaming
  reset(fileApi); calls.length = 0; run("setStreaming(true)");
  check("attach button disabled while streaming", get("attach-button").disabled === true);
  run("streamController = {}"); give("__f5", fileObj("late.txt")); await run("uploadFiles([__f5])");
  check("uploads ignored while streaming", calls.length === 0);
  run("streamController = null; setStreaming(false)");
  check("attach button back after streaming", get("attach-button").disabled === false);

  // untrusted file names never become HTML
  run("renderFiles([{id:'x',filename:'<img src=x onerror=alert(1)>.txt',token_estimate:5}])");
  check("file name rendered as text, not HTML", chipPart(0, "file-name").textContent === "<img src=x onerror=alert(1)>.txt" && chipPart(0, "file-name").innerHTML === "");

  // a late answer for a conversation the user already left is ignored
  reset(async () => jsonRes([{ id: "z", filename: "stale.txt", token_estimate: 1 }])); run("renderFiles([])");
  await run("loadFiles('some-other-conversation')");
  check("stale file list ignored", strip().hidden === true);


  // 12 deleting history -------------------------------------------------
  const list = () => get("conversation-list");
  const row = i => list().children[i];
  const selectBtn = i => row(i).children[0];
  const trashBtn = i => row(i).children[1];
  const settle = (ms = 25) => new Promise(r => realSetTimeout(r, ms));
  let convs, dcalls, deleteStatus, bulkResult, bulkStatus;
  const delApi = async (url, opts = {}) => {
    const method = opts.method || "GET"; dcalls.push(method + " " + url);
    if (method === "DELETE" && url.startsWith("/conversations/?")) {
      if (bulkStatus && bulkStatus !== 200) return jsonRes({ detail: "Give exactly one of older_than_days or scope=all." }, bulkStatus);
      if (url.includes("scope=all")) convs = []; else convs = convs.slice(0, 1);
      return jsonRes(bulkResult);
    }
    if (method === "DELETE") { if (deleteStatus === 200 || deleteStatus === undefined) convs = convs.filter(c => url !== "/conversations/" + c.id); return deleteStatus && deleteStatus !== 200 ? jsonRes({ detail: "boom" }, deleteStatus) : jsonRes({ status: "deleted" }); }
    if (url === "/conversations/") return jsonRes(convs);
    if (url.endsWith("/messages/")) return jsonRes([]);
    return jsonRes([]);
  };
  const setup = async (list0, active) => { reset(delApi); dcalls = []; deleteStatus = undefined; bulkStatus = undefined; bulkResult = { deleted: 2 }; convs = list0; run("CONFIRM_MS = 3000"); run(`activeConversationId = ${JSON.stringify(active)}`); await run("loadConversations()"); dcalls.length = 0; get("clear-status").textContent = ""; };

  await setup([{ id: "a", title: "First chat" }, { id: "b", title: "<img src=x onerror=alert(1)>" }], "a");
  check("sidebar: a row per conversation (select button + trash button)", list().children.length === 2 && selectBtn(0).textContent === "First chat" && trashBtn(0).textContent === "\u{1F5D1}" && trashBtn(0).title === "Delete conversation");
  check("sidebar: titles are text, never HTML", selectBtn(1).textContent === "<img src=x onerror=alert(1)>" && selectBtn(1).innerHTML === "");

  // two-step confirm
  await trashBtn(1).listeners.click[0]();
  check("trash: first click only arms it (label 'Delete?', no request)", trashBtn(1).textContent === "Delete?" && trashBtn(1).classList.contains("confirming") && dcalls.length === 0);
  await trashBtn(1).listeners.click[0](); await settle();
  check("trash: second click deletes, then the list is refreshed", dcalls[0] === "DELETE /conversations/b" && dcalls.includes("GET /conversations/") && list().children.length === 1);
  check("deleting a different chat keeps the open one", run("activeConversationId") === "a");

  // confirmation times out
  await setup([{ id: "a", title: "A" }, { id: "b", title: "B" }], "a"); run("CONFIRM_MS = 20");
  await trashBtn(1).listeners.click[0](); await settle(70);
  check("arming expires: label restored", trashBtn(1).textContent === "\u{1F5D1}" && !trashBtn(1).classList.contains("confirming"));
  await trashBtn(1).listeners.click[0]();
  check("after expiry the next click arms again instead of deleting", dcalls.length === 0 && trashBtn(1).textContent === "Delete?");

  // deleting the open chat resets the view
  await setup([{ id: "a", title: "A" }, { id: "b", title: "B" }], "a");
  run("renderMessage({role:'user', content:'hello'}); renderFiles([{id:'f',filename:'x.txt',token_estimate:3}])");
  await trashBtn(0).listeners.click[0](); await trashBtn(0).listeners.click[0](); await settle();
  check("deleting the open chat: view reset, no active chat, files strip hidden",
    run("activeConversationId") === null && get("messages").innerHTML.includes("Welcome to ORION") && get("file-strip").hidden === true && list().children.length === 1);

  // already gone (404) is fine; real errors are reported
  await setup([{ id: "a", title: "A" }], "x"); deleteStatus = 404;
  await trashBtn(0).listeners.click[0](); await trashBtn(0).listeners.click[0](); await settle();
  check("404 on delete: no alert, list refreshed", alerts.length === 0 && dcalls.includes("GET /conversations/"));
  await setup([{ id: "a", title: "A" }], "x"); deleteStatus = 500;
  await trashBtn(0).listeners.click[0](); await trashBtn(0).listeners.click[0](); await settle();
  check("server error on delete: message shown, chat still listed", alerts[0] === "boom" && list().children.length === 1);
  await setup([{ id: "a", title: "A" }], "x"); deleteStatus = 401; loc.href = "";
  await trashBtn(0).listeners.click[0](); await trashBtn(0).listeners.click[0](); await settle();
  check("401 on delete -> /login", loc.href === "/login");

  // clear-history panel
  await setup([{ id: "a", title: "A" }, { id: "b", title: "B" }, { id: "c", title: "C" }], "a");
  get("clear-panel").hidden = true;                 // the page ships it with the HTML `hidden` attribute
  check("panel starts closed", get("clear-panel").hidden === true);
  get("clear-toggle").listeners.click[0]();
  check("toggle opens the panel", get("clear-panel").hidden === false && get("clear-toggle").attrs["aria-expanded"] === "true");
  get("clear-toggle").listeners.click[0]();
  check("toggle closes it again", get("clear-panel").hidden === true && get("clear-toggle").attrs["aria-expanded"] === "false");

  get("clear-age").value = "7"; bulkResult = { deleted: 2 };
  await get("clear-older").listeners.click[0]();
  check("older-than: first click only arms it", get("clear-older").textContent === "Click again to confirm" && dcalls.length === 0);
  await get("clear-older").listeners.click[0](); await settle();
  check("older-than: confirmed -> DELETE with the chosen days", dcalls[0] === "DELETE /conversations/?older_than_days=7", dcalls[0]);
  check("older-than: reports the count and refreshes the list", get("clear-status").textContent === "Deleted 2 conversations." && list().children.length === 1 && get("clear-older").textContent === "Delete older chats");
  check("older-than: the open chat survived so it stays open", run("activeConversationId") === "a");

  bulkResult = { deleted: 1 }; await get("clear-older").listeners.click[0](); await get("clear-older").listeners.click[0](); await settle();
  check("singular wording", get("clear-status").textContent === "Deleted 1 conversation.");
  bulkResult = { deleted: 0 }; await get("clear-older").listeners.click[0](); await get("clear-older").listeners.click[0](); await settle();
  check("zero -> 'Nothing to delete.'", get("clear-status").textContent === "Nothing to delete.");

  await setup([{ id: "a", title: "A" }, { id: "b", title: "B" }], "a"); bulkResult = { deleted: 2 };
  run("renderMessage({role:'user', content:'hello'})");
  await get("clear-all").listeners.click[0](); await get("clear-all").listeners.click[0](); await settle();
  check("delete all: DELETE ?scope=all", dcalls[0] === "DELETE /conversations/?scope=all", dcalls[0]);
  check("delete all: sidebar empty and the open chat's view reset", list().children.length === 0 && run("activeConversationId") === null && get("messages").innerHTML.includes("Welcome to ORION"));

  await setup([{ id: "a", title: "A" }], "a"); bulkStatus = 400;
  await get("clear-all").listeners.click[0](); await get("clear-all").listeners.click[0](); await settle();
  check("bulk error: shown in the panel, nothing removed from the list", get("clear-status").textContent === "Give exactly one of older_than_days or scope=all." && list().children.length === 1 && alerts.length === 0);

  // 10 banner
  const banner = s => { run(`renderBanner(${JSON.stringify(s)})`); return [get("ai-banner").hidden, get("ai-banner").className, get("ai-banner").textContent]; };
  let r = banner({ provider: "llama", state: "loading" }); check("banner: loading", !r[0] && r[1] === "ai-banner info");
  r = banner({ provider: "llama", state: "ready" }); check("banner: ready hidden", r[0] === true);
  r = banner({ provider: "llama", state: "error", detail: "boom" }); check("banner: error", r[1] === "ai-banner error" && /boom/.test(r[2]));
  r = banner({ provider: "echo", state: "ready", detail: "Echo mode (no AI model)." }); check("banner: echo note", !r[0] && /Echo/.test(r[2]));

  // 11 model dropdown
  const sel = get("model-select");
  const GB = 1024 ** 3, MB = 1024 ** 2;
  let serverModel = "b.gguf", serverState = "ready", posted = null, postReply = null;
  const st = (extra = {}) => ({ provider: "llama", state: serverState, model: serverModel, can_switch: true,
    models: [{ id: "a.gguf", name: "a", size: 2 * GB }, { id: "b.gguf", name: "b", size: 300 * MB }], ...extra });
  const render = s => run(`modelListKey = ""; renderModels(${JSON.stringify(s)})`);
  const opts = () => sel.children.map(o => o.textContent);

  render(st());
  check("models: one option per model with its size", JSON.stringify(opts()) === JSON.stringify(["a (2.0 GB)", "b (300 MB)"]), opts());
  check("models: running model selected and enabled", sel.value === "b.gguf" && sel.disabled === false);
  render(st({ can_switch: false })); check("models: disabled when switching is unavailable", sel.disabled === true);
  render({ provider: "echo", state: "ready", models: [], can_switch: false });
  check("models: empty folder -> 'No models found', disabled", JSON.stringify(opts()) === JSON.stringify(["No models found"]) && sel.disabled === true);
  render(st({ model: "gone.gguf" })); check("models: a running model missing from the folder is still shown", opts().length === 3 && sel.value === "gone.gguf");

  const children = sel.children;
  run(`renderModels(${JSON.stringify(st({ model: "gone.gguf" }))})`);
  check("models: an unchanged list is not rebuilt (keeps an open dropdown open)", sel.children === children);

  fetchImpl = async (url, opts) => {
    if (url === "/ai/model") { posted = JSON.parse(opts.body); return postReply(); }
    return { ok: true, status: 200, json: async () => st({ model: serverModel }) };
  };

  serverModel = "b.gguf"; render(st());
  postReply = () => { serverModel = "a.gguf"; serverState = "loading"; return { ok: true, status: 200, json: async () => st({ state: "loading", model: "a.gguf" }) }; };
  sel.value = "a.gguf"; await sel.listeners.change[0](); await settle();
  check("switch: POSTs the chosen file name", posted && posted.model === "a.gguf", JSON.stringify(posted));
  check("switch: dropdown shows the new model and the loading banner appears", sel.value === "a.gguf" && /Loading/.test(get("ai-banner").textContent) && /a\.gguf/.test(get("ai-banner").textContent));
  clearTimeout(run("statusTimer")); serverState = "ready";

  serverModel = "a.gguf"; render(st()); alerts.length = 0;
  postReply = () => ({ ok: false, status: 409, json: async () => ({ detail: "ORION is writing a reply right now." }) });
  sel.value = "b.gguf"; await sel.listeners.change[0](); await settle();
  check("switch refused: the reason is shown", alerts[0] === "ORION is writing a reply right now.", alerts[0]);
  check("switch refused: dropdown snaps back to the model that is really loaded", sel.value === "a.gguf" && sel.disabled === false);
  clearTimeout(run("statusTimer"));

  // 13 chat tools: regenerate, search, rename, export --------------------
  const actionRows = () => get("messages").children.map((m, i) => (m.querySelector(".message-actions") ? i : -1)).filter(i => i >= 0);
  const msgsFor = list => async url => (url.endsWith("/messages/") ? jsonRes(list) : jsonRes([]));
  const QA = [{ role: "user", content: "q" }, { role: "assistant", content: "old answer" }];
  const regenBtn = i => msg(i).querySelector(".regenerate");
  let regenUrl = null, regenBody = null, oldWhileStreaming = null, rowsWhileStreaming = null, oldEl = null, fetchCount = 0;
  const regenRoute = respond => async (url, o = {}) => {
    if (url.endsWith("/regenerate")) { regenUrl = url; regenBody = o.body; oldWhileStreaming = oldEl && oldEl.style.display; rowsWhileStreaming = actionRows().length; fetchCount++; return respond(); }
    return jsonRes([]);
  };

  reset(msgsFor(QA)); await run("loadConversation('c1')");
  check("regenerate: button sits under the last message only", JSON.stringify(actionRows()) === "[1]" && regenBtn(1).textContent === "\u21BB Regenerate", JSON.stringify(actionRows()));

  oldEl = msg(1);
  fetchImpl = regenRoute(() => okResponse(sse([{ type: "delta", content: "new **answer**" }, { type: "done" }])));
  await regenBtn(1).listeners.click[0]();
  check("regenerate: POSTs to /regenerate with no new question", regenUrl === "/conversations/c1/messages/regenerate" && regenBody === "{}", regenUrl + " " + regenBody);
  check("regenerate: old answer hidden and buttons gone while streaming", oldWhileStreaming === "none" && rowsWhileStreaming === 0, oldWhileStreaming + " " + rowsWhileStreaming);
  check("regenerate: new answer takes the old one's place", get("messages").children.length === 2 && msg(1) !== oldEl && body(1).innerHTML.includes("<strong>answer</strong>") && msg(0).className === "message user");
  check("regenerate: button moves to the new answer, controls restored", JSON.stringify(actionRows()) === "[1]" && get("send-button").textContent === "Send" && !get("message-input").disabled);

  // 409 / network failure: the old answer comes back and the reason is shown
  reset(msgsFor(QA)); await run("loadConversation('c1')"); oldEl = msg(1);
  fetchImpl = regenRoute(() => jsonRes({ detail: "There is no message to generate a reply for." }, 409));
  await regenBtn(1).listeners.click[0]();
  check("regenerate refused: old answer restored, reason shown", get("messages").children.length === 2 && msg(1) === oldEl && oldEl.style.display !== "none" && alerts[0] === "There is no message to generate a reply for." && JSON.stringify(actionRows()) === "[1]", JSON.stringify([alerts, actionRows()]));

  // model error before any text: server keeps the old answer, so must the screen
  reset(msgsFor(QA)); await run("loadConversation('c1')"); oldEl = msg(1);
  fetchImpl = regenRoute(() => okResponse(sse([{ type: "error", detail: "the model is down" }])));
  await regenBtn(1).listeners.click[0]();
  check("regenerate fails with no text: old answer kept, error shown", get("messages").children.length === 2 && msg(1) === oldEl && oldEl.style.display !== "none" && alerts[0] === "the model is down", JSON.stringify(alerts));

  // partial text then an error: the server saved the partial in place of the old answer
  reset(msgsFor(QA)); await run("loadConversation('c1')"); oldEl = msg(1);
  fetchImpl = regenRoute(() => okResponse(sse([{ type: "delta", content: "half an answer" }, { type: "error", detail: "model crashed" }])));
  await regenBtn(1).listeners.click[0]();
  check("regenerate fails midway: partial answer replaces the old one", get("messages").children.length === 2 && msg(1) !== oldEl && body(1).innerHTML.includes("half an answer") && content(1).children[1].textContent === "model crashed" && alerts.length === 0, JSON.stringify(alerts));

  // Stop with nothing received: old answer restored, no alert
  reset(msgsFor(QA)); await run("loadConversation('c1')"); oldEl = msg(1);
  fetchImpl = async (url, o = {}) => url.endsWith("/regenerate")
    ? okResponse(new ReadableStream({ start(c) { o.signal.addEventListener("abort", () => c.error(Object.assign(new Error("aborted"), { name: "AbortError" }))); } }))
    : jsonRes([]);
  const regenPending = regenBtn(1).listeners.click[0]();
  await settle(30);
  await get("message-form").listeners.submit[0]({ preventDefault() {} });
  await regenPending;
  check("regenerate stopped before any text: old answer back, no alert", get("messages").children.length === 2 && msg(1) === oldEl && oldEl.style.display !== "none" && alerts.length === 0, JSON.stringify(alerts));

  // the reply to the last question was never saved: Regenerate retries it
  reset(msgsFor([{ role: "user", content: "unanswered" }])); await run("loadConversation('c1')");
  check("a chat ending with an unanswered question offers Regenerate", JSON.stringify(actionRows()) === "[0]", JSON.stringify(actionRows()));
  oldEl = null; fetchImpl = regenRoute(() => okResponse(sse([{ type: "delta", content: "fresh" }, { type: "done" }])));
  await regenBtn(0).listeners.click[0]();
  check("retry: reply appears after the question, nothing removed", get("messages").children.length === 2 && msg(0).className === "message user" && content(0).textContent === "unanswered" && body(1).innerHTML.includes("fresh") && JSON.stringify(actionRows()) === "[1]", JSON.stringify(actionRows()));

  // ordinary sends keep exactly one button, on the newest reply
  reset(async url => (url.endsWith("/stream") ? okResponse(sse([{ type: "user_message" }, { type: "delta", content: "ok" }, { type: "done" }])) : history()));
  await run("streamReply('one')"); await run("streamReply('two')");
  check("after two sends only the newest reply has the button", get("messages").children.length === 4 && JSON.stringify(actionRows()) === "[3]", JSON.stringify(actionRows()));

  // not while a reply is streaming
  fetchCount = 0; fetchImpl = regenRoute(() => okResponse(sse([{ type: "done" }]))); run("streamController = {}");
  await regenBtn(3).listeners.click[0](); run("streamController = null");
  check("regenerate ignored while a reply is streaming", fetchCount === 0);

  // ---- search ----------------------------------------------------------
  run("SEARCH_DELAY_MS = 10");
  const sInput = get("search-input");
  const allConvs = [{ id: "a", title: "Alpha" }, { id: "b", title: "Beta" }];
  let searchCalls = [];
  const searchApi = results => async url => {
    searchCalls.push(url);
    if (url.startsWith("/conversations/search")) return jsonRes(await results(url));
    if (url === "/conversations/") return jsonRes(allConvs);
    return jsonRes([]);
  };
  const snippetOf = i => selectBtn(i).children[1];

  reset(searchApi(() => [{ id: "b", title: "Beta", snippet: "\u2026the Zebra crossing plan\u2026" }]));
  sInput.value = "  zebra "; sInput.listeners.input[0](); await settle(60);
  check("search: typing sends one trimmed query after a pause", searchCalls.filter(u => u.startsWith("/conversations/search")).join() === "/conversations/search?q=zebra", searchCalls.join());
  check("search: result row shows title and excerpt, trash button kept", list().children.length === 1 && selectBtn(0).children[0].textContent === "Beta" && trashBtn(0).title === "Delete conversation");
  const mark = snippetOf(0).children.find(c => c.tag === "mark");
  check("search: the match is highlighted, keeping the original case", mark && mark.textContent === "Zebra", snippetOf(0).textContent);

  reset(searchApi(() => [{ id: "x", title: "<b>t</b>", snippet: "<img src=x onerror=alert(1)> zebra" }]));
  sInput.value = "zebra"; sInput.listeners.input[0](); await settle(60);
  check("search: titles and excerpts are text, never HTML", selectBtn(0).children[0].innerHTML === "" && snippetOf(0).innerHTML === "" && snippetOf(0).textContent === "<img src=x onerror=alert(1)> zebra", snippetOf(0).textContent);

  reset(searchApi(() => [])); sInput.value = "nope"; sInput.listeners.input[0](); await settle(60);
  check("search: no matches -> friendly message", list().children.length === 1 && list().children[0].textContent === "No chats match \u201cnope\u201d.", list().children[0] && list().children[0].textContent);

  reset(searchApi(async url => {
    if (url.endsWith("q=ab")) { await settle(120); return [{ id: "a", title: "OLD", snippet: "x" }]; }
    return [{ id: "b", title: "NEW", snippet: "y" }];
  }));
  sInput.value = "ab"; sInput.listeners.input[0](); await settle(40);
  sInput.value = "abc"; sInput.listeners.input[0](); await settle(250);
  check("search: a slow older answer never overwrites a newer one", list().children.length === 1 && selectBtn(0).children[0].textContent === "NEW", list().children[0] && selectBtn(0).children[0].textContent);

  reset(searchApi(() => [])); sInput.value = ""; sInput.listeners.input[0](); await settle(60);
  check("search: clearing the box brings back the full list", list().children.length === 2 && selectBtn(0).textContent === "Alpha" && selectBtn(1).textContent === "Beta");

  reset(searchApi(() => [{ id: "b", title: "Beta", snippet: "zebra" }])); searchCalls = [];
  sInput.value = "zebra"; await run("loadConversations()");
  check("search: reloading the list (e.g. after a rename) keeps the results", list().children.length === 1 && searchCalls.includes("/conversations/") && searchCalls.some(u => u.startsWith("/conversations/search")));

  reset(searchApi(() => [])); sInput.value = "zebra"; sInput.listeners.keydown[0]({ key: "Escape" }); await settle(60);
  check("search: Escape clears the box and restores the list", sInput.value === "" && list().children.length === 2);
  sInput.value = "";

  // ---- rename ----------------------------------------------------------
  const titleEl = get("chat-title");
  let patched = null, serverTitle = "Old name", patchReply = null;
  const renameApi = async (url, o = {}) => {
    if (o.method === "PATCH") { patched = [url, o.body]; return patchReply(); }
    if (url === "/conversations/") return jsonRes([{ id: "c1", title: serverTitle }]);
    return jsonRes([]);
  };
  const editor = () => titleEl.children[0];
  const press = (key) => editor().listeners.keydown[0]({ key, preventDefault() {} });
  const startEdit = () => { get("rename-button").listeners.click[0](); return editor(); };

  reset(renameApi); run("conversationTitles = { c1: 'Old name' }; renaming = false; updateChatHeader()");
  check("header: shows the open chat's title", get("chat-header").hidden === false && titleEl.textContent === "Old name");
  run("activeConversationId = null; updateChatHeader()");
  check("header: hidden when no chat is open", get("chat-header").hidden === true);
  run("activeConversationId = 'c1'; updateChatHeader()");

  patchReply = () => { serverTitle = "Shiny new name"; return jsonRes({ id: "c1", title: "Shiny new name" }); };
  let input = startEdit();
  check("rename: pencil turns the title into a text box holding it", input && input.tag === "input" && input.value === "Old name" && input.maxLength === 100);
  input.value = "  Shiny   new name "; await press("Enter"); await settle();
  check("rename: Enter saves the cleaned title with PATCH", patched && patched[0] === "/conversations/c1" && JSON.parse(patched[1]).title === "Shiny new name", JSON.stringify(patched));
  check("rename: header and sidebar show the new title", titleEl.textContent === "Shiny new name" && titleEl.children.length === 0 && list().children.length === 1 && selectBtn(0).textContent === "Shiny new name");

  patched = null; input = startEdit(); input.value = "zzz"; await press("Escape"); await settle();
  check("rename: Escape cancels without a request", patched === null && titleEl.textContent === "Shiny new name" && titleEl.children.length === 0);

  patched = null; input = startEdit(); await press("Enter"); await settle();
  check("rename: unchanged title sends nothing", patched === null && titleEl.textContent === "Shiny new name");

  patched = null; input = startEdit(); input.value = "   "; await press("Enter"); await settle();
  check("rename: blank title sends nothing and keeps the old one", patched === null && titleEl.textContent === "Shiny new name");

  patchReply = () => jsonRes({ detail: "The title can be at most 100 characters." }, 422);
  input = startEdit(); input.value = "a different name"; await press("Enter"); await settle();
  check("rename refused: reason shown, old title back", alerts[0] === "The title can be at most 100 characters." && titleEl.textContent === "Shiny new name" && titleEl.children.length === 0, JSON.stringify(alerts));

  patchReply = () => { serverTitle = "Via blur"; return jsonRes({ id: "c1", title: "Via blur" }); };
  patched = null; input = startEdit(); input.value = "Via blur"; await input.listeners.blur[0](); await settle();
  check("rename: clicking away saves too", patched && JSON.parse(patched[1]).title === "Via blur" && titleEl.textContent === "Via blur");

  patched = null; titleEl.listeners.dblclick[0]();
  check("rename: double-clicking the title starts editing", editor() && editor().tag === "input");
  await press("Escape");

  // ---- export ----------------------------------------------------------
  const clicked = [];
  El.prototype.click = function () { clicked.push([this.href, this.download, bodyEl.children.includes(this)]); };
  reset(renameApi); const bodyBefore = bodyEl.children.length;
  get("export-button").listeners.click[0]();
  check("export: a download link to the chat's export URL is clicked", clicked.length === 1 && clicked[0][0] === "/conversations/c1/export" && clicked[0][1] === "" && clicked[0][2] === true, JSON.stringify(clicked));
  check("export: the temporary link is removed again", bodyEl.children.length === bodyBefore);
  run("activeConversationId = null"); get("export-button").listeners.click[0]();
  check("export: nothing happens with no chat open", clicked.length === 1);

  console.log(failures ? failures + " FAILED" : "ALL PASSED"); process.exit(failures ? 1 : 0);
})();
