const logoutButton = document.getElementById("logout-button");
const newConversationButton = document.getElementById("new-conversation");
const conversationList = document.getElementById("conversation-list");

const messagesElement = document.getElementById("messages");
const messageForm = document.getElementById("message-form");
const messageInput = document.getElementById("message-input");

let activeConversationId = null;


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

    messageElement.className = `message message-${message.role}`;

    const contentElement = document.createElement("div");

    contentElement.className = "message-content";
    contentElement.textContent = message.content;

    messageElement.appendChild(contentElement);
    messagesElement.appendChild(messageElement);
}


async function loadConversations() {
    const conversations = await apiRequest("/conversations/");

    conversationList.innerHTML = "";

    for (const conversation of conversations) {
        const button = document.createElement("button");

        button.type = "button";
        button.className = "conversation-item";
        button.textContent = conversation.title;
        button.dataset.conversationId = conversation.id;

        button.addEventListener("click", () => {
            loadConversation(conversation.id);
        });

        conversationList.appendChild(button);
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

    await loadConversations();

    updateActiveConversation();

    messageInput.focus();
}


async function sendMessage(content) {
    if (!activeConversationId) {
        await createConversation();
    }

    return apiRequest(
        `/conversations/${activeConversationId}/messages/`,
        {
            method: "POST",
            body: JSON.stringify({
                role: "user",
                content,
            }),
        }
    );
}


messageForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const content = messageInput.value.trim();

    if (!content) {
        return;
    }

    messageInput.value = "";
    messageInput.disabled = true;

    try {
        const response = await sendMessage(content);

        clearMessages();

        const messages = await apiRequest(
            `/conversations/${activeConversationId}/messages/`
        );

        for (const message of messages) {
            renderMessage(message);
        }

        await loadConversations();
        updateActiveConversation();

        messageInput.focus();
    } catch (error) {
        console.error(error);

        messageInput.value = content;

        alert(
            error instanceof Error
                ? error.message
                : "Unable to send message."
        );
    } finally {
        messageInput.disabled = false;
        messageInput.focus();
    }
});


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
    try {
        const conversations = await loadConversations();

        if (conversations.length > 0) {
            await loadConversation(conversations[0].id);
        } else {
            activeConversationId = null;
            showEmptyState();
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
