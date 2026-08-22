(() => {
  "use strict";

  const canvas = document.getElementById("brain-canvas");
  const context = canvas.getContext("2d", { alpha: true });
  const wrap = document.getElementById("brain-wrap");
  const search = document.getElementById("graph-search");
  const motionToggle = document.getElementById("motion-toggle");
  const recenterButton = document.getElementById("recenter-button");

  const state = {
    snapshot: null,
    nodes: [],
    edges: [],
    nodeMap: new Map(),
    positions: new Map(),
    selected: null,
    hovered: null,
    focus: "all",
    query: "",
    paused: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    connected: false,
    source: null,
    pollTimer: null,
    lastFrame: 0,
    time: 0,
    dpr: 1,
    width: 0,
    height: 0,
    pointer: { x: 0, y: 0 },
  };

  const palette = {
    system: "#b6ffe7",
    "runtime-root": "#76ffd0",
    "runtime-cluster": "#83f8d2",
    "document-cluster": "#83a79d",
    work: "#76ffd0",
    job: "#9fffe0",
    agent: "#ffce73",
    "agent-session": "#72ddff",
    client: "#ffffff",
    model: "#8de9ff",
    knowledge: "#62e8ff",
    memory: "#c7a3ff",
    scheduler: "#ffce73",
    report: "#ff9cc3",
    document: "#86a9a0",
  };

  const clusterCenters = {
    system: [0.50, 0.48],
    "runtime-root": [0.50, 0.53],
    work: [0.30, 0.31],
    run: [0.48, 0.27],
    client: [0.50, 0.22],
    model: [0.70, 0.31],
    knowledge: [0.71, 0.61],
    memory: [0.31, 0.64],
    scheduler: [0.51, 0.72],
    reports: [0.20, 0.50],
    architecture: [0.35, 0.20],
    operations: [0.52, 0.18],
    contracts: [0.72, 0.46],
    docs: [0.62, 0.76],
    core: [0.40, 0.76],
  };

  function hash(value) {
    let result = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      result ^= value.charCodeAt(index);
      result = Math.imul(result, 16777619);
    }
    return result >>> 0;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function fmtTime(value, withSeconds = true) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("tr-TR", {
      hour: "2-digit",
      minute: "2-digit",
      second: withSeconds ? "2-digit" : undefined,
      hour12: false,
    }).format(date);
  }

  function relativeTime(value) {
    if (!value) return "—";
    const delta = Date.now() - new Date(value).getTime();
    if (!Number.isFinite(delta)) return "—";
    const seconds = Math.max(0, Math.round(delta / 1000));
    if (seconds < 60) return `${seconds} sn önce`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} dk önce`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} sa önce`;
    return `${Math.round(hours / 24)} gün önce`;
  }

  function elapsedTime(value) {
    if (!value) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${seconds} sn`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} dk ${seconds % 60} sn`;
    return `${Math.floor(seconds / 3600)} sa ${Math.floor((seconds % 3600) / 60)} dk`;
  }

  function setConnection(mode, label) {
    const status = document.getElementById("live-status");
    const stream = document.getElementById("stream-state");
    const liveLabel = document.getElementById("live-label");
    status.classList.remove("is-live", "is-error", "is-connecting");
    status.classList.add(mode === "live" ? "is-live" : mode === "error" ? "is-error" : "is-connecting");
    liveLabel.textContent = label;
    stream.textContent = mode === "live" ? "SSE canlı" : mode === "error" ? "polling" : "bağlanıyor";
    state.connected = mode === "live";
  }

  function tileGlyph(key) {
    return { work: "◇", run: "◉", model: "△", knowledge: "⌁", memory: "∞", scheduler: "◷" }[key] || "·";
  }

  function renderTiles(tiles) {
    const target = document.getElementById("tile-grid");
    target.innerHTML = (tiles || []).map((tile) => `
      <article class="metric-tile" data-key="${escapeHtml(tile.key)}">
        <div class="metric-top"><span>${escapeHtml(tile.title)}</span><span class="metric-glyph">${tileGlyph(tile.key)}</span></div>
        <strong>${Number(tile.value || 0).toLocaleString("tr-TR")}</strong>
        <small title="${escapeHtml(tile.drill_down)}">${escapeHtml(tile.detail || tile.drill_down)}</small>
      </article>
    `).join("");
  }

  function renderAgents(agents) {
    const list = document.getElementById("agent-list");
    const activeStates = new Set(["active", "running", "claimed", "executing", "in_progress"]);
    const rows = [...(agents || [])].sort((left, right) => Number(activeStates.has(right.state)) - Number(activeStates.has(left.state)));
    const activeCount = rows.filter((agent) => activeStates.has(agent.state)).length;
    document.getElementById("agent-count").textContent = activeCount.toLocaleString("tr-TR");
    document.getElementById("agent-badge").textContent = `${activeCount} LIVE`;
    document.getElementById("active-session-count").textContent = activeCount.toLocaleString("tr-TR");
    if (!rows.length) {
      list.innerHTML = '<div class="agent-empty">Aktif lease gözlenmiyor.<br>Belge grafı çalışmaya devam ediyor.</div>';
      return;
    }
    list.innerHTML = rows.map((agent) => {
      const initials = String(agent.client || "zk").slice(0, 2).toUpperCase();
      return `
        <article class="agent-card ${activeStates.has(agent.state) ? "is-active" : ""}" data-agent="${escapeHtml(agent.agent_id)}">
          <div class="agent-top">
            <div class="agent-identity">
              <span class="agent-avatar">${escapeHtml(initials)}</span>
              <div><strong>${escapeHtml(agent.task_label || agent.label)}</strong><small>${escapeHtml(agent.client)} · ${escapeHtml(agent.model_ref || "model —")}</small></div>
            </div>
            <span class="agent-state">${escapeHtml(agent.state)}</span>
          </div>
          <div class="agent-progress"><span></span></div>
          <div class="agent-meta">
            <span>${escapeHtml(agent.current_tool ? `tool ${agent.current_tool}` : agent.step_id || "tool bekleniyor")}</span>
            <span>${elapsedTime(agent.started_at)} · ${relativeTime(agent.heartbeat_at)}</span>
          </div>
        </article>
      `;
    }).join("");
  }

  function normalizedClient(value) {
    const client = String(value || "").toLowerCase();
    if (client.includes("opencode")) return "opencode";
    if (client.includes("codex")) return "codex";
    if (client.includes("claude")) return "claude";
    return client || "zekam";
  }

  function renderClients(agents, events) {
    const target = document.getElementById("client-grid");
    const known = [
      { key: "opencode", label: "OpenCode", glyph: "OC" },
      { key: "codex", label: "Codex", glyph: "CX" },
      { key: "claude", label: "Claude", glyph: "CL" },
    ];
    const now = Date.now();
    target.innerHTML = known.map((client) => {
      const sessions = (agents || []).filter((agent) => normalizedClient(agent.client) === client.key);
      const clientEvents = (events || []).filter((event) => {
        if (normalizedClient(event.source) === client.key) return true;
        return sessions.some((agent) => agent.agent_id === event.agent_id);
      });
      const timestamps = [...sessions.map((item) => item.heartbeat_at), ...clientEvents.map((item) => item.occurred_at)]
        .map((value) => Date.parse(value || ""))
        .filter(Number.isFinite);
      const latest = timestamps.length ? Math.max(...timestamps) : 0;
      const live = latest > 0 && now - latest < 30000;
      const status = live ? "CANLI" : sessions.length ? "BEKLEMEDE" : "SİNYAL YOK";
      return `
        <article class="client-card client-${client.key} ${live ? "is-live" : ""}">
          <span class="client-glyph">${client.glyph}</span>
          <div><strong>${client.label}</strong><small>${sessions.length} oturum · ${latest ? relativeTime(new Date(latest).toISOString()) : "veri yok"}</small></div>
          <span class="client-status">${status}</span>
        </article>
      `;
    }).join("");
  }

  function renderEvents(events) {
    const list = document.getElementById("event-list");
    const rows = events || [];
    document.getElementById("event-count").textContent = `${rows.length} olay`;
    document.getElementById("event-rate").textContent = rows.length.toLocaleString("tr-TR");
    if (!rows.length) {
      list.innerHTML = '<div class="empty-copy">Henüz içeriksiz runtime olayı yok.</div>';
      return;
    }
    list.innerHTML = rows.slice(0, 40).map((event) => `
      <div class="event-row">
        <time datetime="${escapeHtml(event.occurred_at)}">${fmtTime(event.occurred_at)}</time>
        <span class="event-mark"></span>
        <div><strong>${escapeHtml(event.event_type)}</strong><small>${escapeHtml(event.job_id ? `job ${event.job_id.slice(0, 8)}` : event.canonical_ref)}</small></div>
        <span class="event-source source-${escapeHtml(normalizedClient(event.source))}">${escapeHtml(normalizedClient(event.source).toUpperCase())}</span>
      </div>
    `).join("");
  }

  function renderReports(reports) {
    const list = document.getElementById("report-list");
    const rows = reports || [];
    document.getElementById("report-count").textContent = rows.length;
    if (!rows.length) {
      list.innerHTML = '<div class="empty-copy">Rapor bulunamadı.</div>';
      return;
    }
    list.innerHTML = rows.map((report) => `
      <div class="report-item" title="${escapeHtml(report.canonical_ref)}">
        <strong>${escapeHtml(report.title)}</strong>
        <span>${relativeTime(report.modified_at)} · ${escapeHtml(report.relative_path)}</span>
      </div>
    `).join("");
  }

  function graphDomain(node) {
    if (node.kind === "client") return "client";
    if (node.kind === "agent-session") return "run";
    if (node.node_id.startsWith("runtime:cluster:")) return node.node_id.split(":").at(-1);
    if (node.node_id.startsWith("cluster:docs:")) return node.node_id.split(":").at(-1);
    const kind = node.kind;
    if (["work", "job", "agent", "model", "knowledge", "memory", "scheduler"].includes(kind)) return kind === "job" || kind === "agent" ? "run" : kind;
    if (kind === "report") return "reports";
    if (kind === "system" || kind === "runtime-root") return kind;
    const ref = node.canonical_ref.toLowerCase();
    if (ref.includes("mimari")) return "architecture";
    if (ref.includes("operasyon")) return "operations";
    if (ref.includes("bellek") || ref.includes("knowledge") || ref.includes("rag")) return "knowledge";
    if (ref.includes("guvenlik") || ref.includes("model") || ref.includes("sozlesme")) return "contracts";
    return "docs";
  }

  function calculatePositions(nodes) {
    const positions = new Map();
    const groups = new Map();
    for (const node of nodes) {
      const domain = graphDomain(node);
      if (!groups.has(domain)) groups.set(domain, []);
      groups.get(domain).push(node);
    }

    for (const [domain, rows] of groups.entries()) {
      const center = clusterCenters[domain] || clusterCenters.docs;
      rows.forEach((node, index) => {
        const seed = hash(node.node_id);
        const isRoot = node.kind === "system" || node.kind === "runtime-root";
        const isCluster = node.kind.endsWith("cluster");
        const angle = ((seed % 10000) / 10000) * Math.PI * 2 + index * 0.31;
        const radius = isRoot ? 0 : isCluster ? 0.012 : 0.035 + ((seed >>> 8) % 1000) / 1000 * Math.min(0.115, 0.026 + rows.length * 0.0017);
        const x = center[0] + Math.cos(angle) * radius;
        const y = center[1] + Math.sin(angle) * radius * 0.72;
        positions.set(node.node_id, {
          x: Math.max(0.08, Math.min(0.92, x)),
          y: Math.max(0.08, Math.min(0.91, y)),
          vx: 0,
          vy: 0,
          seed,
        });
      });
    }
    state.positions = positions;
  }

  function applySnapshot(snapshot) {
    if (!snapshot || snapshot.schema !== "zekam-observatory-snapshot/v1") return;
    state.snapshot = snapshot;
    state.nodes = snapshot.graph?.nodes || [];
    state.edges = snapshot.graph?.edges || [];
    state.nodeMap = new Map(state.nodes.map((node) => [node.node_id, node]));
    calculatePositions(state.nodes);

    renderTiles(snapshot.dashboard?.tiles || []);
    renderClients(snapshot.agents || [], snapshot.events || []);
    renderAgents(snapshot.agents || []);
    renderEvents(snapshot.events || []);
    renderReports(snapshot.reports || []);

    const runtime = snapshot.runtime || {};
    document.getElementById("runtime-mode").textContent = runtime.available ? "CANLI REALM" : "BELGE MODU";
    document.getElementById("digest-value").textContent = String(snapshot.projection_digest || "—").replace("sha256:", "").slice(0, 10);
    document.getElementById("snapshot-time").textContent = `snapshot ${fmtTime(snapshot.generated_at)}`;
    document.getElementById("node-count").textContent = state.nodes.length.toLocaleString("tr-TR");
    document.getElementById("edge-count").textContent = state.edges.length.toLocaleString("tr-TR");
    document.getElementById("graph-empty").hidden = state.nodes.length > 0;

    if (state.selected && !state.nodeMap.has(state.selected.node_id)) selectNode(null);
  }

  async function fetchSnapshot() {
    const response = await fetch("/api/observatory/snapshot", { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    applySnapshot(await response.json());
  }

  function schedulePolling() {
    if (state.pollTimer) return;
    state.pollTimer = window.setInterval(async () => {
      try {
        await fetchSnapshot();
      } catch (_) {
        setConnection("error", "YENİDEN BAĞLANIYOR");
      }
    }, 5000);
  }

  function connectStream() {
    if (state.source) state.source.close();
    setConnection("connecting", "AKIŞ BAĞLANIYOR");
    const source = new EventSource("/api/observatory/events");
    state.source = source;
    source.addEventListener("snapshot", (event) => {
      try {
        applySnapshot(JSON.parse(event.data));
        setConnection("live", "SSE CANLI");
        if (state.pollTimer) {
          clearInterval(state.pollTimer);
          state.pollTimer = null;
        }
      } catch (_) {
        setConnection("error", "SNAPSHOT HATASI");
      }
    });
    source.onopen = () => setConnection("live", "SSE CANLI");
    source.onerror = () => {
      setConnection("error", "POLLING YEDEK");
      schedulePolling();
    };
  }

  function resizeCanvas() {
    const bounds = wrap.getBoundingClientRect();
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    state.width = Math.max(1, Math.round(bounds.width));
    state.height = Math.max(1, Math.round(bounds.height));
    canvas.width = Math.round(state.width * state.dpr);
    canvas.height = Math.round(state.height * state.dpr);
    canvas.style.width = `${state.width}px`;
    canvas.style.height = `${state.height}px`;
    context.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  }

  function brainPath(width, height) {
    const path = new Path2D();
    const cx = width * 0.5;
    const cy = height * 0.51;
    const sx = width * 0.39;
    const sy = height * 0.40;
    path.moveTo(cx, cy - sy * 0.92);
    path.bezierCurveTo(cx - sx * 0.13, cy - sy * 1.08, cx - sx * 0.54, cy - sy * 1.02, cx - sx * 0.72, cy - sy * 0.73);
    path.bezierCurveTo(cx - sx * 1.04, cy - sy * 0.61, cx - sx * 1.03, cy - sy * 0.18, cx - sx * 0.91, cy - sy * 0.02);
    path.bezierCurveTo(cx - sx * 1.04, cy + sy * 0.23, cx - sx * 0.82, cy + sy * 0.55, cx - sx * 0.61, cy + sy * 0.61);
    path.bezierCurveTo(cx - sx * 0.50, cy + sy * 0.92, cx - sx * 0.12, cy + sy * 1.03, cx, cy + sy * 0.76);
    path.bezierCurveTo(cx + sx * 0.15, cy + sy * 1.04, cx + sx * 0.53, cy + sy * 0.91, cx + sx * 0.63, cy + sy * 0.60);
    path.bezierCurveTo(cx + sx * 0.91, cy + sy * 0.48, cx + sx * 1.04, cy + sy * 0.19, cx + sx * 0.90, cy - sy * 0.04);
    path.bezierCurveTo(cx + sx * 1.03, cy - sy * 0.29, cx + sx * 0.96, cy - sy * 0.63, cx + sx * 0.70, cy - sy * 0.73);
    path.bezierCurveTo(cx + sx * 0.53, cy - sy * 1.03, cx + sx * 0.14, cy - sy * 1.08, cx, cy - sy * 0.92);
    path.closePath();
    return path;
  }

  function drawBrainBase() {
    const path = brainPath(state.width, state.height);
    context.save();
    context.shadowBlur = 28;
    context.shadowColor = "rgba(118,255,208,.18)";
    const fill = context.createRadialGradient(state.width * 0.50, state.height * 0.48, 20, state.width * 0.50, state.height * 0.50, state.width * 0.42);
    fill.addColorStop(0, "rgba(42,108,86,.12)");
    fill.addColorStop(0.62, "rgba(12,47,38,.10)");
    fill.addColorStop(1, "rgba(2,12,10,.03)");
    context.fillStyle = fill;
    context.fill(path);
    context.shadowBlur = 0;
    context.strokeStyle = "rgba(136,255,218,.14)";
    context.lineWidth = 1.2;
    context.stroke(path);
    context.restore();

    context.save();
    context.clip(path);
    drawGyri();
    context.restore();

    context.save();
    context.beginPath();
    context.moveTo(state.width * 0.5, state.height * 0.14);
    context.bezierCurveTo(state.width * 0.48, state.height * 0.33, state.width * 0.52, state.height * 0.69, state.width * 0.50, state.height * 0.83);
    context.strokeStyle = "rgba(118,255,208,.09)";
    context.lineWidth = 1;
    context.stroke();
    context.restore();
  }

  function drawGyri() {
    const width = state.width;
    const height = state.height;
    context.lineWidth = 0.75;
    for (let index = 0; index < 26; index += 1) {
      const seed = hash(`gyrus-${index}`);
      const side = index % 2 === 0 ? -1 : 1;
      const startX = width * (0.5 + side * (0.06 + ((seed % 100) / 100) * 0.25));
      const startY = height * (0.21 + (((seed >>> 8) % 100) / 100) * 0.57);
      const length = width * (0.07 + (((seed >>> 16) % 100) / 100) * 0.10);
      context.beginPath();
      context.moveTo(startX, startY);
      context.bezierCurveTo(
        startX + side * length * 0.28,
        startY - height * 0.045,
        startX + side * length * 0.65,
        startY + height * 0.045,
        startX + side * length,
        startY + Math.sin(index) * height * 0.025,
      );
      context.strokeStyle = `rgba(126, 235, 199, ${0.035 + (index % 3) * 0.012})`;
      context.stroke();
    }
  }

  function nodeVisible(node) {
    const query = state.query.trim().toLowerCase();
    const domain = graphDomain(node);
    const focusMatch = state.focus === "all" || domain === state.focus || (state.focus === "run" && ["job", "agent"].includes(node.kind));
    if (!focusMatch) return false;
    if (!query) return true;
    return `${node.label} ${node.kind} ${node.canonical_ref}`.toLowerCase().includes(query);
  }

  function pointFor(nodeId) {
    const position = state.positions.get(nodeId);
    if (!position) return null;
    const marginX = state.width * 0.08;
    const marginY = state.height * 0.05;
    return {
      x: marginX + position.x * (state.width - marginX * 2),
      y: marginY + position.y * (state.height - marginY * 2),
    };
  }

  function drawEdges() {
    const activeStates = new Set(["active", "running", "claimed", "executing", "in_progress"]);
    const activeIds = new Set((state.snapshot?.agents || []).filter((agent) => activeStates.has(agent.state)).flatMap((agent) => [agent.agent_id, agent.job_id ? `job:${agent.job_id}` : ""]));
    for (const edge of state.edges) {
      const sourceNode = state.nodeMap.get(edge.source);
      const targetNode = state.nodeMap.get(edge.target);
      if (!sourceNode || !targetNode || !nodeVisible(sourceNode) || !nodeVisible(targetNode)) continue;
      const source = pointFor(edge.source);
      const target = pointFor(edge.target);
      if (!source || !target) continue;
      const active = activeIds.has(edge.source) || activeIds.has(edge.target) || edge.kind.includes("active") || edge.kind.includes("lease") || edge.kind.includes("running");
      const alpha = active ? 0.72 : edge.kind === "markdown-link" ? 0.08 : 0.12;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const bend = Math.min(30, Math.hypot(dx, dy) * 0.12);
      const controlX = (source.x + target.x) / 2 - dy / Math.max(1, Math.hypot(dx, dy)) * bend;
      const controlY = (source.y + target.y) / 2 + dx / Math.max(1, Math.hypot(dx, dy)) * bend;

      context.beginPath();
      context.moveTo(source.x, source.y);
      context.quadraticCurveTo(controlX, controlY, target.x, target.y);
      context.strokeStyle = active ? `rgba(118,255,208,${alpha})` : `rgba(123,183,165,${alpha})`;
      context.lineWidth = active ? 2.1 : 0.7;
      context.stroke();

      if (!state.paused && (active || hash(edge.kind) % 7 === 0)) {
        const phase = (state.time * (active ? 0.00022 : 0.00009) + (hash(`${edge.source}:${edge.target}`) % 1000) / 1000) % 1;
        const oneMinus = 1 - phase;
        const pulseX = oneMinus * oneMinus * source.x + 2 * oneMinus * phase * controlX + phase * phase * target.x;
        const pulseY = oneMinus * oneMinus * source.y + 2 * oneMinus * phase * controlY + phase * phase * target.y;
        context.beginPath();
        context.arc(pulseX, pulseY, active ? 2.1 : 1.2, 0, Math.PI * 2);
        context.fillStyle = active ? "rgba(190,255,233,.95)" : "rgba(118,255,208,.55)";
        context.shadowBlur = active ? 12 : 7;
        context.shadowColor = "#76ffd0";
        context.fill();
        context.shadowBlur = 0;
      }
    }
  }

  function nodeRadius(node) {
    if (node.kind === "system") return 9;
    if (node.kind === "runtime-root") return 7;
    if (node.kind.endsWith("cluster")) return 5.4;
    if (node.kind === "agent") return 5;
    if (node.kind === "agent-session") return 6.2;
    if (["work", "job", "model"].includes(node.kind)) return 4.1;
    return 3.1;
  }

  function drawNodes() {
    const activeStates = new Set(["active", "running", "claimed", "executing", "in_progress"]);
    const activeAgents = new Set((state.snapshot?.agents || []).filter((agent) => activeStates.has(agent.state)).map((agent) => agent.agent_id));
    for (const node of state.nodes) {
      if (!nodeVisible(node)) continue;
      const point = pointFor(node.node_id);
      if (!point) continue;
      const radius = nodeRadius(node);
      const color = palette[node.kind] || palette.document;
      const selected = state.selected?.node_id === node.node_id;
      const hovered = state.hovered?.node_id === node.node_id;
      const active = activeAgents.has(node.node_id) || node.kind === "job" && /running|recovery/.test(node.label.toLowerCase());
      const pulse = state.paused ? 0 : Math.sin(state.time * 0.003 + (hash(node.node_id) % 100)) * 0.8;

      if (active || selected || hovered || node.kind === "system") {
        context.beginPath();
        context.arc(point.x, point.y, radius + 5 + Math.max(0, pulse), 0, Math.PI * 2);
        context.strokeStyle = selected ? "rgba(255,255,255,.52)" : active ? "rgba(118,255,208,.32)" : "rgba(118,255,208,.18)";
        context.lineWidth = selected ? 1.2 : 0.7;
        context.stroke();
      }

      if (active && !state.paused) {
        for (let ring = 0; ring < 3; ring += 1) {
          const expansion = (state.time * 0.035 + ring * 11 + hash(node.node_id) % 17) % 34;
          context.beginPath();
          context.arc(point.x, point.y, radius + 7 + expansion, 0, Math.PI * 2);
          context.strokeStyle = `rgba(114,221,255,${Math.max(0, 0.34 - expansion / 100)})`;
          context.lineWidth = 1.4;
          context.stroke();
        }
      }

      context.beginPath();
      context.arc(point.x, point.y, radius + (active ? pulse * 0.15 : 0), 0, Math.PI * 2);
      context.fillStyle = color;
      context.shadowBlur = active || selected ? 17 : 8;
      context.shadowColor = color;
      context.fill();
      context.shadowBlur = 0;

      if (selected || hovered || node.kind === "system" || node.kind.endsWith("cluster")) {
        const label = node.label.length > 30 ? `${node.label.slice(0, 29)}…` : node.label;
        context.font = `${selected ? 10 : 9}px ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
        context.fillStyle = selected ? "rgba(235,255,248,.95)" : "rgba(188,218,208,.72)";
        context.textAlign = "center";
        context.fillText(label, point.x, point.y + radius + 14);
      }
    }
  }

  function drawFrame(timestamp) {
    if (!state.paused) state.time = timestamp;
    context.clearRect(0, 0, state.width, state.height);
    drawBrainBase();
    context.save();
    context.clip(brainPath(state.width, state.height));
    drawEdges();
    drawNodes();
    context.restore();
    state.lastFrame = timestamp;
    window.requestAnimationFrame(drawFrame);
  }

  function nearestNode(clientX, clientY) {
    const bounds = canvas.getBoundingClientRect();
    const x = clientX - bounds.left;
    const y = clientY - bounds.top;
    let nearest = null;
    let distance = 16;
    for (const node of state.nodes) {
      if (!nodeVisible(node)) continue;
      const point = pointFor(node.node_id);
      if (!point) continue;
      const current = Math.hypot(point.x - x, point.y - y);
      if (current < distance) {
        distance = current;
        nearest = node;
      }
    }
    return { node: nearest, x, y };
  }

  function countLinks(nodeId) {
    return state.edges.reduce((total, edge) => total + Number(edge.source === nodeId || edge.target === nodeId), 0);
  }

  function selectNode(node, point = null) {
    state.selected = node;
    const empty = document.getElementById("selection-empty");
    const content = document.getElementById("selection-content");
    const card = document.getElementById("node-card");
    if (!node) {
      empty.hidden = false;
      content.hidden = true;
      card.hidden = true;
      return;
    }
    empty.hidden = true;
    content.hidden = false;
    document.getElementById("selection-kind").textContent = node.kind;
    document.getElementById("selection-label").textContent = node.label;
    document.getElementById("selection-ref").textContent = node.canonical_ref;
    document.getElementById("selection-links").textContent = countLinks(node.node_id);
    document.getElementById("selection-glyph").textContent = node.kind === "memory" ? "∞" : node.kind === "knowledge" ? "⌁" : node.kind === "model" ? "△" : "◉";

    document.getElementById("node-kind").textContent = node.kind;
    document.getElementById("node-label").textContent = node.label;
    document.getElementById("node-ref").textContent = node.canonical_ref;
    if (point) {
      const left = Math.min(state.width - 296, Math.max(16, point.x + 14));
      const top = Math.min(state.height - 180, Math.max(16, point.y - 22));
      card.style.left = `${left}px`;
      card.style.top = `${top}px`;
    }
    card.hidden = false;
  }

  canvas.addEventListener("pointermove", (event) => {
    const result = nearestNode(event.clientX, event.clientY);
    state.hovered = result.node;
    canvas.style.cursor = result.node ? "pointer" : "crosshair";
  });
  canvas.addEventListener("pointerleave", () => { state.hovered = null; });
  canvas.addEventListener("click", (event) => {
    const result = nearestNode(event.clientX, event.clientY);
    selectNode(result.node, result);
  });

  document.getElementById("node-card-close").addEventListener("click", () => selectNode(null));
  document.getElementById("copy-ref").addEventListener("click", async () => {
    if (!state.selected) return;
    try {
      await navigator.clipboard.writeText(state.selected.canonical_ref);
      document.getElementById("copy-ref").textContent = "Kopyalandı";
      window.setTimeout(() => { document.getElementById("copy-ref").textContent = "Kanonik referansı kopyala"; }, 1200);
    } catch (_) {
      document.getElementById("copy-ref").textContent = "Kopyalanamadı";
    }
  });

  search.addEventListener("input", () => { state.query = search.value; });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement !== search) {
      event.preventDefault();
      search.focus();
    }
    if (event.key === "Escape") {
      search.value = "";
      state.query = "";
      selectNode(null);
    }
    const focusByKey = { "1": "all", "2": "run", "3": "knowledge", "4": "memory", "5": "model" };
    if (focusByKey[event.key] && document.activeElement !== search) setFocus(focusByKey[event.key]);
  });

  function setFocus(focus) {
    state.focus = focus;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.focus === focus));
  }
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => setFocus(item.dataset.focus)));

  motionToggle.addEventListener("click", () => {
    state.paused = !state.paused;
    motionToggle.textContent = state.paused ? "▶" : "Ⅱ";
    motionToggle.setAttribute("aria-pressed", String(state.paused));
  });
  recenterButton.addEventListener("click", () => {
    calculatePositions(state.nodes);
    selectNode(null);
  });

  new ResizeObserver(resizeCanvas).observe(wrap);
  window.setInterval(() => {
    document.getElementById("clock").textContent = new Intl.DateTimeFormat("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
  }, 1000);

  async function boot() {
    resizeCanvas();
    try {
      if (window.__ZEKAM_PREVIEW__) {
        applySnapshot(window.__ZEKAM_PREVIEW__);
        setConnection("live", "ÖNİZLEME");
      } else {
        await fetchSnapshot();
        connectStream();
      }
    } catch (_) {
      setConnection("error", "POLLING YEDEK");
      schedulePolling();
    }
    window.requestAnimationFrame(drawFrame);
  }

  boot();
})();
