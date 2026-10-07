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

  console.log(failures ? failures + " FAILED" : "ALL PASSED"); process.exit(failures ? 1 : 0);
})();
