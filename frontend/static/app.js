const logoutButton = document.getElementById("logout-button");
const newConversationButton = document.getElementById("new-conversation");
const conversationList = document.getElementById("conversation-list");

const messagesElement = document.getElementById("messages");
const messageForm = document.getElementById("message-form");
const messageInput = document.getElementById("message-input");
const sendButton = document.getElementById("send-button");
const aiBanner = document.getElementById("ai-banner");
const modelSelect = document.getElementById("model-select");
const chatElement = document.getElementById("chat");
const attachButton = document.getElementById("attach-button");
const fileInput = document.getElementById("file-input");
const fileStrip = document.getElementById("file-strip");
const clearToggle = document.getElementById("clear-toggle");
const clearPanel = document.getElementById("clear-panel");
const clearAge = document.getElementById("clear-age");
const clearOlder = document.getElementById("clear-older");
const clearAll = document.getElementById("clear-all");
const clearStatus = document.getElementById("clear-status");
const searchInput = document.getElementById("search-input");
const chatHeader = document.getElementById("chat-header");
const chatTitle = document.getElementById("chat-title");
const renameButton = document.getElementById("rename-button");
const exportButton = document.getElementById("export-button");

let activeConversationId = null;
let streamController = null; // set while a reply is streaming
let CONFIRM_MS = 3000; // how long a "click again to confirm" button waits
let SEARCH_DELAY_MS = 250; // typing pause before a search is sent
let conversationTitles = {}; // id -> title, from the newest conversation list
let renaming = false;        // the chat title is being edited
let searchSeq = 0;           // lets a newer search discard an older answer
let searchTimer = null;


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


function clearMessages() {
    messagesElement.innerHTML = "";
}


function showEmptyState() {
    messagesElement.innerHTML = `
        <div class="empty-state">
            <h2>Welcome to ORION</h2>
            <p>
                Start a conversation with your private AI assistant.
            </p>
        </div>
    `;
}


function renderMessage(message) {
    const messageElement = document.createElement("div");

    messageElement.className = `message ${message.role}`;

    const contentElement = document.createElement("div");

    contentElement.className = "message-content";

    if (message.role === "assistant") {
        const body = document.createElement("div");

        body.className = "markdown";
        body.innerHTML = renderMarkdown(message.content);

        contentElement.appendChild(body);
    } else {
        contentElement.textContent = message.content;
    }

    messageElement.appendChild(contentElement);
    messagesElement.appendChild(messageElement);

    return messageElement;
}


// Adds `text` to `parent`, wrapping every occurrence of `needle` in <mark>.
// Built from text nodes, never innerHTML, so chat content cannot inject HTML.
function appendHighlighted(parent, text, needle) {
    const lower = text.toLowerCase();
    const wanted = needle.toLowerCase();

    let position = 0;

    while (wanted) {
        const hit = lower.indexOf(wanted, position);

        if (hit === -1) {
            break;
        }

        if (hit > position) {
            parent.appendChild(document.createTextNode(text.slice(position, hit)));
        }

        const mark = document.createElement("mark");

        mark.textContent = text.slice(hit, hit + wanted.length);
        parent.appendChild(mark);

        position = hit + wanted.length;
    }

    if (position < text.length) {
        parent.appendChild(document.createTextNode(text.slice(position)));
    }
}


// Draws the sidebar. `items` are {id, title, snippet?}; `query` is set when
// they are search results, which also get a highlighted excerpt.
function renderConversationList(items, query = "") {
    conversationList.innerHTML = "";

    if (query && items.length === 0) {
        const empty = document.createElement("div");

        empty.className = "list-empty";
        empty.textContent = `No chats match \u201c${query}\u201d.`;
        conversationList.appendChild(empty);

        return;
    }

    for (const conversation of items) {
        const row = document.createElement("div");

        row.className = "conversation-row";

        const button = document.createElement("button");

        button.type = "button";
        button.className = "conversation-item";
        button.title = conversation.title;
        button.dataset.conversationId = conversation.id;

        if (conversation.snippet) {
            const title = document.createElement("span");
            const snippet = document.createElement("span");

            title.className = "conversation-title";
            title.textContent = conversation.title;

            snippet.className = "conversation-snippet";
            appendHighlighted(snippet, conversation.snippet, query);

            button.appendChild(title);
            button.appendChild(snippet);
        } else {
            button.textContent = conversation.title;
        }

        button.addEventListener("click", () => {
            loadConversation(conversation.id);
        });

        const remove = document.createElement("button");

        remove.type = "button";
        remove.className = "conversation-delete";
        remove.title = "Delete conversation";
        remove.setAttribute("aria-label", `Delete ${conversation.title}`);

        twoStep(remove, "\u{1F5D1}", "Delete?", () =>
            deleteConversation(conversation.id)
        );

        row.appendChild(button);
        row.appendChild(remove);
        conversationList.appendChild(row);
    }
}


async function loadConversations() {
    const conversations = await apiRequest("/conversations/");

    conversationTitles = {};

    for (const conversation of conversations) {
        conversationTitles[conversation.id] = conversation.title;
    }

    // While a search is active the sidebar keeps showing its results.
    if (searchInput.value.trim()) {
        await runSearch();
    } else {
        renderConversationList(conversations);
    }

    return conversations;
}


async function runSearch() {
    const query = searchInput.value.trim().replace(/\s+/g, " ");
    const seq = ++searchSeq;

    if (!query) {
        await loadConversations();
        return;
    }

    try {
        const results = await apiRequest(
            `/conversations/search?q=${encodeURIComponent(query)}`
        );

        // The user kept typing: a newer search owns the sidebar now.
        if (seq !== searchSeq) {
            return;
        }

        renderConversationList(results, query);
        updateActiveConversation();
    } catch (error) {
        console.error(error);
    }
}


searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);

    searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
});

searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
        searchInput.value = "";
        clearTimeout(searchTimer);
        runSearch();
    }
});


// ---- Chat title: rename and export -----------------------------------------

function updateChatHeader() {
    const title = activeConversationId
        ? conversationTitles[activeConversationId]
        : undefined;

    chatHeader.hidden = title === undefined;

    // Leave the text box alone while it is being edited.
    if (title !== undefined && !renaming) {
        chatTitle.textContent = title;
    }
}


function startRename() {
    if (!activeConversationId || renaming) {
        return;
    }

    const conversationId = activeConversationId;
    const original = conversationTitles[conversationId] || "";
    const input = document.createElement("input");

    input.type = "text";
    input.className = "chat-title-input";
    input.value = original;
    input.maxLength = 100;
    input.setAttribute("aria-label", "Conversation title");

    renaming = true;
    chatTitle.textContent = "";
    chatTitle.appendChild(input);
    input.focus();
    input.select();

    let finished = false;

    async function finish(save) {
        if (finished) {
            return;
        }

        finished = true;

        const title = input.value.trim().replace(/\s+/g, " ");

        renaming = false;
        updateChatHeader(); // puts the old title back

        if (!save || !title || title === original) {
            return;
        }

        try {
            await apiRequest(`/conversations/${conversationId}`, {
                method: "PATCH",
                body: JSON.stringify({ title }),
            });

            await loadConversations();

            updateActiveConversation();
        } catch (error) {
            console.error(error);

            alert(
                error instanceof Error
                    ? error.message
                    : "Unable to rename the conversation."
            );
        }
    }

    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            finish(true);
        } else if (event.key === "Escape") {
            event.preventDefault();
            finish(false);
        }
    });

    input.addEventListener("blur", () => finish(true));
}


function exportConversation() {
    if (!activeConversationId) {
        return;
    }

    // A plain link with `download`: the server names the file, and the
    // browser's own cookie carries the login, so no fetch is needed.
    const link = document.createElement("a");

    link.href = `/conversations/${encodeURIComponent(activeConversationId)}/export`;
    link.download = "";
    link.style.display = "none";

    document.body.appendChild(link);
    link.click();
    link.remove();
}


renameButton.addEventListener("click", startRename);
chatTitle.addEventListener("dblclick", startRename);
exportButton.addEventListener("click", exportConversation);


async function loadConversation(conversationId) {
    const messages = await apiRequest(
        `/conversations/${conversationId}/messages/`
    );

    activeConversationId = conversationId;

    clearMessages();

    for (const message of messages) {
        renderMessage(message);
    }

    if (messages.length === 0) {
        showEmptyState();
    }

    updateMessageActions();
    updateActiveConversation();

    loadFiles(conversationId).catch(console.error);

    messageInput.focus();
}


function updateActiveConversation() {
    updateChatHeader();

    const items = document.querySelectorAll(".conversation-item");

    for (const item of items) {
        item.classList.toggle(
            "active",
            item.dataset.conversationId === activeConversationId
        );
    }
}


async function createConversation() {
    const conversation = await apiRequest("/conversations/", {
        method: "POST",
        body: JSON.stringify({}),
    });

    activeConversationId = conversation.id;

    clearMessages();
    showEmptyState();
    renderFiles([]);

    await loadConversations();

    updateActiveConversation();

    messageInput.focus();
}


function isNearBottom() {
    return (
        messagesElement.scrollHeight
        - messagesElement.scrollTop
        - messagesElement.clientHeight
    ) < 80;
}


function scrollToBottom() {
    messagesElement.scrollTop = messagesElement.scrollHeight;
}


function setStreaming(active) {
    document.body.classList.toggle("streaming", active);

    messageInput.disabled = active;
    messageInput.placeholder = active
        ? "ORION is replying..."
        : "Message ORION...";

    attachButton.disabled = active;

    sendButton.textContent = active ? "Stop" : "Send";
    sendButton.classList.toggle("stop", active);

    if (!active) {
        messageInput.focus();
    }
}


function showTyping(contentElement) {
    contentElement.innerHTML =
        '<span class="typing"><i></i><i></i><i></i></span>';
}


function addNote(contentElement, text, isError) {
    const note = document.createElement("div");

    note.className = isError ? "reply-note error" : "reply-note";
    note.textContent = text;

    contentElement.appendChild(note);
}


function clearTyping(bodyElement, hasText) {
    if (!hasText) {
        bodyElement.textContent = "";
    }
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


// One listener handles every code block, including ones re-rendered mid-stream.
messagesElement.addEventListener("click", async (event) => {
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


async function errorDetail(response) {
    try {
        const data = await response.json();

        if (typeof data.detail === "string") {
            return data.detail;
        }
    } catch {
        // Fall through to the generic message.
    }

    return "Request failed.";
}


async function* readEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let buffer = "";

    while (true) {
        const { done, value } = await reader.read();

        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });

        let boundary;

        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
            const block = buffer.slice(0, boundary);

            buffer = buffer.slice(boundary + 2);

            for (const line of block.split("\n")) {
                if (!line.startsWith("data:")) {
                    continue;
                }

                try {
                    yield JSON.parse(line.slice(5).trim());
                } catch {
                    // Ignore a malformed event rather than abort the reply.
                }
            }
        }
    }
}


// Streams a reply into the chat. With `regenerate`, `content` is ignored: the
// last question is answered again and the new reply takes the old one's place.
async function streamReply(content, { regenerate = false } = {}) {
    if (!activeConversationId) {
        await createConversation();
    }

    const conversationId = activeConversationId;

    const emptyState = messagesElement.querySelector(".empty-state");

    if (emptyState) {
        emptyState.remove();
    }

    // Show the user's message immediately, then wait for the reply.
    // When regenerating there is no new question, and the old answer stays on
    // screen (hidden) until the new one has really arrived.
    let userElement = null;
    let replaced = null;

    if (regenerate) {
        const last = lastMessageElement();

        if (last && last.classList.contains("assistant")) {
            replaced = last;
            replaced.style.display = "none";
        }
    } else {
        userElement = renderMessage({ role: "user", content });
    }

    const assistantElement = renderMessage({ role: "assistant", content: "" });
    const assistantContent = assistantElement.querySelector(".message-content");
    const assistantBody = assistantContent.querySelector(".markdown");

    showTyping(assistantBody);
    scrollToBottom();

    const controller = new AbortController();

    streamController = controller;
    setStreaming(true);
    updateMessageActions(); // drops the old Regenerate button

    let received = "";
    let userSaved = false;
    let failed = false;
    let paintQueued = false;
    let regenerateError = null; // why a regeneration produced nothing

    // Re-render at most once per frame; the whole reply is re-parsed so that
    // half-finished markdown (open code fence, unclosed **) displays sanely.
    function paint() {
        paintQueued = false;

        const stick = isNearBottom();

        assistantBody.innerHTML = renderMarkdown(received);

        if (stick) {
            scrollToBottom();
        }
    }

    function schedulePaint() {
        if (paintQueued) {
            return;
        }

        paintQueued = true;

        (window.requestAnimationFrame || setTimeout)(paint);
    }

    try {
        const response = await fetch(
            `/conversations/${conversationId}/messages/${regenerate ? "regenerate" : "stream"}`,
            {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(regenerate ? {} : { content }),
                signal: controller.signal,
            }
        );

        if (response.status === 401) {
            window.location.href = "/login";
            return;
        }

        if (!response.ok) {
            throw new Error(await errorDetail(response));
        }

        for await (const event of readEvents(response)) {
            if (event.type === "user_message") {
                userSaved = true;

                // The first message sets the conversation title.
                loadConversations()
                    .then(updateActiveConversation)
                    .catch(console.error);
            } else if (event.type === "delta") {
                received += event.content;
                schedulePaint();
            } else if (event.type === "error") {
                failed = true;
                regenerateError = event.detail;

                paint();
                clearTyping(assistantBody, received.length > 0);
                assistantContent.classList.add("failed");
                addNote(assistantContent, event.detail, true);
            }
        }

        if (!received && !failed) {
            regenerateError = "No response was produced.";

            clearTyping(assistantBody, false);
            addNote(assistantContent, "No response was produced.", false);
        }
    } catch (error) {
        if (error.name === "AbortError") {
            paint();
            clearTyping(assistantBody, received.length > 0);
            addNote(assistantContent, "Stopped.", false);
        } else if (regenerate && !received) {
            // Nothing arrived, so there is nothing to keep; the old answer
            // is put back below.
            regenerateError = error instanceof Error
                ? error.message
                : "Unable to regenerate the reply.";
        } else if (!userSaved && !regenerate) {
            // The message never reached the server: undo and let them retry.
            userElement.remove();
            assistantElement.remove();

            messageInput.value = content;

            alert(
                error instanceof Error
                    ? error.message
                    : "Unable to send message."
            );
        } else {
            paint();
            clearTyping(assistantBody, received.length > 0);
            assistantContent.classList.add("failed");
            addNote(assistantContent, "Connection lost while replying.", true);
        }
    } finally {
        if (received) {
            paint(); // final render, in case a frame was still pending
        }

        if (regenerate) {
            if (received.trim()) {
                // The server replaced the old answer; do the same on screen.
                if (replaced) {
                    replaced.remove();
                }
            } else {
                // The server kept the old answer, so bring it back.
                assistantElement.remove();

                if (replaced) {
                    replaced.style.display = "";
                }

                if (regenerateError) {
                    alert(regenerateError);
                }
            }
        }

        streamController = null;
        setStreaming(false);
        updateMessageActions();
        refreshAiStatus();
    }
}


// ---- Regenerate ---------------------------------------------------------

function lastMessageElement() {
    const items = messagesElement.children;

    for (let i = items.length - 1; i >= 0; i--) {
        if (items[i].classList && items[i].classList.contains("message")) {
            return items[i];
        }
    }

    return null;
}


// Shows a Regenerate button under the last message, and nowhere else.
function updateMessageActions() {
    for (const item of Array.from(messagesElement.children)) {
        const old = item.querySelector ? item.querySelector(".message-actions") : null;

        if (old) {
            old.remove();
        }
    }

    if (streamController || !activeConversationId) {
        return;
    }

    const last = lastMessageElement();

    if (!last) {
        return;
    }

    const row = document.createElement("div");
    const button = document.createElement("button");

    row.className = "message-actions";

    button.type = "button";
    button.className = "message-action regenerate";
    button.textContent = "\u21BB Regenerate";
    button.title = "Generate a new answer";

    button.addEventListener("click", regenerateReply);

    row.appendChild(button);
    last.appendChild(row);
}


async function regenerateReply() {
    if (streamController || !activeConversationId) {
        return;
    }

    try {
        await streamReply("", { regenerate: true });
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to regenerate the reply."
        );
    }
}


messageForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    // While a reply is streaming the button reads "Stop".
    if (streamController) {
        streamController.abort();
        return;
    }

    const content = messageInput.value.trim();

    if (!content) {
        return;
    }

    messageInput.value = "";

    try {
        await streamReply(content);
    } catch (error) {
        console.error(error);

        messageInput.value = content;

        alert(
            error instanceof Error
                ? error.message
                : "Unable to send message."
        );
    }
});


document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && streamController) {
        streamController.abort();
    }
});


// ---- Deleting history ---------------------------------------------------

// A button that asks "are you sure?" by changing its label: the first click
// arms it, a second click within CONFIRM_MS runs the action.
function twoStep(button, idleLabel, confirmLabel, action) {
    let timer = null;

    function reset() {
        clearTimeout(timer);

        timer = null;
        button.textContent = idleLabel;
        button.classList.remove("confirming");
    }

    reset();

    button.addEventListener("click", async () => {
        if (timer === null) {
            button.textContent = confirmLabel;
            button.classList.add("confirming");

            timer = setTimeout(reset, CONFIRM_MS);

            return;
        }

        reset();

        await action();
    });
}


function resetChatView() {
    activeConversationId = null;

    clearMessages();
    showEmptyState();
    renderFiles([]);
    updateChatHeader();
}


async function deleteConversation(conversationId) {
    try {
        const response = await fetch(`/conversations/${conversationId}`, {
            method: "DELETE",
            credentials: "same-origin",
        });

        if (response.status === 401) {
            window.location.href = "/login";
            return;
        }

        // 404 just means it is already gone; refresh the list either way.
        if (!response.ok && response.status !== 404) {
            throw new Error(await errorDetail(response));
        }

        if (conversationId === activeConversationId) {
            resetChatView();
        }

        await loadConversations();

        updateActiveConversation();
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to delete the conversation."
        );
    }
}


function describeDeleted(count) {
    if (count === 0) {
        return "Nothing to delete.";
    }

    return `Deleted ${count} conversation${count === 1 ? "" : "s"}.`;
}


async function bulkDelete(query) {
    clearStatus.textContent = "Deleting...";

    try {
        const response = await fetch(`/conversations/?${query}`, {
            method: "DELETE",
            credentials: "same-origin",
        });

        if (response.status === 401) {
            window.location.href = "/login";
            return;
        }

        if (!response.ok) {
            throw new Error(await errorDetail(response));
        }

        const result = await response.json();
        const conversations = await loadConversations();

        if (
            activeConversationId &&
            !conversations.some((item) => item.id === activeConversationId)
        ) {
            resetChatView();
        }

        updateActiveConversation();

        clearStatus.textContent = describeDeleted(result.deleted);
    } catch (error) {
        console.error(error);

        clearStatus.textContent =
            error instanceof Error ? error.message : "Unable to delete.";
    }
}


twoStep(clearOlder, "Delete older chats", "Click again to confirm", () =>
    bulkDelete(`older_than_days=${encodeURIComponent(clearAge.value)}`)
);

twoStep(clearAll, "Delete all chats", "Click again to confirm", () =>
    bulkDelete("scope=all")
);

clearToggle.addEventListener("click", () => {
    const opening = clearPanel.hidden;

    clearPanel.hidden = !opening;
    clearToggle.setAttribute("aria-expanded", String(opening));
    clearStatus.textContent = "";
});


// ---- Attached files -----------------------------------------------------

function formatTokens(count) {
    return count < 1000 ? String(count) : `${(count / 1000).toFixed(1)}k`;
}


function renderFiles(files) {
    fileStrip.innerHTML = "";
    fileStrip.hidden = files.length === 0;
    chatElement.classList.toggle("has-files", files.length > 0);

    for (const file of files) {
        const chip = document.createElement("div");

        chip.className = "file-chip";

        const name = document.createElement("span");

        name.className = "file-name";
        name.textContent = file.filename; // textContent: names are untrusted
        name.title = file.filename;

        const meta = document.createElement("span");

        meta.className = "file-meta";
        meta.textContent = `~${formatTokens(file.token_estimate)} tokens`;

        const remove = document.createElement("button");

        remove.type = "button";
        remove.className = "file-remove";
        remove.title = "Remove this file";
        remove.setAttribute("aria-label", `Remove ${file.filename}`);
        remove.textContent = "\u00d7";

        remove.addEventListener("click", () => {
            removeFile(file.id);
        });

        chip.appendChild(name);
        chip.appendChild(meta);
        chip.appendChild(remove);
        fileStrip.appendChild(chip);
    }
}


async function loadFiles(conversationId) {
    const files = await apiRequest(
        `/conversations/${conversationId}/attachments/`
    );

    // Ignore a late answer for a conversation the user already left.
    if (conversationId === activeConversationId) {
        renderFiles(files);
    }
}


async function uploadFile(file) {
    if (!activeConversationId) {
        await createConversation();
    }

    const conversationId = activeConversationId;

    const response = await fetch(
        `/conversations/${conversationId}/attachments/` +
            `?filename=${encodeURIComponent(file.name)}`,
        {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/octet-stream" },
            body: file, // the raw file; no multipart needed
        }
    );

    if (response.status === 401) {
        window.location.href = "/login";
        return false;
    }

    if (!response.ok) {
        throw new Error(`${file.name}: ${await errorDetail(response)}`);
    }

    await loadFiles(conversationId);

    return true;
}


async function uploadFiles(files) {
    if (streamController || files.length === 0) {
        return;
    }

    attachButton.disabled = true;
    attachButton.classList.add("busy");

    try {
        // One at a time, so the per-conversation limits are checked in order.
        for (const file of files) {
            if (!(await uploadFile(file))) {
                return;
            }
        }
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error ? error.message : "Unable to attach the file."
        );
    } finally {
        attachButton.disabled = streamController !== null;
        attachButton.classList.remove("busy");
        messageInput.focus();
    }
}


async function removeFile(attachmentId) {
    const conversationId = activeConversationId;

    try {
        const response = await fetch(
            `/conversations/${conversationId}/attachments/${attachmentId}`,
            { method: "DELETE", credentials: "same-origin" }
        );

        if (!response.ok && response.status !== 404) {
            throw new Error(await errorDetail(response));
        }

        await loadFiles(conversationId);
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error ? error.message : "Unable to remove the file."
        );
    }
}


attachButton.addEventListener("click", () => {
    fileInput.click();
});


fileInput.addEventListener("change", async () => {
    const files = Array.from(fileInput.files);

    fileInput.value = ""; // allow picking the same file again later

    await uploadFiles(files);
});


// Drag and drop onto the chat area.
chatElement.addEventListener("dragover", (event) => {
    event.preventDefault();
    chatElement.classList.add("dragging");
});

chatElement.addEventListener("dragleave", (event) => {
    if (event.target === chatElement) {
        chatElement.classList.remove("dragging");
    }
});

chatElement.addEventListener("drop", async (event) => {
    event.preventDefault();
    chatElement.classList.remove("dragging");

    await uploadFiles(Array.from(event.dataTransfer.files));
});


// ---- AI status banner ---------------------------------------------------

let statusTimer = null;
let lastStatus = null;   // newest /ai/status, used to restore the dropdown
let modelListKey = "";   // what the dropdown currently shows


function renderBanner(status) {
    let text = "";
    let kind = "";

    if (status.provider === "llama" && status.state !== "ready") {
        if (status.state === "error") {
            text = `The AI model failed to start: ${status.detail || "unknown error"}. Choose another model or restart ORION to try again.`;
            kind = "error";
        } else {
            const name = status.model ? ` ${status.model}` : "";
            text = `Loading the AI model${name}... the first reply may take a moment.`;
            kind = "info";
        }
    } else if (status.provider === "unavailable") {
        text = status.detail || "The local AI model is unavailable.";
        kind = "error";
    } else if (status.provider === "echo") {
        text = status.detail || "Echo mode: no AI model is loaded.";
        kind = "";
    }

    aiBanner.hidden = !text;
    aiBanner.textContent = text;
    aiBanner.className = `ai-banner ${kind}`.trim();
}


function formatModelSize(bytes) {
    if (!bytes) {
        return "";
    }

    const gb = bytes / (1024 ** 3);

    return gb >= 1
        ? `${gb.toFixed(1)} GB`
        : `${Math.max(1, Math.round(bytes / (1024 ** 2)))} MB`;
}


function renderModels(status) {
    const models = status.models || [];
    const key = JSON.stringify([
        models.map((model) => [model.id, model.size]),
        status.model,
        status.can_switch,
    ]);

    // Rebuilding an open <select> closes it, so only touch it on a change.
    if (key === modelListKey) {
        return;
    }

    modelListKey = key;
    modelSelect.innerHTML = "";

    const add = (value, label) => {
        const option = document.createElement("option");

        option.value = value;
        option.textContent = label;
        modelSelect.appendChild(option);
    };

    if (models.length === 0 && !status.model) {
        add("", "No models found");
        modelSelect.disabled = true;
        modelSelect.title = "Put .gguf model files in ORION's models folder.";
        return;
    }

    // The running model may have been deleted from the folder since it loaded.
    if (status.model && !models.some((model) => model.id === status.model)) {
        add(status.model, status.model);
    }

    for (const model of models) {
        const size = formatModelSize(model.size);

        add(model.id, size ? `${model.name} (${size})` : model.name);
    }

    modelSelect.value = status.model || models[0].id;
    modelSelect.disabled = !status.can_switch;
    modelSelect.title = status.can_switch
        ? "Choose the AI model"
        : "Switching models is not available right now.";
}


modelSelect.addEventListener("change", async () => {
    const wanted = modelSelect.value;

    modelSelect.disabled = true;

    try {
        const status = await apiRequest("/ai/model", {
            method: "POST",
            body: JSON.stringify({ model: wanted }),
        });

        lastStatus = status;
        renderBanner(status);
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to switch the model."
        );
    }

    // Success or failure, show what the server says is really loaded.
    modelListKey = "";
    renderModels(lastStatus || { models: [] });

    refreshAiStatus(); // polls quickly while the new model loads
});


async function refreshAiStatus() {
    clearTimeout(statusTimer);

    let delay = 30000;

    try {
        const status = await apiRequest("/ai/status");

        lastStatus = status;
        renderBanner(status);
        renderModels(status);

        if (status.provider === "llama" && status.state === "loading") {
            delay = 1500; // keep checking until the model is ready
        }
    } catch (error) {
        console.error(error);
    }

    statusTimer = setTimeout(refreshAiStatus, delay);
}


newConversationButton.addEventListener("click", async () => {
    newConversationButton.disabled = true;

    try {
        await createConversation();
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to create conversation."
        );
    } finally {
        newConversationButton.disabled = false;
    }
});


logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    logoutButton.textContent = "Logging out...";

    try {
        const response = await fetch("/auth/logout", {
            method: "POST",
            credentials: "same-origin",
        });

        if (!response.ok) {
            throw new Error("Logout failed.");
        }

        window.location.href = "/login";
    } catch (error) {
        console.error(error);

        logoutButton.disabled = false;
        logoutButton.textContent = "Logout";

        alert("Unable to log out. Please try again.");
    }
});


async function initialize() {
    refreshAiStatus();

    try {
        const conversations = await loadConversations();

        if (conversations.length > 0) {
            await loadConversation(conversations[0].id);
        } else {
            activeConversationId = null;
            showEmptyState();
            renderFiles([]);
            messageInput.focus();
        }
    } catch (error) {
        console.error(error);

        alert(
            error instanceof Error
                ? error.message
                : "Unable to load ORION."
        );
    }
}


initialize();
