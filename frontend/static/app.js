const logoutButton = document.getElementById("logout-button");
const newConversationButton = document.getElementById("new-conversation");
const conversationList = document.getElementById("conversation-list");

const messagesElement = document.getElementById("messages");
const messageForm = document.getElementById("message-form");
const messageInput = document.getElementById("message-input");
const sendButton = document.getElementById("send-button");
const aiBanner = document.getElementById("ai-banner");
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

let activeConversationId = null;
let streamController = null; // set while a reply is streaming
let CONFIRM_MS = 3000; // how long a "click again to confirm" button waits


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


async function loadConversations() {
    const conversations = await apiRequest("/conversations/");

    conversationList.innerHTML = "";

    for (const conversation of conversations) {
        const row = document.createElement("div");

        row.className = "conversation-row";

        const button = document.createElement("button");

        button.type = "button";
        button.className = "conversation-item";
        button.textContent = conversation.title;
        button.title = conversation.title;
        button.dataset.conversationId = conversation.id;

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

    return conversations;
}


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

    updateActiveConversation();

    loadFiles(conversationId).catch(console.error);

    messageInput.focus();
}


function updateActiveConversation() {
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


async function streamReply(content) {
    if (!activeConversationId) {
        await createConversation();
    }

    const conversationId = activeConversationId;

    const emptyState = messagesElement.querySelector(".empty-state");

    if (emptyState) {
        emptyState.remove();
    }

    // Show the user's message immediately, then wait for the reply.
    const userElement = renderMessage({ role: "user", content });
    const assistantElement = renderMessage({ role: "assistant", content: "" });
    const assistantContent = assistantElement.querySelector(".message-content");
    const assistantBody = assistantContent.querySelector(".markdown");

    showTyping(assistantBody);
    scrollToBottom();

    const controller = new AbortController();

    streamController = controller;
    setStreaming(true);

    let received = "";
    let userSaved = false;
    let failed = false;
    let paintQueued = false;

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
            `/conversations/${conversationId}/messages/stream`,
            {
                method: "POST",
                credentials: "same-origin",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content }),
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

                paint();
                clearTyping(assistantBody, received.length > 0);
                assistantContent.classList.add("failed");
                addNote(assistantContent, event.detail, true);
            }
        }

        if (!received && !failed) {
            clearTyping(assistantBody, false);
            addNote(assistantContent, "No response was produced.", false);
        }
    } catch (error) {
        if (error.name === "AbortError") {
            paint();
            clearTyping(assistantBody, received.length > 0);
            addNote(assistantContent, "Stopped.", false);
        } else if (!userSaved) {
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

        streamController = null;
        setStreaming(false);
        refreshAiStatus();
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


function renderBanner(status) {
    let text = "";
    let kind = "";

    if (status.provider === "llama" && status.state !== "ready") {
        if (status.state === "error") {
            text = `The AI model failed to start: ${status.detail || "unknown error"}. Restart ORION to try again.`;
            kind = "error";
        } else {
            text = "Loading the AI model... the first reply may take a moment.";
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


async function refreshAiStatus() {
    clearTimeout(statusTimer);

    let delay = 30000;

    try {
        const status = await apiRequest("/ai/status");

        renderBanner(status);

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
