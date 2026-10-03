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

  // 10 banner
  const banner = s => { run(`renderBanner(${JSON.stringify(s)})`); return [get("ai-banner").hidden, get("ai-banner").className, get("ai-banner").textContent]; };
  let r = banner({ provider: "llama", state: "loading" }); check("banner: loading", !r[0] && r[1] === "ai-banner info");
  r = banner({ provider: "llama", state: "ready" }); check("banner: ready hidden", r[0] === true);
  r = banner({ provider: "llama", state: "error", detail: "boom" }); check("banner: error", r[1] === "ai-banner error" && /boom/.test(r[2]));
  r = banner({ provider: "echo", state: "ready", detail: "Echo mode (no AI model)." }); check("banner: echo note", !r[0] && /Echo/.test(r[2]));

  console.log(failures ? failures + " FAILED" : "ALL PASSED"); process.exit(failures ? 1 : 0);
})();
