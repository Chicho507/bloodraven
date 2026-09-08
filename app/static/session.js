"use strict";
window.BloodRaven = (() => {
  let session, activeAt = 0;
  const ready = fetch("/api/auth/context", {credentials:"same-origin",cache:"no-store"}).then(async response => {
    if (!response.ok) throw new Error("No se pudo verificar la sesión.");
    session = await response.json();
    if (!session.user) { location.replace("/login"); throw new Error("Sesión finalizada"); }
    document.querySelectorAll("[data-admin]").forEach(node => { node.hidden = session.user.role !== "admin"; });
    document.querySelectorAll("[data-user]").forEach(node => { node.textContent = session.user.name; });
    document.querySelectorAll("[data-version]").forEach(node => { node.textContent = `Beta ${session.version}`; });
    return session;
  });
  async function request(url, options = {}) {
    await ready;
    const headers = new Headers(options.headers || {});
    if (options.method && options.method.toUpperCase() !== "GET") {
      headers.set("X-CSRF-Token", session.csrf); headers.set("Content-Type", "application/json");
    }
    const response = await fetch(url, {...options,headers,credentials:"same-origin",cache:"no-store"});
    if (response.status === 401) { location.replace("/login"); throw new Error("Tu sesión ha terminado."); }
    return response;
  }
  async function json(url, options = {}) {
    const response = await request(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail + (data.fields?.filter(Boolean).length ? ` (${data.fields.filter(Boolean).join(", ")})` : ""));
    return data;
  }
  document.addEventListener("click", async event => {
    if (event.target.closest("[data-logout]")) {
      try { await json("/api/auth/logout", {method:"POST",body:"{}"}); location.replace("/login"); }
      catch { alert("No se pudo cerrar la sesión. Intenta nuevamente."); }
    }
  });
  // Only user activity renews idle time; automatic dashboard polling does not.
  const activity = () => {
    if (!session?.user || document.hidden || Date.now() - activeAt < 60000) return;
    activeAt = Date.now(); json("/api/auth/keepalive",{method:"POST",body:"{}"}).catch(()=>{});
  };
  document.addEventListener("pointerdown",activity,{passive:true});
  document.addEventListener("keydown",activity,{passive:true});
  ready.catch(()=>{});
  return {ready,fetch:request,json};
})();
