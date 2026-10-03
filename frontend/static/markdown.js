/*
 * Minimal, dependency-free Markdown renderer for ORION chat replies.
 *
 * Safety: ALL text is HTML-escaped before any tag is produced, and the only
 * tags ever emitted are the fixed ones below. Links are limited to http(s).
 * The model's output can therefore never inject HTML or script into the page.
 *
 * Supports: paragraphs, headings, **bold**, *italic*, `code`, fenced code
 * blocks (with a Copy button), bullet/numbered/nested lists, blockquotes,
 * tables, horizontal rules and [links](https://...). Unclosed constructs
 * (normal while a reply is still streaming) render as far as they go.
 */
(function (root) {
    "use strict";

    function escapeHtml(text) {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ---- inline ---------------------------------------------------------

    function emphasis(escaped) {
        return escaped
            .replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>")
            .replace(/(^|[^*])\*([^*\s][^*\n]*?)\*(?!\*)/g, "$1<em>$2</em>");
    }

    function renderInline(text) {
        const stash = [];

        function hold(html) {
            stash.push(html);
            return "\u0000" + (stash.length - 1) + "\u0000";
        }

        // Code spans first: nothing inside them is formatted.
        let work = text
            .replace(/\u0000/g, "")
            .replace(/`([^`\n]+)`/g, (_, code) =>
                hold("<code>" + escapeHtml(code) + "</code>")
            );

        work = escapeHtml(work);

        // Links next, so emphasis characters in a URL can't corrupt the href.
        work = work.replace(
            /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
            (_, label, url) =>
                hold(
                    '<a href="' + url + '" target="_blank" ' +
                    'rel="noopener noreferrer">' + emphasis(label) + "</a>"
                )
        );

        work = emphasis(work);

        // Placeholders may be nested (code inside a link label).
        let previous;

        do {
            previous = work;
            work = work.replace(/\u0000(\d+)\u0000/g, (_, n) => stash[Number(n)]);
        } while (work !== previous);

        return work;
    }

    // ---- blocks ---------------------------------------------------------

    const FENCE_OPEN = /^\s*(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
    const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
    const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
    const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(\S.*)$/;
    const QUOTE = /^\s*>/;
    const TABLE_SEPARATOR = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

    function indentOf(line) {
        return line.match(/^[ \t]*/)[0].replace(/\t/g, "    ").length;
    }

    function isTableStart(lines, i) {
        return (
            i + 1 < lines.length &&
            lines[i].includes("|") &&
            TABLE_SEPARATOR.test(lines[i + 1]) &&
            lines[i + 1].includes("-")
        );
    }

    function isBlockStart(lines, i) {
        const line = lines[i];

        return (
            FENCE_OPEN.test(line) ||
            HEADING.test(line) ||
            RULE.test(line) ||
            LIST_ITEM.test(line) ||
            QUOTE.test(line) ||
            isTableStart(lines, i)
        );
    }

    function renderCodeBlock(language, codeLines) {
        const label = language.replace(/[^A-Za-z0-9+#._-]/g, "").slice(0, 20);

        return (
            '<div class="code-block"><div class="code-header">' +
            '<span class="code-lang">' + escapeHtml(label || "code") + "</span>" +
            '<button type="button" class="code-copy">Copy</button></div>' +
            "<pre><code>" + escapeHtml(codeLines.join("\n")) + "</code></pre></div>"
        );
    }

    function splitRow(line) {
        let row = line.trim();

        if (row.startsWith("|")) row = row.slice(1);
        if (row.endsWith("|")) row = row.slice(0, -1);

        return row.split("|").map((cell) => cell.trim());
    }

    function renderTable(headerLine, separatorLine, bodyLines) {
        const aligns = splitRow(separatorLine).map((cell) => {
            const left = cell.startsWith(":");
            const right = cell.endsWith(":");

            if (left && right) return "center";
            if (right) return "right";
            return left ? "left" : "";
        });

        function cell(tag, text, index) {
            const align = aligns[index] ? ' class="align-' + aligns[index] + '"' : "";

            return "<" + tag + align + ">" + renderInline(text) + "</" + tag + ">";
        }

        const head = splitRow(headerLine).map((t, i) => cell("th", t, i)).join("");
        const body = bodyLines
            .map((line) => "<tr>" + splitRow(line).map((t, i) => cell("td", t, i)).join("") + "</tr>")
            .join("");

        return (
            '<div class="table-wrap"><table><thead><tr>' + head + "</tr></thead>" +
            "<tbody>" + body + "</tbody></table></div>"
        );
    }

    function buildList(items, start) {
        const base = items[start].indent;
        const ordered = items[start].ordered;
        const tag = ordered ? "ol" : "ul";
        const startAttr =
            ordered && items[start].number !== 1
                ? ' start="' + items[start].number + '"'
                : "";

        let html = "<" + tag + startAttr + ">";
        let index = start;

        while (index < items.length) {
            const item = items[index];

            if (item.indent < base || (item.indent === base && item.ordered !== ordered)) {
                break;
            }

            let content = renderInline(item.text);

            index += 1;

            while (index < items.length && items[index].indent > base) {
                const nested = buildList(items, index);

                content += nested.html;
                index = nested.index;
            }

            html += "<li>" + content + "</li>";
        }

        return { html: html + "</" + tag + ">", index };
    }

    function renderBlocks(source) {
        const lines = source.replace(/\r\n?/g, "\n").split("\n");
        const out = [];

        let i = 0;

        while (i < lines.length) {
            const line = lines[i];

            if (line.trim() === "") {
                i += 1;
                continue;
            }

            // Fenced code block (an unclosed fence runs to the end, which is
            // exactly what is wanted while the reply is still streaming).
            const fence = line.match(FENCE_OPEN);

            if (fence) {
                const marker = fence[1][0];
                const closer = new RegExp("^\\s*" + marker + "{" + fence[1].length + ",}\\s*$");
                const code = [];

                i += 1;

                while (i < lines.length && !closer.test(lines[i])) {
                    code.push(lines[i]);
                    i += 1;
                }

                i += 1; // skip the closing fence if there is one
                out.push(renderCodeBlock(fence[2], code));
                continue;
            }

            const heading = line.match(HEADING);

            if (heading) {
                const level = heading[1].length;

                out.push("<h" + level + ">" + renderInline(heading[2]) + "</h" + level + ">");
                i += 1;
                continue;
            }

            if (RULE.test(line)) {
                out.push("<hr>");
                i += 1;
                continue;
            }

            if (QUOTE.test(line)) {
                const quoted = [];

                while (i < lines.length && QUOTE.test(lines[i])) {
                    quoted.push(lines[i].replace(/^\s*> ?/, ""));
                    i += 1;
                }

                out.push("<blockquote>" + renderBlocks(quoted.join("\n")) + "</blockquote>");
                continue;
            }

            if (isTableStart(lines, i)) {
                const header = lines[i];
                const separator = lines[i + 1];
                const body = [];

                i += 2;

                while (i < lines.length && lines[i].trim() !== "" && lines[i].includes("|")) {
                    body.push(lines[i]);
                    i += 1;
                }

                out.push(renderTable(header, separator, body));
                continue;
            }

            if (LIST_ITEM.test(line)) {
                const items = [];

                while (i < lines.length) {
                    const match = lines[i].match(LIST_ITEM);

                    if (match) {
                        const marker = match[2];

                        items.push({
                            indent: indentOf(match[1]),
                            ordered: /\d/.test(marker),
                            number: parseInt(marker, 10),
                            text: match[3],
                        });
                        i += 1;
                    } else if (
                        lines[i].trim() === "" &&
                        i + 1 < lines.length &&
                        LIST_ITEM.test(lines[i + 1])
                    ) {
                        i += 1; // a blank line between items keeps the list going
                    } else if (
                        lines[i].trim() !== "" &&
                        indentOf(lines[i]) >= 2 &&
                        !isBlockStart(lines, i) &&
                        items.length > 0
                    ) {
                        items[items.length - 1].text += " " + lines[i].trim(); // wrapped item text
                        i += 1;
                    } else {
                        break;
                    }
                }

                let index = 0;

                while (index < items.length) {
                    const built = buildList(items, index);

                    out.push(built.html);
                    index = built.index;
                }

                continue;
            }

            // Paragraph: consecutive plain lines.
            const paragraph = [line];

            i += 1;

            while (i < lines.length && lines[i].trim() !== "" && !isBlockStart(lines, i)) {
                paragraph.push(lines[i]);
                i += 1;
            }

            out.push("<p>" + paragraph.map(renderInline).join("<br>") + "</p>");
        }

        return out.join("");
    }

    const api = { renderMarkdown: renderBlocks, escapeHtml: escapeHtml };

    if (typeof module !== "undefined" && module.exports) {
        module.exports = api;
    } else {
        root.renderMarkdown = renderBlocks;
    }
})(typeof globalThis !== "undefined" ? globalThis : window);
