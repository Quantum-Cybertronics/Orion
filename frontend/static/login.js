const form = document.getElementById("login-form");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const loginButton = document.getElementById("login-button");
const buttonText = document.getElementById("button-text");
const buttonLoading = document.getElementById("button-loading");
const errorElement = document.getElementById("login-error");

function showError(message) {
errorElement.textContent = message;
errorElement.hidden = false;
}

function clearError() {
errorElement.textContent = "";
errorElement.hidden = true;
}

function setLoading(loading) {
loginButton.disabled = loading;
buttonText.hidden = loading;
buttonLoading.hidden = !loading;
}

form.addEventListener("submit", async (event) => {
event.preventDefault();

clearError();
setLoading(true);

try {
    const response = await fetch("/auth/login", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        credentials: "same-origin",
        body: JSON.stringify({
            username: usernameInput.value.trim(),
            password: passwordInput.value,
        }),
    });

    if (!response.ok) {
        let message = "Unable to sign in.";

        try {
            const data = await response.json();

            if (typeof data.detail === "string") {
                message = data.detail;
            }
        } catch {
            // Keep the default error message.
        }

        throw new Error(message);
    }

    window.location.href = "/app";
} catch (error) {
    showError(
        error instanceof Error
            ? error.message
            : "Unable to sign in."
    );
} finally {
    setLoading(false);
}


});