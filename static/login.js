// Signing in from another device (iPhone over Meshnet). See app/auth.py.

// "Show" lets you check what you typed; it never shows a saved passcode (only a hash is kept).
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-show-pass]");
  if (!btn) return;
  const input = document.getElementById(btn.dataset.showPass);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.textContent = show ? "Hide" : "Show";
  btn.setAttribute("aria-pressed", show);
  input.focus();
});
document.getElementById("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const msg = document.getElementById("login-msg");
  msg.className = "msg"; msg.textContent = "Signing in…";
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode: document.getElementById("passcode").value }),
  });
  if (res.ok) { location.replace("/"); return; }
  const data = await res.json().catch(() => ({}));
  msg.className = "msg error"; msg.textContent = data.detail || "Couldn't sign in.";
};
