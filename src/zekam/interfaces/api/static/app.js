(() => {
  "use strict";

  const snapshotSchema = "zekam-observatory-snapshot/v3";
  const structureSchema = "zekam-observatory-structure/v1";
  const telemetrySchema = "zekam-observatory-telemetry/v1";
  const liveStates = new Set(["live", "waiting", "unbound"]);
  const dangerStates = new Set(["recovery-required", "receiptless", "completed-unbound", "blocked", "failed", "expired"]);
  const labels = { opencode: "OpenCode", codex: "Codex", claude: "Claude", zekam: "Zekam CLI", runtime: "Runtime" };
  const actionLabels = { unknown: "Bilinmiyor", planning: "Planlıyor", executing: "Yürütüyor", tool: "Tool çalışıyor", waiting: "Bekliyor" };
  const palette = { live: "#f6b84a", waiting: "#f07822", unbound: "#9b7040", stale: "#747b86", danger: "#df3b2f", runtime: "#d8892d", verified: "#67c66b", text: "#f3eadc" };
  const anchors = { opencode: [.2, .28], codex: [.23, .72], claude: [.52, .25], zekam: [.53, .72], runtime: [.79, .5] };
  const MAX_RING = 120, MAX_PARTICLES = 96, MAX_LABELS = 72, MAX_GRAPH_NODES = 512, MAX_GRAPH_EDGES = 1024;

  const byId = (id) => document.getElementById(id);
  const canvas = byId("execution-canvas");
  const context = canvas.getContext?.("2d") || null;
  const spark = byId("telemetry-sparkline");
  const sparkContext = spark.getContext?.("2d") || null;
  const stage = byId("graph-stage");
  const fallback = byId("graph-fallback");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const state = {
    snapshot: null, structure: null, telemetry: null, nodes: [], edges: [], positions: new Map(),
    clusters: [], nodeMap: new Map(), query: "", client: "all", status: "all", binding: "all",
    project: "all", windowSeconds: 300, paused: reducedMotion.matches, listMode: !context,
    hovered: null, selected: null, width: 0, height: 0, time: 0, structureDigest: "",
    telemetryDigest: "", pollingTimer: null, stream: null, animationId: null, transform: { x: 0, y: 0, scale: 1 },
    dragging: null, keyboardIndex: 0, ring: [], frameTimes: [], lastFrameAt: 0, lastSnapshotAt: 0,
    streamConnectCount: 0, telemetryEventCount: 0, lastDiagnosticsAt: 0,
  };

  function publishRuntimeDiagnostics(timestamp = performance.now()) {
    if (timestamp - state.lastDiagnosticsAt < 1000 && state.lastDiagnosticsAt) return;
    state.lastDiagnosticsAt = timestamp;
    const memory = performance.memory;
    const frameSamples = [...state.frameTimes].sort((a, b) => a - b);
    document.documentElement.dataset.zekamRuntimeDiagnostics = JSON.stringify(Object.freeze({
      streamConnectCount: state.streamConnectCount,
      telemetryEventCount: state.telemetryEventCount,
      ringLength: state.ring.length,
      ringLimit: MAX_RING,
      domNodeCount: document.getElementsByTagName("*").length,
      animationActive: Boolean(state.animationId),
      documentHidden: document.hidden,
      frameSampleCount: frameSamples.length,
      frameMedianMs: frameSamples.length ? frameSamples[Math.floor(frameSamples.length / 2)] : null,
      frameP95Ms: frameSamples.length ? frameSamples[Math.floor(frameSamples.length * .95)] : null,
      heapUsedBytes: Number.isFinite(memory?.usedJSHeapSize) ? memory.usedJSHeapSize : null,
    }));
  }

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
    const valueText = String(value);
    return valueText.length <= 12 ? valueText : `${valueText.slice(0, 7)}…${valueText.slice(-4)}`;
  }
  function text(element, value) { element.textContent = value == null ? "—" : String(value); }
  function fmtTime(value) {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
  }
  function fmtAge(value) {
    const parsed = Date.parse(value || "");
    if (!Number.isFinite(parsed)) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
    if (seconds < 60) return `${seconds} sn`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} dk`;
    return `${Math.floor(seconds / 3600)} sa`;
  }
  function pid(value) {
    const parts = String(value || "").split(":");
    return parts.length >= 3 && /^\d+$/.test(parts[1]) ? parts[1] : "—";
  }
  function stateLabel(value) {
    return ({ live: "CANLI", waiting: "BEKLİYOR", stale: "STALE", unbound: "UNBOUND", "recovery-required": "RECOVERY", receiptless: "RECEIPT BEKLİYOR", blocked: "BLOKLU", failed: "HATALI", completed: "TAMAMLANDI", active: "AKTİF", running: "ÇALIŞIYOR", ready: "HAZIR" })[value] || String(value || "BİLİNMİYOR").toLocaleUpperCase("tr-TR");
  }
  function setConnection(kind, label) {
    const target = byId("connection-state");
    target.className = `status-chip is-${kind}`;
    target.lastChild.textContent = ` ${label}`;
  }
  function currentAgents() { return state.telemetry?.agents || state.snapshot?.agents || []; }
  function currentCanonical() { return state.structure?.canonical_runtime || state.snapshot?.canonical_runtime || { entities: [], contradictions: [], available: false }; }
  function allEvents() { return state.telemetry?.events || state.snapshot?.events || []; }
  function currentEvents() {
    const cutoff = state.windowSeconds === "all" ? 0 : Date.now() - Number(state.windowSeconds) * 1000;
    return allEvents().filter((item) => !cutoff || Date.parse(item.occurred_at || "") >= cutoff);
  }
  function bindingOf(item) { return String(item.binding_confidence || item.binding || (item.process_id ? "exact" : "unbound")).toLowerCase(); }
  function matches(item) {
    if (state.client !== "all" && item.client && item.client !== state.client && item.source !== state.client) return false;
    const valueState = item.availability || item.state || "unknown";
    if (state.status !== "all" && valueState !== state.status) return false;
    if (state.binding !== "all" && bindingOf(item) !== state.binding) return false;
    if (state.project !== "all" && item.project_id !== state.project) return false;
    if (!state.query) return true;
    return [item.label, item.client, item.source, item.state, item.availability, item.current_action, item.kind, item.entity_id, item.work_item_id, item.job_id]
      .filter(Boolean).join(" ").toLocaleLowerCase("tr-TR").includes(state.query);
  }

  function updateProjectFilter() {
    const select = byId("project-filter"), values = new Set();
    for (const item of currentAgents()) if (item.project_id) values.add(item.project_id);
    for (const item of currentCanonical().entities || []) if (item.project_id) values.add(item.project_id);
    const previous = select.value;
    select.replaceChildren(new Option("Tüm projeler", "all"));
    for (const value of [...values].sort()) select.append(new Option(shortId(value), value));
    select.value = values.has(previous) ? previous : "all";
    state.project = select.value;
  }

  function renderMetrics() {
    const agents = currentAgents(), entities = currentCanonical().entities || [];
    const roots = new Set(agents.filter((item) => item.process_id && item.process_role === "cli-root" && liveStates.has(item.availability)).map((item) => item.process_id));
    const sessions = new Set(agents.filter((item) => item.process_id && item.session_id && ["live", "waiting"].includes(item.availability)).map((item) => `${item.client}:${item.session_id}`));
    const activeAgents = new Set(agents.filter((item) => ["live", "waiting"].includes(item.availability)).map((item) => item.agent_id || item.process_id).filter(Boolean));
    const running = new Set(entities.filter((item) => item.kind === "job" && item.state === "running").map((item) => item.job_id || item.entity_id));
    const receiptClaims = new Set(entities.filter((item) => item.kind === "receipt").map((item) => item.parent_id || item.claim_id).filter(Boolean));
    const openClaims = new Set(entities.filter((item) => item.kind === "claim" && !item.terminal_receipt_bound && !receiptClaims.has(item.entity_id)).map((item) => item.entity_id));
    const signals = [...agents.flatMap((item) => [item.heartbeat_at, item.started_at]), ...allEvents().map((item) => item.occurred_at)].filter((item) => Number.isFinite(Date.parse(item || ""))).sort((a, b) => Date.parse(b) - Date.parse(a));
    const values = { "open-cli": roots.size, "active-session": sessions.size, "active-agent": activeAgents.size, "running-work": running.size, "open-claim": openClaims.size, "last-live-signal": fmtAge(signals[0]) };
    for (const [key, value] of Object.entries(values)) text(document.querySelector(`[data-metric="${key}"] strong`), value);
  }

  function buildGraph() {
    const nodes = [], edges = [], known = new Set(), agentNodes = new Map();
    const add = (node) => { if (!known.has(node.id) && nodes.length < MAX_GRAPH_NODES) { known.add(node.id); nodes.push(node); } };
    const agents = currentAgents().filter(matches);
    for (const agent of agents) {
      const clientId = `client:${agent.client}`;
      add({ id: clientId, label: labels[agent.client] || agent.client, kind: "client", client: agent.client, state: "live", cluster: `client:${agent.client}`, data: { client: agent.client, state: "live" } });
      const rootId = agent.process_id || `agent:${agent.agent_id || agent.session_id}`;
      const cluster = agent.session_id ? `session:${agent.client}:${agent.session_id}` : `client:${agent.client}`;
      add({ id: rootId, label: agent.executable_label || labels[agent.client] || agent.client, kind: agent.process_role === "tool-child" ? "tool-child" : agent.process_id ? "process" : "session", client: agent.client, state: agent.availability, cluster, project_id: agent.project_id, data: agent });
      agentNodes.set(agent.agent_id, rootId); if (agent.process_id) agentNodes.set(agent.process_id, rootId);
      if (agent.process_role !== "tool-child") edges.push({ source: clientId, target: rootId, kind: agent.process_id ? "exact-process" : "unbound-session", active: liveStates.has(agent.availability) });
      if (agent.session_id && agent.process_id && agent.process_role !== "tool-child") {
        const sessionId = `session:${agent.session_id}`;
        add({ id: sessionId, label: `Session ${shortId(agent.session_id)}`, kind: "session", client: agent.client, state: agent.availability, cluster, data: agent });
        edges.push({ source: rootId, target: sessionId, kind: `${bindingOf(agent)}-bind`, active: ["live", "waiting"].includes(agent.availability) });
      }
    }
    for (const agent of agents) {
      const source = agent.parent_agent_id ? agentNodes.get(agent.parent_agent_id) : null, target = agentNodes.get(agent.agent_id);
      if (source && target && source !== target) edges.push({ source, target, kind: agent.process_role === "tool-child" ? "exact-tool-child" : "exact-delegates", active: true });
    }
    const canonical = currentCanonical();
    const entities = (canonical.entities || []).filter(matches).slice(0, MAX_GRAPH_NODES - nodes.length);
    for (const entity of entities) {
      const client = entity.client || "runtime";
      add({ id: entity.entity_id, label: `${entity.kind} ${shortId(entity.entity_id.split(":").pop())}`, kind: entity.kind.startsWith("agent-") ? "agent" : entity.kind, client, state: entity.state, cluster: entity.job_id ? `job:${entity.job_id}` : "runtime", project_id: entity.project_id, data: entity });
    }
    for (const entity of entities) if (entity.parent_id && known.has(entity.parent_id)) edges.push({ source: entity.parent_id, target: entity.entity_id, kind: "exact-canonical", active: ["running", "active"].includes(entity.state) });
    for (const agent of agents) {
      const source = agentNodes.get(agent.agent_id), target = agent.job_id ? `job:${agent.job_id}` : agent.work_item_id ? `work:${agent.work_item_id}` : null;
      if (source && target && known.has(target)) edges.push({ source, target, kind: `${bindingOf(agent)}-runtime`, active: true });
    }
    for (const item of canonical.contradictions || []) {
      if (!matches(item)) continue;
      const id = `contradiction:${item.contradiction_id}`;
      add({ id, label: item.kind, kind: "contradiction", client: "runtime", state: "recovery-required", cluster: item.job_id ? `job:${item.job_id}` : "runtime", data: item });
      const target = item.job_id ? `job:${item.job_id}` : null;
      if (target && known.has(target)) edges.push({ source: target, target: id, kind: "contradiction", active: true });
    }
    state.nodes = nodes; state.edges = edges.filter((edge) => known.has(edge.source) && known.has(edge.target)).slice(0, MAX_GRAPH_EDGES);
    state.nodeMap = new Map(nodes.map((node) => [node.id, node]));
    layoutGraph(); renderFallback();
    text(byId("visible-node-count"), nodes.length); text(byId("visible-edge-count"), state.edges.length);
    byId("graph-empty").hidden = nodes.length > 0;
  }

  function layoutGraph() {
    const groups = new Map();
    for (const node of state.nodes) {
      const key = node.cluster || `client:${node.client || "runtime"}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(node);
    }
    const positions = new Map(), clusters = [], keys = [...groups.keys()].sort();
    for (const key of keys) {
      const rows = groups.get(key).sort((a, b) => a.id.localeCompare(b.id));
      const client = rows[0]?.client in anchors ? rows[0].client : "runtime";
      const base = anchors[client] || anchors.runtime, seed = hash(key) / 4294967295;
      const clusterX = Math.max(.08, Math.min(.92, base[0] + Math.cos(seed * Math.PI * 2) * .09));
      const clusterY = Math.max(.12, Math.min(.88, base[1] + Math.sin(seed * Math.PI * 2) * .12));
      const radius = Math.min(.16, .035 + Math.sqrt(rows.length) * .018);
      rows.forEach((node, index) => {
        const angle = index * 2.399963 + (hash(node.id) % 360) * Math.PI / 180;
        const distance = node.kind === "client" ? 0 : Math.min(radius, .018 + Math.sqrt(index + 1) * .022);
        positions.set(node.id, { x: clusterX + Math.cos(angle) * distance, y: clusterY + Math.sin(angle) * distance });
      });
      clusters.push({ key, x: clusterX, y: clusterY, radius: radius + .035, count: rows.length, client });
    }
    state.positions = positions; state.clusters = clusters;
  }

  function worldPoint(id) {
    const p = state.positions.get(id); if (!p) return null;
    const x = (p.x - .5) * state.width, y = (p.y - .5) * state.height;
    return { x: state.width / 2 + state.transform.x + x * state.transform.scale, y: state.height / 2 + state.transform.y + y * state.transform.scale };
  }
  function nodeColor(node) {
    if (node.kind === "contradiction" || dangerStates.has(node.state)) return palette.danger;
    if (node.kind === "receipt" && node.data?.terminal_receipt_bound) return palette.verified;
    if (node.client === "runtime") return palette.runtime;
    return palette[node.state] || palette.stale;
  }
  function curve(edge) {
    const a = worldPoint(edge.source), b = worldPoint(edge.target); if (!a || !b) return null;
    const bundle = hash(`${edge.source}:${edge.target}`) % 31 - 15;
    return { a, b, cx: (a.x + b.x) / 2 + bundle * state.transform.scale, cy: (a.y + b.y) / 2 - bundle * state.transform.scale };
  }

  function drawScene(timestamp = performance.now()) {
    if (!context || !state.width || !state.height) return;
    const started = performance.now(); state.time = timestamp;
    context.clearRect(0, 0, state.width, state.height);
    context.save();
    for (const cluster of state.clusters) {
      const center = worldPoint(state.nodes.find((node) => node.cluster === cluster.key)?.id); if (!center) continue;
      const radius = cluster.radius * Math.min(state.width, state.height) * state.transform.scale;
      const glow = context.createRadialGradient(center.x, center.y, 0, center.x, center.y, radius);
      const tint = cluster.client === "runtime" ? "216,137,45" : "240,120,34";
      glow.addColorStop(0, `rgba(${tint},.10)`); glow.addColorStop(.55, `rgba(${tint},.035)`); glow.addColorStop(1, `rgba(${tint},0)`);
      context.fillStyle = glow; context.beginPath(); context.arc(center.x, center.y, radius, 0, Math.PI * 2); context.fill();
      context.setLineDash([2, 8]); context.strokeStyle = `rgba(${tint},.10)`; context.stroke(); context.setLineDash([]);
    }
    for (const edge of state.edges) {
      const c = curve(edge); if (!c) continue;
      const heuristic = edge.kind.includes("heuristic"), unbound = edge.kind.includes("unbound"), danger = edge.kind === "contradiction";
      context.beginPath(); context.moveTo(c.a.x, c.a.y); context.quadraticCurveTo(c.cx, c.cy, c.b.x, c.b.y);
      context.setLineDash(heuristic ? [5, 7] : unbound ? [1, 7] : []);
      context.strokeStyle = danger ? "rgba(223,59,47,.72)" : edge.active ? "rgba(240,120,34,.34)" : "rgba(246,184,74,.105)";
      context.lineWidth = danger ? 1.6 : edge.active ? 1.05 : .65; context.stroke(); context.setLineDash([]);
    }
    if (shouldAnimate()) {
      const active = state.edges.filter((edge) => edge.active).slice(0, MAX_PARTICLES);
      active.forEach((edge, index) => {
        const c = curve(edge); if (!c) return;
        const t = (timestamp * .00012 + (hash(edge.source) % 1000) / 1000 + index / Math.max(1, active.length)) % 1;
        const u = 1 - t, x = u * u * c.a.x + 2 * u * t * c.cx + t * t * c.b.x, y = u * u * c.a.y + 2 * u * t * c.cy + t * t * c.b.y;
        context.beginPath(); context.arc(x, y, edge.kind === "contradiction" ? 2 : 1.35, 0, Math.PI * 2); context.fillStyle = edge.kind === "contradiction" ? palette.danger : palette.gold; context.fill();
      });
    }
    const ordered = [...state.nodes].sort((a, b) => Number(b.kind === "client") - Number(a.kind === "client") || a.id.localeCompare(b.id));
    let labelCount = 0;
    for (const node of ordered) {
      const p = worldPoint(node.id); if (!p) continue;
      const selected = state.selected?.id === node.id, hovered = state.hovered?.id === node.id;
      const base = node.kind === "client" ? 9 : node.kind === "process" ? 7 : node.kind === "contradiction" ? 6 : 3.7;
      const radius = Math.max(2.5, base * Math.sqrt(state.transform.scale));
      const color = nodeColor(node);
      if (selected || hovered || ["client", "process", "contradiction"].includes(node.kind)) {
        context.beginPath(); context.arc(p.x, p.y, radius + 7, 0, Math.PI * 2); context.strokeStyle = `${color}55`; context.stroke();
      }
      context.beginPath(); context.arc(p.x, p.y, radius, 0, Math.PI * 2); context.fillStyle = color; context.shadowBlur = selected ? 20 : 11; context.shadowColor = color; context.fill(); context.shadowBlur = 0;
      if ((selected || hovered || ["client", "process", "contradiction"].includes(node.kind)) && labelCount < MAX_LABELS) {
        context.font = `${node.kind === "client" ? 10 : 8}px Cascadia Code, monospace`; context.textAlign = "center"; context.fillStyle = "rgba(243,234,220,.78)";
        const value = node.label.length > 25 ? `${node.label.slice(0, 24)}…` : node.label; context.fillText(value, p.x, p.y + radius + 13); labelCount += 1;
      }
    }
    context.restore();
    const elapsed = performance.now() - started;
    state.frameTimes.push(elapsed); if (state.frameTimes.length > 240) state.frameTimes.shift();
    if (state.lastFrameAt) state.ring.push({ at: Date.now(), frameGap: timestamp - state.lastFrameAt, nodes: state.nodes.length, edges: state.edges.length });
    state.lastFrameAt = timestamp; if (state.ring.length > MAX_RING) state.ring.splice(0, state.ring.length - MAX_RING);
    publishRuntimeDiagnostics(timestamp);
    drawSparkline();
  }

  function shouldAnimate() { return Boolean(context && !state.paused && !reducedMotion.matches && !document.hidden && !state.listMode); }
  function animationLoop(timestamp) {
    state.animationId = null; drawScene(timestamp);
    if (shouldAnimate()) state.animationId = window.requestAnimationFrame(animationLoop);
  }
  function syncAnimation() {
    if (!shouldAnimate() && state.animationId !== null) { window.cancelAnimationFrame(state.animationId); state.animationId = null; }
    if (shouldAnimate() && state.animationId === null) state.animationId = window.requestAnimationFrame(animationLoop);
    if (!shouldAnimate()) drawScene();
  }

  function nearest(clientX, clientY) {
    const bounds = canvas.getBoundingClientRect(), x = clientX - bounds.left, y = clientY - bounds.top;
    let found = null, distance = 20;
    for (const node of state.nodes) { const p = worldPoint(node.id); if (!p) continue; const d = Math.hypot(p.x - x, p.y - y); if (d < distance) { found = node; distance = d; } }
    return found;
  }
  function showTooltip(node, clientX, clientY) {
    const tooltip = byId("graph-tooltip");
    if (!node) { tooltip.hidden = true; return; }
    tooltip.replaceChildren();
    const strong = document.createElement("strong"), meta = document.createElement("span");
    text(strong, node.label); text(meta, `${node.kind} · ${stateLabel(node.state)} · ${bindingOf(node.data || node)}`);
    tooltip.append(strong, meta);
    const bounds = stage.getBoundingClientRect(); tooltip.style.left = `${Math.min(state.width - 220, Math.max(8, clientX - bounds.left + 12))}px`; tooltip.style.top = `${Math.min(state.height - 58, Math.max(8, clientY - bounds.top + 12))}px`; tooltip.hidden = false;
  }

  const detailKeys = ["client", "state", "availability", "binding_confidence", "current_action", "process_status", "process_role", "executable_label", "child_process_count", "entity_id", "kind", "work_item_id", "job_id", "parent_id", "terminal_receipt_bound", "event_type", "source", "occurred_at", "heartbeat_at", "started_at"];
  function inspect(data, kind) {
    if (!data) return;
    state.selected = state.nodes.find((node) => node.data === data) || state.selected;
    const target = byId("session-inspector"); target.replaceChildren();
    const title = document.createElement("p"); title.className = "selection-title"; text(title, data.label || data.kind || data.event_type || kind || "Detay"); target.append(title);
    const dl = document.createElement("dl");
    for (const key of detailKeys) if (data[key] !== undefined && data[key] !== null) {
      const group = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
      text(dt, key.replaceAll("_", " ")); text(dd, key === "current_action" ? actionLabels[data[key]] || data[key] : data[key]); group.append(dt, dd); dl.append(group);
    }
    target.append(dl); syncAnimation();
  }
  function clearSelection() { state.selected = null; byId("session-inspector").replaceChildren(Object.assign(document.createElement("p"), { textContent: "Graf veya tablodan güvenli bir kimlik seçin." })); syncAnimation(); }

  function renderEvents() {
    const target = byId("event-feed"), rows = currentEvents().filter(matches).slice(0, 60); target.replaceChildren();
    for (const event of rows) {
      const li = document.createElement("li"), strong = document.createElement("strong"), meta = document.createElement("span");
      if (dangerStates.has(event.state) || /fail|recovery|error/.test(event.event_type || "")) li.className = "is-danger";
      text(strong, event.event_type); text(meta, `${event.source || "runtime"} · ${fmtTime(event.occurred_at)} · ${shortId(event.job_id || event.agent_id)}`);
      li.append(strong, meta); li.tabIndex = 0; li.addEventListener("click", () => inspect(event, "OLAY")); li.addEventListener("keydown", (e) => { if (e.key === "Enter") inspect(event, "OLAY"); }); target.append(li);
    }
    text(byId("event-total"), rows.length);
  }

  function renderIntegrity() {
    const canonical = currentCanonical(), entities = canonical.entities || [];
    const jobs = entities.filter((item) => item.kind === "job"), claims = entities.filter((item) => item.kind === "claim"), receipts = entities.filter((item) => item.kind === "receipt");
    const boundReceipts = receipts.filter((item) => item.terminal_receipt_bound !== false).length;
    const coverage = claims.length ? Math.round(boundReceipts / claims.length * 100) : null;
    const healthyJobs = jobs.filter((item) => !dangerStates.has(item.state)).length;
    const jobHealth = jobs.length ? Math.round(healthyJobs / jobs.length * 100) : null;
    const contradictions = (canonical.contradictions || []).length;
    const scoreParts = [coverage, jobHealth].filter((item) => item !== null);
    const score = scoreParts.length ? Math.max(0, Math.round(scoreParts.reduce((a, b) => a + b, 0) / scoreParts.length - contradictions * 5)) : null;
    text(byId("integrity-score"), score === null ? "N/A" : `${score}%`);
    const metrics = byId("integrity-metrics"); metrics.replaceChildren();
    for (const [value, label] of [[coverage === null ? "N/A" : `${coverage}%`, "RECEIPT"], [jobHealth === null ? "N/A" : `${jobHealth}%`, "JOB HEALTH"], [contradictions, "ÇELİŞKİ"]]) {
      const box = document.createElement("div"), strong = document.createElement("strong"), span = document.createElement("span"); text(strong, value); text(span, label); box.append(strong, span); metrics.append(box);
    }
    const chain = byId("runtime-chain"); chain.replaceChildren();
    for (const entity of entities.filter((item) => ["job", "attempt", "lease", "claim", "receipt"].includes(item.kind)).filter(matches).slice(0, 45)) {
      const row = document.createElement("button"), kind = document.createElement("span"), id = document.createElement("span"), status = document.createElement("span");
      row.type = "button"; row.className = `chain-row${dangerStates.has(entity.state) ? " is-danger" : ""}`; kind.className = "kind"; id.className = "id"; status.className = "chain-state";
      text(kind, entity.kind); text(id, shortId(entity.entity_id)); text(status, stateLabel(entity.state)); row.append(kind, id, status); row.addEventListener("click", () => inspect(entity, "RUNTIME")); chain.append(row);
    }
  }

  function renderRegistry() {
    const target = byId("session-registry"), rows = currentAgents().filter((item) => item.session_id && item.process_role !== "tool-child" && matches(item)).slice(0, 80); target.replaceChildren();
    for (const item of rows) {
      const tr = document.createElement("tr");
      const values = [labels[item.client] || item.client, pid(item.process_id), shortId(item.session_id), shortId(item.project_id), fmtTime(item.started_at), stateLabel(item.availability), bindingOf(item)];
      values.forEach((value, index) => { const td = document.createElement("td"); if (index === 5) td.className = "state-mark"; text(td, value); tr.append(td); });
      tr.tabIndex = 0; tr.addEventListener("click", () => inspect(item, "SESSION")); tr.addEventListener("keydown", (e) => { if (e.key === "Enter") inspect(item, "SESSION"); }); target.append(tr);
    }
    text(byId("session-total"), rows.length);
  }

  function renderHeatmap() {
    const events = currentEvents(), buckets = Array.from({ length: 24 }, () => 0);
    for (const item of events) { const date = new Date(item.occurred_at || ""); if (!Number.isNaN(date.getTime())) buckets[date.getHours()] += 1; }
    const max = Math.max(0, ...buckets), target = byId("event-heatmap"); target.replaceChildren();
    buckets.forEach((count, hour) => {
      const cell = document.createElement("div"), label = document.createElement("span"); cell.className = "heat-cell";
      cell.style.setProperty("--heat", String(max ? .035 + count / max * .48 : .02)); cell.title = `${hour.toString().padStart(2, "0")}:00 · ${count} olay`; text(label, `${hour.toString().padStart(2, "0")} · ${count}`); cell.append(label); target.append(cell);
    });
    text(byId("heatmap-total"), events.length);
  }

  function renderRanking() {
    const counts = new Map();
    for (const item of currentAgents().filter(matches)) {
      const key = labels[item.client] || item.client || "Unknown"; counts.set(key, (counts.get(key) || 0) + 1);
    }
    for (const event of currentEvents().filter(matches)) {
      const key = labels[event.source] || event.source || "Runtime"; counts.set(key, (counts.get(key) || 0) + 1);
    }
    const target = byId("agent-ranking"); target.replaceChildren();
    for (const [name, count] of [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12)) {
      const li = document.createElement("li"), body = document.createElement("span"), b = document.createElement("b"), small = document.createElement("small"), strong = document.createElement("strong");
      text(b, name); text(small, "agent + event"); text(strong, count); body.append(b, small); li.append(body, strong); target.append(li);
    }
  }

  function renderStateTelemetry() {
    const values = new Map();
    for (const item of [...currentAgents().filter(matches), ...(currentCanonical().entities || []).filter(matches)]) {
      const key = item.availability || item.state || "unknown"; values.set(key, (values.get(key) || 0) + 1);
    }
    const total = [...values.values()].reduce((a, b) => a + b, 0), donut = byId("state-donut"), legend = byId("state-legend"); legend.replaceChildren();
    const colorFor = (key) => dangerStates.has(key) ? palette.danger : key === "live" || key === "completed" ? palette.verified : key === "waiting" || key === "running" ? palette.gold : palette.stale;
    let degrees = 0; const segments = [];
    for (const [key, count] of [...values.entries()].sort((a, b) => b[1] - a[1])) {
      const next = degrees + (total ? count / total * 360 : 0); segments.push(`${colorFor(key)} ${degrees}deg ${next}deg`); degrees = next;
      const row = document.createElement("div"), b = document.createElement("b"), span = document.createElement("span"); text(b, stateLabel(key)); text(span, count); row.append(b, span); legend.append(row);
    }
    donut.style.background = total ? `conic-gradient(${segments.join(",")})` : "conic-gradient(#222 0deg 360deg)";
    text(donut.querySelector("strong"), total || "N/A");
    const sorted = [...state.frameTimes].sort((a, b) => a - b), median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
    text(byId("diagnostics-badge"), median === null ? "N/A" : `${median.toFixed(1)}ms`);
    drawSparkline();
  }

  function drawSparkline() {
    if (!sparkContext) return;
    const width = spark.width, height = spark.height, rows = state.ring.slice(-MAX_RING);
    sparkContext.clearRect(0, 0, width, height);
    if (rows.length < 2) { sparkContext.fillStyle = "#85817a"; sparkContext.font = "10px monospace"; sparkContext.fillText("N/A · örnek bekleniyor", 10, 38); return; }
    const values = rows.map((item) => Math.min(250, Math.max(0, item.frameGap || 0))), max = Math.max(16.7, ...values);
    sparkContext.beginPath();
    values.forEach((value, index) => { const x = index / Math.max(1, values.length - 1) * width, y = height - value / max * (height - 10) - 5; if (!index) sparkContext.moveTo(x, y); else sparkContext.lineTo(x, y); });
    sparkContext.strokeStyle = palette.gold; sparkContext.lineWidth = 1.4; sparkContext.stroke();
  }

  function renderFallback() {
    fallback.replaceChildren();
    for (const node of state.nodes.slice(0, 256)) {
      const row = document.createElement("button"), kind = document.createElement("span"), label = document.createElement("span"), status = document.createElement("small");
      row.type = "button"; row.className = "fallback-row"; text(kind, node.kind); text(label, node.label); text(status, stateLabel(node.state)); row.append(kind, label, status); row.addEventListener("click", () => inspect(node.data, node.kind)); fallback.append(row);
    }
  }
  function renderAll() {
    updateProjectFilter(); renderMetrics(); buildGraph(); renderEvents(); renderIntegrity(); renderRegistry(); renderHeatmap(); renderRanking(); renderStateTelemetry();
    const source = state.snapshot || state.telemetry || {};
    text(byId("snapshot-clock"), fmtTime(source.generated_at)); text(byId("snapshot-digest"), `DIGEST ${shortId(source.projection_digest || state.telemetryDigest || state.structureDigest)}`);
    state.lastSnapshotAt = Date.now(); syncAnimation();
  }

  function validateBase(document) { return document && document.read_only === true && document.grants_authority === false; }
  function applySnapshot(document) {
    if (!validateBase(document) || document.schema !== snapshotSchema) throw new Error("unsafe snapshot");
    state.snapshot = document; state.structure = null; state.telemetry = null; state.structureDigest = ""; state.telemetryDigest = ""; renderAll(); setConnection("live", "CANLI");
  }
  function applyStructure(document) {
    if (!validateBase(document) || document.schema !== structureSchema || document.projection_digest === state.structureDigest) return;
    state.structureDigest = document.projection_digest; state.structure = document; renderAll();
  }
  function applyTelemetry(document) {
    if (!validateBase(document) || document.schema !== telemetrySchema || document.projection_digest === state.telemetryDigest) return;
    state.telemetryDigest = document.projection_digest; state.telemetry = document; renderAll();
  }
  async function fetchSnapshot() {
    const response = await fetch("/api/observatory/snapshot", { headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    applySnapshot(await response.json());
  }
  function schedulePolling() {
    if (state.pollingTimer) return;
    state.pollingTimer = window.setInterval(() => { if (!document.hidden) fetchSnapshot().catch(() => setConnection("error", "DEGRADE")); }, 5000);
  }
  function connectStream() {
    if (!("EventSource" in window)) { schedulePolling(); return; }
    if (state.stream) state.stream.close();
    state.stream = new EventSource("/api/observatory/events");
    state.streamConnectCount += 1; publishRuntimeDiagnostics();
    state.stream.addEventListener("structure", (event) => { try { applyStructure(JSON.parse(event.data)); } catch (_) { setConnection("error", "VERİ REDDEDİLDİ"); } });
    state.stream.addEventListener("telemetry", (event) => { try { state.telemetryEventCount += 1; applyTelemetry(JSON.parse(event.data)); publishRuntimeDiagnostics(); setConnection("live", "CANLI"); } catch (_) { setConnection("error", "VERİ REDDEDİLDİ"); } });
    state.stream.onerror = () => { state.stream.close(); state.stream = null; setConnection("error", "POLLING YEDEK"); schedulePolling(); };
  }

  function resizeCanvas() {
    if (!context) return;
    const bounds = stage.getBoundingClientRect(), ratio = Math.min(window.devicePixelRatio || 1, 2);
    state.width = Math.max(1, bounds.width); state.height = Math.max(1, bounds.height);
    canvas.width = Math.round(state.width * ratio); canvas.height = Math.round(state.height * ratio); canvas.style.width = `${state.width}px`; canvas.style.height = `${state.height}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0); syncAnimation();
  }
  function zoom(factor, focusX = state.width / 2, focusY = state.height / 2) {
    const old = state.transform.scale, next = Math.max(.55, Math.min(3.2, old * factor));
    state.transform.x = focusX - state.width / 2 - (focusX - state.width / 2 - state.transform.x) * next / old;
    state.transform.y = focusY - state.height / 2 - (focusY - state.height / 2 - state.transform.y) * next / old;
    state.transform.scale = next; syncAnimation();
  }
  function resetView() { state.transform = { x: 0, y: 0, scale: 1 }; syncAnimation(); }
  function focusSelected() {
    if (!state.selected) return;
    const p = worldPoint(state.selected.id); if (!p) return;
    state.transform.x += state.width / 2 - p.x; state.transform.y += state.height / 2 - p.y; state.transform.scale = Math.max(1.35, state.transform.scale); syncAnimation();
  }

  function benchmarkGraph(nodeCount = 512, edgeCount = 1024) {
    const count = Math.max(1, Math.min(1024, Number(nodeCount) || 512)), edgeTotal = Math.max(0, Math.min(4096, Number(edgeCount) || 1024));
    const nodes = Array.from({ length: count }, (_, index) => ({ id: `benchmark:${index}`, client: "runtime", kind: "job", state: index % 31 ? "active" : "blocked", cluster: `benchmark:${index % 12}` }));
    const positions = new Map(nodes.map((node, index) => [node.id, { x: .08 + (index % 32) / 36, y: .08 + Math.floor(index / 32) / 18 }]));
    const edges = Array.from({ length: edgeTotal }, (_, index) => ({ source: nodes[index % count].id, target: nodes[(index * 17 + 1) % count].id }));
    const surface = document.createElement("canvas"); surface.width = 1440; surface.height = 900; const ctx = surface.getContext("2d"), samples = [];
    for (let pass = 0; pass < 15; pass += 1) {
      const started = performance.now();
      if (ctx) {
        ctx.clearRect(0, 0, 1440, 900); ctx.strokeStyle = "rgba(240,120,34,.2)";
        for (const edge of edges) { const a = positions.get(edge.source), b = positions.get(edge.target); ctx.beginPath(); ctx.moveTo(a.x * 1440, a.y * 900); ctx.lineTo(b.x * 1440, b.y * 900); ctx.stroke(); }
        ctx.fillStyle = palette.runtime; for (const node of nodes) { const p = positions.get(node.id); ctx.beginPath(); ctx.arc(p.x * 1440, p.y * 900, 3, 0, Math.PI * 2); ctx.fill(); }
      }
      samples.push(performance.now() - started);
    }
    const ordered = [...samples].sort((a, b) => a - b), medianMs = ordered[Math.floor(ordered.length / 2)], p95Ms = ordered[Math.floor(ordered.length * .95)];
    return Object.freeze({ nodeCount: count, edgeCount: edgeTotal, samples: samples.length, medianMs, p95Ms, estimatedFps: Math.min(60, 1000 / Math.max(1, medianMs)), boundedParticles: MAX_PARTICLES, boundedLabels: MAX_LABELS, hiddenTabThrottle: true });
  }

  function loadDiagnosticGraph(nodeCount = 512, edgeCount = 1024) {
    const count = Math.max(1, Math.min(MAX_GRAPH_NODES, Number(nodeCount) || 512));
    const edgeTotal = Math.max(0, Math.min(MAX_GRAPH_EDGES, Number(edgeCount) || 1024));
    state.nodes = Array.from({ length: count }, (_, index) => ({
      id: `diagnostic:${index}`,
      label: index % 43 === 0 ? `DIAGNOSTIC ${index}` : `D${index}`,
      client: ["opencode", "codex", "claude", "zekam", "runtime"][index % 5],
      kind: index % 47 === 0 ? "contradiction" : "job",
      state: index % 47 === 0 ? "recovery-required" : index % 11 === 0 ? "waiting" : "active",
      cluster: `diagnostic:${index % 12}`,
      data: { entity_id: `diagnostic:${index}`, kind: "synthetic-diagnostic", state: "non-authoritative" },
    }));
    state.edges = Array.from({ length: edgeTotal }, (_, index) => ({
      source: state.nodes[index % count].id,
      target: state.nodes[(index * 17 + 31) % count].id,
      kind: index % 9 === 0 ? "heuristic-diagnostic" : "exact-diagnostic",
      active: index < MAX_PARTICLES,
    }));
    state.nodeMap = new Map(state.nodes.map((node) => [node.id, node]));
    state.keyboardIndex = 0;
    layoutGraph(); renderFallback();
    text(byId("visible-node-count"), state.nodes.length); text(byId("visible-edge-count"), state.edges.length);
    byId("graph-empty").hidden = true;
    stage.setAttribute("aria-label", `Sentetik diagnostics grafigi: ${count} dugum, ${edgeTotal} bag. Authority uretmez.`);
    document.body.classList.add("is-diagnostics");
    syncAnimation();
  }

  function updateFilters() {
    state.query = byId("search-input").value.trim().toLocaleLowerCase("tr-TR"); state.client = byId("client-filter").value; state.status = byId("state-filter").value; state.binding = byId("binding-filter").value; state.project = byId("project-filter").value; state.windowSeconds = byId("time-window").value === "all" ? "all" : Number(byId("time-window").value); renderAll();
  }
  ["search-input", "client-filter", "state-filter", "binding-filter", "project-filter", "time-window"].forEach((id) => byId(id).addEventListener("input", updateFilters));
  document.querySelectorAll(".rail-link").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".rail-link").forEach((item) => item.classList.remove("is-active")); link.classList.add("is-active"); }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement !== byId("search-input")) { event.preventDefault(); byId("search-input").focus(); }
    if (event.key === "Escape") { clearSelection(); byId("search-input").value = ""; updateFilters(); }
  });
  stage.addEventListener("keydown", (event) => {
    const move = 34;
    if (event.key === "ArrowLeft") state.transform.x += move;
    else if (event.key === "ArrowRight") state.transform.x -= move;
    else if (event.key === "ArrowUp") state.transform.y += move;
    else if (event.key === "ArrowDown") state.transform.y -= move;
    else if (event.key === "+" || event.key === "=") zoom(1.18);
    else if (event.key === "-") zoom(.84);
    else if (event.key === "0") resetView();
    else if ((event.key === "Enter" || event.key === " ") && state.nodes.length) {
      const target = state.hovered || state.selected || state.nodes[state.keyboardIndex % state.nodes.length];
      state.selected = target; state.keyboardIndex = Math.max(0, state.nodes.indexOf(target)); inspect(target.data, target.kind);
    } else return;
    event.preventDefault(); syncAnimation();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (state.dragging) { state.transform.x = state.dragging.x + event.clientX - state.dragging.clientX; state.transform.y = state.dragging.y + event.clientY - state.dragging.clientY; syncAnimation(); return; }
    state.hovered = nearest(event.clientX, event.clientY); canvas.style.cursor = state.hovered ? "pointer" : "grab"; showTooltip(state.hovered, event.clientX, event.clientY); syncAnimation();
  });
  canvas.addEventListener("pointerdown", (event) => { const node = nearest(event.clientX, event.clientY); if (!node) { state.dragging = { clientX: event.clientX, clientY: event.clientY, x: state.transform.x, y: state.transform.y }; canvas.setPointerCapture(event.pointerId); } });
  canvas.addEventListener("pointerup", (event) => { if (state.dragging) { state.dragging = null; canvas.releasePointerCapture(event.pointerId); } else { const node = nearest(event.clientX, event.clientY); if (node) { state.selected = node; inspect(node.data, node.kind); } } });
  canvas.addEventListener("pointerleave", () => { state.hovered = null; state.dragging = null; showTooltip(null); syncAnimation(); });
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); const bounds = canvas.getBoundingClientRect(); zoom(event.deltaY < 0 ? 1.12 : .89, event.clientX - bounds.left, event.clientY - bounds.top); }, { passive: false });
  byId("zoom-in").addEventListener("click", () => zoom(1.18)); byId("zoom-out").addEventListener("click", () => zoom(.84)); byId("view-reset").addEventListener("click", resetView); byId("focus-selected").addEventListener("click", focusSelected); byId("selection-clear").addEventListener("click", clearSelection);
  byId("motion-toggle").addEventListener("click", () => { state.paused = !state.paused; byId("motion-toggle").setAttribute("aria-pressed", String(state.paused)); text(byId("motion-toggle"), state.paused ? "HAREKETİ BAŞLAT" : "HAREKETİ DURDUR"); syncAnimation(); });
  byId("view-toggle").addEventListener("click", () => { state.listMode = !state.listMode; byId("view-toggle").setAttribute("aria-pressed", String(state.listMode)); text(byId("view-toggle"), state.listMode ? "GRAF" : "LİSTE"); stage.hidden = state.listMode; fallback.hidden = !state.listMode; syncAnimation(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.stream && !state.pollingTimer) connectStream(); syncAnimation(); publishRuntimeDiagnostics(); });
  reducedMotion.addEventListener?.("change", syncAnimation);
  new ResizeObserver(resizeCanvas).observe(stage);

  const diagnostics = new URLSearchParams(window.location.search);
  const diagnosticMode = diagnostics.get("diagnostics") === "graph";
  if (diagnosticMode) document.documentElement.dataset.zekamBenchmark = JSON.stringify(benchmarkGraph(diagnostics.get("nodes"), diagnostics.get("edges")));

  async function boot() {
    resizeCanvas();
    if (state.listMode) { stage.hidden = true; fallback.hidden = false; }
    try {
      if (diagnosticMode) { loadDiagnosticGraph(diagnostics.get("nodes"), diagnostics.get("edges")); setConnection("live", "SENTETİK DIAGNOSTICS"); }
      else if (window.__ZEKAM_PREVIEW__) applySnapshot(window.__ZEKAM_PREVIEW__);
      else { await fetchSnapshot(); connectStream(); }
    }
    catch (_) { setConnection("error", "POLLING YEDEK"); schedulePolling(); }
    syncAnimation();
  }
  boot();
})();
