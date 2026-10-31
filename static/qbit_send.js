/* Submit Torrent Getter links without reloading or moving the catalogue. */
(() => {
    "use strict";

    const container = document.getElementById("qbit-notifications");
    if (!container || !window.fetch || !window.FormData) {
        return; // Ordinary HTML form POST remains the fallback.
    }

    const SUCCESS_DURATION_MS = 6000;
    const ERROR_DURATION_MS = 10000;

    function makeNotification(movieTitle) {
        const toast = document.createElement("div");
        toast.className = "qbit-toast";
        toast.dataset.state = "pending";
        toast.setAttribute("role", "status");

        const content = document.createElement("div");
        content.className = "qbit-toast-content";

        const heading = document.createElement("strong");
        heading.className = "qbit-toast-title";
        heading.textContent = "Sending to qBittorrent";

        const message = document.createElement("p");
        message.className = "qbit-toast-message";
        message.textContent = movieTitle || "Submitting the selected link…";

        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.className = "qbit-toast-dismiss";
        dismiss.setAttribute("aria-label", "Dismiss notification");
        dismiss.textContent = "×";

        let timerId = null;
        dismiss.addEventListener("click", () => {
            if (timerId !== null) {
                window.clearTimeout(timerId);
            }
            toast.remove();
        });

        content.append(heading, message);
        toast.append(content, dismiss);
        container.prepend(toast);

        function update(state, title, detail) {
            if (!toast.isConnected) {
                return;
            }
            if (timerId !== null) {
                window.clearTimeout(timerId);
            }

            toast.dataset.state = state;
            toast.setAttribute("role", state === "error" ? "alert" : "status");
            heading.textContent = title;
            message.textContent = detail;

            timerId = window.setTimeout(() => {
                toast.remove();
                timerId = null;
            }, state === "error" ? ERROR_DURATION_MS : SUCCESS_DURATION_MS);
        }

        return { update };
    }

    document.addEventListener("submit", async (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement) || !form.matches("form.send-form")) {
            return;
        }

        event.preventDefault();
        if (form.dataset.sending === "true") {
            return;
        }

        const button = form.querySelector('button[type="submit"]');
        const originalLabel = button ? button.textContent : "";
        const notice = makeNotification(form.dataset.itemTitle || "");
        form.dataset.sending = "true";

        if (button) {
            button.disabled = true;
            button.textContent = "Sending…";
        }

        try {
            const response = await fetch(form.action, {
                method: "POST",
                body: new FormData(form),
                credentials: "same-origin",
                redirect: "error",
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest"
                }
            });
            if (!(response.headers.get("content-type") || "").includes("application/json")) {
                throw new Error("Unexpected server response. Check Torrent Getter logs.");
            }
            const result = await response.json();
            if (!response.ok || !result.ok) {
                throw new Error(result.message || "qBittorrent did not accept the request.");
            }
            notice.update(
                "success",
                "Submitted to qBittorrent",
                (result.title ? result.title + ": " : "") +
                    (result.message || "Check your qBittorrent queue.")
            );
        } catch (error) {
            notice.update(
                "error",
                "Unable to add to qBittorrent",
                error instanceof Error
                    ? error.message
                    : "The request could not be confirmed. Check qBittorrent before retrying."
            );
        } finally {
            delete form.dataset.sending;
            if (button) {
                button.disabled = false;
                button.textContent = originalLabel;
            }
        }
    });
})();
