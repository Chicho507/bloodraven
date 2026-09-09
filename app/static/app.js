"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const state = {
    overview: null, selected: null, view: "general", apiError: false,
    refreshing: false, timer: null, history: [], historyKey: null,
    historyController: null, historyRequest: 0, eventsController: null,
    eventsRequest: 0, botBusy: false, firstDemo: true,
  };
  const labels = {ok: "Respondiendo", warning: "Advertencia", offline: "Sin respuesta SNMP", pending: "Pendiente de lectura", stale: "Datos desactualizados"};
  const statuses = new Set(Object.keys(labels));
  const formatter = new Intl.NumberFormat("es-PA", {maximumFractionDigits: 1});
  const fullDate = new Intl.DateTimeFormat("es-PA", {dateStyle: "medium", timeStyle: "medium"});
  const shortTime = new Intl.DateTimeFormat("es-PA", {hour: "2-digit", minute: "2-digit", hour12: false});
  const dateOnly = new Intl.DateTimeFormat("es-PA", {day: "2-digit", month: "short"});
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const number = (value, suffix = "") => finite(value) ? formatter.format(value) + suffix : "—";
  const date = (value) => { if (!value) return null; const result = new Date(value); return Number.isNaN(result.valueOf()) ? null : result; };
  const stamp = (value) => { const parsed = date(value); return parsed ? fullDate.format(parsed) : "Sin lectura registrada"; };
  const device = () => state.overview?.devices.find((item) => item.id === state.selected);
  const status = (item) => state.apiError ? "stale" : statuses.has(item?.status) ? item.status : "pending";
  const usable = (item) => item && ["ok", "warning"].includes(status(item));
  const metric = (item, key, suffix = "") => usable(item) ? number(item[key], suffix) : "—";
  const text = (selector, value) => { $(selector).textContent = value; };

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function age(value) {
    const parsed = date(value);
    if (!parsed) return "sin lectura";
    const seconds = Math.max(0, Math.floor((Date.now() - parsed.valueOf()) / 1000));
    if (seconds < 60) return `hace ${seconds} s`;
    if (seconds < 3600) return `hace ${Math.floor(seconds / 60)} min`;
    if (seconds < 86400) return `hace ${Math.floor(seconds / 3600)} h ${Math.floor(seconds % 3600 / 60)} min`;
    return `hace ${Math.floor(seconds / 86400)} d`;
  }

  function uptime(value) {
    if (!finite(value)) return "—";
    const seconds = Math.max(0, Math.floor(value));
    if (seconds < 60) return `${seconds} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
    const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600);
    return days ? `${days} d ${hours} h` : `${hours} h ${Math.floor(seconds % 3600 / 60)} min`;
  }

  async function request(url, options = {}) {
    const timeout = new AbortController();
    const abort = () => timeout.abort();
    const parent = options.signal;
    if (parent?.aborted) timeout.abort();
    else parent?.addEventListener("abort", abort, {once: true});
    const timer = window.setTimeout(abort, 12000);
    try {
      const response = await BloodRaven.fetch(url, {...options, signal: timeout.signal, credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json", ...options.headers}});
      if (!response.ok) {
        if (response.status === 401) throw new Error("La sesión requiere autenticación. Vuelve a cargar el panel para identificarte.");
        throw new Error(`El servidor respondió con HTTP ${response.status}.`);
      }
      return await response.json();
    } finally {
      window.clearTimeout(timer);
      parent?.removeEventListener("abort", abort);
    }
  }

  function updateTimes() {
    text("#clock", new Intl.DateTimeFormat("es-PA", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(new Date()));
    const overview = state.overview;
    if (!overview) return;
    text("#poll-age", `Última consulta: ${age(overview.last_poll_at)}`);
    text("#snapshot-time", `${overview.mode === "demo" ? "Datos ficticios" : "Datos del servidor"} · Actualización: ${stamp(overview.generated_at)}`);
    document.querySelectorAll("[data-last-success]").forEach((node) => { node.textContent = `Último dato: ${age(node.dataset.lastSuccess)}`; });
    const current = device();
    if (current) {
      text("#detail-last", `Último dato válido: ${age(current.last_success_at)}`);
      $("#detail-last").title = stamp(current.last_success_at);
    }
  }

  function renderSummary() {
    const overview = state.overview;
    const summary = overview.summary || {};
    const total = finite(summary.total) ? summary.total : overview.devices.length;
    const responding = finite(summary.responding) ? summary.responding : overview.devices.filter((item) => ["ok", "warning"].includes(item.status)).length;
    const count = (name) => finite(summary[name]) ? summary[name] : overview.devices.filter((item) => item.status === name).length;
    const offline = count("offline"), warning = count("warning"), pending = count("pending"), stale = count("stale");
    const sites = new Set(overview.devices.map((item) => item.site)).size;
    text("#inventory-description", `${sites} ${sites === 1 ? "sucursal" : "sucursales"} · ${total} ${total === 1 ? "switch" : "switches"} en inventario`);
    const badge = $("#mode-badge");
    badge.className = `mode-badge ${overview.mode === "demo" ? "demo" : "live"}`;
    badge.textContent = overview.mode === "demo" ? "DEMO · Datos ficticios" : "MODO REAL · SNMP";
    text("#health-count", state.apiError ? "—" : `${responding}/${total}`);
    const degrees = state.apiError || total === 0 ? 0 : Math.min(360, Math.max(0, responding / total * 360));
    $("#health-ring").style.background = `conic-gradient(var(--ok) ${degrees}deg, var(--line) ${degrees}deg)`;
    $("#health-ring").setAttribute("aria-label", state.apiError ? "Estado actual sin confirmar: la API no responde" : `${responding} de ${total} switches responden a SNMP`);
    text("#health-title", state.apiError ? "Sin actualizar" : total === 0 ? "Sin equipos configurados" : `${responding} ${responding === 1 ? "equipo responde" : "equipos responden"}`);
    text("#health-description", state.apiError ? "Comprueba la conexión al servidor" : warning ? `${warning} con advertencia` : offline ? `${offline} sin respuesta` : pending || stale ? `${pending + stale} por confirmar` : "Sin alertas activas");
    const attention = [];
    if (offline) attention.push(`${offline} sin respuesta SNMP`);
    if (warning) attention.push(`${warning} con advertencia`);
    if (pending) attention.push(`${pending} ${pending === 1 ? "pendiente de lectura" : "pendientes de lectura"}`);
    if (stale) attention.push(`${stale} con datos desactualizados`);
    $("#attention").hidden = attention.length === 0 || state.apiError;
    text("#attention-text", attention.join(" · "));
    const demo = overview.mode === "demo";
    $("#telegram-demo").hidden = !demo;
    $("#telegram-live").hidden = demo;
    text("#telegram-subtitle", demo ? "Telegram · conversación simulada" : "Guía de consultas desde tu teléfono");
    text("#live-site-command", `/sucursal ${state.selected || "sede-uno"}`);
  }

  function renderDevices() {
    const container = $("#devices");
    const focused = document.activeElement?.dataset.device;
    const fragment = document.createDocumentFragment();
    for (const item of state.overview.devices) {
      const card = element("button", "device-card");
      card.type = "button";
      card.dataset.device = item.id;
      card.dataset.status = status(item);
      card.setAttribute("aria-pressed", String(item.id === state.selected));
      card.setAttribute("aria-label", `Ver ${item.site}, ${item.name}: ${labels[status(item)]}`);
      const top = element("div", "device-top");
      const dot = element("span", "device-dot");
      dot.setAttribute("aria-hidden", "true");
      top.append(element("span", "device-site", item.site), dot);
      card.append(top, element("div", "device-id", item.name), element("div", "device-model", item.model), element("div", "device-state", labels[status(item)]));
      const cpu = element("div", "device-metric");
      cpu.append(element("span", "", "CPU"), element("strong", "", metric(item, "cpu_percent", " %")));
      const meter = element("div", "meter");
      meter.setAttribute("aria-hidden", "true");
      if (usable(item) && finite(item.cpu_percent)) {
        const fill = element("span");
        fill.style.width = `${Math.max(0, Math.min(100, item.cpu_percent))}%`;
        meter.append(fill);
      } else meter.classList.add("unavailable");
      const metrics = element("div", "mini-metrics");
      const temperature = element("div"), traffic = element("div");
      temperature.append(element("span", "", "Temperatura"), element("strong", "", metric(item, "temperature_c", " °C")));
      traffic.append(element("span", "", "↓ Entrada / ↑ Salida"));
      const trafficValue = element("strong", "", `${metric(item, "rx_mbps")} / ${metric(item, "tx_mbps")}`);
      trafficValue.append(element("small", "", " Mbps"));
      traffic.append(trafficValue);
      metrics.append(temperature, traffic);
      const foot = element("div", "device-foot");
      const last = element("span", "", `Último dato: ${age(item.last_success_at)}`);
      last.dataset.lastSuccess = item.last_success_at || "";
      last.title = stamp(item.last_success_at);
      const arrow = element("span", "arrow", "↗");
      arrow.setAttribute("aria-hidden", "true");
      foot.append(last, arrow);
      card.append(cpu, meter, metrics, foot);
      fragment.append(card);
    }
    if (!state.overview.devices.length) fragment.append(element("div", "empty-state", "Todavía no hay switches en el inventario del servidor."));
    container.replaceChildren(fragment);
    container.setAttribute("aria-busy", "false");
    if (focused) [...container.querySelectorAll("[data-device]")].find((node) => node.dataset.device === focused)?.focus({preventScroll: true});
  }

  function renderDetail() {
    const item = device();
    if (!item) {
      text("#detail-name", "Sin equipos en inventario");
      text("#detail-device", "Agrega los switches en la configuración del servidor.");
      $("#detail-values").replaceChildren();
      return;
    }
    const currentStatus = status(item);
    text("#detail-name", item.site);
    text("#detail-device", `${item.name} · ${item.model}`);
    text("#detail-status", labels[currentStatus]);
    $("#detail-status").className = `status-pill ${currentStatus}`;
    text("#detail-uptime", `Tiempo encendido: ${usable(item) ? uptime(item.uptime_seconds) : "—"}`);
    const values = document.createDocumentFragment();
    for (const [label, key, unit] of [["CPU", "cpu_percent", "%"], ["Temperatura", "temperature_c", "°C"]]) {
      const column = element("div"), value = element("strong", "value-number", metric(item, key));
      if (usable(item) && finite(item[key])) value.append(element("small", "", ` ${unit}`));
      column.append(element("span", "value-label", label), value);
      values.append(column);
    }
    const traffic = element("div"), trafficValue = element("strong", "value-number traffic-value", `${metric(item, "rx_mbps")} / ${metric(item, "tx_mbps")}`);
    traffic.append(element("span", "value-label", "↓ Entrada / ↑ Salida"), trafficValue, element("div", "value-label", "Mbps"));
    values.append(traffic);
    $("#detail-values").replaceChildren(values);
    text("#interface-name", item.interface ? `${item.interface.name || "Interfaz"} · índice ${item.interface.index ?? "—"}` : "Interfaz de enlace por configurar");
    let note = "El tráfico corresponde a la interfaz seleccionada. Su relación con internet depende de la topología de la sucursal.";
    if (currentStatus === "offline") note = "Sin respuesta SNMP. Las métricas actuales no están disponibles; este estado no confirma que el switch esté apagado.";
    else if (currentStatus === "stale") note = "Los datos dejaron de actualizarse. Las métricas actuales se ocultan hasta recibir una lectura reciente.";
    else if (currentStatus === "pending") note = "Esperando la primera lectura válida. Revisa en el servidor la configuración y el acceso SNMP de este equipo.";
    else if (currentStatus === "warning") note = "El equipo responde y presenta una advertencia. Consulta los eventos para revisar el motivo.";
    if (item.error) note += ` Detalle del servidor: ${String(item.error)}`;
    if (state.overview.mode === "demo") note += " Todas las lecturas de este modo son ficticias.";
    text("#detail-note", note);
    text("#live-site-command", `/sucursal ${item.id}`);
    updateTimes();
  }

  const svgNode = (name, attributes = {}, content) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (content !== undefined) node.textContent = String(content);
    return node;
  };

  function drawChart() {
    const svg = $("#history-chart");
    if ($("#detail").hidden) return;
    const width = Math.max(230, svg.getBoundingClientRect().width), height = svg.getBoundingClientRect().height || 174;
    const left = 40, right = 10, top = 20, bottom = 26, plotW = width - left - right, plotH = height - top - bottom;
    const end = date(state.overview?.generated_at)?.valueOf() || Date.now(), start = end - 3600000;
    const samples = state.history.map((sample) => ({...sample, time: date(sample.timestamp)?.valueOf()})).filter((sample) => finite(sample.time) && sample.time >= start && sample.time <= end).sort((a, b) => a.time - b.time);
    const valid = (sample, key) => (sample.status === "ok" || sample.status === "warning") && finite(sample[key]) && sample[key] >= 0;
    const values = samples.flatMap((sample) => ["rx_mbps", "tx_mbps"].filter((key) => valid(sample, key)).map((key) => sample[key]));
    const maxValue = values.length ? Math.max(...values) : 0;
    const rawMax = Math.max(1, maxValue * 1.15), magnitude = 10 ** Math.floor(Math.log10(rawMax));
    const ceiling = Math.ceil(rawMax / magnitude * 2) / 2 * magnitude;
    const x = (time) => left + (time - start) / 3600000 * plotW;
    const y = (value) => top + plotH - value / ceiling * plotH;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const fragment = document.createDocumentFragment();
    fragment.append(svgNode("title", {}, `${device()?.site || "Switch"}: historial de entrada y salida. Las muestras ausentes interrumpen las líneas.`));
    for (const fraction of [0, .5, 1]) {
      const level = ceiling * fraction;
      fragment.append(svgNode("line", {x1: left, y1: y(level), x2: width - right, y2: y(level), stroke: "#29384a", "stroke-dasharray": "3 4"}), svgNode("text", {x: left - 8, y: y(level) + 3, "text-anchor": "end", fill: "#a0afc1", "font-size": 9}, number(level)));
    }
    for (const [position, anchor] of [[0, "start"], [.5, "middle"], [1, "end"]]) fragment.append(svgNode("text", {x: left + plotW * position, y: height - 5, "text-anchor": anchor, fill: "#a0afc1", "font-size": 9}, shortTime.format(new Date(start + 3600000 * position))));
    for (const [key, color] of [["rx_mbps", "#68e4b5"], ["tx_mbps", "#80b7ef"]]) {
      let segment = [];
      const flush = () => {
        if (segment.length > 1) fragment.append(svgNode("polyline", {points: segment.map((sample) => `${x(sample.time).toFixed(2)},${y(sample[key]).toFixed(2)}`).join(" "), fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round"}));
        else if (segment.length === 1) fragment.append(svgNode("circle", {cx: x(segment[0].time), cy: y(segment[0][key]), r: 2.5, fill: color}));
        segment = [];
      };
      for (const sample of samples) {
        if (!valid(sample, key)) { flush(); continue; }
        if (segment.length && sample.time - segment[segment.length - 1].time > 90000) flush();
        segment.push(sample);
      }
      flush();
    }
    svg.replaceChildren(fragment);
    if (!$("#history-message").dataset.error && !$("#history-message").dataset.loading) {
      $("#history-message").hidden = values.length > 0;
      text("#history-message", samples.length ? "No hay mediciones de tráfico válidas en esta hora. El primer cálculo requiere dos lecturas de los contadores de la interfaz." : "Todavía no hay muestras para esta hora. El historial se construye con las lecturas del servidor.");
    }
    text("#history-count", `${samples.length} ${samples.length === 1 ? "muestra" : "muestras"} · huecos = sin lectura`);
  }

  async function loadHistory(force = false) {
    if (!state.selected || !state.overview) return;
    const key = `${state.selected}|${state.overview.last_poll_at || state.overview.generated_at}|${state.overview.mode}`;
    if (!force && key === state.historyKey) return;
    state.historyController?.abort();
    const controller = new AbortController();
    state.historyController = controller;
    const id = ++state.historyRequest, selected = state.selected;
    const message = $("#history-message");
    message.dataset.loading = "true";
    delete message.dataset.error;
    message.hidden = false;
    text("#history-message", "Consultando historial…");
    try {
      const result = await request(`/api/devices/${encodeURIComponent(selected)}/history?hours=1`, {signal: controller.signal});
      if (id !== state.historyRequest || selected !== state.selected) return;
      if (!Array.isArray(result.samples)) throw new Error("El servidor no devolvió un historial válido.");
      state.history = result.samples;
      state.historyKey = key;
      delete message.dataset.loading;
      drawChart();
    } catch (error) {
      if (controller.signal.aborted || id !== state.historyRequest) return;
      state.history = [];
      state.historyKey = null;
      delete message.dataset.loading;
      message.dataset.error = "true";
      message.hidden = false;
      text("#history-message", "No se pudo cargar el historial. Se volverá a intentar con la próxima actualización.");
      drawChart();
    }
  }

  const eventNames = {offline: "Sin respuesta SNMP", recovery: "Comunicación recuperada", recovered: "Comunicación recuperada", warning: "Advertencia", temperature: "Temperatura elevada", temperature_high: "Temperatura elevada", cpu_high: "CPU elevada", stale: "Datos desactualizados", ok: "Comunicación recuperada"};

  async function loadEvents() {
    state.eventsController?.abort();
    const controller = new AbortController();
    state.eventsController = controller;
    const id = ++state.eventsRequest;
    $("#events").setAttribute("aria-busy", "true");
    try {
      const result = await request("/api/events?hours=24", {signal: controller.signal});
      if (id !== state.eventsRequest) return;
      if (!Array.isArray(result.events)) throw new Error("Registro de eventos inválido.");
      const fragment = document.createDocumentFragment();
      const events = [...result.events].sort((a, b) => (date(b.started_at)?.valueOf() || 0) - (date(a.started_at)?.valueOf() || 0));
      for (const item of events) {
        const row = element("article", "event"), time = element("time");
        const started = date(item.started_at);
        time.textContent = started ? shortTime.format(started) : "—";
        if (started) { time.dateTime = started.toISOString(); time.title = stamp(item.started_at); }
        time.append(element("span", "", started ? dateOnly.format(started) : "Sin fecha"));
        const recovery = ["recovery", "recovered", "ok"].includes(item.kind);
        const dot = element("span", `event-dot ${recovery ? "recovery" : item.kind === "offline" ? "offline" : ""}`);
        dot.setAttribute("aria-hidden", "true");
        const body = element("div");
        body.append(element("strong", "", `${item.site || item.device_name || "Equipo"} · ${eventNames[item.kind] || item.kind || "Evento"}`), element("p", "", item.message || "Sin detalle adicional."));
        body.append(element("p", "event-resolution", item.ended_at ? `Finalizado: ${stamp(item.ended_at)}` : recovery ? "Recuperación registrada" : "Sin cierre registrado"));
        row.append(time, dot, body);
        if (state.overview?.devices.some((current) => current.id === item.device_id)) {
          const link = element("button", "event-device", "Ver equipo ↗");
          link.type = "button";
          link.dataset.device = item.device_id;
          row.append(link);
        }
        fragment.append(row);
      }
      if (!events.length) fragment.append(element("p", "empty-state", "Sin eventos registrados en las últimas 24 horas."));
      $("#events").replaceChildren(fragment);
      text("#event-count", `${events.length}`);
    } catch (error) {
      if (controller.signal.aborted || id !== state.eventsRequest) return;
      $("#events").replaceChildren(element("p", "empty-state", "No se pudo cargar el registro. Se volverá a intentar con la próxima actualización."));
      text("#event-count", "—");
    } finally {
      if (id === state.eventsRequest) $("#events").setAttribute("aria-busy", "false");
    }
  }

  async function sendCommand(raw) {
    const command = String(raw || "").trim().slice(0, 160);
    if (!command || state.botBusy || state.overview?.mode !== "demo") return;
    state.botBusy = true;
    text("#user-message", command);
    text("#bot-message", "Consultando los datos de demostración…");
    $("#command-submit").disabled = true;
    document.querySelectorAll("[data-command]").forEach((button) => { button.disabled = true; });
    try {
      const response = await request("/api/demo/telegram", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({command})});
      if (typeof response.reply !== "string" || response.simulated !== true) throw new Error("Respuesta de simulación inválida.");
      text("#bot-message", response.reply);
      text("#bot-stamp", `Simulación · ${shortTime.format(new Date())} · datos ficticios`);
    } catch (error) {
      text("#bot-message", "No se pudo consultar el simulador. Comprueba la conexión al servidor e inténtalo otra vez.");
      text("#bot-stamp", "Consulta sin respuesta");
    } finally {
      state.botBusy = false;
      $("#command-submit").disabled = false;
      document.querySelectorAll("[data-command]").forEach((button) => { button.disabled = false; });
    }
  }

  function selectDevice(id) {
    if (!state.overview?.devices.some((item) => item.id === id)) return;
    const changed = state.selected !== id;
    state.selected = id;
    if (changed) { state.history = []; state.historyKey = null; }
    renderDevices();
    renderDetail();
    drawChart();
    if (state.view !== "general") setView("general");
    else loadHistory();
  }

  function setView(next) {
    state.view = ["general", "events", "telegram"].includes(next) ? next : "general";
    document.querySelectorAll("[data-view]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.view === state.view)));
    $("#overview").hidden = state.view !== "general";
    $("#detail").hidden = state.view !== "general";
    $("#events-panel").hidden = state.view !== "events";
    $("#telegram-panel").hidden = state.view === "events";
    $("#bottom").classList.toggle("telegram-only", state.view === "telegram");
    $("#bottom").classList.toggle("events-only", state.view === "events");
    text("#page-title", {general: "Tu red, de un vistazo.", events: "Cada evento, en contexto.", telegram: "Tu red, en el bolsillo."}[state.view]);
    if (state.view === "events") loadEvents();
    if (state.view === "general") { requestAnimationFrame(drawChart); loadHistory(); }
  }

  async function refresh() {
    if (state.refreshing) return;
    window.clearTimeout(state.timer);
    state.refreshing = true;
    $("#retry").disabled = true;
    try {
      const overview = await request("/api/overview");
      if (!Array.isArray(overview.devices) || !["demo", "live"].includes(overview.mode)) throw new Error("El servidor no devolvió un inventario válido.");
      const modeChanged = state.overview && overview.mode !== state.overview.mode;
      state.overview = overview;
      state.apiError = false;
      $("#api-error").hidden = true;
      if (!device()) state.selected = overview.devices[0]?.id || null;
      if (modeChanged) { state.history = []; state.historyKey = null; }
      renderSummary();
      renderDevices();
      renderDetail();
      updateTimes();
      if (state.view === "general") loadHistory();
      if (state.view === "events") loadEvents();
      if (overview.mode === "demo" && state.firstDemo) { state.firstDemo = false; sendCommand("/estado"); }
    } catch (error) {
      state.apiError = true;
      $("#api-error").hidden = false;
      text("#api-error-message", error.name === "AbortError" ? "El servidor tardó demasiado en responder. Reintentamos automáticamente cada 15 segundos." : `${error.message} Reintentamos automáticamente cada 15 segundos.`);
      if (state.overview) { renderSummary(); renderDevices(); renderDetail(); }
      else {
        $("#devices").replaceChildren(element("p", "empty-state", "El inventario aparecerá cuando se restablezca la conexión."));
        $("#devices").setAttribute("aria-busy", "false");
        text("#health-title", "Servidor sin respuesta");
        text("#health-description", "Aún no hay datos disponibles");
        text("#mode-badge", "Sin conexión");
      }
    } finally {
      state.refreshing = false;
      $("#retry").disabled = false;
      state.timer = window.setTimeout(refresh, 15000);
    }
  }

  $("#devices").addEventListener("click", (event) => { const button = event.target.closest("[data-device]"); if (button) selectDevice(button.dataset.device); });
  $("#events").addEventListener("click", (event) => { const button = event.target.closest("[data-device]"); if (button) { selectDevice(button.dataset.device); $("#detail-name").scrollIntoView({block: "center"}); } });
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $(".monogram").addEventListener("click", (event) => { event.preventDefault(); setView("general"); });
  $("#show-events").addEventListener("click", () => setView("events"));
  $("#retry").addEventListener("click", refresh);
  document.querySelectorAll("[data-command]").forEach((button) => button.addEventListener("click", () => sendCommand(button.dataset.command === "selected" ? `/sucursal ${state.selected || "sede-uno"}` : button.dataset.command)));
  $("#command-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#command-input");
    if (!state.botBusy && input.value.trim()) { sendCommand(input.value); input.value = ""; }
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  if ("ResizeObserver" in window) new ResizeObserver(() => requestAnimationFrame(drawChart)).observe($("#history-chart"));
  else window.addEventListener("resize", drawChart);
  updateTimes();
  window.setInterval(updateTimes, 1000);
  refresh();
})();
