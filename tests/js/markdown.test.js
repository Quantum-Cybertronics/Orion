// Run with:  node tests/js/markdown.test.js
const assert = require("assert");
const path = require("path");
const { renderMarkdown: md } = require(path.join(__dirname, "../../frontend/static/markdown.js"));

let failed = 0;
function test(name, fn) {
    try { fn(); console.log("PASS " + name); }
    catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message.split("\n").join("\n     ")); }
}
const has = (html, s) => assert.ok(html.includes(s), `expected ${JSON.stringify(s)} in ${html}`);
const lacks = (html, s) => assert.ok(!html.includes(s), `did not expect ${JSON.stringify(s)} in ${html}`);

// ---- safety -------------------------------------------------------------
test("raw HTML is escaped", () => {
    const h = md("<script>alert(1)</script> <img src=x onerror=alert(1)>");
    lacks(h, "<script"); lacks(h, "<img"); has(h, "&lt;script&gt;");
});
test("javascript: and data: links are not linked", () => {
    lacks(md("[x](javascript:alert(1))"), "<a ");
    lacks(md("[x](data:text/html;base64,AAAA)"), "<a ");
    lacks(md("[x](JaVaScRiPt:alert(1))"), "<a ");
});
test("quotes in a URL cannot break out of the href", () => {
    const h = md('[x](https://a.com/"onmouseover="alert(1))');
    lacks(h, '" onmouseover'); lacks(h, '"onmouseover="alert');
});
test("https links open safely in a new tab", () => {
    has(md("[docs](https://example.com/a?b=1&c=2)"), 'href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a>');
});
test("emphasis characters in a URL do not corrupt the href", () => {
    has(md("[a](https://x.com/*b*)"), 'href="https://x.com/*b*"');
});
test("language label is sanitised", () => {
    const h = md("```\"><script>alert(1)</script>\ncode\n```");
    lacks(h, "<script");
});
test("fuzz: only known tags are ever emitted", () => {
    const frags = ["<", ">", "&", '"', "'", "`", "```", "~~~", "*", "**", "_", "[", "]", "(", ")", "https://a.b/c", "javascript:x", "|", "---", ":--:", "#", "> ", "- ", "1. ", "  ", "\n", "\n\n", "x", "<script>", "</p>", "onerror=", "\u0000", "\t"];
    const allowed = [
        /<\/?(p|br|strong|em|code|pre|ul|ol|li|blockquote|hr|table|thead|tbody|tr|h[1-6])>/g,
        /<(th|td)( class="align-(left|right|center)")?>/g, /<\/(th|td)>/g,
        /<ol start="\d+">/g,
        /<div class="(code-block|code-header|table-wrap)">/g, /<\/div>/g,
        /<span class="code-lang">/g, /<\/span>/g,
        /<button type="button" class="code-copy">/g, /<\/button>/g,
        /<a href="https?:\/\/[^"<>\s]*" target="_blank" rel="noopener noreferrer">/g, /<\/a>/g,
    ];
    let seed = 12345; const rnd = n => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) % n;
    for (let n = 0; n < 4000; n++) {
        let s = ""; for (let k = rnd(40); k >= 0; k--) s += frags[rnd(frags.length)];
        let h = md(s); for (const re of allowed) h = h.replace(re, "");
        assert.ok(!/[<>]/.test(h) && !h.includes('"'), "unexpected markup from input " + JSON.stringify(s) + " -> " + h);
    }
});

// ---- inline -------------------------------------------------------------
test("bold, italic, inline code", () => {
    has(md("a **b** *c* `d`"), "<strong>b</strong>");
    has(md("a **b** *c* `d`"), "<em>c</em>");
    has(md("a **b** *c* `d`"), "<code>d</code>");
});
test("code spans are escaped and unformatted", () => {
    const h = md("`x<y **no**`");
    has(h, "<code>x&lt;y **no**</code>"); lacks(h, "<strong>");
});
test("snake_case and math are left alone", () => {
    has(md("use my_var_name here"), "my_var_name"); lacks(md("use my_var_name here"), "<em>");
    lacks(md("2 * 3 * 4"), "<em>");
});
test("unclosed emphasis stays literal (streaming)", () => {
    has(md("this is **half"), "**half"); lacks(md("this is **half"), "<strong>");
});
test("single newlines become <br>, blank lines split paragraphs", () => {
    assert.strictEqual(md("a\nb\n\nc"), "<p>a<br>b</p><p>c</p>");
});

// ---- blocks -------------------------------------------------------------
test("headings", () => {
    has(md("# One\n### Three"), "<h1>One</h1>"); has(md("# One\n### Three"), "<h3>Three</h3>");
});
test("bullet and numbered lists, numbering start kept", () => {
    assert.strictEqual(md("- a\n- b"), "<ul><li>a</li><li>b</li></ul>");
    assert.strictEqual(md("1. a\n2. b"), "<ol><li>a</li><li>b</li></ol>");
    has(md("3. c\n4. d"), '<ol start="3">');
});
test("nested lists", () => {
    assert.strictEqual(md("- a\n  - b\n    - c\n- d"), "<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>");
});
test("blank line between items keeps one list", () => {
    assert.strictEqual(md("1. a\n\n2. b"), "<ol><li>a</li><li>b</li></ol>");
});
test("'* ' lists work and '**bold**' lines are not lists", () => {
    has(md("* a\n* b"), "<ul>"); lacks(md("**bold** line"), "<ul>");
});
test("horizontal rule", () => {
    has(md("a\n\n---\n\nb"), "<hr>"); has(md("* * *"), "<hr>");
});
test("blockquote", () => {
    has(md("> hello\n> **there**"), "<blockquote><p>hello<br><strong>there</strong></p></blockquote>");
});
test("table with alignment", () => {
    const h = md("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |");
    has(h, '<th class="align-left">a</th>'); has(h, '<th class="align-center">b</th>');
    has(h, '<td class="align-right">3</td>'); has(h, '<div class="table-wrap">');
});
test("pipe in a plain paragraph is not a table", () => {
    lacks(md("a | b"), "<table");
});

// ---- code blocks --------------------------------------------------------
test("fenced code: language, copy button, escaped, unformatted", () => {
    const h = md("```python\nprint('<hi>')  # **x**\n```");
    has(h, '<span class="code-lang">python</span>'); has(h, 'class="code-copy"');
    has(h, "<pre><code>print(&#39;&lt;hi&gt;&#39;)  # **x**</code></pre>"); lacks(h, "<strong>");
});
test("code keeps indentation and blank lines", () => {
    has(md("```\ndef f():\n\n    return 1\n```"), "def f():\n\n    return 1");
});
test("unclosed fence still renders a block (streaming)", () => {
    const h = md("Here:\n```js\nlet a = 1;");
    has(h, "<pre><code>let a = 1;</code></pre>"); has(h, "<p>Here:</p>");
});
test("tilde fences and longer fences", () => {
    has(md("~~~\nx\n~~~"), "<pre><code>x</code></pre>");
    has(md("````\n```\nx\n```\n````"), "<pre><code>```\nx\n```</code></pre>");
});
test("text after a closed fence continues normally", () => {
    const h = md("```\na\n```\nafter **it**");
    has(h, "<p>after <strong>it</strong></p>");
});

// ---- streaming ----------------------------------------------------------
test("every prefix of a rich reply renders without throwing", () => {
    const text = "# T\n\nSome **bold** text\n\n- a\n  - b\n\n1. x\n2. y\n\n```python\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> q\n\n[l](https://a.b)";
    for (let n = 0; n <= text.length; n++) md(text.slice(0, n));
});
test("realistic model answer (list + code)", () => {
    const h = md("Here's a breakdown:\n\n* **`array` module:** Python's functionality.\n* **Example:**\n\n```python\nimport array\nmy = array.array('i', [1, 2])\n```\n\nThe `'i'` means integer.");
    has(h, "<li><strong><code>array</code> module:</strong> Python&#39;s functionality.</li>");
    has(h, "import array\nmy = array.array(&#39;i&#39;, [1, 2])"); has(h, "<code>&#39;i&#39;</code>");
});

console.log(failed ? `${failed} FAILED` : "ALL PASSED");
process.exit(failed ? 1 : 0);
