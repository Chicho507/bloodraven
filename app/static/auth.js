"use strict";
(() => {
  let csrf = "";
  const form = document.querySelector("#login-form"), button = document.querySelector("#login-submit"), error = document.querySelector("#login-error"), password = document.querySelector("#password");
  const showError = (message) => { error.textContent = message; error.hidden = false; };
  async function context() {
    const response = await fetch("/api/auth/context", {credentials:"same-origin", cache:"no-store"});
    if (!response.ok) throw new Error("No se pudo conectar con el portal. Recarga la página.");
    const data = await response.json();
    if (data.user) { location.replace("/"); return; }
    csrf = data.csrf;
    document.querySelector("#version").textContent = `Versión ${data.version} · Piloto interno`;
    button.disabled = false;
  }
  document.querySelector("#show-password").addEventListener("click", (event) => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    event.currentTarget.textContent = show ? "Ocultar" : "Mostrar";
    event.currentTarget.setAttribute("aria-pressed", String(show));
    event.currentTarget.setAttribute("aria-label", show ? "Ocultar contraseña" : "Mostrar contraseña");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); button.disabled = true; error.hidden = true;
    try {
      const response = await fetch("/api/auth/login", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf}, body:JSON.stringify({username:form.username.value.trim(), password:password.value})});
      const data = await response.json();
      if (!response.ok) { if (response.status === 403) await context(); throw new Error(data.detail || "No se pudo iniciar sesión."); }
      password.value = ""; location.replace("/");
    } catch (failure) { showError(failure.message); password.value = ""; button.disabled = false; }
  });
  context().catch(failure=>showError(failure.message));
})();
