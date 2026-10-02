const form = document.getElementById("signup-form");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const confirmPasswordInput = document.getElementById("confirm-password");
const signupButton = document.getElementById("signup-button");

const errorMessage = document.getElementById("error-message");
const successMessage = document.getElementById("success-message");

function showError(message) {
errorMessage.textContent = message;
errorMessage.hidden = false;
successMessage.hidden = true;
}

function showSuccess(message) {
successMessage.textContent = message;
successMessage.hidden = false;
errorMessage.hidden = true;
}

function clearMessages() {
errorMessage.hidden = true;
successMessage.hidden = true;
}

form.addEventListener("submit", async (event) => {
event.preventDefault();

clearMessages();

const username = usernameInput.value.trim();
const password = passwordInput.value;
const confirmPassword = confirmPasswordInput.value;

if (!username) {
    showError("Please enter a username.");
    return;
}

if (!password) {
    showError("Please enter a password.");
    return;
}

if (password !== confirmPassword) {
    showError("Passwords do not match.");
    return;
}

signupButton.disabled = true;
signupButton.textContent = "Creating account...";

try {
    const response = await fetch("/auth/register", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            username,
            password,
        }),
    });

    const data = await response.json();

    if (!response.ok) {
        const detail = data.detail;

        if (Array.isArray(detail)) {
            showError(
                detail
                    .map((error) => error.msg)
                    .join(", ")
            );
        } else {
            showError(detail || "Unable to create account.");
        }

        return;
    }

    showSuccess("Account created successfully. Redirecting to login...");

    setTimeout(() => {
        window.location.href = "/login";
    }, 1000);
} catch (error) {
    showError(
        "Unable to connect to ORION. Please try again."
    );
} finally {
    signupButton.disabled = false;
    signupButton.textContent = "Create account";
}


});