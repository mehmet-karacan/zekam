(() => {
  "use strict";

  const snapshotSchema = "zekam-observatory-snapshot/v3";
  const liveStates = new Set(["live", "waiting", "unbound"]);
  const dangerousStates = new Set(["recovery-required", "receiptless", "completed-unbound", "blocked", "expired"]);
  const clientLabels = { opencode: "OpenCode", codex: "Codex", claude: "Claude", zekam: "Zekam CLI" };
  const safeActionLabels = { unknown: "Bilinmiyor", planning: "Planlıyor", executing: "Yürütüyor", tool: "Tool çalışıyor", waiting: "Bekliyor" };
  const anchors = { opencode: [0.18, 0.28], codex: [0.22, 0.73], claude: [0.52, 0.23], zekam: [0.54, 0.72], runtime: [0.80, 0.50] };
  const colors = { live: "#ffc15a", waiting: "#ff941f", unbound: "#a76f32", stale: "#665c50", danger: "#f03a2f", runtime: "#d88a2c" };

  const canvas = document.getElementById("execution-canvas");
  const context = canvas.getContext?.("2d") || null;
  const graphStage = document.getElementById("graph-stage");
  const fallback = document.getElementById("graph-fallback");
  const searchInput = document.getElementById("search-input");
  const clientFilter = document.getElementById("client-filter");
  const stateFilter = document.getElementById("state-filter");
  const projectFilter = document.getElementById("project-filter");
  const motionToggle = document.getElementById("motion-toggle");
  const viewToggle = document.getElementById("view-toggle");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const state = {
    snapshot: null,
    structure: null,
    telemetry: null,
    nodes: [],
    edges: [],
    positions: new Map(),
    nodeMap: new Map(),
    query: "",
    client: "all",
    status: "all",
    project: "all",
    paused: reducedMotion.matches,
    listMode: !context,
    hovered: null,
    selected: null,
    width: 0,
    height: 0,
    time: 0,
    structureDigest: "",
    telemetryDigest: "",
    pollingTimer: null,
  };

  function hash(value) {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return result >>> 0;
  }

  function shortId(value) {
    if (!value) return "—";
    const text = String(value);
    return text.length <= 12 ? text : `${text.slice(0, 7)}…${text.slice(-4)}`;
  }

  function fmtTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
  }

  function fmtAge(value) {
    if (!value) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (seconds < 60) return `${seconds} sn`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} dk`;
    return `${Math.floor(seconds / 3600)} sa`;
  }

  function text(element, value) {
    element.textContent = value == null ? "—" : String(value);
  }

  function setConnection(kind, label) {
    const target = document.getElementById("connection-state");
    target.className = `status-chip is-${kind}`;
    target.lastChild.textContent = ` ${label}`;
  }

  function currentAgents() {
    return state.telemetry?.agents || state.snapshot?.agents || [];
  }

  function currentEvents() {
    return state.telemetry?.events || state.snapshot?.events || [];
  }

  function currentCanonical() {
    return state.structure?.canonical_runtime || state.snapshot?.canonical_runtime || { entities: [], contradictions: [], available: false };
  }

  function matchesFilters(item) {
    if (state.client !== "all" && item.client && item.client !== state.client) return false;
    const itemState = item.availability || item.state || "unknown";
    if (state.status !== "all" && itemState !== state.status) return false;
    if (state.project !== "all" && item.project_id !== state.project) return false;
    if (!state.query) return true;
    const haystack = [item.label, item.client, item.state, item.availability, item.current_action, item.kind, item.entity_id, item.work_item_id, item.job_id]
      .filter(Boolean).join(" ").toLocaleLowerCase("tr-TR");
    return haystack.includes(state.query);
  }

  function updateProjectFilter() {
    const values = new Set();
    for (const agent of currentAgents()) if (agent.project_id) values.add(agent.project_id);
    for (const entity of currentCanonical().entities || []) if (entity.project_id) values.add(entity.project_id);
    const current = projectFilter.value;
    projectFilter.replaceChildren(new Option("Tüm projeler", "all"));
    for (const value of [...values].sort()) projectFilter.append(new Option(shortId(value), value));
    projectFilter.value = values.has(current) ? current : "all";
    state.project = projectFilter.value;
  }

  function renderMetrics() {
    const agents = currentAgents();
    const canonical = currentCanonical();
    const openProcessIds = new Set(agents.filter((item) => item.process_id && liveStates.has(item.availability)).map((item) => item.process_id));
    const liveSessions = agents.filter((item) => item.session_id && item.process_id && ["live", "waiting"].includes(item.availability)).length;
    const activeAgents = (canonical.entities || []).filter((item) => item.kind.startsWith("agent-") && item.state === "active").length;
    const runningTools = agents.filter((item) => item.current_action === "tool" && item.process_id).length;
    const recovery = (canonical.contradictions || []).filter((item) => item.state === "recovery-required").length + (canonical.entities || []).filter((item) => item.state === "recovery-required").length;
    const receiptless = (canonical.entities || []).filter((item) => item.kind === "claim" && item.state === "receiptless").length;
    const values = { "open-cli": openProcessIds.size, "live-session": liveSessions, "active-agent": activeAgents, "running-tool": runningTools, recovery, receiptless };
    for (const [key, value] of Object.entries(values)) text(document.querySelector(`[data-metric="${key}"] strong`), value);
  }

  function stateLabel(value) {
    const labels = { live: "CANLI", waiting: "BEKLİYOR", stale: "STALE", unbound: "UNBOUND", "recovery-required": "RECOVERY", blocked: "BLOKLU", completed: "TAMAMLANDI", "completed-unbound": "KANITSIZ" };
    return labels[value] || String(value || "BİLİNMİYOR").toLocaleUpperCase("tr-TR");
  }

  function renderSessionCards() {
    const target = document.getElementById("session-cards");
    const roots = currentAgents().filter((item) => item.process_id && liveStates.has(item.availability) && matchesFilters(item));
    target.replaceChildren();
    for (const agent of roots) {
      const card = document.createElement("article");
      card.className = "session-card";
      card.dataset.state = agent.availability;
      card.tabIndex = 0;
      const header = document.createElement("header");
      const title = document.createElement("h3");
      text(title, clientLabels[agent.client] || agent.client);
      const status = document.createElement("span");
      status.className = "state";
      text(status, stateLabel(agent.availability));
      header.append(title, status);
      const facts = document.createElement("dl");
      const rows = [
        ["Process", shortId(agent.process_id)],
        ["Session", agent.session_id ? shortId(agent.session_id) : "eşleşmedi"],
        ["Bağ güveni", agent.binding_confidence],
        ["Aksiyon", safeActionLabels[agent.current_action] || "Bilinmiyor"],
        ["OS durumu", agent.process_status || "unknown"],
        ["Yaş", fmtAge(agent.started_at)],
      ];
      for (const [label, value] of rows) {
        const row = document.createElement("div");
        const dt = document.createElement("dt"); const dd = document.createElement("dd");
        text(dt, label); text(dd, value); row.append(dt, dd); facts.append(row);
      }
      card.append(header, facts);
      card.addEventListener("click", () => openDetail(agent, "CLI / SESSION"));
      card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") openDetail(agent, "CLI / SESSION"); });
      target.append(card);
    }
    text(document.getElementById("root-process-count"), `${roots.length} CLI`);
    if (!roots.length) {
      const empty = document.createElement("p"); empty.className = "empty-state"; text(empty, "Filtreye uyan açık root CLI yok."); target.append(empty);
    }
  }

  function renderRegistry() {
    const target = document.getElementById("session-registry");
    const rows = currentAgents().filter((item) => item.session_id && matchesFilters(item));
    target.replaceChildren();
    for (const agent of rows.slice(0, 64)) {
      const tr = document.createElement("tr");
      const values = [clientLabels[agent.client] || agent.client, stateLabel(agent.availability), agent.binding_confidence, safeActionLabels[agent.current_action] || "Bilinmiyor", fmtAge(agent.heartbeat_at)];
      for (const [index, value] of values.entries()) { const td = document.createElement("td"); if (index === 1) td.className = "state-mark"; text(td, value); tr.append(td); }
      tr.addEventListener("click", () => openDetail(agent, "SESSION"));
      target.append(tr);
    }
    text(document.getElementById("session-total"), rows.length);
  }

  function renderEvents() {
    const target = document.getElementById("event-feed");
    const rows = currentEvents().filter((item) => state.client === "all" || item.source === state.client).slice(0, 48);
    target.replaceChildren();
    for (const event of rows) {
      const li = document.createElement("li"); const strong = document.createElement("strong"); const meta = document.createElement("span");
      text(strong, event.event_type); text(meta, `${event.source} · ${fmtTime(event.occurred_at)} · ${shortId(event.job_id || event.agent_id)}`);
      li.append(strong, meta); li.addEventListener("click", () => openDetail(event, "OLAY")); target.append(li);
    }
    text(document.getElementById("event-total"), rows.length);
  }

  function renderRuntimeChain() {
    const target = document.getElementById("runtime-chain");
    const canonical = currentCanonical();
    const acceptedKinds = new Set(["job", "attempt", "lease", "claim", "receipt"]);
    const rows = (canonical.entities || []).filter((item) => (acceptedKinds.has(item.kind) || item.kind.startsWith("agent-")) && matchesFilters(item)).slice(0, 48);
    target.replaceChildren();
    for (const entity of rows) {
      const row = document.createElement("button"); row.type = "button"; row.className = `chain-row${dangerousStates.has(entity.state) ? " is-danger" : ""}`;
      const kind = document.createElement("span"); kind.className = "kind"; text(kind, entity.kind);
      const id = document.createElement("span"); id.className = "id"; text(id, shortId(entity.entity_id));
      const status = document.createElement("span"); status.className = "chain-state"; text(status, stateLabel(entity.state));
      row.append(kind, id, status); row.addEventListener("click", () => openDetail(entity, "KANONİK RUNTIME")); target.append(row);
    }
    text(document.getElementById("contradiction-total"), (canonical.contradictions || []).length);
  }

  function renderResources() {
    const target = document.getElementById("resource-bars");
    const rows = currentAgents().filter((item) => item.process_id && liveStates.has(item.availability) && matchesFilters(item));
    target.replaceChildren();
    for (const agent of rows) {
      const cpu = Math.min(100, Math.max(0, Number(agent.cpu_percent || 0)));
      const memoryMb = Math.max(0, Number(agent.rss_bytes || 0) / 1048576);
      const item = document.createElement("div"); item.className = "resource-item";
      const header = document.createElement("header"); const label = document.createElement("span"); const value = document.createElement("span");
      text(label, clientLabels[agent.client] || agent.client); text(value, `${cpu.toFixed(1)}% · ${memoryMb.toFixed(0)} MB`); header.append(label, value);
      const track = document.createElement("div"); track.className = "resource-track"; const fill = document.createElement("i"); fill.style.width = `${Math.max(2, cpu)}%`; track.append(fill); item.append(header, track); target.append(item);
    }
    if (!rows.length) { const empty = document.createElement("p"); empty.className = "hero-copy"; text(empty, "Canlı process telemetrisi yok."); target.append(empty); }
  }

  function graphKind(entity) {
    if (entity.kind.startsWith("agent-")) return "agent";
    return entity.kind;
  }

  function buildGraph() {
    const nodes = [];
    const edges = [];
    const known = new Set();
    const addNode = (node) => { if (!known.has(node.id)) { known.add(node.id); nodes.push(node); } };
    const agents = currentAgents().filter(matchesFilters);
    for (const agent of agents) {
      const clientId = `client:${agent.client}`;
      addNode({ id: clientId, label: clientLabels[agent.client] || agent.client, kind: "client", client: agent.client, state: "live", data: { client: agent.client, state: "live" } });
      const nodeId = agent.process_id || `session:${agent.session_id || agent.agent_id}`;
      addNode({ id: nodeId, label: agent.process_id ? (clientLabels[agent.client] || agent.client) : `${clientLabels[agent.client] || agent.client} stale`, kind: agent.process_id ? "process" : "session", client: agent.client, state: agent.availability, project_id: agent.project_id, data: agent });
      edges.push({ source: clientId, target: nodeId, kind: agent.process_id ? "runs-process" : "observed-session" });
      if (agent.session_id && agent.process_id) {
        const sessionId = `session:${agent.session_id}`;
        addNode({ id: sessionId, label: `Session ${shortId(agent.session_id)}`, kind: "session", client: agent.client, state: agent.availability, data: agent });
        edges.push({ source: nodeId, target: sessionId, kind: `${agent.binding_confidence}-bind` });
      }
    }
    const canonical = currentCanonical();
    const runtimeEntities = (canonical.entities || []).filter(matchesFilters).slice(0, 160);
    for (const entity of runtimeEntities) {
      addNode({ id: entity.entity_id, label: `${entity.kind} ${shortId(entity.entity_id.split(":").pop())}`, kind: graphKind(entity), client: "runtime", state: entity.state, project_id: entity.project_id, data: entity });
    }
    for (const entity of runtimeEntities) if (entity.parent_id && known.has(entity.parent_id) && known.has(entity.entity_id)) edges.push({ source: entity.parent_id, target: entity.entity_id, kind: "canonical-chain" });
    for (const contradiction of canonical.contradictions || []) {
      if (!matchesFilters(contradiction)) continue;
      const id = `contradiction:${contradiction.contradiction_id}`;
      addNode({ id, label: contradiction.kind, kind: "contradiction", client: "runtime", state: "recovery-required", data: contradiction });
      const target = contradiction.job_id ? `job:${contradiction.job_id}` : null;
      if (target && known.has(target)) edges.push({ source: target, target: id, kind: "contradiction" });
    }
    state.nodes = nodes;
    state.edges = edges.filter((edge) => known.has(edge.source) && known.has(edge.target));
    state.nodeMap = new Map(nodes.map((node) => [node.id, node]));
    calculatePositions();
    renderFallback();
    text(document.getElementById("visible-node-count"), nodes.length);
    text(document.getElementById("visible-edge-count"), state.edges.length);
    document.getElementById("graph-empty").hidden = nodes.length > 0;
  }

  function calculatePositions() {
    const groups = new Map();
    for (const node of state.nodes) { const group = node.client in anchors ? node.client : "runtime"; if (!groups.has(group)) groups.set(group, []); groups.get(group).push(node); }
    const positions = new Map();
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (const [group, rows] of groups.entries()) {
      rows.sort((left, right) => left.id.localeCompare(right.id));
      const [anchorX, anchorY] = anchors[group] || anchors.runtime;
      rows.forEach((node, index) => {
        if (node.kind === "client") { positions.set(node.id, { x: anchorX, y: anchorY }); return; }
        const seed = hash(node.id) / 4294967295;
        const radius = Math.min(group === "runtime" ? .22 : .16, .035 + Math.sqrt((index + 1) / Math.max(1, rows.length)) * (group === "runtime" ? .20 : .14));
        const angle = index * goldenAngle + seed * .7;
        positions.set(node.id, { x: Math.max(.04, Math.min(.96, anchorX + Math.cos(angle) * radius)), y: Math.max(.08, Math.min(.92, anchorY + Math.sin(angle) * radius)) });
      });
    }
    state.positions = positions;
  }

  function renderFallback() {
    fallback.replaceChildren();
    for (const node of state.nodes.slice(0, 256)) {
      const row = document.createElement("button"); row.type = "button"; row.className = "fallback-row";
      const kind = document.createElement("span"); const label = document.createElement("span"); const status = document.createElement("small");
      text(kind, node.kind); text(label, node.label); text(status, stateLabel(node.state)); row.append(kind, label, status);
      row.addEventListener("click", () => openDetail(node.data, node.kind)); fallback.append(row);
    }
  }

  function resizeCanvas() {
    if (!context) return;
    const bounds = graphStage.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    state.width = Math.max(1, bounds.width); state.height = Math.max(1, bounds.height);
    canvas.width = Math.round(state.width * ratio); canvas.height = Math.round(state.height * ratio);
    canvas.style.width = `${state.width}px`; canvas.style.height = `${state.height}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  function point(nodeId) {
    const position = state.positions.get(nodeId); if (!position) return null;
    return { x: position.x * state.width, y: position.y * state.height };
  }

  function nodeColor(node) {
    if (node.kind === "contradiction" || dangerousStates.has(node.state)) return colors.danger;
    if (node.client === "runtime") return colors.runtime;
    return colors[node.state] || colors.stale;
  }

  function draw(timestamp) {
    if (!context) return;
    if (!state.paused) state.time = timestamp;
    context.clearRect(0, 0, state.width, state.height);
    for (const edge of state.edges) {
      const source = point(edge.source); const target = point(edge.target); if (!source || !target) continue;
      const danger = edge.kind === "contradiction";
      context.beginPath(); context.moveTo(source.x, source.y);
      const bend = (hash(`${edge.source}:${edge.target}`) % 31) - 15;
      context.quadraticCurveTo((source.x + target.x) / 2 + bend, (source.y + target.y) / 2 - bend, target.x, target.y);
      context.strokeStyle = danger ? "rgba(240,58,47,.72)" : "rgba(255,145,31,.24)";
      context.lineWidth = danger ? 1.7 : 1; context.stroke();
      if (!state.paused && !reducedMotion.matches && state.edges.indexOf(edge) < 120) {
        const phase = (state.time * .00014 + (hash(edge.source) % 1000) / 1000) % 1;
        const x = source.x + (target.x - source.x) * phase; const y = source.y + (target.y - source.y) * phase;
        context.beginPath(); context.arc(x, y, danger ? 2.1 : 1.4, 0, Math.PI * 2); context.fillStyle = danger ? colors.red : colors.gold; context.fill();
      }
    }
    const ordered = [...state.nodes].sort((a, b) => Number(b.kind === "client") - Number(a.kind === "client") || a.id.localeCompare(b.id));
    for (const node of ordered) {
      const p = point(node.id); if (!p) continue;
      const hovered = state.hovered?.id === node.id; const selected = state.selected?.id === node.id;
      const radius = node.kind === "client" ? 10 : node.kind === "process" ? 8 : node.kind === "contradiction" ? 7 : 4.5;
      const color = nodeColor(node); const pulse = state.paused ? 0 : Math.sin(state.time * .003 + hash(node.id)) * 1.2;
      if (["client", "process", "contradiction"].includes(node.kind) || hovered || selected) {
        context.beginPath(); context.arc(p.x, p.y, radius + 7 + Math.max(0, pulse), 0, Math.PI * 2); context.strokeStyle = `${color}55`; context.stroke();
      }
      context.beginPath(); context.arc(p.x, p.y, radius, 0, Math.PI * 2); context.fillStyle = color; context.shadowBlur = 14; context.shadowColor = color; context.fill(); context.shadowBlur = 0;
      if (["client", "process", "contradiction"].includes(node.kind) || hovered || selected) {
        const label = node.label.length > 26 ? `${node.label.slice(0, 25)}…` : node.label;
        context.font = `${node.kind === "client" ? 11 : 9}px ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
        context.textAlign = "center"; context.fillStyle = "rgba(246,234,215,.78)"; context.fillText(label, p.x, p.y + radius + 14);
      }
    }
    window.requestAnimationFrame(draw);
  }

  function nearest(clientX, clientY) {
    const bounds = canvas.getBoundingClientRect(); const x = clientX - bounds.left; const y = clientY - bounds.top;
    let candidate = null; let distance = 18;
    for (const node of state.nodes) { const p = point(node.id); if (!p) continue; const value = Math.hypot(p.x - x, p.y - y); if (value < distance) { candidate = node; distance = value; } }
    return candidate;
  }

  function openDetail(data, kind) {
    if (!data) return;
    const drawer = document.getElementById("detail-drawer"); const facts = document.getElementById("detail-facts");
    text(document.getElementById("detail-kind"), kind); text(document.getElementById("detail-title"), data.label || data.kind || data.event_type || "Detay");
    facts.replaceChildren();
    const allowed = ["client", "state", "availability", "binding_confidence", "current_action", "process_status", "entity_id", "kind", "work_item_id", "job_id", "parent_id", "terminal_receipt_bound", "event_type", "source", "occurred_at", "heartbeat_at", "started_at"];
    for (const key of allowed) if (data[key] !== undefined && data[key] !== null) {
      const row = document.createElement("div"); const dt = document.createElement("dt"); const dd = document.createElement("dd");
      text(dt, key.replaceAll("_", " ")); text(dd, data[key]); row.append(dt, dd); facts.append(row);
    }
    drawer.hidden = false;
  }

  function renderAll() {
    updateProjectFilter(); renderMetrics(); renderSessionCards(); renderRegistry(); renderEvents(); renderRuntimeChain(); renderResources(); buildGraph();
    const source = state.snapshot || state.telemetry || {};
    text(document.getElementById("snapshot-clock"), fmtTime(source.generated_at));
    text(document.getElementById("snapshot-digest"), `DIGEST ${shortId(source.projection_digest || state.telemetryDigest || state.structureDigest)}`);
  }

  function applySnapshot(document) {
    if (!document || document.schema !== snapshotSchema || document.read_only !== true || document.grants_authority !== false) throw new Error("unsafe snapshot");
    state.snapshot = document; state.structure = null; state.telemetry = null;
    state.structureDigest = ""; state.telemetryDigest = "";
    renderAll(); setConnection("live", "CANLI");
  }

  function applyStructure(document) {
    if (!document || document.schema !== "zekam-observatory-structure/v1" || document.read_only !== true) return;
    if (document.projection_digest === state.structureDigest) return;
    state.structureDigest = document.projection_digest; state.structure = document; renderAll();
  }

  function applyTelemetry(document) {
    if (!document || document.schema !== "zekam-observatory-telemetry/v1" || document.read_only !== true) return;
    if (document.projection_digest === state.telemetryDigest) return;
    state.telemetryDigest = document.projection_digest; state.telemetry = document; renderAll();
  }

  async function fetchSnapshot() {
    const response = await fetch("/api/observatory/snapshot", { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    applySnapshot(await response.json());
  }

  function schedulePolling() {
    if (state.pollingTimer) return;
    state.pollingTimer = window.setInterval(() => fetchSnapshot().catch(() => setConnection("error", "DEGRADE")), 5000);
  }

  function connectStream() {
    if (!("EventSource" in window)) { schedulePolling(); return; }
    const stream = new EventSource("/api/observatory/events");
    stream.addEventListener("structure", (event) => { try { applyStructure(JSON.parse(event.data)); } catch (_) { setConnection("error", "VERİ REDDEDİLDİ"); } });
    stream.addEventListener("telemetry", (event) => { try { applyTelemetry(JSON.parse(event.data)); setConnection("live", "CANLI"); } catch (_) { setConnection("error", "VERİ REDDEDİLDİ"); } });
    stream.onerror = () => { stream.close(); setConnection("error", "POLLING YEDEK"); schedulePolling(); };
  }

  function updateFilters() {
    state.query = searchInput.value.trim().toLocaleLowerCase("tr-TR"); state.client = clientFilter.value; state.status = stateFilter.value; state.project = projectFilter.value; renderAll();
  }
  [searchInput, clientFilter, stateFilter, projectFilter].forEach((element) => element.addEventListener("input", updateFilters));
  document.addEventListener("keydown", (event) => { if (event.key === "/" && document.activeElement !== searchInput) { event.preventDefault(); searchInput.focus(); } if (event.key === "Escape") { document.getElementById("detail-drawer").hidden = true; searchInput.value = ""; updateFilters(); } });
  motionToggle.addEventListener("click", () => { state.paused = !state.paused; motionToggle.setAttribute("aria-pressed", String(state.paused)); text(motionToggle, state.paused ? "HAREKETİ BAŞLAT" : "HAREKETİ DURDUR"); });
  viewToggle.addEventListener("click", () => { state.listMode = !state.listMode; viewToggle.setAttribute("aria-pressed", String(state.listMode)); text(viewToggle, state.listMode ? "GRAF GÖRÜNÜMÜ" : "LİSTE GÖRÜNÜMÜ"); graphStage.hidden = state.listMode; fallback.hidden = !state.listMode; });
  document.getElementById("detail-close").addEventListener("click", () => { document.getElementById("detail-drawer").hidden = true; });
  canvas.addEventListener("pointermove", (event) => { state.hovered = nearest(event.clientX, event.clientY); canvas.style.cursor = state.hovered ? "pointer" : "crosshair"; });
  canvas.addEventListener("pointerleave", () => { state.hovered = null; });
  canvas.addEventListener("click", (event) => { const node = nearest(event.clientX, event.clientY); state.selected = node; if (node) openDetail(node.data, node.kind); });
  new ResizeObserver(resizeCanvas).observe(graphStage);
  window.addEventListener("resize", resizeCanvas);

  async function boot() {
    resizeCanvas();
    if (state.listMode) { graphStage.hidden = true; fallback.hidden = false; }
    try {
      if (window.__ZEKAM_PREVIEW__) applySnapshot(window.__ZEKAM_PREVIEW__); else { await fetchSnapshot(); connectStream(); }
    } catch (_) { setConnection("error", "POLLING YEDEK"); schedulePolling(); }
    window.requestAnimationFrame(draw);
  }

  boot();
})();
