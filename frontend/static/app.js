const logoutButton = document.getElementById("logout-button");

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