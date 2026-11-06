(() => {
  const card = document.querySelector(".auth-card");
  const form = document.getElementById("auth-form");
  const errorEl = document.getElementById("auth-error");
  const submit = document.getElementById("auth-submit");
  const endpoint = card.dataset.mode === "signup" ? "/api/signup" : "/api/login";

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.hidden = !msg;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showError("");
    const email = form.email.value.trim();
    const password = form.password.value;

    if (!email || !password) return showError("Enter your email and password.");
    if (endpoint === "/api/signup" && password.length < 8) {
      return showError("Password must be at least 8 characters.");
    }

    submit.disabled = true;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      window.location.href = "/";
    } catch (err) {
      showError(err.message);
      submit.disabled = false;
    }
  });
})();
