(() => {
  "use strict";

  const canvas = document.getElementById("brain-canvas");
  const context = canvas.getContext("2d", { alpha: true });
  const wrap = document.getElementById("brain-wrap");
  const search = document.getElementById("graph-search");
  const motionToggle = document.getElementById("motion-toggle");
  const recenterButton = document.getElementById("recenter-button");
  const liveNetworkToggle = document.getElementById("live-network-toggle");

  const state = {
    snapshot: null,
    nodes: [],
    edges: [],
    nodeMap: new Map(),
    positions: new Map(),
    selected: null,
    hovered: null,
    focus: "all",
    liveMode: false,
    liveNodeIds: new Set(),
    activeNodeIds: new Set(),
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
    tool: "#76ffd0",
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

  const domainPalette = {
    system: "#b6ffe7", "runtime-root": "#76ffd0", work: "#76ffd0",
    run: "#72ddff", model: "#8de9ff", knowledge: "#62e8ff",
    memory: "#c7a3ff", scheduler: "#ffce73", reports: "#ff9cc3",
    architecture: "#8ed7c5", operations: "#7be7c7", contracts: "#8db8ff",
    docs: "#86a9a0", core: "#83a79d",
  };

  const domainLabels = {
    work: "İŞLER", run: "ÇALIŞMA AĞI", model: "MODELLER", knowledge: "BİLGİ",
    memory: "BELLEK", scheduler: "ZAMANLAYICI", reports: "RAPOR VE KANIT",
    architecture: "MİMARİ", operations: "OPERASYON", contracts: "SÖZLEŞMELER",
    docs: "BELGELER", core: "KANONİK ÇEKİRDEK",
  };

  function colorWithAlpha(color, alpha) {
    const value = color.replace("#", "");
    return `rgba(${Number.parseInt(value.slice(0, 2), 16)},${Number.parseInt(value.slice(2, 4), 16)},${Number.parseInt(value.slice(4, 6), 16)},${alpha})`;
  }

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
    const runtimeAvailable = Boolean(state.snapshot?.runtime?.available);
    const visibleTiles = (tiles || []).filter((tile) => runtimeAvailable || Number(tile.value || 0) > 0);
    target.hidden = visibleTiles.length === 0;
    target.innerHTML = visibleTiles.map((tile) => `
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
    const allRows = [...(agents || [])];
    const byId = new Map(allRows.map((agent) => [agent.agent_id, agent]));
    const visibleIds = new Set(allRows.filter((agent) => activeStates.has(agent.state)).map((agent) => agent.agent_id));
    for (const agent of allRows) {
      if (!visibleIds.has(agent.agent_id)) continue;
      let parent = agent.parent_agent_id;
      while (parent && byId.has(parent)) {
        visibleIds.add(parent);
        parent = byId.get(parent).parent_agent_id;
      }
    }
    const depth = (agent) => {
      let value = 0;
      let parent = agent.parent_agent_id;
      while (parent && byId.has(parent) && value < 4) {
        value += 1;
        parent = byId.get(parent).parent_agent_id;
      }
      return value;
    };
    const rows = allRows.filter((agent) => visibleIds.has(agent.agent_id)).sort((left, right) => depth(left) - depth(right));
    const activeCount = allRows.filter((agent) => activeStates.has(agent.state)).length;
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
        <article class="agent-card ${activeStates.has(agent.state) ? "is-active" : "is-context"}" data-agent="${escapeHtml(agent.agent_id)}" style="--session-depth:${depth(agent)}">
          <div class="agent-top">
            <div class="agent-identity">
              <span class="agent-avatar">${escapeHtml(initials)}</span>
              <div><strong>${escapeHtml(agent.task_label || agent.label)}</strong><small>${escapeHtml(agent.client)} · ${escapeHtml(agent.model_ref || "model —")}</small></div>
            </div>
            <span class="agent-state">${escapeHtml(agent.state)}</span>
          </div>
          <div class="agent-progress"><span></span></div>
          <div class="agent-meta">
            <span>${escapeHtml(agent.active_tool ? `aktif araç ${agent.active_tool}` : agent.current_tool ? `son araç ${agent.current_tool}` : agent.step_id || "araç bekleniyor")}</span>
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

  function toolNodeId(agent) {
    return `tool:${agent.agent_id}:${String(agent.active_tool || "tool").toLowerCase()}`;
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
    if (node.kind === "client") return "run";
    if (node.kind === "agent-session") return "run";
    if (node.kind === "tool") return "run";
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
      rows.sort((left, right) => left.node_id.localeCompare(right.node_id)).forEach((node, index) => {
        const seed = hash(node.node_id);
        const isRoot = node.kind === "system" || node.kind === "runtime-root";
        const isCluster = node.kind.endsWith("cluster");
        const goldenAngle = Math.PI * (3 - Math.sqrt(5));
        const angle = index * goldenAngle + ((seed % 1000) / 1000 - 0.5) * 0.24;
        const clusterRadius = Math.min(0.19, 0.055 + Math.sqrt(rows.length) * 0.012);
        const radius = isRoot ? 0 : isCluster ? 0.012 : 0.028 + Math.sqrt((index + 1) / rows.length) * clusterRadius;
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
    const agents = (state.snapshot?.agents || []).filter((agent) => state.liveNodeIds.has(agent.agent_id)).sort((left, right) => left.agent_id.localeCompare(right.agent_id));
    const activeMap = new Map(agents.map((agent) => [agent.agent_id, agent]));
    const roots = agents.filter((agent) => !agent.parent_agent_id || !activeMap.has(agent.parent_agent_id)).sort((left, right) => left.agent_id.localeCompare(right.agent_id));
    const clientAnchors = { opencode: 0.32, codex: 0.5, claude: 0.68, zekam: 0.5 };
    positions.set("client:opencode", { x: 0.32, y: 0.11, vx: 0, vy: 0, seed: 1 });
    positions.set("client:codex", { x: 0.5, y: 0.12, vx: 0, vy: 0, seed: 2 });
    positions.set("client:claude", { x: 0.68, y: 0.11, vx: 0, vy: 0, seed: 3 });
    positions.set("system:zekam", { x: 0.5, y: 0.86, vx: 0, vy: 0, seed: 4 });
    roots.forEach((agent) => {
      const client = normalizedClient(agent.client);
      const siblings = roots.filter((candidate) => normalizedClient(candidate.client) === client);
      const siblingIndex = siblings.findIndex((candidate) => candidate.agent_id === agent.agent_id);
      const rootX = (clientAnchors[client] || 0.5) + (siblingIndex - (siblings.length - 1) / 2) * 0.1;
      const placeBranch = (parent, x, y, depth) => {
        const safeX = Math.max(0.12, Math.min(0.88, x));
        const safeY = Math.max(0.18, Math.min(0.78, y));
        positions.set(parent.agent_id, { x: safeX, y: safeY, vx: 0, vy: 0, seed: hash(parent.agent_id) });
        if (depth >= 3) return;
        const children = agents.filter((child) => child.parent_agent_id === parent.agent_id).sort((left, right) => left.agent_id.localeCompare(right.agent_id));
        const branchWidth = Math.max(0.12, 0.56 / Math.max(1, roots.length));
        const spread = Math.max(0.1, branchWidth / Math.max(1, children.length));
        children.forEach((child, index) => {
          placeBranch(child, safeX + (index - (children.length - 1) / 2) * spread, safeY + 0.2, depth + 1);
        });
      };
      placeBranch(agent, rootX, 0.32, 0);
    });
    agents.filter((agent) => agent.active_tool).forEach((agent) => {
      const parent = positions.get(agent.agent_id);
      if (!parent) return;
      const offset = (hash(toolNodeId(agent)) % 2 ? 1 : -1) * 0.055;
      positions.set(toolNodeId(agent), {
        x: Math.max(0.12, Math.min(0.88, parent.x + offset)),
        y: Math.max(0.22, Math.min(0.8, parent.y + 0.13)),
        vx: 0,
        vy: 0,
        seed: hash(toolNodeId(agent)),
      });
    });
    state.positions = positions;
  }

  function applySnapshot(snapshot) {
    if (!snapshot || snapshot.schema !== "zekam-observatory-snapshot/v1") return;
    state.snapshot = snapshot;
    document.body.classList.toggle("live-network-mode", state.liveMode);
    const activeStates = new Set(["active", "running", "claimed", "executing", "in_progress"]);
    const activeAgents = (snapshot.agents || []).filter((agent) => activeStates.has(agent.state));
    const toolNodes = activeAgents.filter((agent) => agent.active_tool).map((agent) => ({
      node_id: toolNodeId(agent),
      kind: "tool",
      label: `aktif araç ${agent.active_tool}`,
      canonical_ref: `runtime:agent/${agent.agent_id}/tool/${String(agent.active_tool).toLowerCase()}`,
      status: agent.state,
      source: normalizedClient(agent.client),
    }));
    const toolEdges = activeAgents.filter((agent) => agent.active_tool).map((agent) => ({
      source: agent.agent_id,
      target: toolNodeId(agent),
      kind: "executes-tool",
    }));
    state.nodes = [...(snapshot.graph?.nodes || []), ...toolNodes];
    state.edges = [...(snapshot.graph?.edges || []), ...toolEdges];
    state.nodeMap = new Map(state.nodes.map((node) => [node.node_id, node]));
    const liveIds = new Set((snapshot.agents || []).filter((agent) => activeStates.has(agent.state)).map((agent) => agent.agent_id));
    const activeIds = new Set(liveIds);
    if (!liveIds.size) {
      for (const nodeId of state.liveNodeIds) if (state.nodeMap.has(nodeId)) liveIds.add(nodeId);
    }
    if (!liveIds.size && snapshot.agents?.length) {
      const latest = [...snapshot.agents].sort((left, right) => Date.parse(right.heartbeat_at || "") - Date.parse(left.heartbeat_at || ""))[0];
      liveIds.add(latest.agent_id);
    }
    for (let pass = 0; pass < 4; pass += 1) {
      for (const edge of state.edges) {
        const joinsConversation = edge.kind === "delegates" && (liveIds.has(edge.source) || liveIds.has(edge.target));
        const joinsClient = edge.kind === "runs-session" && liveIds.has(edge.target);
        const joinsTool = edge.kind === "executes-tool" && liveIds.has(edge.source);
        const joinsSystem = edge.kind === "reports-observation" && liveIds.has(edge.source);
        if (joinsConversation || joinsClient || joinsTool || joinsSystem) {
          liveIds.add(edge.source);
          liveIds.add(edge.target);
        }
      }
    }
    for (let pass = 0; pass < 4; pass += 1) {
      for (const edge of state.edges) {
        const joinsConversation = edge.kind === "delegates" && activeIds.has(edge.target);
        const joinsClient = edge.kind === "runs-session" && activeIds.has(edge.target);
        const joinsTool = edge.kind === "executes-tool" && activeIds.has(edge.source);
        const joinsSystem = edge.kind === "reports-observation" && activeIds.has(edge.source);
        if (joinsConversation || joinsClient || joinsTool || joinsSystem) {
          activeIds.add(edge.source);
          activeIds.add(edge.target);
        }
      }
    }
    state.liveNodeIds = liveIds;
    state.activeNodeIds = activeIds;
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
    const visibleNodes = state.liveMode && liveIds.size ? liveIds.size : state.nodes.length;
    const visibleEdges = state.liveMode && liveIds.size ? state.edges.filter((edge) => liveIds.has(edge.source) && liveIds.has(edge.target)).length : state.edges.length;
    document.getElementById("node-count").textContent = visibleNodes.toLocaleString("tr-TR");
    document.getElementById("edge-count").textContent = visibleEdges.toLocaleString("tr-TR");
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
    context.strokeStyle = "rgba(136,255,218,.30)";
    context.lineWidth = 1.8;
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
      context.strokeStyle = `rgba(126, 235, 199, ${0.07 + (index % 3) * 0.018})`;
      context.stroke();
    }
  }

  function nodeVisible(node) {
    if (state.liveMode && state.liveNodeIds.size) return state.liveNodeIds.has(node.node_id);
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

  function focusedNeighborhood() {
    const focusId = state.hovered?.node_id || state.selected?.node_id;
    if (!focusId) return null;
    const nodeIds = new Set([focusId]);
    const edgeKeys = new Set();
    for (const edge of state.edges) {
      if (edge.source !== focusId && edge.target !== focusId) continue;
      nodeIds.add(edge.source);
      nodeIds.add(edge.target);
      edgeKeys.add(`${edge.source}\u0000${edge.target}\u0000${edge.kind}`);
    }
    return { nodeIds, edgeKeys };
  }

  function drawClusterRegions() {
    const groups = new Map();
    for (const node of state.nodes) {
      if (!nodeVisible(node)) continue;
      const domain = graphDomain(node);
      if (["system", "runtime-root"].includes(domain)) continue;
      const point = pointFor(node.node_id);
      if (!point) continue;
      if (!groups.has(domain)) groups.set(domain, []);
      groups.get(domain).push(point);
    }
    for (const [domain, points] of groups.entries()) {
      if (points.length < 2) continue;
      const centerX = points.reduce((sum, point) => sum + point.x, 0) / points.length;
      const centerY = points.reduce((sum, point) => sum + point.y, 0) / points.length;
      const radiusX = Math.max(54, Math.max(...points.map((point) => Math.abs(point.x - centerX))) + 34);
      const radiusY = Math.max(38, Math.max(...points.map((point) => Math.abs(point.y - centerY))) + 26);
      const color = domainPalette[domain] || domainPalette.docs;
      const gradient = context.createRadialGradient(centerX, centerY, 0, centerX, centerY, Math.max(radiusX, radiusY));
      gradient.addColorStop(0, colorWithAlpha(color, 0.055));
      gradient.addColorStop(1, colorWithAlpha(color, 0));
      context.beginPath();
      context.ellipse(centerX, centerY, radiusX, radiusY, 0, 0, Math.PI * 2);
      context.fillStyle = gradient;
      context.fill();
      context.strokeStyle = colorWithAlpha(color, 0.12);
      context.lineWidth = 0.8;
      context.stroke();
      if (domainLabels[domain] && points.length >= 3) {
        context.font = `11px ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
        context.fillStyle = colorWithAlpha(color, 0.5);
        context.textAlign = "center";
        context.fillText(domainLabels[domain], centerX, centerY - radiusY + 17);
      }
    }
  }

  function drawEdges() {
    const neighborhood = focusedNeighborhood();
    for (const edge of state.edges) {
      const sourceNode = state.nodeMap.get(edge.source);
      const targetNode = state.nodeMap.get(edge.target);
      if (!sourceNode || !targetNode || !nodeVisible(sourceNode) || !nodeVisible(targetNode)) continue;
      const sourceCenter = pointFor(edge.source);
      const targetCenter = pointFor(edge.target);
      if (!sourceCenter || !targetCenter) continue;
      const centerDx = targetCenter.x - sourceCenter.x;
      const centerDy = targetCenter.y - sourceCenter.y;
      const centerDistance = Math.max(1, Math.hypot(centerDx, centerDy));
      const unitX = centerDx / centerDistance;
      const unitY = centerDy / centerDistance;
      const sourcePadding = Math.min(nodeRadius(sourceNode) + 4, centerDistance * 0.36);
      const targetPadding = Math.min(nodeRadius(targetNode) + 8, centerDistance * 0.36);
      const source = {
        x: sourceCenter.x + unitX * sourcePadding,
        y: sourceCenter.y + unitY * sourcePadding,
      };
      const target = {
        x: targetCenter.x - unitX * targetPadding,
        y: targetCenter.y - unitY * targetPadding,
      };
      const active = state.activeNodeIds.has(edge.source) && state.activeNodeIds.has(edge.target) && ["delegates", "runs-session", "executes-tool", "reports-observation"].includes(edge.kind);
      const edgeKey = `${edge.source}\u0000${edge.target}\u0000${edge.kind}`;
      const related = neighborhood?.edgeKeys.has(edgeKey) || false;
      const alpha = active ? 0.86 : related ? 0.64 : neighborhood ? 0.035 : edge.kind === "markdown-link" ? 0.1 : 0.22;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const direction = hash(`${edge.source}:${edge.target}:${edge.kind}`) % 2 ? 1 : -1;
      const bend = Math.min(active ? 72 : 46, distance * (active ? 0.2 : 0.13));
      const controlX = (source.x + target.x) / 2 - dy / distance * bend * direction;
      const controlY = (source.y + target.y) / 2 + dx / distance * bend * direction;

      context.beginPath();
      context.moveTo(source.x, source.y);
      context.quadraticCurveTo(controlX, controlY, target.x, target.y);
      const edgeColor = domainPalette[graphDomain(sourceNode)] || domainPalette.docs;
      context.strokeStyle = active ? colorWithAlpha(domainPalette.work, alpha) : colorWithAlpha(edgeColor, alpha);
      context.lineWidth = active ? 2.8 : related ? 1.8 : edge.kind === "markdown-link" ? 0.65 : 1;
      context.stroke();

      if (state.liveMode || active) {
        const arrowT = 0.9;
        const oneMinusArrow = 1 - arrowT;
        const arrowX = oneMinusArrow * oneMinusArrow * source.x + 2 * oneMinusArrow * arrowT * controlX + arrowT * arrowT * target.x;
        const arrowY = oneMinusArrow * oneMinusArrow * source.y + 2 * oneMinusArrow * arrowT * controlY + arrowT * arrowT * target.y;
        const tangentX = 2 * oneMinusArrow * (controlX - source.x) + 2 * arrowT * (target.x - controlX);
        const tangentY = 2 * oneMinusArrow * (controlY - source.y) + 2 * arrowT * (target.y - controlY);
        const angle = Math.atan2(tangentY, tangentX);
        context.beginPath();
        context.moveTo(arrowX, arrowY);
        context.lineTo(arrowX - 10 * Math.cos(angle - 0.45), arrowY - 10 * Math.sin(angle - 0.45));
        context.lineTo(arrowX - 10 * Math.cos(angle + 0.45), arrowY - 10 * Math.sin(angle + 0.45));
        context.closePath();
        context.fillStyle = active ? "rgba(190,255,233,.95)" : "rgba(123,183,165,.55)";
        context.fill();

      }

      if (!state.paused && active) {
        const phase = (state.time * 0.00022 + (hash(`${edge.source}:${edge.target}`) % 1000) / 1000) % 1;
        const oneMinus = 1 - phase;
        const pulseX = oneMinus * oneMinus * source.x + 2 * oneMinus * phase * controlX + phase * phase * target.x;
        const pulseY = oneMinus * oneMinus * source.y + 2 * oneMinus * phase * controlY + phase * phase * target.y;
        context.beginPath();
        context.arc(pulseX, pulseY, 2.1, 0, Math.PI * 2);
        context.fillStyle = "rgba(190,255,233,.95)";
        context.shadowBlur = 12;
        context.shadowColor = "#76ffd0";
        context.fill();
        context.shadowBlur = 0;
      }
    }
  }

  function nodeRadius(node) {
    if (node.kind === "system") return 12;
    if (node.kind === "runtime-root") return 10;
    if (node.kind.endsWith("cluster")) return 7.2;
    if (node.kind === "agent") return 7;
    if (node.kind === "agent-session") return 8.2;
    if (node.kind === "tool") return 7.2;
    if (["work", "job", "model"].includes(node.kind)) return 6.4;
    return 6.2;
  }

  function drawNodes() {
    const labelBoxes = [];
    const neighborhood = focusedNeighborhood();
    const orderedNodes = [...state.nodes].sort((left, right) => Number(state.activeNodeIds.has(right.node_id)) - Number(state.activeNodeIds.has(left.node_id)) || left.node_id.localeCompare(right.node_id));
    for (const node of orderedNodes) {
      if (!nodeVisible(node)) continue;
      const point = pointFor(node.node_id);
      if (!point) continue;
      const radius = nodeRadius(node);
      const color = palette[node.kind] || palette.document;
      const selected = state.selected?.node_id === node.node_id;
      const hovered = state.hovered?.node_id === node.node_id;
      const active = state.activeNodeIds.has(node.node_id);
      const related = neighborhood?.nodeIds.has(node.node_id) || false;
      const dimmed = Boolean(neighborhood) && !related && !active;
      const pulse = state.paused ? 0 : Math.sin(state.time * 0.003 + (hash(node.node_id) % 100)) * 0.8;

      context.save();
      context.globalAlpha = dimmed ? 0.22 : 1;

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
      context.shadowBlur = active || selected ? 20 : 11;
      context.shadowColor = color;
      context.fill();
      context.shadowBlur = 0;

      if (selected || hovered || active || node.kind === "system" || node.kind.endsWith("cluster") || (state.liveMode && state.liveNodeIds.has(node.node_id))) {
        const label = node.label.length > 24 ? `${node.label.slice(0, 23)}…` : node.label;
        context.font = `${selected ? 17 : active ? 17 : state.liveMode ? 14 : 12}px ${getComputedStyle(document.documentElement).getPropertyValue("--mono")}`;
        context.fillStyle = selected ? "rgba(235,255,248,.95)" : "rgba(188,218,208,.72)";
        context.textAlign = "center";
        const labelWidth = context.measureText(label).width + 12;
        const candidates = [
          { x: point.x, y: point.y + radius + 17 },
          { x: point.x, y: point.y - radius - 10 },
          { x: point.x + labelWidth / 2 + radius + 7, y: point.y + 4 },
          { x: point.x - labelWidth / 2 - radius - 7, y: point.y + 4 },
        ];
        let placedBox = null;
        const position = candidates.find((candidate) => {
          const box = { left: candidate.x - labelWidth / 2, right: candidate.x + labelWidth / 2, top: candidate.y - 13, bottom: candidate.y + 4 };
          const inside = box.left >= 8 && box.right <= state.width - 8 && box.top >= 8 && box.bottom <= state.height - 8;
          const clear = labelBoxes.every((other) => box.right < other.left || box.left > other.right || box.bottom < other.top || box.top > other.bottom);
          if (inside && clear) { placedBox = box; labelBoxes.push(box); return true; }
          return false;
        });
        if (position && placedBox) {
          context.fillStyle = "rgba(3,12,10,.82)";
          context.fillRect(placedBox.left - 3, placedBox.top - 2, labelWidth + 6, 20);
          context.fillStyle = selected ? "rgba(235,255,248,.95)" : "rgba(188,218,208,.82)";
          context.fillText(label, position.x, position.y);
        }
      }
      context.restore();
    }
  }

  function drawFrame(timestamp) {
    if (!state.paused) state.time = timestamp;
    context.clearRect(0, 0, state.width, state.height);
    drawBrainBase();
    context.save();
    context.clip(brainPath(state.width, state.height));
    drawClusterRegions();
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
  liveNetworkToggle.addEventListener("click", () => {
    state.liveMode = !state.liveMode;
    liveNetworkToggle.setAttribute("aria-pressed", String(state.liveMode));
    liveNetworkToggle.textContent = state.liveMode ? "TÜM AĞA DÖN" : "CANLI ODAK";
    document.body.classList.toggle("live-network-mode", state.liveMode);
    applySnapshot(state.snapshot);
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
