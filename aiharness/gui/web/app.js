/* Dr.Wang desktop frontend.
 *
 * One WebSocket, one dispatcher, no framework. The backend owns all state;
 * this file only renders what arrives and sends what the user clicks.
 *
 * Everything from the model or a tool is escaped before it reaches the DOM.
 * Tool output can contain arbitrary text read off disk or off the web, so
 * treating any of it as markup would be an injection hole in a window that
 * can run shell commands.
 */

"use strict";

const PROTOCOL_VERSION = 1;
/** Default / floor height of the composer textarea (px). */
const COMPOSER_MIN_HEIGHT_PX = 96;
/** Cap while auto-growing with typed content (px). */
const COMPOSER_MAX_HEIGHT_PX = 360;
/** How long to wait after typing before persisting the draft. */
const DRAFT_SAVE_MS = 400;
const RECONNECT_DELAY_MS = 1200;
const TOAST_MS = 4200;
const SCROLL_STICK_PX = 120;
/** Cap on images attached to one unsent prompt. */
const ATTACH_MAX_COUNT = 32;
/** Largest paste/drop accepted in the composer (bytes). */
const ATTACH_MAX_BYTES = 8 * 1024 * 1024;
const ATTACH_MIME = new Set(["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]);

/* Category colours for the context breakdown, matching the backend order. */
const SLICE_COLOURS = {
  "Messages":      "#4f8ef7",
  "System tools":  "#5aa9e6",
  "MCP tools":     "#8fbcbb",
  "System prompt": "#e8a05c",
  "Rules":         "#d08770",
  "Skills":        "#b48ead",
  "Free space":    "#3d342d",
};

/* Animated mascot: one looping GIF per agent state, swapped by setPet(). */
const PET_GIFS = {
  idle: "/static/pet/idle.gif", busy: "/static/pet/busy.gif",
  thinking: "/static/pet/thinking.gif", happy: "/static/pet/happy.gif",
  error: "/static/pet/error.gif", compact: "/static/pet/compact.gif",
};

/* ── tiny helpers ──────────────────────────────────────────────────── */

const $  = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

/* A deliberately small markdown subset. Escaping happens first, so no input
 * can introduce markup; only these patterns can. */
function renderMarkdown(source) {
  let html = escapeHtml(source);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, body) =>
    `<pre><code data-lang="${lang}">${body.replace(/\n$/, "")}</code></pre>`);
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/^### (.*)$/gm, "<h3>$1</h3>")
             .replace(/^## (.*)$/gm, "<h2>$1</h2>")
             .replace(/^# (.*)$/gm, "<h1>$1</h1>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  html = html.replace(/^\s*[-*] (.*)$/gm, "<li>$1</li>");
  html = html.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
  return html.split(/\n{2,}/)
             .map((block) => (/^\s*<(pre|ul|h[123])/.test(block) ? block : `<p>${block.replace(/\n/g, "<br>")}</p>`))
             .join("");
}

const compactTokens = (n) =>
  n >= 1_000_000 ? (n / 1_000_000).toFixed(1) + "M"
  : n >= 1_000   ? (n / 1_000).toFixed(1) + "k"
  : String(n ?? 0);

/* ── application state ─────────────────────────────────────────────── */

const state = {
  socket: null,
  connected: false,
  busy: false,
  status: null,
  config: null,
  uiMode: "agent", // "agent" | "codex" | "claude"
  codex: {
    state: "stopped",
    busy: false,
    home_kind: "kimi",
    selection: "kimi",
    home_path: "",
    model: "",
    model_provider: "",
    error: "",
    profiles: [],
    templates: [],
  },
  codexStreaming: null,
  codexBuffer: "",
  codexThinking: null,
  codexTools: new Map(),
  claude: {
    state: "stopped",
    busy: false,
    error: "",
    session_id: "",
    selection: "anthropic",
    profiles: [],
    templates: [],
    model: "",
  },
  claudeStreaming: null,
  claudeBuffer: "",
  claudeThinking: null,
  claudeTools: new Map(),
  codexAttachments: [],
  claudeAttachments: [],
  streaming: null,   // the assistant bubble currently receiving deltas
  thinking: null,
  buffer: "",
  streamFrame: 0,
  tools: new Map(),
  refs: [],
  fileTreePath: "",
  previewPath: "",
  pendingEdits: [],
  //: Pending-edit / todo strips default collapsed so the transcript stays visible.
  editReviewCollapsed: true,
  todoStripCollapsed: true,
  lastTodos: [],
  quest: null,
  pendingModal: null,
  setupShown: false,
  //: Workspace groups the user has folded away in the sidebar.
  collapsed: new Set(),
  //: Turns in the open conversation. Zero means it is still a blank page,
  //: which is the only moment the working directory may still be chosen.
  turns: 0,
  //: Caps accepted by the heartbeat dialog, echoed on the goal banner.
  armedLimits: "",
  //: Live "思考中 / 运行中" dock above the composer (does not scroll away).
  activity: null,
  //: Parallel activity rows keyed by source id (main / research:0 / …).
  activities: new Map(),
  activityHost: null,
  //: Session whose draft is currently loaded into #prompt.
  draftSessionId: "",
  draftTimer: null,
  //: Session the user intends to view (set on click before status arrives).
  //: Stream events from the previous chat must not paint during this gap.
  viewSessionId: "",
  //: Codex / Claude panel viewed session ids (optimistic, for stream filter).
  viewCodexSessionId: "",
  viewClaudeSessionId: "",
  codexReplayPending: false,
  claudeReplayPending: false,
  //: After open_session / new chat, the next transcript replay must land on
  //: the latest message (ignore prior scroll position of the previous chat).
  pinBottomOnReady: false,
  //: Ordered pending images for the next send (data URLs for thumbs).
  pendingImages: [],
  //: Prompts waiting while the open session is busy (editable guidance).
  promptQueue: [],
  //: Image annotate modal state.
  imageEditor: null,
  //: True while a capture_screen round-trip is in flight.
  capturingScreen: false,
  authToken: "",
  //: Paths touched this session (tool display), keyed by absolute path.
  resources: new Map(),
  //: @ paths from the latest user turn (context panel).
  turnRefs: [],
  //: Canvas editor buffer for the open path.
  canvasPath: "",
  canvasDirty: false,
  //: Defer tool regroup until the turn finishes (less flicker).
  pendingToolFold: false,
  //: True if the current turn streamed any visible assistant text.
  turnHadText: false,
  //: Prompt dropped while the local WebSocket was down; flushed on reconnect.
  pendingPrompt: null,
  //: True after the first successful socket open (so close is not a false alarm).
  everConnected: false,
  connBannerTimer: null,
};

const IMAGE_SUFFIX = /\.(png|jpe?g|webp|gif|bmp)$/i;

/* ── transport ─────────────────────────────────────────────────────── */

function connect() {
  const token = new URLSearchParams(location.search).get("token") || "";
  state.authToken = token;
  const socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
  state.socket = socket;

  socket.onopen = () => {
    const wasDown = state.everConnected && !state.connected;
    state.connected = true;
    state.everConnected = true;
    if (wasDown) {
      setConnBanner("ok", t("conn.restored"));
      toast(t("conn.restored"), "ok");
    } else {
      setConnBanner(null);
    }
    send("refresh", {});
    flushPendingPrompt();
  };
  socket.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); }
    catch { return; }
    handle(payload);
  };
  socket.onerror = () => {
    // onclose always follows; surface the failure before the timer reconnects.
    if (state.everConnected) setConnBanner("error", t("conn.lost"));
  };
  socket.onclose = () => {
    state.connected = false;
    if (state.everConnected) {
      setConnBanner("error", t("conn.retrying"));
      toast(state.busy ? t("conn.turnKept") : t("conn.lost"), "warn");
    }
    setTimeout(connect, RECONNECT_DELAY_MS);
  };
}

function send(type, payload) {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify({ type, ...payload }));
    return true;
  }
  return false;
}

function setConnBanner(kind, text) {
  const bar = $("conn-banner");
  if (!bar) return;
  if (state.connBannerTimer) {
    clearTimeout(state.connBannerTimer);
    state.connBannerTimer = null;
  }
  if (!kind) {
    bar.className = "conn-banner hidden";
    bar.textContent = "";
    return;
  }
  bar.className = `conn-banner ${kind}`;
  bar.textContent = text || "";
  if (kind === "ok") {
    state.connBannerTimer = setTimeout(() => setConnBanner(null), 2400);
  }
}

function flushPendingPrompt() {
  const pending = state.pendingPrompt;
  if (!pending) return;
  state.pendingPrompt = null;
  setActivity(t("activity.sending"), "pending");
  if (!send("prompt", pending)) {
    state.pendingPrompt = pending;
    setConnBanner("error", t("conn.retrying"));
  }
}

/* ── inbound dispatch ──────────────────────────────────────────────── */

/** Stream / HITL events must not paint onto a chat the user is not viewing. */
const SESSION_SCOPED = new Set([
  "text", "thinking", "turn_start", "turn_end", "done",
  "tool_start", "tool_end", "activity", "compacted", "notice",
  "ask", "permission", "plan", "todos", "quest", "edit_review",
]);

/** Codex / Claude stream events scoped by panel_session_id. */
const PANEL_SESSION_SCOPED = new Set([
  "codex_text", "codex_thinking", "codex_activity", "codex_tool_start",
  "codex_tool_end", "codex_notice", "codex_permission", "codex_done", "codex_error",
  "claude_text", "claude_thinking", "claude_activity", "claude_tool_start",
  "claude_tool_end", "claude_notice", "claude_permission", "claude_done", "claude_error",
]);

function forActiveSession(msg) {
  if (!msg || !msg.session_id || !SESSION_SCOPED.has(msg.type)) return true;
  // Prefer the optimistic view id so a late "k3 思考中" from the previous
  // chat cannot land after the user already clicked another session.
  const id = state.viewSessionId
    || (state.status && state.status.session_id)
    || "";
  return !id || msg.session_id === id;
}

function forActivePanelSession(msg) {
  if (!msg || !PANEL_SESSION_SCOPED.has(msg.type)) return true;
  const pid = msg.panel_session_id || "";
  if (String(msg.type).startsWith("codex_")) {
    const id = state.viewCodexSessionId
      || (state.codex && (state.codex.viewed_id || state.codex.panel_session_id))
      || "";
    // Untagged stream events are dropped once a viewed session exists —
    // otherwise an old singleton push could paint into the wrong chat.
    if (!id) return true;
    return !!pid && pid === id;
  }
  if (String(msg.type).startsWith("claude_")) {
    const id = state.viewClaudeSessionId
      || (state.claude && (state.claude.viewed_id || state.claude.panel_session_id))
      || "";
    if (!id) return true;
    return !!pid && pid === id;
  }
  return true;
}

function handle(msg) {
  // Background Ask/Plan/Permission: remember + toast, do not open a global modal.
  if (msg && msg.session_id && ["ask", "permission", "plan"].includes(msg.type)
      && !forActiveSession(msg)) {
    state.pendingHitl = state.pendingHitl || {};
    state.pendingHitl[msg.session_id] = msg;
    toast(t("toast.waitingHitl"), "warn");
    return;
  }
  if (!forActiveSession(msg)) return;
  if (!forActivePanelSession(msg)) return;
  const handler = HANDLERS[msg.type];
  if (handler) handler(msg);
}

const HANDLERS = {
  ready(msg) {
    if (msg.protocol && msg.protocol !== PROTOCOL_VERSION) {
      toast(t("toast.protoMismatch", { remote: msg.protocol, local: PROTOCOL_VERSION }), "error");
    }
    // Empty transcript on every reconnect used to wipe the open chat.
    // Only replay when the backend actually sent history (refresh / open).
    // Never clobber an in-flight stream — that made answers vanish until restart.
    // Session switches set pinBottomOnReady and abandon the previous stream view.
    if (!Array.isArray(msg.transcript)) return;
    const switchIn = state.pinBottomOnReady;
    if (!switchIn && !msg.transcript.length) return;
    if (!switchIn && (state.streaming || (state.busy && state.buffer))) {
      return;
    }
    state.pinBottomOnReady = false;
    replayTranscript(msg.transcript, msg.compactions || []);
    scrollToLatest();
  },

  text(msg) {
    if (msg.delta) state.turnHadText = true;
    setActivity(
      modelActivityLabel(
        t("activity.answering"),
        msg.model,
        msg.account,
      ),
      "streaming",
    );
    appendText(msg.delta);
  },
  thinking(msg) {
    setActivity(
      modelActivityLabel(
        t("activity.thinking"),
        msg.model,
        msg.account,
      ),
      "thinking",
    );
    appendThinking(msg.delta);
  },
  path_index(msg) { renderAtMenu(msg.paths || [], msg.query || ""); },
  file_tree(msg) { renderFileTree(msg.path || "", msg.nodes || []); },
  file_preview(msg) { renderFilePreview(msg); },
  rules_list(msg) { renderRulesEditor(msg.rules || []); },
  memories(msg) { renderMemories(msg.memories || []); },
  quest(msg) { renderQuest(msg.quest || null); },

  turn_start(msg) {
    finishStreaming();
    setPet("thinking");
    state.turns += 1;
    state.turnHadText = false;
    state.turnRefs = Array.isArray(msg.refs) ? msg.refs.slice() : [];
    renderTurnRefs(state.turnRefs);
    renderWorkspaceChips();
    const model = msg.model || (state.status && state.status.model) || "";
    const account = msg.account || (state.status && state.status.account) || "";
    setActivity(modelActivityLabel(t("activity.thinking"), model, account), "thinking");
  },

  turn_end() { /* usage lands in the status message */ },

  done(msg) {
    finishStreaming();
    // If deltas were dropped (refresh wipe / reconnect), still paint final text.
    ensureAssistantVisible(msg.text);
    foldPendingTools();
    clearActivity();
    const interrupted = !!msg.interrupted;
    const emptyText = !(msg.text || "").trim() && !state.turnHadText;
    setPet(interrupted ? "error" : emptyText ? "error" : "happy");
    if (interrupted) toast(t("toast.interrupted"), "warn");
    else if (emptyText) toast(t("toast.emptyTurn"), "warn");
    // Status may flip busy→idle a tick later; queue flush also watches applyStatus.
    setTimeout(flushPromptQueue, 0);
  },

  tool_start(msg) {
    finishStreaming();
    setPet("busy");
    setActivity(t("activity.running", { name: msg.headline || msg.name }), "busy");
    addToolEntry(msg);
  },
  tool_end(msg) {
    // Fold finished tools immediately so long Bash runs don't bury the answer.
    completeToolEntry(msg, false);
    if (msg.is_error && /timed out/i.test(msg.content || msg.summary || "")) {
      toast(t("toast.toolTimeout", { name: msg.name || "tool" }), "warn");
    }
    if (state.busy) setActivity(modelActivityLabel(t("activity.busy")), "busy");
  },

  activity(msg) {
    if (msg.text) {
      setActivity(formatActivity(msg.text), "busy", msg.source || activitySource(msg.text));
    }
  },

  notice(msg) {
    addNotice(msg.text, msg.level);
    const text = String(msg.text || "").trim();
    if (!text) return;
    // Provider / network failures must not hide in scrollback only.
    if (msg.level === "error") {
      toast(text, "error");
      setActivity(text.length > 80 ? `${text.slice(0, 77)}…` : text, "error");
      setPet("error");
      return;
    }
    // Busy rejection after optimistic Send/Continue — surface it and unstick UI.
    if (
      msg.level === "warn"
      && /仍在运行|Still working|実行中|请先打断|interrupt first/i.test(text)
    ) {
      toast(text, "warn");
      send("refresh", {});
      return;
    }
    if (
      msg.level === "warn"
      && /看图|图片|vision|image|retry|重试|网络|timeout|timed out|可见文字|no visible answer|見える回答/i.test(text)
    ) {
      toast(text, "warn");
    }
    if (
      msg.level === "info"
      && /继续未完成|Continuing unfinished|未完了タスクを続行|没有未完成|No open todos|未完了のタスクはありません/i.test(text)
    ) {
      toast(text, "ok");
    }
  },

  screenshot(msg) {
    state.capturingScreen = false;
    const btn = $("screenshot-button");
    if (btn) btn.disabled = false;
    const codexBtn = $("codex-screenshot");
    if (codexBtn) codexBtn.disabled = false;
    const claudeBtn = $("claude-screenshot");
    if (claudeBtn) claudeBtn.disabled = false;
    if (msg.cancelled) {
      toast(t("composer.screenshotCancel"), "ok");
      return;
    }
    if (!msg.data) {
      toast(t("composer.screenshotBusy"), "warn");
      return;
    }
    const bucket = panelAttachBucket();
    if (bucket.length >= ATTACH_MAX_COUNT) {
      toast(t("composer.screenshotFull"), "warn");
      return;
    }
    const mime = (msg.mime || "image/png").toLowerCase();
    const data = String(msg.data || "");
    const dataUrl = data.startsWith("data:")
      ? data
      : `data:${mime};base64,${data}`;
    const base64 = dataUrl.includes(",") ? dataUrl.split(",")[1] : data;
    const item = {
      mime: mime === "image/jpg" ? "image/jpeg" : mime,
      name: msg.name || "screenshot.png",
      data: base64,
      dataUrl,
      data_url: dataUrl,
    };
    bucket.push(item);
    if (state.uiMode === "codex") renderPanelAttachStrip("codex");
    else if (state.uiMode === "claude") renderPanelAttachStrip("claude");
    else {
      renderAttachStrip();
      if (msg.open_editor !== false) {
        openImageEditor(state.pendingImages.length - 1);
      }
    }
    toast(t("composer.screenshotOk"), "ok");
  },

  learn_result(msg) {
    const box = $("learn-results");
    if (!box) return;
    const lines = [msg.text || ""];
    for (const item of msg.candidates || []) {
      lines.push(`· ${item.name}（${item.occurrences || 0} 次）— ${item.description || ""}`);
    }
    box.textContent = lines.filter(Boolean).join("\n");
  },

  market_result(msg) {
    const out = $("market-out");
    if (out) out.textContent = msg.text || "";
    if (msg.equity && msg.equity.length) {
      renderEquityChart(msg.equity, msg.symbol || "");
    } else {
      renderMarketChart(msg.ok ? (msg.bars || []) : [], msg.symbol || "");
    }
  },
  market_alert(msg) {
    if (msg.text) toast(msg.text, "warn");
    const out = $("market-out");
    if (out && msg.text) out.textContent = `${msg.text}\n${out.textContent || ""}`;
  },
  search_hits(msg) { renderSearchHits(msg.hits || [], msg.query || ""); },
  onboarding(msg) { renderOnboarding(msg); },

  canvas_hint(msg) {
    if (!msg.path) return;
    const input = $("canvas-path");
    if (input) input.value = msg.path;
    // Soft notify — do not yank focus away from the chat mid-turn.
    toast(t("toast.canvasHint", { path: shortPathLabel(msg.path) }), "ok");
  },

  compacted(msg) { addCompaction(msg); setPet("compact"); },

  status(msg) {
    applyStatus(msg.status);
    if (msg.status && Array.isArray(msg.status.turn_refs)) {
      state.turnRefs = msg.status.turn_refs.slice();
      renderTurnRefs(state.turnRefs);
    }
  },
  context(msg) { renderContext(msg); },
  sessions(msg) {
    // Agent session list must not overwrite Codex/Claude panel sidebars.
    if (state.uiMode === "codex" || state.uiMode === "claude") return;
    renderSessions(msg);
  },
  config(msg) {
    if (msg.catalogue) { renderCatalogue(msg.catalogue); return; }
    if (msg.account_result) showAccountResult(msg.account_result);
    state.config = msg.config;
    renderConfig(msg.config);
  },
  todos(msg) {
    // Empty list clears the strip when switching to a chat with no todos.
    renderTodos(msg.todos || []);
  },
  edit_review(msg) {
    const next = msg.pending || [];
    // First pending item of a batch: expand so "待审改动 - 1" is not an empty bar.
    if (next.length && !(state.pendingEdits && state.pendingEdits.length)) {
      state.editReviewCollapsed = false;
    }
    state.pendingEdits = next;
    renderEditReview(state.pendingEdits);
    renderEditDiffPanel(state.pendingEdits);
  },

  permission(msg) { showPermissionInline(msg); },
  ask(msg) { showQuestionsInline(msg); },
  plan(msg) { showPlanInline(msg); },

  codex_status(msg) { applyCodexStatus(msg); },
  codex_text(msg) { appendCodexText(msg.delta || ""); },
  codex_thinking(msg) { appendPanelThinking("codex", msg.delta || ""); },
  codex_activity(msg) { setPanelActivity("codex", msg.text || "", msg.kind || "busy"); },
  codex_tool_start(msg) {
    setPanelActivity("codex", msg.headline || msg.name || "tool", "busy");
    addPanelToolEntry("codex", msg);
  },
  codex_tool_end(msg) { completePanelToolEntry("codex", msg); },
  codex_notice(msg) {
    if (msg.text) {
      addCodexEntry("notice", msg.text);
      toast(msg.text, msg.level === "error" ? "error" : (msg.level || "ok"));
    }
  },
  codex_permission(msg) { showCodexPermissionInline(msg); },
  codex_done(msg) {
    finishCodexStream();
    clearPanelActivity("codex");
    state.codex.busy = false;
    applyCodexStatus({ ...state.codex, busy: false });
  },
  codex_error(msg) {
    finishCodexStream();
    clearPanelActivity("codex");
    if (msg.message) {
      addCodexEntry("notice", msg.message);
      toast(msg.message, "error");
    }
  },

  claude_status(msg) { applyClaudeStatus(msg); },
  claude_text(msg) { appendClaudeText(msg.delta || ""); },
  claude_thinking(msg) { appendPanelThinking("claude", msg.delta || ""); },
  claude_activity(msg) { setPanelActivity("claude", msg.text || "", msg.kind || "busy"); },
  claude_tool_start(msg) {
    setPanelActivity("claude", msg.headline || msg.name || "tool", "busy");
    addPanelToolEntry("claude", msg);
  },
  claude_tool_end(msg) { completePanelToolEntry("claude", msg); },
  claude_notice(msg) {
    if (msg.text) {
      addClaudeEntry("notice", msg.text);
      toast(msg.text, msg.level === "error" ? "error" : (msg.level || "ok"));
    }
  },
  claude_permission(msg) { showClaudePermissionInline(msg); },
  claude_done(msg) {
    finishClaudeStream();
    clearPanelActivity("claude");
    state.claude.busy = false;
    applyClaudeStatus({ ...state.claude, busy: false });
  },
  claude_error(msg) {
    finishClaudeStream();
    clearPanelActivity("claude");
    if (msg.message) {
      addClaudeEntry("notice", msg.message);
      toast(msg.message, "error");
    }
  },

  heartbeat(msg) {
    $("heartbeat-badge").classList.toggle("hidden", !msg.active);
    if (msg.active && msg.iterations) {
      $("heartbeat-badge").textContent = `♥ ${msg.iterations}`;
    } else if (msg.active) {
      $("heartbeat-badge").textContent = "♥ 心跳";
    }
    if (msg.limits) state.armedLimits = msg.limits;
    setGoalMode(!!msg.armed);
    if (!msg.active && (msg.reason_zh || msg.reason)) {
      toast(t("toast.heartbeatStop", {
        reason: msg.reason_zh || msg.reason || "",
        n: msg.iterations || 0,
        cost: (msg.spent || 0).toFixed(4),
      }));
    }
  },

  workspace(msg) { renderWorkspace(msg); },
  skills(msg) { renderSkills(msg); },

  error(msg) { toast(msg.message, "error"); },
};

/* ── workspace ─────────────────────────────────────────────────────── */

function renderWorkspace(msg) {
  state.workspace = msg;
  renderWorkspaceChips();
  renderPanelWorkspaceChips("codex");
  renderPanelWorkspaceChips("claude");
}

function shortWorkspaceLabel(path) {
  if (!path) return "—";
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return path;
  return parts.slice(-2).join("\\");
}

/* Codex / Claude: workspace chips live above the composer (same place as Agent). */
function renderPanelWorkspaceChips(kind) {
  const row = $(`${kind}-workspace-chips`);
  if (!row) return;
  const status = kind === "codex" ? state.codex : state.claude;
  const current = (status && status.workspace) || "";
  const groups = (status && status.sessions && status.sessions.workspaces) || [];
  const recents = (state.workspace && state.workspace.recents) || [];
  const paths = [current, ...groups.map((g) => g.path), ...recents].filter(Boolean);
  row.innerHTML = "";
  row.classList.remove("hidden");
  row.appendChild(el("span", "chips-label", t("panel.workspace") || "工作目录"));
  const labels = disambiguate(paths);
  const seen = new Set();
  for (const path of paths) {
    if (seen.has(path)) continue;
    seen.add(path);
    const chip = el(
      "button",
      `chip${path === current ? " chip-active" : ""}`,
      labels.get(path) || shortWorkspaceLabel(path),
    );
    chip.title = path;
    chip.onclick = () => send(`${kind}_set_workspace`, { path });
    row.appendChild(chip);
  }
  const add = el("button", "chip chip-add", "＋");
  add.title = t("panel.workspacePick") || "选择…";
  add.onclick = () => chooseWorkspace(kind);
  row.appendChild(add);
}

function syncPanelModeSelect(kind) {
  const select = $(`${kind}-mode-select`);
  if (!select) return;
  const status = kind === "codex" ? state.codex : state.claude;
  const mode = (status && status.permission_mode) || "ask";
  if (["ask", "auto", "yolo"].includes(mode)) select.value = mode;
}

/* The chip row above the composer, shown only while the conversation is
 * still empty. Claude Code puts the directory choice here rather than in a
 * global switcher because a conversation belongs to one tree for its whole
 * life — once there are messages, changing it would silently move the
 * ground under everything already said. */
function renderWorkspaceChips() {
  const row = $("workspace-chips");
  const info = state.workspace;
  const status = state.status;
  const fresh = !status || !status.session_title || state.turns === 0;
  row.innerHTML = "";
  if (!info || !fresh) { row.classList.add("hidden"); return; }
  row.classList.remove("hidden");

  row.appendChild(el("span", "chips-label", "工作目录"));
  const paths = [info.path, ...(info.recents || [])].filter(Boolean);
  const labels = disambiguate(paths);
  const seen = new Set();
  for (const path of paths) {
    if (seen.has(path)) continue;
    seen.add(path);
    const chip = el("button", `chip${path === info.path ? " chip-active" : ""}`,
                    labels.get(path) || path);
    chip.title = path;
    chip.onclick = () => send("new_session", { path });
    row.appendChild(chip);
  }
  const add = el("button", "chip chip-add", "＋");
  add.title = "选择别的文件夹";
  add.onclick = chooseWorkspace;
  row.appendChild(add);
}

/* Two projects can share a folder name — "another-project" under two
 * different parents is still two different trees, and picking the wrong one
 * points the agent at the wrong code. So a bare name is used only when it is
 * unique; otherwise just enough parent segments are added to tell them apart. */
function disambiguate(paths) {
  const split = (p) => p.split(/[\\/]/).filter(Boolean);
  const segments = new Map(paths.map((p) => [p, split(p)]));
  const tail = (p, depth) => segments.get(p).slice(-depth).join("\\");

  const labels = new Map();
  for (const path of paths) {
    const parts = segments.get(path);
    if (!parts.length) { labels.set(path, path); continue; }
    let depth = 1;
    while (depth < parts.length) {
      const mine = tail(path, depth);
      const clashes = paths.some((other) => other !== path && tail(other, depth) === mine);
      if (!clashes) break;
      depth += 1;
    }
    labels.set(path, tail(path, depth));
  }
  return labels;
}

/* The native folder dialog only exists when a desktop window is hosting the
 * page. In browser mode there is nothing to open, so offer a typed path and
 * a list of recents instead. */
function chooseWorkspace(target) {
  // onclick handlers pass a MouseEvent as the first arg — only accept explicit mode strings.
  const mode = (target === "codex" || target === "claude" || target === "agent")
    ? target
    : (state.uiMode === "codex" || state.uiMode === "claude" ? state.uiMode : "agent");
  const setCmd = mode === "codex"
    ? "codex_set_workspace"
    : mode === "claude"
      ? "claude_set_workspace"
      : "set_workspace";
  const current = mode === "codex"
    ? (state.codex && state.codex.workspace)
    : mode === "claude"
      ? (state.claude && state.claude.workspace)
      : (state.workspace && state.workspace.path);
  const recents = (state.workspace && state.workspace.recents) || [];
  openModal("选择工作目录", (body) => {
    body.appendChild(el("div", "hint",
      "工作目录决定 agent 能碰哪些文件、加载哪些 skill、列出哪些会话。"));

    const browse = el("button", "primary-button", "浏览文件夹…");
    browse.style.cssText = "width:100%;margin:12px 0;";
    browse.onclick = () => {
      closeModal();
      send("pick_workspace", { target: mode });
    };
    body.appendChild(browse);

    if (recents.length) {
      body.appendChild(el("div", "section-label", "最近使用"));
      for (const path of recents) {
        const item = el("div", "catalogue-item", path);
        item.onclick = () => { closeModal(); send(setCmd, { path }); };
        body.appendChild(item);
      }
    }

    body.appendChild(el("div", "section-label", "或直接填路径"));
    const input = el("input");
    input.placeholder = "C:\\path\\to\\project";
    input.value = current || "";
    input.style.cssText =
      "width:100%;background:var(--panel);color:var(--text);border:1px solid " +
      "var(--line);border-radius:6px;padding:10px;outline:none;";
    input.onkeydown = (event) => {
      if (event.key === "Enter" && input.value.trim()) {
        closeModal();
        send(setCmd, { path: input.value.trim() });
      }
    };
    body.appendChild(input);
    state.workspaceInput = input;
    state.workspaceSetCmd = setCmd;
  }, [
    { label: "取消", run: () => {} },
    { label: "使用这个路径", primary: true, run: () => {
        const value = state.workspaceInput && state.workspaceInput.value.trim();
        const cmd = state.workspaceSetCmd || "set_workspace";
        if (value) send(cmd, { path: value });
      } },
  ]);
}

/* ── skills ────────────────────────────────────────────────────────── */

function renderSkills(msg) {
  const list = $("skill-list");
  list.innerHTML = "";
  if (!(msg.skills || []).length) {
    list.appendChild(el("div", "hint",
      "还没有 skill。把含 SKILL.md 的文件夹放进下面任一目录即可。"));
  }
  for (const skill of msg.skills || []) {
    const row = el("div", "skill-row");
    const head = el("div", "skill-name", skill.name);
    head.appendChild(el("span", "skill-source", skill.source));
    row.append(head, el("div", "skill-desc", skill.description));
    row.title = skill.path;
    list.appendChild(row);
  }

  $("skill-roots").innerHTML = "";
  for (const root of msg.roots || []) {
    $("skill-roots").appendChild(el("div", "skill-root", root));
  }

  const paths = $("skill-paths");
  paths.innerHTML = "";
  for (const path of msg.paths || []) {
    const row = el("div", "row");
    row.appendChild(el("div", "row-main", path));
    const drop = el("button", "ghost-button", "移除");
    drop.onclick = () => send("remove_skill_path", { path });
    row.appendChild(drop);
    paths.appendChild(row);
  }

  $("skill-errors").textContent = (msg.errors || []).length
    ? "跳过：" + msg.errors.join("；") : "";
}

/* ── transcript ────────────────────────────────────────────────────── */

const transcript = () => $("transcript");

function atBottom() {
  const node = transcript();
  return node.scrollHeight - node.scrollTop - node.clientHeight < SCROLL_STICK_PX;
}

function scrollToLatest() {
  const node = transcript();
  if (!node) return;
  node.scrollTop = node.scrollHeight;
  // Images / markdown can grow layout after paint — stick again next frames.
  requestAnimationFrame(() => {
    node.scrollTop = node.scrollHeight;
    requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
  });
}

function mount(node, options) {
  const stick = options && options.stick === false ? false : atBottom();
  transcript().appendChild(node);
  if (stick) transcript().scrollTop = transcript().scrollHeight;
  return node;
}

/** Drop in-view stream handles when leaving a chat (DOM is about to rebuild). */
function abandonLiveStreamView() {
  if (state.streamFrame) {
    cancelAnimationFrame(state.streamFrame);
    state.streamFrame = 0;
  }
  state.streaming = null;
  state.buffer = "";
  state.thinking = null;
  state.turnHadText = false;
}

function authUrl(path) {
  if (!path) return "";
  if (path.startsWith("data:") || path.startsWith("blob:")) return path;
  const join = path.includes("?") ? "&" : "?";
  return `${path}${join}token=${encodeURIComponent(state.authToken || "")}`;
}

function addUser(text, images, userIndex) {
  const node = el("div", "entry user");
  if (userIndex != null) node.dataset.userIndex = String(userIndex);
  const gallery = el("div", "user-images");
  for (const image of images || []) {
    const img = document.createElement("img");
    const src = authUrl(image.url || image.dataUrl || "");
    img.src = src;
    img.alt = image.name || "image";
    img.title = image.name || t("viewer.openHint");
    img.onclick = (event) => {
      event.stopPropagation();
      openImageViewer(src, image.name || "");
    };
    gallery.appendChild(img);
  }
  node.appendChild(gallery);
  if (text) node.appendChild(el("div", "user-text", text));
  attachBubbleActions(node, text, userIndex);
  mount(node);
}

function openImageViewer(src, title) {
  const root = $("image-viewer");
  const img = $("image-viewer-img");
  const label = $("image-viewer-title");
  if (!root || !img) return;
  img.src = src || "";
  img.alt = title || "";
  if (label) label.textContent = title || t("viewer.title");
  root.classList.remove("hidden");
}

function closeImageViewer() {
  const root = $("image-viewer");
  const img = $("image-viewer-img");
  if (root) root.classList.add("hidden");
  if (img) img.removeAttribute("src");
}

async function copyImageFromUrl(src) {
  if (!src) {
    toast(t("copyFail"), "warn");
    return;
  }
  try {
    const response = await fetch(src);
    const blob = await response.blob();
    const type = blob.type && blob.type.startsWith("image/")
      ? blob.type
      : "image/png";
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([
        new ClipboardItem({ [type]: blob }),
      ]);
      toast(t("viewer.copied"), "ok");
      return;
    }
    // Fallback: open data URL is useless in WebView; copy as text link.
    await navigator.clipboard.writeText(src);
    toast(t("viewer.copiedLink"), "ok");
  } catch {
    toast(t("copyFail"), "warn");
  }
}

function wireImageViewer() {
  const root = $("image-viewer");
  if (!root || root.dataset.wired) return;
  root.dataset.wired = "1";
  const closeBtn = $("image-viewer-close");
  const copyBtn = $("image-viewer-copy");
  const stage = $("image-viewer-stage");
  if (closeBtn) closeBtn.onclick = () => closeImageViewer();
  if (copyBtn) {
    copyBtn.onclick = () => {
      const img = $("image-viewer-img");
      copyImageFromUrl(img && img.src);
    };
  }
  if (stage) {
    stage.onclick = (event) => {
      if (event.target === stage) closeImageViewer();
    };
  }
  root.addEventListener("click", (event) => {
    if (event.target === root) closeImageViewer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && root && !root.classList.contains("hidden")) {
      closeImageViewer();
    }
  });
}

function attachBubbleActions(node, text, userIndex) {
  if (!node) return;
  const bar = el("div", "bubble-actions");
  const raw = String(text || "").trim();
  if (raw) {
    const pin = el("button", "ghost-button pin-memory", t("pin.memory"));
    pin.type = "button";
    pin.title = t("pin.title");
    pin.onclick = (event) => {
      event.stopPropagation();
      const selected = (window.getSelection && String(window.getSelection())) || "";
      const body = (selected.trim() || raw).slice(0, 2000);
      send("add_memory", { text: body, pinned: true });
      toast(t("pin.ok"), "ok");
    };
    bar.appendChild(pin);
  }
  if (userIndex != null) {
    const rewind = el("button", "ghost-button rewind-turn", t("rewind.turn"));
    rewind.type = "button";
    rewind.title = t("rewind.title");
    rewind.onclick = (event) => {
      event.stopPropagation();
      if (state.busy) {
        toast(t("toast.busy"), "warn");
        return;
      }
      askConfirm(
        t("rewind.confirmTitle"),
        t("rewind.confirmBody"),
        () => send("rewind_turn", { user_index: userIndex }),
      );
    };
    bar.appendChild(rewind);
  }
  if (bar.childNodes.length) node.appendChild(bar);
}

function renderAttachStrip() {
  const strip = $("attach-strip");
  if (!strip) return;
  strip.innerHTML = "";
  strip.classList.toggle("hidden", state.pendingImages.length === 0);
  state.pendingImages.forEach((item, index) => {
    const chip = el("div", "attach-chip");
    chip.title = t("attach.openHint");
    const img = document.createElement("img");
    img.src = item.dataUrl;
    img.alt = item.name || `图${index + 1}`;
    chip.appendChild(img);
    chip.appendChild(el("span", "attach-index", `图${index + 1}`));
    chip.onclick = (event) => {
      if (event.target.closest(".attach-remove")) return;
      openImageEditor(index);
    };
    const remove = el("button", "attach-remove", "×");
    remove.type = "button";
    remove.title = "移除";
    remove.onclick = (event) => {
      event.stopPropagation();
      state.pendingImages.splice(index, 1);
      renderAttachStrip();
    };
    chip.appendChild(remove);
    strip.appendChild(chip);
  });
}

function clearPendingImages() {
  state.pendingImages = [];
  renderAttachStrip();
}

const IMAGE_EDITOR_COLORS = [
  "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#3498db", "#9b59b6", "#ffffff", "#111111",
];
const IMAGE_EDITOR_FONT = '"Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif';
const IMAGE_EDITOR_FONT_SCALE = { sm: 0.022, md: 0.032, lg: 0.048, xl: 0.07 };

function openImageEditor(index) {
  const item = state.pendingImages[index];
  const root = $("image-editor");
  const canvas = $("image-editor-canvas");
  if (!item || !root || !canvas) return;
  cancelImageTextInput();
  const ctx = canvas.getContext("2d");
  const img = new Image();
  img.onload = () => {
    canvas.width = img.naturalWidth || img.width;
    canvas.height = img.naturalHeight || img.height;
    ctx.drawImage(img, 0, 0);
    const sizeSelect = $("image-editor-font-size");
    state.imageEditor = {
      index,
      tool: "rect",
      color: IMAGE_EDITOR_COLORS[0],
      fontSize: (sizeSelect && sizeSelect.value) || "md",
      drawing: false,
      startX: 0,
      startY: 0,
      snapshot: null,
      undo: [ctx.getImageData(0, 0, canvas.width, canvas.height)],
      text: null,
    };
    const colors = $("image-editor-colors");
    if (colors) {
      colors.innerHTML = "";
      IMAGE_EDITOR_COLORS.forEach((hex) => {
        const swatch = document.createElement("button");
        swatch.type = "button";
        swatch.style.background = hex;
        swatch.title = hex;
        swatch.className = hex === state.imageEditor.color ? "active" : "";
        swatch.onclick = () => {
          state.imageEditor.color = hex;
          colors.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
          swatch.classList.add("active");
          syncImageTextInputStyle();
        };
        colors.appendChild(swatch);
      });
    }
    root.querySelectorAll(".img-tool").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tool === state.imageEditor.tool);
      btn.onclick = () => {
        if (state.imageEditor.tool === "text" && btn.dataset.tool !== "text") {
          commitImageTextInput();
        }
        state.imageEditor.tool = btn.dataset.tool;
        root.querySelectorAll(".img-tool").forEach((b) => {
          b.classList.toggle("active", b.dataset.tool === state.imageEditor.tool);
        });
      };
    });
    root.classList.remove("hidden");
  };
  img.src = item.dataUrl;
}

function closeImageEditor() {
  cancelImageTextInput();
  const root = $("image-editor");
  if (root) root.classList.add("hidden");
  state.imageEditor = null;
}

function canvasEventPos(canvas, event) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (event.clientX - rect.left) * scaleX,
    y: (event.clientY - rect.top) * scaleY,
    viewX: event.clientX - rect.left,
    viewY: event.clientY - rect.top,
  };
}

function imageEditorFontPx(ed, canvas) {
  const scale = IMAGE_EDITOR_FONT_SCALE[ed.fontSize] || IMAGE_EDITOR_FONT_SCALE.md;
  return Math.max(14, Math.round(canvas.width * scale));
}

function cancelImageTextInput() {
  const input = $("image-editor-text");
  if (input) {
    input.classList.add("hidden");
    input.value = "";
  }
  if (state.imageEditor) state.imageEditor.text = null;
}

function syncImageTextInputStyle() {
  const ed = state.imageEditor;
  const input = $("image-editor-text");
  const canvas = $("image-editor-canvas");
  if (!ed || !input || !canvas || !ed.text) return;
  const viewScale = canvas.getBoundingClientRect().width / canvas.width;
  const fontPx = imageEditorFontPx(ed, canvas) * viewScale;
  input.style.color = ed.color;
  input.style.fontSize = `${Math.max(12, fontPx)}px`;
  input.style.caretColor = ed.color;
}

function beginImageTextInput(pos) {
  const ed = state.imageEditor;
  const canvas = $("image-editor-canvas");
  const wrap = canvas && canvas.parentElement;
  const input = $("image-editor-text");
  if (!ed || !canvas || !wrap || !input) return;
  commitImageTextInput();
  const canvasRect = canvas.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  ed.text = { x: pos.x, y: pos.y };
  input.classList.remove("hidden");
  input.value = "";
  input.style.left = `${canvasRect.left - wrapRect.left + wrap.scrollLeft + pos.viewX}px`;
  input.style.top = `${canvasRect.top - wrapRect.top + wrap.scrollTop + pos.viewY}px`;
  syncImageTextInputStyle();
  requestAnimationFrame(() => {
    input.focus();
    input.select();
  });
}

function commitImageTextInput() {
  const ed = state.imageEditor;
  const canvas = $("image-editor-canvas");
  const input = $("image-editor-text");
  if (!ed || !canvas || !input || !ed.text) {
    cancelImageTextInput();
    return;
  }
  const text = input.value.replace(/\s+$/g, "");
  const anchor = ed.text;
  cancelImageTextInput();
  if (!text) return;
  const ctx = canvas.getContext("2d");
  pushImageUndo();
  const fontPx = imageEditorFontPx(ed, canvas);
  ctx.save();
  ctx.font = `600 ${fontPx}px ${IMAGE_EDITOR_FONT}`;
  ctx.textBaseline = "top";
  ctx.lineJoin = "round";
  ctx.miterLimit = 2;
  // Dark outline keeps light or saturated text readable on busy screenshots.
  ctx.lineWidth = Math.max(2, Math.round(fontPx / 8));
  ctx.strokeStyle = "rgba(0,0,0,0.75)";
  ctx.fillStyle = ed.color;
  const lines = text.split("\n");
  const lineHeight = Math.round(fontPx * 1.25);
  lines.forEach((line, index) => {
    const y = anchor.y + index * lineHeight;
    ctx.strokeText(line, anchor.x, y);
    ctx.fillText(line, anchor.x, y);
  });
  ctx.restore();
}

function pushImageUndo() {
  const canvas = $("image-editor-canvas");
  const ed = state.imageEditor;
  if (!canvas || !ed) return;
  const ctx = canvas.getContext("2d");
  ed.undo.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
  if (ed.undo.length > 30) ed.undo.shift();
}

function wireImageEditor() {
  const root = $("image-editor");
  const canvas = $("image-editor-canvas");
  if (!root || !canvas || root.dataset.wired) return;
  root.dataset.wired = "1";
  const ctx = canvas.getContext("2d");
  const textInput = $("image-editor-text");
  const sizeSelect = $("image-editor-font-size");

  if (sizeSelect) {
    sizeSelect.onchange = () => {
      if (!state.imageEditor) return;
      state.imageEditor.fontSize = sizeSelect.value || "md";
      syncImageTextInputStyle();
    };
  }
  if (textInput) {
    textInput.onkeydown = (event) => {
      event.stopPropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        cancelImageTextInput();
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        commitImageTextInput();
      }
    };
    textInput.onblur = () => {
      // Delay so a toolbar click (color / size) can update style first.
      setTimeout(() => {
        if (document.activeElement === textInput) return;
        if (state.imageEditor && state.imageEditor.text) commitImageTextInput();
      }, 120);
    };
  }

  canvas.onmousedown = (event) => {
    const ed = state.imageEditor;
    if (!ed || ed.tool === "pan") return;
    const pos = canvasEventPos(canvas, event);
    if (ed.tool === "text") {
      event.preventDefault();
      beginImageTextInput(pos);
      return;
    }
    ed.drawing = true;
    ed.startX = pos.x;
    ed.startY = pos.y;
    ed.snapshot = ctx.getImageData(0, 0, canvas.width, canvas.height);
    if (ed.tool === "pen") {
      pushImageUndo();
      ctx.strokeStyle = ed.color;
      ctx.lineWidth = Math.max(2, Math.round(canvas.width / 400));
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(pos.x, pos.y);
    }
  };
  canvas.onmousemove = (event) => {
    const ed = state.imageEditor;
    if (!ed || !ed.drawing) return;
    const pos = canvasEventPos(canvas, event);
    if (ed.tool === "pen") {
      ctx.lineTo(pos.x, pos.y);
      ctx.stroke();
      return;
    }
    if (!ed.snapshot) return;
    ctx.putImageData(ed.snapshot, 0, 0);
    ctx.strokeStyle = ed.color;
    ctx.lineWidth = Math.max(2, Math.round(canvas.width / 350));
    const w = pos.x - ed.startX;
    const h = pos.y - ed.startY;
    if (ed.tool === "rect") {
      ctx.strokeRect(ed.startX, ed.startY, w, h);
    } else if (ed.tool === "ellipse") {
      ctx.beginPath();
      ctx.ellipse(
        ed.startX + w / 2,
        ed.startY + h / 2,
        Math.abs(w / 2),
        Math.abs(h / 2),
        0, 0, Math.PI * 2,
      );
      ctx.stroke();
    }
  };
  const endStroke = (event) => {
    const ed = state.imageEditor;
    if (!ed || !ed.drawing) return;
    if (ed.tool !== "pen" && ed.snapshot) {
      // Commit final shape onto a fresh undo snapshot of the pre-drag frame.
      const committed = ed.snapshot;
      ed.undo.push(committed);
      if (ed.undo.length > 30) ed.undo.shift();
      canvas.onmousemove(event);
    }
    ed.drawing = false;
    ed.snapshot = null;
  };
  canvas.onmouseup = endStroke;
  canvas.onmouseleave = endStroke;

  $("img-undo").onclick = () => {
    cancelImageTextInput();
    const ed = state.imageEditor;
    if (!ed || ed.undo.length < 2) return;
    ed.undo.pop();
    ctx.putImageData(ed.undo[ed.undo.length - 1], 0, 0);
  };
  $("img-close").onclick = closeImageEditor;
  $("img-copy").onclick = async () => {
    commitImageTextInput();
    try {
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("blob");
      if (navigator.clipboard && window.ClipboardItem) {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        toast(t("copied"), "ok");
      } else {
        throw new Error("clipboard");
      }
    } catch {
      toast(t("attach.copyFail"), "warn");
    }
  };
  $("img-apply").onclick = () => {
    commitImageTextInput();
    const ed = state.imageEditor;
    if (!ed) return;
    const dataUrl = canvas.toDataURL("image/png");
    const base64 = dataUrl.includes(",") ? dataUrl.split(",")[1] : dataUrl;
    const item = state.pendingImages[ed.index];
    if (!item) return;
    item.mime = "image/png";
    item.data = base64;
    item.dataUrl = dataUrl;
    if (item.name && !/\.png$/i.test(item.name)) {
      item.name = item.name.replace(/\.[^.]+$/, "") + ".png";
    }
    renderAttachStrip();
    closeImageEditor();
    toast(t("attach.applied"), "ok");
  };
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (state.imageEditor && state.imageEditor.text) {
        cancelImageTextInput();
        return;
      }
      closeImageEditor();
    }
  });
}

function fileToPending(file) {
  return new Promise((resolve, reject) => {
    const mime = (file.type || "").toLowerCase();
    if (!ATTACH_MIME.has(mime)) {
      reject(new Error(`不支持的图片类型：${mime || file.name}`));
      return;
    }
    if (file.size > ATTACH_MAX_BYTES) {
      reject(new Error("单张图片不能超过 8 MB"));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const base64 = dataUrl.includes(",") ? dataUrl.split(",")[1] : dataUrl;
      resolve({
        mime: mime === "image/jpg" ? "image/jpeg" : mime,
        name: file.name || "paste.png",
        data: base64,
        dataUrl,
      });
    };
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}

async function enqueueImageFiles(files) {
  const list = Array.from(files || []).filter((file) => ATTACH_MIME.has((file.type || "").toLowerCase()));
  if (!list.length) return;
  if (state.pendingImages.length + list.length > ATTACH_MAX_COUNT) {
    toast(`一次最多附带 ${ATTACH_MAX_COUNT} 张图片`, "warn");
    return;
  }
  try {
    for (const file of list) {
      state.pendingImages.push(await fileToPending(file));
    }
    renderAttachStrip();
    const spec = $("model-select") && $("model-select").value;
    if (spec && !modelSupportsVision(spec)) {
      toast(t("composer.visionAttachWarn"), "warn");
    }
  } catch (error) {
    toast(error.message || String(error), "warn");
  }
}

function modelActivityLabel(verb, model, account) {
  const id = model || (state.status && state.status.model) || "";
  const acc = account || (state.status && state.status.account) || "";
  const who = id ? (acc ? `${id}@${acc}` : id) : "对话模型";
  return `${who} ${verb}…`;
}

/* Turn terse English progress from tools into a readable status line. */
function activitySource(raw) {
  const text = String(raw || "");
  const research = text.match(/^researching with/i);
  if (research) return "research";
  const verify = text.match(/^verifying with/i);
  if (verify) return "verify";
  const delegate = text.match(/^delegating to/i);
  if (delegate) return `delegate:${text.slice(0, 40)}`;
  const sub = text.match(/^subagent '([^']+)'/i);
  if (sub) return `sub:${sub[1]}`;
  const team = text.match(/^team: (\S+)/i);
  if (team) return `team:${team[1]}`;
  const review = text.match(/^adversarial review/i);
  if (review) return "review";
  return "main";
}

function formatActivity(raw) {
  const text = String(raw || "").trim();
  if (!text) return modelActivityLabel("处理中");
  const research = text.match(/^researching with (\d+) agent\(s\): (.+)$/i);
  if (research) return `调研中 · ${research[2]}（${research[1]} 路）…`;
  const verify = text.match(/^verifying with (.+)$/i);
  if (verify) return `验证中 · ${verify[1]}…`;
  const delegate = text.match(/^delegating to (.+)$/i);
  if (delegate) return `委派中 · ${delegate[1]}…`;
  const sub = text.match(/^subagent '([^']+)' on (.+)$/i);
  if (sub) return `${sub[1]} · ${sub[2]}…`;
  const team = text.match(/^team: (\S+) on (.+)$/i);
  if (team) return `协同 ${team[1]} · ${team[2]}…`;
  const review = text.match(/^adversarial review: .+ on (.+)$/i);
  if (review) return `对抗审查 · ${review[1]}…`;
  if (text.length > 80) return `${text.slice(0, 77)}…`;
  return text.endsWith("…") || text.endsWith("...") ? text : `${text}…`;
}

function activityDock() {
  return $("activity-dock");
}

function setActivity(text, kind, source) {
  const key = source || "main";
  const dock = activityDock();
  if (dock) {
    if (!state.activityHost || state.activityHost !== dock) {
      // Migrate off any leftover in-transcript host from older sessions.
      if (state.activityHost && state.activityHost !== dock) {
        state.activityHost.remove();
      }
      state.activityHost = dock;
      dock.innerHTML = "";
      state.activities.clear();
    }
    dock.classList.remove("hidden");
  } else if (!state.activityHost || !state.activityHost.isConnected) {
    state.activityHost = mount(el("div", "entry activity-host"));
    state.activities.clear();
  }
  let row = state.activities.get(key);
  if (!row) {
    row = el("div", `activity ${kind || ""}`, text);
    state.activityHost.appendChild(row);
    state.activities.set(key, row);
  } else {
    row.className = `activity ${kind || ""}`;
    row.textContent = text;
  }
  state.activity = state.activities.get("main") || row;
}

function clearActivity(source) {
  const dock = activityDock();
  if (source) {
    const row = state.activities.get(source);
    if (row) row.remove();
    state.activities.delete(source);
    if (state.activities.size === 0) {
      if (dock) {
        dock.innerHTML = "";
        dock.classList.add("hidden");
      } else if (state.activityHost) {
        state.activityHost.remove();
      }
      state.activityHost = null;
    }
    state.activity = state.activities.get("main") || null;
    return;
  }
  if (dock) {
    dock.innerHTML = "";
    dock.classList.add("hidden");
  } else if (state.activityHost) {
    state.activityHost.remove();
  }
  state.activityHost = null;
  state.activities.clear();
  state.activity = null;
}

function appendText(delta) {
  if (!state.streaming) {
    // Do not wipe buffer — a mid-turn transcript refresh may have dropped
    // the streaming node while deltas already lived in state.buffer.
    state.streaming = mount(el("div", "entry assistant"));
  }
  state.buffer += delta;
  // Paint at most once per frame so rapid deltas don't thrash layout.
  if (!state.streamFrame) {
    state.streamFrame = requestAnimationFrame(() => {
      state.streamFrame = 0;
      if (state.streaming) {
        state.streaming.textContent = state.buffer;
        if (atBottom()) transcript().scrollTop = transcript().scrollHeight;
      }
    });
  }
}

function appendThinking(delta) {
  if (!state.thinking) {
    const box = el("div", "entry thinking collapsed");
    const head = el("div", "thinking-head");
    const glyph = el("span", "thinking-glyph", "›");
    const label = el("span", "thinking-label", t("thinking.live"));
    head.append(glyph, label);
    const body = el("div", "thinking-body");
    head.onclick = () => {
      box.classList.toggle("collapsed");
      glyph.textContent = box.classList.contains("collapsed") ? "›" : "⌄";
    };
    box.append(head, body);
    state.thinking = mount(box);
  }
  const body = state.thinking.querySelector(".thinking-body");
  if (body) body.textContent += delta;
  const label = state.thinking.querySelector(".thinking-label");
  if (label) label.textContent = t("thinking.live");
}

async function copyText(text, button) {
  const value = String(text ?? "");
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    if (button) {
      button.classList.add("copied");
      const prev = button.textContent;
      button.textContent = t("copied");
      setTimeout(() => {
        button.textContent = prev;
        button.classList.remove("copied");
      }, 1200);
    } else {
      toast(t("copied"), "ok");
    }
  } catch {
    toast(t("copyFail"), "warn");
  }
}

function decorateCopyables(root) {
  if (!root) return;
  root.querySelectorAll("pre").forEach((pre) => {
    if (pre.parentElement && pre.parentElement.classList.contains("code-block")) return;
    const wrap = el("div", "code-block");
    const button = el("button", "copy-btn", t("copy"));
    button.type = "button";
    button.title = t("copyCode");
    button.onclick = (event) => {
      event.stopPropagation();
      copyText(pre.textContent, button);
    };
    pre.replaceWith(wrap);
    wrap.append(button, pre);
  });
  root.querySelectorAll("code").forEach((code) => {
    if (code.closest("pre")) return;
    if (code.classList.contains("inline-copy")) return;
    const text = (code.textContent || "").trim();
    if (!text || text.length > 240) return;
    code.classList.add("inline-copy");
    code.title = "点击复制";
    code.onclick = (event) => {
      event.stopPropagation();
      copyText(text, null);
    };
  });
}

/* Markdown is parsed once, at the end of the turn. Doing it per delta is
 * the single most expensive thing a streaming chat UI can do. */
function finishStreaming() {
  if (state.streamFrame) {
    cancelAnimationFrame(state.streamFrame);
    state.streamFrame = 0;
  }
  if (state.streaming) {
    const text = state.buffer;
    state.streaming.innerHTML = renderMarkdown(text);
    decorateCopyables(state.streaming);
    attachPinMemory(state.streaming, text);
    state.streaming = null;
    state.buffer = "";
  } else if ((state.buffer || "").trim()) {
    // streaming node was wiped (transcript refresh) but deltas remain in buffer.
    const text = state.buffer;
    state.buffer = "";
    const node = mount(el("div", "entry assistant"));
    node.innerHTML = renderMarkdown(text);
    decorateCopyables(node);
    attachPinMemory(node, text);
    state.turnHadText = true;
  }
  if (state.thinking) {
    const body = state.thinking.querySelector(".thinking-body");
    const label = state.thinking.querySelector(".thinking-label");
    const glyph = state.thinking.querySelector(".thinking-glyph");
    const chars = (body && body.textContent) ? body.textContent.trim().length : 0;
    if (label) {
      label.textContent = chars
        ? t("thinking.done", { n: chars })
        : t("thinking.empty");
    }
    state.thinking.classList.add("collapsed");
    if (glyph) glyph.textContent = "›";
    state.thinking = null;
  }
}

function addNotice(text, level) {
  mount(el("div", `entry notice ${level || "info"}`, text));
}

/** Guarantee the final answer is on screen even if stream bubbles were lost. */
function ensureAssistantVisible(text) {
  const body = String(text || "").trim();
  if (!body) return;
  const host = transcript();
  if (!host) return;
  let last = host.lastElementChild;
  // Skip trailing notices / activity hosts / edit chrome.
  while (
    last
    && (
      last.classList.contains("notice")
      || last.classList.contains("activity-host")
      || last.classList.contains("thinking")
    )
  ) {
    last = last.previousElementSibling;
  }
  if (last && last.classList.contains("assistant")) {
    const existing = (last.textContent || "").trim();
    if (existing.length >= Math.min(40, body.length)) {
      state.turnHadText = true;
      return;
    }
    last.innerHTML = renderMarkdown(body);
    decorateCopyables(last);
    attachPinMemory(last, body);
    state.turnHadText = true;
    return;
  }
  const node = mount(el("div", "entry assistant"));
  node.innerHTML = renderMarkdown(body);
  decorateCopyables(node);
  attachPinMemory(node, body);
  state.turnHadText = true;
  if (atBottom()) host.scrollTop = host.scrollHeight;
}

function workspaceFileUrl(path) {
  const session = (state.status && state.status.session_id) || "";
  if (!path || !session) return "";
  const query = new URLSearchParams({
    session,
    path,
    token: state.authToken || "",
  });
  return `/workspace-file?${query.toString()}`;
}

function shortPathLabel(path) {
  const text = String(path || "");
  const parts = text.replace(/\\/g, "/").split("/");
  return parts.slice(-2).join("/") || text;
}

function rememberResource(path, kind) {
  if (!path) return;
  const key = String(path);
  if (!state.resources.has(key)) {
    state.resources.set(key, { path: key, kind: kind || "file" });
    renderResourceStrip();
  }
}

function renderResourceStrip() {
  const strip = $("resource-strip");
  if (!strip) return;
  strip.innerHTML = "";
  strip.classList.toggle("hidden", state.resources.size === 0);
  for (const item of state.resources.values()) {
    strip.appendChild(resourceChip(item.path, item.kind));
  }
}

function resourceChip(path, kind) {
  const chip = el("button", "resource-chip");
  chip.type = "button";
  chip.title = path;
  const isImage = kind === "screenshot" || IMAGE_SUFFIX.test(path);
  if (isImage) {
    const img = document.createElement("img");
    img.src = workspaceFileUrl(path);
    img.alt = "";
    chip.appendChild(img);
  }
  chip.appendChild(el("span", "resource-label", shortPathLabel(path)));
  chip.onclick = (event) => {
    event.stopPropagation();
    if (isImage) {
      openImageViewer(workspaceFileUrl(path), shortPathLabel(path));
      return;
    }
    send("open_path", { path, mode: "reveal" });
  };
  chip.oncontextmenu = (event) => {
    event.preventDefault();
    send("open_path", { path, mode: "reveal" });
  };
  return chip;
}

function mountToolResources(box, display, args) {
  const path = (display && display.path)
    || (args && (args.file_path || args.path))
    || "";
  if (!path) return;
  const kind = (display && display.kind) || "file";
  rememberResource(path, kind);
  let row = box.querySelector(".tool-resources");
  if (!row) {
    row = el("div", "tool-resources");
    box.appendChild(row);
  }
  row.innerHTML = "";
  row.appendChild(resourceChip(path, kind));
}

function addToolEntry(msg) {
  const box = el("div", "entry tool running");
  const head = el("div", "tool-head");
  head.append(
    el("span", "tool-glyph", "⟳"),
    el("span", "tool-name", msg.headline || msg.name),
    el("span", "tool-summary", ""),
    el("span", "tool-time", ""),
  );
  const body = el("div", "tool-body hidden");
  head.onclick = () => body.classList.toggle("hidden");
  box.append(head, body);
  state.tools.set(msg.call_id, { box, head, body, args: msg.args || {} });
  mount(box);
  mountToolResources(box, null, msg.args || {});
}

function toolSummaryLine(msg) {
  const display = msg.display || {};
  const path = display.path || "";
  const label = path ? shortPathLabel(path) : "";
  if (display.kind === "edit" && label) {
    const add = Number(display.added || 0);
    const rem = Number(display.removed || 0);
    let line = `Edited ${label}`;
    if (add || rem) line += ` +${add} −${rem}`;
    return line;
  }
  if (display.kind === "write" && label) return `Wrote ${label}`;
  if (display.kind === "read" && label) return `Read ${label}`;
  if (display.kind === "screenshot" && label) return `Screenshot ${label}`;
  return msg.summary || msg.name || "";
}

function completeToolEntry(msg, deferFold) {
  const entry = state.tools.get(msg.call_id);
  if (!entry) return;
  state.tools.delete(msg.call_id);

  const summary = toolSummaryLine(msg);
  entry.box.dataset.summary = summary;
  entry.box.dataset.kind = (msg.display && msg.display.kind) || msg.name || "";
  entry.box.dataset.added = String((msg.display && msg.display.added) || 0);
  entry.box.dataset.removed = String((msg.display && msg.display.removed) || 0);
  entry.box.dataset.callId = msg.call_id || "";

  entry.box.classList.remove("running");
  entry.box.classList.add(msg.is_error ? "failed" : "ok");
  entry.head.children[0].textContent = msg.is_error ? "✗" : "✓";
  entry.head.children[2].textContent = summary;
  entry.head.children[3].textContent = `${(msg.duration || 0).toFixed(1)}s`;

  // Edit: show a compact +/- diff preview inside the collapsible body.
  const display = msg.display || {};
  if (display.kind === "edit" && (display.old || display.new)) {
    entry.body.innerHTML = "";
    const diff = el("div", "edit-diff");
    if (display.old) {
      const rem = el("pre", "diff-del");
      rem.textContent = display.old;
      diff.appendChild(rem);
    }
    if (display.new) {
      const add = el("pre", "diff-add");
      add.textContent = display.new;
      diff.appendChild(add);
    }
    entry.body.appendChild(diff);
  } else {
    entry.body.textContent = msg.content || "";
  }
  if (msg.is_error) entry.body.classList.remove("hidden");
  if (msg.content && !entry.head.querySelector(".copy-btn")) {
    const copy = el("button", "copy-btn", t("copy"));
    copy.type = "button";
    copy.title = t("copyTool");
    copy.onclick = (event) => {
      event.stopPropagation();
      copyText(msg.content, copy);
    };
    entry.head.appendChild(copy);
  }
  mountToolResources(entry.box, msg.display || {}, entry.args || msg.args || {});
  if (deferFold && !msg.is_error) {
    state.pendingToolFold = true;
    return;
  }
  regroupTools(entry.box, msg.is_error);
}

function foldPendingTools() {
  if (!state.pendingToolFold) return;
  state.pendingToolFold = false;
  const host = transcript();
  if (!host) return;
  const tools = [...host.querySelectorAll(".entry.tool.ok")].filter(
    (node) => !node.closest(".tool-group"),
  );
  for (const box of tools) {
    regroupTools(box, false);
  }
}

/* A run of tool calls is one thought, not ten. Left as separate rows they
 * push the actual answer off the screen, so consecutive finished calls fold
 * into a single line you can open. Failures are never folded away: a run that
 * went wrong is the one you need to see. */
function groupOf(node) {
  return node && node.classList && node.classList.contains("tool-group") ? node : null;
}

function regroupTools(box, failed) {
  if (failed) return;
  const previous = box.previousElementSibling;
  const existing = groupOf(previous);

  if (existing) {
    existing.querySelector(".tool-group-body").appendChild(box);
    countGroup(existing);
    return;
  }
  // A lone finished call stays a lone row; grouping starts at the second.
  if (!previous || !previous.classList.contains("tool") ||
      previous.classList.contains("running") ||
      previous.classList.contains("failed")) {
    return;
  }

  const group = el("div", "entry tool-group");
  const head = el("div", "tool-group-head");
  head.append(el("span", "tool-glyph", "›"), el("span", "tool-group-label", ""));
  const body = el("div", "tool-group-body hidden");
  head.onclick = () => {
    body.classList.toggle("hidden");
    head.querySelector(".tool-glyph").textContent =
      body.classList.contains("hidden") ? "›" : "⌄";
  };
  group.append(head, body);

  previous.replaceWith(group);
  body.append(previous, box);
  countGroup(group);
}

function countGroup(group) {
  const tools = [...group.querySelectorAll(".tool")];
  const kinds = { edit: 0, write: 0, write: 0, search: 0, other: 0 };
  let added = 0;
  let removed = 0;
  for (const tool of tools) {
    const kind = (tool.dataset.kind || "").toLowerCase();
    added += Number(tool.dataset.added || 0);
    removed += Number(tool.dataset.removed || 0);
    if (kind === "edit" || kind === "write") kinds.edit += 1;
    else if (kind === "read") kinds.read += 1;
    else if (kind === "glob" || kind === "grep" || kind.includes("search")) kinds.search += 1;
    else if (kind === "screenshot") kinds.other += 1;
    else kinds.other += 1;
  }
  const parts = [];
  if (kinds.edit) parts.push(`编辑 ${kinds.edit} 个文件`);
  if (kinds.read) parts.push(`读取 ${kinds.read} 个文件`);
  if (kinds.search) parts.push(`${kinds.search} 次搜索`);
  if (kinds.other) parts.push(`${kinds.other} 个操作`);
  if (!parts.length) parts.push(`运行了 ${tools.length} 个工具`);
  let label = parts.join("，");
  if (added || removed) label += ` +${added} −${removed}`;
  group.querySelector(".tool-group-label").textContent = label;
}

function addCompaction(msg) {
  const saved = Math.max((msg.before || 0) - (msg.after || 0), 0);
  const box = el("div", "entry compaction");
  box.append(el("div", "compaction-head",
    `上下文已压缩 · ${msg.replaced} 条消息 → 摘要 · ` +
    `${compactTokens(msg.before)} → ${compactTokens(msg.after)}（省 ${compactTokens(saved)}）`));
  const note = el("div", "compaction-note", msg.summary || "");
  note.title = "点击展开完整交接笔记";
  note.onclick = () => note.classList.toggle("open");
  box.append(note);
  mount(box);
}

function replayTranscript(entries, compactions) {
  const host = transcript();
  host.innerHTML = "";
  // Reset scroll before rebuild so mount()'s sticky check starts from top=0
  // of an empty pane (otherwise the previous chat's offset can stick mid-way).
  host.scrollTop = 0;
  state.tools.clear();
  state.resources.clear();
  renderResourceStrip();
  state.activity = null;
  state.streaming = null;
  state.buffer = "";
  state.thinking = null;
  clearInlineHitl();
  state.turns = entries.length;
  renderWorkspaceChips();
  let userIndex = 0;
  for (const item of entries) {
    if (item.role === "user") addUser(item.text, item.images || [], userIndex++);
    else {
      const node = mount(el("div", "entry assistant"));
      node.innerHTML = renderMarkdown(item.text);
      decorateCopyables(node);
    }
  }
  for (const record of compactions) {
    addCompaction({ summary: record.summary, before: record.before,
                    after: record.after, replaced: record.replaced });
  }
  scrollToLatest();
}

/* ── status ────────────────────────────────────────────────────────── */

function applyStatus(status) {
  if (!status) return;
  const prevSession = state.viewSessionId
    || (state.status && state.status.session_id)
    || "";
  const nextSession = status.session_id || "";
  const sessionChanged = !!(prevSession && nextSession && prevSession !== nextSession);
  if (nextSession) state.viewSessionId = nextSession;
  state.status = status;
  const wasBusy = state.busy;
  state.busy = status.busy;
  // Activity dock is global DOM — always retarget it when the viewed chat
  // changes, otherwise "k3@Kimi 思考中" leaks onto a DeepSeek session.
  if (sessionChanged) {
    clearActivity();
    abandonLiveStreamView();
    state.pinBottomOnReady = true;
    if (status.busy) {
      const line = (status.activity || "").trim()
        || modelActivityLabel(t("activity.busy"), status.model, status.account);
      setActivity(line, "busy");
    }
  } else if (!status.busy) {
    clearActivity();
  } else if (!state.activities.size) {
    // Switched away and back (or dock was cleared) while still running.
    const line = (status.activity || "").trim()
      || modelActivityLabel(t("activity.busy"), status.model, status.account);
    setActivity(line, "busy");
  }
  // Cancel paths may clear busy via status without a DONE frame — still
  // fold sticky streaming / open tool cards the same way done() does.
  if (wasBusy && !status.busy) {
    finishStreaming();
    foldPendingTools();
    clearActivity();
    // Do NOT refresh+replay transcript here. push_transcript only has
    // user/assistant text — replaying wipes every live tool card and makes
    // a finished turn look empty. Missed answer text is handled by done().
  } else if (sessionChanged) {
    send("refresh", { transcript: true });
  } else if (!wasBusy && status.busy) {
    // Sidebar "运行中" only — do not replay transcript mid-stream.
    send("refresh", {});
  }
  applyComposerDraft(status);

  // Keep Send enabled while busy so the user can queue the next guidance.
  $("send").disabled = false;
  $("send").textContent = status.busy ? t("composer.queue") : t("composer.send");
  $("interrupt").disabled = !status.busy;
  if (wasBusy && !status.busy) setTimeout(flushPromptQueue, 0);
  renderPromptQueue();
  // Continue visibility tracks busy — refresh the strip when it flips.
  if (wasBusy !== status.busy && state.lastTodos && state.lastTodos.length) {
    renderTodos(state.lastTodos);
  }
  $("plan-badge").classList.toggle("hidden", !status.plan_mode);
  $("plan-badge").classList.toggle("clickable", !!status.plan_mode);
  const exploreBadge = $("explore-badge");
  if (exploreBadge) {
    exploreBadge.classList.toggle("hidden", !status.explore_mode);
    exploreBadge.classList.toggle("clickable", !!status.explore_mode);
  }
  const classify = $("auto-classify");
  if (classify && document.activeElement !== classify) {
    classify.checked = !!status.auto_classify;
  }
  const autoApply = $("auto-apply-edits");
  if (autoApply && document.activeElement !== autoApply) {
    autoApply.checked = !!status.auto_apply_edits;
  }
  $("pet").classList.toggle("busy", status.busy);
  setGoalMode(!!status.heartbeat_armed);
  $("heartbeat-button").classList.toggle("on", !!status.heartbeat);
  renderWorkspaceChips();

  const fraction = status.context_window
    ? status.context_used / status.context_window : 0;
  $("context-fill").style.width = `${Math.min(fraction * 100, 100)}%`;
  $("context-fill").style.background =
    fraction < 0.5 ? "#6bbf7b" : fraction < 0.75 ? "#c9a227"
    : fraction < 0.9 ? "#d97a3d" : "#c04a3c";
  $("context-text").textContent =
    `${compactTokens(status.context_used)} / ${compactTokens(status.context_window)}`;

  const cache = $("cache-stat");
  const runHit = status.run_cache_hit ?? status.cache_hit ?? 0;
  const sessionHit = status.session_cache_hit ?? runHit;
  cache.textContent = `cache 运行 ${Math.round(runHit * 100)}% · 会话 ${Math.round(sessionHit * 100)}%`;
  cache.title = "本次运行（进程内，重启归零） / 本会话累计（持久化）";
  cache.classList.toggle("warn", runHit < 0.3 && runHit > 0);

  $("mode-select").value = status.mode;
  // The slogan line is static (i18n); keep the workspace path as a tooltip.
  $("brand-slogan").title = status.workspace || "";
  syncModelControls(status);
  if (status.language != null) {
    syncLanguageSelect(status.language);
  }

  // Open the settings page once, on the first status of an unconfigured
  // session. Doing it on every status push would slam the panel back to
  // settings the instant the user clicked any other tab.
  if (!status.configured && !state.setupShown) {
    state.setupShown = true;
    openPanel("settings");
  }
}

function syncLanguageSelect(pref) {
  const wanted = pref || "auto";
  if (typeof setLanguagePreference === "function" && wanted !== currentLanguagePref()) {
    setLanguagePreference(wanted);
  }
  const select = $("language-select");
  if (select && document.activeElement !== select) {
    if ([...select.options].some((o) => o.value === wanted)) {
      select.value = wanted;
    }
  }
}

function fillLanguageSelect(config) {
  const select = $("language-select");
  if (!select) return;
  const choices = (config && config.languages) || [];
  if (!choices.length) return;
  const previous = select.value || currentLanguagePref() || "auto";
  select.innerHTML = "";
  for (const item of choices) {
    const opt = document.createElement("option");
    opt.value = item.code;
    opt.textContent = item.label;
    select.appendChild(opt);
  }
  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  }
}

/** Keep composer model/effort/context selects aligned with the open session. */
function syncModelControls(status) {
  const select = $("model-select");
  if (!select || !select.options.length) return;
  const spec = status.account
    ? `${status.model}@${status.account}`
    : (status.model || "");
  if (spec && [...select.options].some((o) => o.value === spec)) {
    select.value = spec;
  }
  if (state.config) {
    fillEffortSelect(state.config, select.value || spec);
    fillContextSelect(state.config, select.value || spec);
  }
  syncVisionBadge(select.value || spec);
  if (status.effort && $("effort-select")) {
    const effort = $("effort-select");
    if ([...effort.options].some((o) => o.value === status.effort)) {
      effort.value = status.effort;
    }
  }
  if (status.context_window && $("context-select")) {
    const ctx = $("context-select");
    const value = String(status.context_window);
    if ([...ctx.options].some((o) => o.value === value)) {
      ctx.value = value;
    }
  }
  // Mid-turn model/effort swaps break thinking-mode providers; lock until idle.
  const locked = !!status.busy;
  select.disabled = locked;
  select.title = locked ? t("composer.modelLocked") : t("composer.model");
  const effort = $("effort-select");
  if (effort) {
    effort.disabled = locked || effort.options.length <= 1;
    effort.title = locked ? t("composer.modelLocked") : t("composer.effort");
  }
}

function setPet(mood) {
  const src = PET_GIFS[mood] || PET_GIFS.idle;
  const img = $("pet-img");
  // Skip redundant swaps so an uninterrupted state keeps its loop running.
  if (img && !img.src.endsWith(src)) img.src = src;
}

function applyComposerDraft(status) {
  const id = status.session_id || "";
  if (!id || id === state.draftSessionId) return;
  // Switching chats: drop any pending save for the previous session's timer
  // (the text for the old id was already flushed on click / is stale).
  if (state.draftTimer) {
    clearTimeout(state.draftTimer);
    state.draftTimer = null;
  }
  state.draftSessionId = id;
  clearPendingImages();
  state.promptQueue = [];
  renderPromptQueue();
  state.resources.clear();
  renderResourceStrip();
  const box = $("prompt");
  if (document.activeElement === box && box.value && !status.draft) {
    // User already typed into a brand-new empty session before status arrived.
    return;
  }
  box.value = status.draft || "";
  box.style.height = "auto";
  box.style.height = Math.max(
    COMPOSER_MIN_HEIGHT_PX,
    Math.min(box.scrollHeight, COMPOSER_MAX_HEIGHT_PX),
  ) + "px";
}

function scheduleDraftSave() {
  if (state.draftTimer) clearTimeout(state.draftTimer);
  state.draftTimer = setTimeout(() => {
    state.draftTimer = null;
    const id = state.status && state.status.session_id;
    if (!id) return;
    send("set_draft", { text: $("prompt").value });
  }, DRAFT_SAVE_MS);
}

function flushDraftNow() {
  if (state.draftTimer) {
    clearTimeout(state.draftTimer);
    state.draftTimer = null;
  }
  const id = state.status && state.status.session_id;
  if (!id) return;
  send("set_draft", { text: $("prompt").value });
}

/* ── sessions ──────────────────────────────────────────────────────── */

/* Sessions are grouped by project. A conversation only makes sense against
 * the tree it ran in, so a flat list across every directory would be a lie
 * about what these things are. */
function panelSessionPrefix() {
  if (state.uiMode === "codex") return "codex";
  if (state.uiMode === "claude") return "claude";
  return "";
}

function clearPanelTranscriptDom(mode) {
  const host = panelTranscript(mode);
  if (host) host.innerHTML = "";
  if (mode === "codex") {
    state.codexStreaming = null;
    state.codexBuffer = "";
    state.codexThinking = null;
    state.codexTools = new Map();
  } else if (mode === "claude") {
    state.claudeStreaming = null;
    state.claudeBuffer = "";
    state.claudeThinking = null;
    state.claudeTools = new Map();
  }
}

function panelTranscriptIsEmpty(mode) {
  const host = panelTranscript(mode);
  return !host || host.children.length === 0;
}

function replayPanelTranscript(mode, rows) {
  clearPanelTranscriptDom(mode);
  const host = panelTranscript(mode);
  if (!host || !Array.isArray(rows)) return;
  for (const row of rows) {
    const role = row.role || "assistant";
    const text = row.text || "";
    if (!text) continue;
    if (mode === "codex") addCodexEntry(role === "user" ? "user" : "assistant", text);
    else addClaudeEntry(role === "user" ? "user" : "assistant", text);
  }
  host.scrollTop = host.scrollHeight;
}

function renderSessions(msg) {
  const list = $("session-list");
  list.innerHTML = "";

  /* Zero projects is a real state, reached by removing the last one. Left
   * blank it reads as a rendering failure, so say what happened and where
   * the way back is. */
  if (!(msg.workspaces || []).length) {
    const empty = el("div", "list-empty");
    empty.append(
      el("div", "", "还没有项目"),
      el("div", "hint", "在下方输入框上面选一个工作目录，开始对话后它就会出现在这里。"),
    );
    list.appendChild(empty);
    return;
  }

  const prefix = panelSessionPrefix();
  for (const group of msg.workspaces || []) {
    const block = el("div", "ws-group");
    const head = el("div", `ws-head${group.active ? " active" : ""}`);
    head.append(
      el("span", "ws-caret", state.collapsed.has(group.path) ? "▸" : "▾"),
      el("span", "ws-name", group.name),
      el("span", "ws-count", String(group.sessions.length)),
    );
    const forget = el("span", "act danger ws-forget", "✕");
    forget.title = "把这个项目从列表里移除（磁盘上的文件不动）";
    forget.onclick = (event) => {
      event.stopPropagation();
      const count = group.sessions.length;
      const detail = count
        ? `会连同它下面的 ${count} 个会话一起删除，磁盘上的文件不动。`
        : "它下面没有会话，只是从列表里移除。";
      const cmd = prefix ? `${prefix}_forget_workspace` : "forget_workspace";
      askConfirm("移除项目", `${group.path}\n\n${detail}`, () =>
        send(cmd, { path: group.path }));
    };
    head.appendChild(forget);

    head.title = group.path;
    head.onclick = () => {
      if (group.active) {
        toggleCollapsed(group.path);
      } else if (prefix) {
        send(`${prefix}_set_workspace`, { path: group.path });
      } else {
        send("set_workspace", { path: group.path });
      }
    };
    block.appendChild(head);

    if (!state.collapsed.has(group.path)) {
      for (const meta of group.sessions) {
        block.appendChild(sessionRow(meta, group));
      }
      if (!group.sessions.length) {
        block.appendChild(el("div", "ws-empty", "还没有会话"));
      }
    }
    list.appendChild(block);
  }

  const toggle = $("archive-toggle");
  toggle.textContent = msg.show_archived
    ? t("nav.hideArchived")
    : `${t("nav.showArchived")}${msg.archived_count ? ` (${msg.archived_count})` : ""}`;
  toggle.classList.toggle("hidden", !msg.archived_count && !msg.show_archived);
}

function toggleCollapsed(path) {
  if (state.collapsed.has(path)) state.collapsed.delete(path);
  else state.collapsed.add(path);
  if (state.uiMode === "codex" && state.codex.sessions) {
    renderSessions(state.codex.sessions);
  } else if (state.uiMode === "claude" && state.claude.sessions) {
    renderSessions(state.claude.sessions);
  } else {
    send("refresh", {});
  }
}

function sessionRow(meta, group) {
  const running = !!meta.running;
  const waiting = !!meta.waiting;
  const item = el("div",
    `session-item${meta.active ? " active" : ""}${meta.archived ? " archived" : ""}${running ? " running" : ""}${waiting ? " waiting" : ""}`);
  const prefix = panelSessionPrefix();

  const actions = el("span", "session-actions");
  const archive = el("span", "act", meta.archived ? "↩" : "⌂");
  archive.title = meta.archived ? "取消归档" : "归档（不删除，只是收起来）";
  archive.onclick = (event) => {
    event.stopPropagation();
    const cmd = prefix ? `${prefix}_archive_session` : "archive_session";
    send(cmd, { id: meta.id, archived: !meta.archived });
  };
  const remove = el("span", "act danger", "✕");
  remove.title = "删除这个会话";
  remove.onclick = (event) => {
    event.stopPropagation();
    const cmd = prefix ? `${prefix}_delete_session` : "delete_session";
    askConfirm(`删除会话「${meta.title}」？`,
      "这会连同它的完整记录一起删除，不可撤销。归档可以只是收起来。",
      () => send(cmd, { id: meta.id }));
  };
  actions.append(archive, remove);

  const title = el("div", "session-title", meta.title);
  title.prepend(actions);
  if (waiting) title.appendChild(el("span", "session-waiting", t("session.waiting")));
  else if (running) title.appendChild(el("span", "session-running", t("session.running")));
  item.append(title, el("div", "session-meta",
    `${meta.updated} · ${meta.messages} 条`));
  item.onclick = () => {
    const id = meta.id;
    if (prefix === "codex") {
      if (id && id !== state.viewCodexSessionId) {
        state.viewCodexSessionId = id;
        clearPanelTranscriptDom("codex");
        clearPanelActivity("codex");
      }
      // Always request a replay — same-session clicks used to skip when the
      // panel was empty after a status-without-transcript race.
      state.codexReplayPending = true;
      send("codex_open_session", { id });
      return;
    }
    if (prefix === "claude") {
      if (id && id !== state.viewClaudeSessionId) {
        state.viewClaudeSessionId = id;
        clearPanelTranscriptDom("claude");
        clearPanelActivity("claude");
      }
      state.claudeReplayPending = true;
      send("claude_open_session", { id });
      return;
    }
    flushDraftNow();
    if (id && id !== state.viewSessionId) {
      // Retarget stream filter + sticky dock immediately — do not wait for
      // status, or the previous chat's "思考中" keeps blinking on this one.
      state.viewSessionId = id;
      clearActivity();
      abandonLiveStreamView();
      state.pinBottomOnReady = true;
      if (meta.running) {
        setActivity(t("session.running") + "…", "busy");
      }
    } else {
      // Same session: still jump to the latest message.
      scrollToLatest();
    }
    // Backend opens across projects atomically; a live turn keeps running.
    send("open_session", { id });
  };
  return item;
}

/* ── context panel ─────────────────────────────────────────────────── */

function renderContext(msg) {
  $("ctx-used").textContent =
    `${compactTokens(msg.used)} / ${compactTokens(msg.window)}`;
  $("ctx-pct").textContent = `${Math.round((msg.fraction || 0) * 100)}%`;

  const bar = $("ctx-bar");
  bar.innerHTML = "";
  const rows = $("ctx-rows");
  rows.innerHTML = "";

  for (const row of msg.rows || []) {
    const colour = SLICE_COLOURS[row.name] || "#8a8a8a";
    const segment = el("div", "ctx-seg");
    segment.style.width = `${(row.share || 0) * 100}%`;
    segment.style.background = colour;
    segment.title = `${row.name} ${compactTokens(row.tokens)}`;
    bar.appendChild(segment);

    const line = el("div", "ctx-row");
    const swatch = el("div", "ctx-swatch");
    swatch.style.background = colour;
    line.append(swatch, el("div", "", row.name),
      el("div", "ctx-tokens", compactTokens(row.tokens)),
      el("div", "ctx-share", `${((row.share || 0) * 100).toFixed(1)}%`));
    rows.appendChild(line);
  }

  const auto = $("ctx-auto");
  if (auto && state.status) {
    const threshold = Math.round((state.status.compact_threshold || 0.82) * 100);
    auto.textContent = state.status.auto_compact
      ? t("ctx.autoOn", { pct: threshold })
      : t("ctx.autoOff");
    auto.classList.toggle("warn", !state.status.auto_compact);
  }

  const detail = $("ctx-detail");
  detail.innerHTML = "";
  const rulesBlock = el("div", "ctx-detail-block");
  const rules = msg.rules || [];
  const projectFiles = msg.project_instructions || [];
  rulesBlock.appendChild(el("div", "", "规则 / Memories / 项目说明"));
  const memories = msg.memories || [];
  if (memories.length) {
    rulesBlock.appendChild(el("div", "", `Memories：${memories.join(" · ")}`));
  }
  if (projectFiles.length) {
    rulesBlock.appendChild(el("div", "", `项目说明：${projectFiles.join(" · ")}`));
  } else {
    rulesBlock.appendChild(el("div", "dim", "未找到 AGENTS.md / CLAUDE.md"));
  }
  if (rules.length) {
    rulesBlock.appendChild(el("div", "", `已加载规则：${rules.join(" · ")}`));
  } else {
    rulesBlock.appendChild(el("div", "dim",
      "无额外规则（可放全局 rules/ 或项目 .aiharness/rules/*.md）"));
  }
  if (msg.rules_dirs && msg.rules_dirs.length) {
    rulesBlock.appendChild(el("div", "dim", `目录：${msg.rules_dirs.join(" · ")}`));
  }
  detail.appendChild(rulesBlock);
  for (const [name, servers] of Object.entries(msg.detail || {})) {
    const block = el("div", "ctx-detail-block");
    block.appendChild(el("div", "", `${name} 按 server 拆分`));
    for (const [server, tokens] of Object.entries(servers)) {
      block.appendChild(el("div", "", `   ${server}  ${compactTokens(tokens)}`));
    }
    detail.appendChild(block);
  }
  const refs = Array.isArray(msg.turn_refs) ? msg.turn_refs : state.turnRefs;
  renderTurnRefs(refs);
}

function renderTurnRefs(refs) {
  const host = $("ctx-turn-refs");
  if (!host) return;
  host.innerHTML = "";
  const list = Array.isArray(refs) ? refs.filter(Boolean) : [];
  if (!list.length) {
    host.textContent = t("ctx.noRefs");
    host.classList.add("hint");
    return;
  }
  host.classList.remove("hint");
  for (const ref of list) {
    const chip = el("button", "ref-chip", `@${ref}`);
    chip.type = "button";
    chip.title = ref;
    chip.onclick = () => {
      openPanel("files");
      send("preview_path", { path: ref });
    };
    host.appendChild(chip);
  }
}

/* ── settings ──────────────────────────────────────────────────────── */

function renderConfig(config) {
  if (!config) return;
  fillLanguageSelect(config);

  const warning = $("setup-warning");
  warning.classList.toggle("hidden", config.ready);
  if (!config.ready) {
    warning.innerHTML = "<b>还不能开始对话：</b><br>" +
      (config.problems || []).map((p) => `· ${escapeHtml(p)}`).join("<br>");
  }

  const accounts = $("account-list");
  accounts.innerHTML = "";
  for (const account of config.accounts || []) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    main.append(el("div", "row-title", account.id),
      el("div", "row-sub", `${account.base_url} · ${account.key} · ${account.models.length} 个模型`),
      el("div", "row-sub", `出口：${account.proxy_label || "跟随系统"}`));
    const route = el("button", "ghost-button", "出口…");
    route.onclick = () => proxyDialog(account);
    const drop = el("button", "ghost-button", "移除");
    drop.onclick = () => send("remove_account", { id: account.id });
    row.append(main, route, drop);
    accounts.appendChild(row);
  }

  renderCapabilities(config.capabilities || []);
  fillProxyPresets(config.proxy_presets || []);

  const models = $("model-list");
  models.innerHTML = "";
  for (const model of config.models || []) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    const effective = model.supports_vision
      ? t("composer.visionOn")
      : t("composer.visionOff");
    const modeKey = {
      auto: "settings.visionAuto",
      on: "settings.visionOn",
      off: "settings.visionOff",
    }[model.vision_mode || "auto"] || "settings.visionAuto";
    main.append(
      el("div", "row-title", model.id),
      el("div", "row-sub",
        `${model.model} · ${model.accounts.join(", ")} · ${effective}`),
    );
    const visionBtn = el(
      "button",
      "ghost-button",
      t("settings.visionToggle", { mode: t(modeKey) }),
    );
    visionBtn.title = t("settings.visionToggleHint");
    visionBtn.onclick = () => {
      const order = ["auto", "on", "off"];
      const cur = model.vision_mode || "auto";
      const next = order[(order.indexOf(cur) + 1) % order.length];
      send("set_model_vision", { id: model.id, mode: next });
    };
    const drop = el("button", "ghost-button", "移除");
    drop.onclick = () => send("remove_model", { id: model.id });
    row.append(main, visionBtn, drop);
    models.appendChild(row);
  }

  renderRoles(config);
  fillModelSelect(config);
  fillAccountSelect(config);
  renderAboutSupport(config);
}

function renderAboutSupport(config) {
  const versionEl = $("about-version");
  if (!versionEl) return;
  const support = (config && config.support) || {};
  const version = support.version || "";
  versionEl.textContent = version
    ? t("settings.aboutVersion", { version })
    : t("settings.about");
}

function showSupportModal() {
  // Support / donate UI is intentionally disabled (no Sponsors / QR exposure).
}

function roleDisplayName(role) {
  return role === "main" ? "默认对话模型" : role;
}

function renderRoles(config) {
  const list = $("role-list");
  list.innerHTML = "";
  for (const entry of config.roles || []) {
    const row = el("div", "row");
    const main = el("div", "row-main");
    main.append(
      el("div", "row-title", roleDisplayName(entry.role)),
      el("div", "row-sub", entry.explicit ? "已指派" : "回落到默认对话模型"),
    );

    const picker = el("select");
    picker.appendChild(new Option("（回落到默认对话模型）", ""));
    for (const model of config.models || []) {
      for (const account of model.accounts) {
        const spec = `${model.id}@${account}`;
        picker.appendChild(new Option(spec, spec));
      }
    }
    picker.value = entry.explicit ? entry.binding.split(" ")[0] : "";
    picker.onchange = () => {
      if (picker.value) send("set_role", { role: entry.role, spec: picker.value });
    };
    row.append(main, picker);
    list.appendChild(row);
  }
}

function fillModelSelect(config) {
  const select = $("model-select");
  const current = state.status
    ? (state.status.account ? `${state.status.model}@${state.status.account}` : state.status.model)
    : "";
  select.innerHTML = "";
  if (!(config.models || []).length) {
    select.appendChild(new Option("尚未配置模型", ""));
    return;
  }
  /* Only model@account. Bare model ids and "未绑定账号" rows looked like
   * duplicates and invited half-configured selections. Unbound models stay
   * visible in the settings list so they can be removed or rebound. */
  let options = 0;
  for (const model of config.models) {
    const visionMark = model.supports_vision
      ? ` · ${t("composer.visionOn")}`
      : "";
    for (const account of model.accounts) {
      const spec = `${model.id}@${account}`;
      select.appendChild(new Option(`${spec}${visionMark}`, spec));
      options += 1;
    }
  }
  if (!options) {
    select.appendChild(new Option("请先在设置里绑定账号", ""));
  }
  if (current && [...select.options].some((o) => o.value === current)) {
    select.value = current;
  }
  fillEffortSelect(config, current);
  fillContextSelect(config, current);
  syncVisionBadge(select.value || current);
}

function modelSupportsVision(spec) {
  const id = String(spec || "").split("@")[0];
  const model = ((state.config && state.config.models) || []).find((m) => m.id === id);
  return !!(model && model.supports_vision);
}

function syncVisionBadge(spec) {
  const badge = $("vision-badge");
  if (!badge) return;
  const known = !!(spec && String(spec).includes("@"));
  if (!known) {
    badge.classList.add("hidden");
    return;
  }
  const id = String(spec || "").split("@")[0];
  const model = ((state.config && state.config.models) || []).find((m) => m.id === id);
  const on = !!(model && model.supports_vision);
  const mode = (model && model.vision_mode) || "auto";
  badge.classList.remove("hidden", "on", "off");
  badge.classList.add(on ? "on" : "off");
  const modeLabel = {
    auto: t("settings.visionAuto"),
    on: t("settings.visionOn"),
    off: t("settings.visionOff"),
  }[mode] || t("settings.visionAuto");
  badge.textContent = on
    ? `${t("composer.visionOn")}·${modeLabel}`
    : `${t("composer.visionOff")}·${modeLabel}`;
  badge.title = on ? t("composer.visionOnHint") : t("composer.visionOffHint");
}

/* Always shown. Hiding it entirely made people think the feature did not
 * exist; a disabled control with a reason is more honest. */
function fillEffortSelect(config, current) {
  const select = $("effort-select");
  const id = (current || "").split("@")[0];
  const model = (config.models || []).find((m) => m.id === id);
  const levels = model ? model.effort_levels : [];

  select.innerHTML = "";
  select.classList.remove("hidden");
  if (!levels.length) {
    select.appendChild(new Option(model ? "该模型无 effort" : "effort", ""));
    select.disabled = true;
    select.title = model
      ? `${model.id} 不接受推理强度参数`
      : "先配置一个模型";
    return;
  }
  select.disabled = false;
  select.title = "推理强度";
  for (const level of levels) select.appendChild(new Option(`effort: ${level}`, level));
  if (state.status && state.status.effort) select.value = state.status.effort;
}

/* Context window sizes the current model supports. */
function fillContextSelect(config, current) {
  const select = $("context-select");
  if (!select) return;
  const id = (current || "").split("@")[0];
  const model = (config.models || []).find((m) => m.id === id);
  const windows = model ? model.context_windows : [];

  select.innerHTML = "";
  if (!windows.length) {
    select.appendChild(new Option("上下文", ""));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const size of windows) {
    select.appendChild(new Option(compactTokens(size), String(size)));
  }
  if (state.status && state.status.context_window) {
    select.value = String(state.status.context_window);
  }
}

function fillAccountSelect(config) {
  const select = $("catalogue-account");
  select.innerHTML = "";
  for (const account of config.accounts || []) {
    select.appendChild(new Option(account.id, account.id));
  }
}

function renderCatalogue(catalogue) {
  const box = $("catalogue");
  box.innerHTML = "";
  box.classList.remove("hidden");
  if (!catalogue.ok) {
    box.appendChild(el("div", "hint", catalogue.detail));
    return;
  }
  if (!catalogue.models.length) {
    box.appendChild(el("div", "hint",
      `${catalogue.detail} —— 这个网关不列模型，请手动填模型 id。`));
    return;
  }
  for (const entry of catalogue.models) {
    const id = typeof entry === "string" ? entry : (entry && entry.id) || "";
    if (!id) continue;
    const vision = typeof entry === "object" && entry ? entry.supports_vision : null;
    let label = id;
    if (vision === true) label = `${id} · ${t("composer.visionOn")}`;
    else if (vision === false) label = `${id} · ${t("composer.visionOff")}`;
    const item = el("div", "catalogue-item", label);
    item.title = "点击添加";
    item.onclick = () => send("add_model", {
      account: catalogue.account,
      model: id,
      supports_vision: vision,
    });
    box.appendChild(item);
  }
}

function renderEditReview(pending) {
  const host = $("edit-review");
  if (!host) return;
  host.innerHTML = "";
  host.classList.toggle("hidden", !pending.length);
  if (!pending.length) return;

  const collapsed = !!state.editReviewCollapsed;
  host.classList.toggle("collapsed", collapsed);

  const head = el("div", "edit-review-head");
  const toggle = el(
    "button",
    "ghost-button edit-review-toggle",
    collapsed ? t("edit.expand") : t("edit.collapse"),
  );
  toggle.type = "button";
  toggle.title = collapsed ? t("edit.expandTitle") : t("edit.collapseTitle");
  toggle.onclick = () => {
    state.editReviewCollapsed = !state.editReviewCollapsed;
    renderEditReview(pending);
  };
  const title = el("button", "edit-review-title", t("edit.pending", { n: pending.length }));
  title.type = "button";
  title.title = toggle.title;
  title.onclick = () => toggle.click();
  head.append(toggle, title);

  const actions = el("div", "edit-review-actions");
  const openPanelBtn = el("button", "ghost-button", t("edit.wide"));
  openPanelBtn.onclick = () => openPanel("files");
  const applyAll = el("button", "ghost-button", t("edit.applyAll"));
  applyAll.onclick = () => send("edit_decision", { action: "apply_all" });
  const rejectAll = el("button", "ghost-button", t("edit.rejectAll"));
  rejectAll.onclick = () => send("edit_decision", { action: "reject_all" });
  actions.append(openPanelBtn, applyAll, rejectAll);
  head.appendChild(actions);
  host.appendChild(head);

  if (collapsed) return;

  for (const item of pending) {
    const row = el("div", "edit-review-item");
    row.onclick = (event) => {
      if (event.target.closest("button")) return;
      openPanel("files");
      renderEditDiffPanel(pending, item.id);
    };
    const meta = el("div", "edit-review-meta");
    const label = item.line
      ? `${item.rel || item.path}:${item.line}`
      : (item.rel || item.path);
    const path = el("div", "edit-review-path", label);
    path.title = item.path || "";
    const stat = el(
      "div",
      "edit-review-stat",
      item.kind === "write"
        ? (item.created ? t("edit.created") : t("edit.writeWhole"))
        : `+${item.added || 0} −${item.removed || 0}`,
    );
    meta.append(path, stat);

    const diff = buildUnifiedDiffView(item, false);
    const rowActions = el("div", "edit-review-actions");
    const apply = el("button", "primary-button", t("edit.accept"));
    apply.onclick = () => send("edit_decision", { action: "apply", id: item.id });
    const reject = el("button", "ghost-button", t("edit.rollback"));
    reject.onclick = () => send("edit_decision", { action: "reject", id: item.id });
    rowActions.append(apply, reject);
    row.append(meta, diff, rowActions);
    host.appendChild(row);
  }
}

function buildUnifiedDiffView(item, wide) {
  const diff = el("div", wide ? "edit-diff wide" : "edit-diff");
  if (item.unified) {
    const uni = el("pre", "diff-unified");
    uni.textContent = item.unified;
    diff.appendChild(uni);
    return diff;
  }
  if (item.old) {
    const rem = el("pre", "diff-del");
    rem.textContent = item.old;
    diff.appendChild(rem);
  }
  if (item.new) {
    const add = el("pre", "diff-add");
    add.textContent = item.new;
    diff.appendChild(add);
  }
  return diff;
}

function renderEditDiffPanel(pending, focusId) {
  const host = $("edit-diff-panel");
  if (!host) return;
  host.innerHTML = "";
  if (!pending.length) {
    host.textContent = t("edit.none");
    return;
  }
  const focus = pending.find((p) => p.id === focusId) || pending[0];
  for (const item of pending) {
    const block = el("div", "edit-review-item");
    if (item.id === focus.id) block.classList.add("focus");
    block.appendChild(el("div", "edit-review-path", item.rel || item.path));
    block.appendChild(buildUnifiedDiffView(item, true));
    host.appendChild(block);
  }
}

function renderRefChips() {
  const host = $("ref-chips");
  if (!host) return;
  host.innerHTML = "";
  host.classList.toggle("hidden", !state.refs.length);
  for (const ref of state.refs) {
    const chip = el("span", "ref-chip", `@${ref}`);
    const drop = el("button", "ref-chip-x", "×");
    drop.onclick = () => {
      state.refs = state.refs.filter((r) => r !== ref);
      renderRefChips();
    };
    chip.appendChild(drop);
    host.appendChild(chip);
  }
}

function addRef(path) {
  const rel = String(path || "").replace(/\\/g, "/");
  if (!rel || state.refs.includes(rel)) return;
  state.refs.push(rel);
  renderRefChips();
}

function renderAtMenu(paths, query) {
  const menu = $("at-menu");
  if (!menu) return;
  menu.innerHTML = "";
  if (!paths.length) {
    menu.classList.add("hidden");
    return;
  }
  menu.classList.remove("hidden");
  for (const item of paths.slice(0, 40)) {
    const row = el("button", "at-item", `${item.kind === "dir" ? "📁 " : ""}${item.path}`);
    row.type = "button";
    row.onclick = () => {
      addRef(item.path);
      const box = $("prompt");
      const at = box.value.lastIndexOf("@");
      if (at >= 0) {
        box.value = box.value.slice(0, at) + box.value.slice(at).replace(/^@[^\s]*/, "");
      }
      menu.classList.add("hidden");
      box.focus();
    };
    menu.appendChild(row);
  }
}

function maybeOpenAtMenu() {
  const box = $("prompt");
  const value = box.value;
  const caret = box.selectionStart || value.length;
  const before = value.slice(0, caret);
  const match = before.match(/@([^\s@]*)$/);
  if (!match) {
    $("at-menu").classList.add("hidden");
    return;
  }
  send("list_paths", { query: match[1] || "" });
}

function renderFileTree(path, nodes) {
  state.fileTreePath = path || "";
  const label = $("file-tree-path");
  if (label) label.textContent = path ? `/${path}` : "/";
  const host = $("file-tree");
  if (!host) return;
  host.innerHTML = "";
  if (path) {
    const up = el("button", "file-tree-item", "‥ 上级");
    up.type = "button";
    up.onclick = () => {
      const parent = path.split("/").slice(0, -1).join("/");
      send("list_tree", { path: parent });
    };
    host.appendChild(up);
  }
  for (const node of nodes) {
    const row = el("button", "file-tree-item", `${node.kind === "dir" ? "📁 " : "📄 "}${node.name}`);
    row.type = "button";
    row.onclick = () => {
      if (node.kind === "dir") send("list_tree", { path: node.path });
      else {
        state.previewPath = node.path;
        send("preview_path", { path: node.path });
        addRef(node.path);
      }
    };
    host.appendChild(row);
  }
}

function renderFilePreview(msg) {
  const host = $("file-preview");
  if (!host) return;
  if (!msg.ok) {
    host.textContent = msg.error || "无法预览";
    return;
  }
  host.textContent = msg.content || "";
}

function renderRulesEditor(rules) {
  const host = $("rules-editor-list");
  if (!host) return;
  host.innerHTML = "";
  for (const rule of rules) {
    const row = el("div", "row");
    row.append(
      el("div", "row-title", `${rule.scope}/${rule.name}`),
      el("div", "row-sub", (rule.body || "").slice(0, 80)),
    );
    const edit = el("button", "ghost-button", "载入");
    edit.onclick = () => {
      $("rule-scope").value = rule.scope;
      $("rule-name").value = rule.name;
      $("rule-body").value = rule.body || "";
    };
    const drop = el("button", "ghost-button", "删");
    drop.onclick = () => send("delete_rule", { scope: rule.scope, name: rule.name });
    row.append(edit, drop);
    host.appendChild(row);
  }
}

function renderMemories(memories) {
  const host = $("memory-list");
  if (!host) return;
  host.innerHTML = "";
  for (const memory of memories) {
    const row = el("div", "row");
    row.append(el("div", "row-title", memory.text));
    const pin = el("button", "ghost-button", memory.pinned ? t("mem.pinned") : t("mem.pin"));
    pin.onclick = () => send("update_memory", { id: memory.id, pinned: !memory.pinned });
    const drop = el("button", "ghost-button", t("mem.delete"));
    drop.onclick = () => send("delete_memory", { id: memory.id });
    row.append(pin, drop);
    host.appendChild(row);
  }
}

function renderQuest(quest) {
  state.quest = quest;
  const strip = $("quest-strip");
  const settings = $("quest-settings");
  if (strip) {
    strip.classList.toggle("hidden", !quest || quest.status === "done" || quest.status === "idle");
    if (quest && quest.status !== "done" && quest.status !== "idle") {
      strip.innerHTML = "";
      const title = el(
        "div",
        "quest-title",
        `Quest · ${quest.status}${quest.blocked_reason ? " · 阻塞" : ""}：${quest.goal}`,
      );
      const step = el("div", "quest-step", `当前：${quest.active_step || "—"}`);
      const resume = el("button", "ghost-button", "续跑");
      resume.onclick = () => send("resume_quest", {});
      strip.append(title, step, resume);
    }
  }
  if (settings) {
    if (!quest) {
      settings.textContent = "尚未开始";
    } else {
      settings.textContent = `${quest.status} · ${quest.goal} · 步骤 ${quest.steps.length}`;
    }
  }
}

function renderTodos(todos) {
  const strip = $("todo-strip");
  state.lastTodos = Array.isArray(todos) ? todos : [];
  strip.classList.toggle("hidden", !state.lastTodos.length);
  strip.innerHTML = "";
  if (!state.lastTodos.length) return;

  const collapsed = !!state.todoStripCollapsed;
  strip.classList.toggle("collapsed", collapsed);
  const active = state.lastTodos.find((t) => t.status === "in_progress");
  const done = state.lastTodos.filter((t) => t.status === "completed").length;
  const head = el("div", "todo-strip-head");
  const toggle = el(
    "button",
    "ghost-button todo-strip-toggle",
    collapsed ? t("todo.expand") : t("todo.collapse"),
  );
  toggle.type = "button";
  toggle.onclick = () => {
    state.todoStripCollapsed = !state.todoStripCollapsed;
    renderTodos(state.lastTodos);
  };
  const summary = el(
    "button",
    "todo-strip-title",
    active
      ? t("todo.summaryActive", { done, n: state.lastTodos.length, current: active.content })
      : t("todo.summary", { done, n: state.lastTodos.length }),
  );
  summary.type = "button";
  summary.onclick = () => toggle.click();
  head.append(toggle, summary);
  const openCount = state.lastTodos.filter((item) => item.status !== "completed").length;
  if (openCount) {
    const cont = el("button", "primary-button todo-continue-btn", t("todo.continue"));
    cont.type = "button";
    cont.title = t("todo.continueTitle");
    cont.disabled = !!state.busy;
    cont.onclick = () => requestContinueWork();
    head.appendChild(cont);
  }
  strip.appendChild(head);
  if (collapsed) return;

  for (const todo of state.lastTodos) {
    const cls = todo.status === "completed" ? "done"
              : todo.status === "in_progress" ? "active" : "";
    const glyph = todo.status === "completed" ? "✓"
                : todo.status === "in_progress" ? "▸" : "○";
    strip.appendChild(el("div", `todo ${cls}`, `${glyph} ${todo.content}`));
  }
}

/* ── modals ────────────────────────────────────────────────────────── */

/* Returns the action buttons so a dialog can relabel or disable them as the
 * user types. A dialog whose buttons never react looks like it is ignoring
 * the input — which is exactly how the plan feedback box read. */
function openModal(title, buildBody, actions) {
  $("modal-title").textContent = title;
  const body = $("modal-body");
  body.innerHTML = "";
  buildBody(body);
  const bar = $("modal-actions");
  bar.innerHTML = "";
  const buttons = {};
  for (const action of actions) {
    const button = el("button", action.primary ? "primary-button" : "ghost-button", action.label);
    button.onclick = () => {
      if (button.disabled) return;
      if (action.keepOpen) { action.run(); return; }
      closeModal();
      action.run();
    };
    if (action.name) buttons[action.name] = button;
    bar.appendChild(button);
  }
  $("modal-backdrop").classList.remove("hidden");
  return buttons;
}

function closeModal() { $("modal-backdrop").classList.add("hidden"); }

function describeCall(msg) {
  const args = msg.args || {};
  if (msg.tool === "Bash") return `$ ${args.command || ""}`;
  if (msg.tool === "Write") return `写入 ${args.file_path}\n\n${args.content || ""}`;
  if (msg.tool === "Edit") return `编辑 ${args.file_path}\n\n- ${args.old_string}\n\n+ ${args.new_string}`;
  return JSON.stringify(args, null, 2);
}

function clearInlineHitl() {
  for (const node of transcript().querySelectorAll(".entry.hitl")) {
    node.remove();
  }
}

function mountHitlCard(title, options) {
  const host = transcript();
  const prev = host.querySelector(".entry.hitl");
  const prevTop = prev ? prev.getBoundingClientRect().top : null;
  clearInlineHitl();
  const node = el("div", "entry hitl");
  node.appendChild(el("div", "hitl-title", title));
  const body = el("div", "hitl-body");
  const actions = el("div", "hitl-actions");
  node.append(body, actions);
  // Replacing a HITL card must not yank the transcript to the bottom —
  // the user is still reading the options.
  const keepPlace = !!(options && options.keepPlace) || prevTop != null;
  mount(node, { stick: !keepPlace && atBottom() });
  if (keepPlace && prevTop != null) {
    const delta = node.getBoundingClientRect().top - prevTop;
    host.scrollTop += delta;
  }
  return { node, body, actions };
}

function hitlButton(label, primary, run) {
  const button = el("button", primary ? "primary-button" : "ghost-button", label);
  button.type = "button";
  button.onclick = () => {
    if (button.disabled) return;
    run(button);
  };
  return button;
}

function showPermissionInline(msg) {
  const card = mountHitlCard(t("hitl.permissionTitle", { tool: msg.tool || "" }));
  if (msg.reason) card.body.appendChild(el("div", "hint", msg.reason));
  card.body.appendChild(el("div", "modal-code", describeCall(msg)));
  if (msg.suggested_rule) {
    card.body.appendChild(el("div", "hint",
      t("hitl.alwaysRule", { rule: msg.suggested_rule })));
  }
  const finish = (decision) => {
    clearInlineHitl();
    send("approve", { id: msg.id, decision });
  };
  card.actions.append(
    hitlButton(t("hitl.deny"), false, () => finish("deny")),
    hitlButton(t("hitl.always"), false, () => finish("always")),
    hitlButton(t("hitl.once"), true, () => finish("once")),
  );
}

function showQuestionsInline(msg) {
  const answers = {};
  let index = 0;
  const questions = msg.questions || [];
  /** @type {Record<string, string[]>} */
  const drafts = {};

  const step = () => {
    if (index >= questions.length) {
      clearInlineHitl();
      send("answer", { id: msg.id, answers });
      return;
    }
    const question = questions[index];
    const multi = !!question.multi_select;
    const last = index === questions.length - 1;
    const prior = drafts[question.header] || [];
    const picked = new Set(prior.filter((value) =>
      (question.options || []).some((option) => option.label === value)));
    const priorOther = prior.find((value) =>
      !(question.options || []).some((option) => option.label === value)) || "";

    const card = mountHitlCard(question.question || t("hitl.askTitle"), { keepPlace: true });
    if (questions.length > 1) {
      card.body.appendChild(el("div", "hint",
        t("hitl.progress", { i: index + 1, n: questions.length })));
    }
    card.body.appendChild(el("div", "hint", multi ? t("hitl.multiHint") : t("hitl.singleHint")));

    let other = null;
    let confirm = null;
    const optionButtons = [];

    const collected = () => {
      const chosen = multi ? [...picked] : [...picked].slice(0, 1);
      const typed = other && other.value.trim();
      if (typed) chosen.push(typed);
      return chosen;
    };

    const refresh = () => {
      if (!confirm) return;
      const n = collected().length;
      if (last) {
        confirm.textContent = n ? t("hitl.confirmN", { n }) : t("hitl.confirm");
      } else {
        confirm.textContent = n ? t("hitl.nextN", { n }) : t("hitl.next");
      }
      confirm.disabled = n === 0;
    };

    const commit = () => {
      const values = collected();
      if (!values.length) return;
      drafts[question.header] = values;
      answers[question.header] = values.join("、");
      index += 1;
      step();
    };

    for (const option of question.options || []) {
      const button = el("button", "option-button");
      button.type = "button";
      button.append(el("div", "option-label", option.label),
                    el("div", "option-desc", option.description || ""));
      if (picked.has(option.label)) button.classList.add("option-picked");
      button.onclick = () => {
        if (!multi) {
          picked.clear();
          picked.add(option.label);
          for (const peer of optionButtons) {
            peer.classList.toggle(
              "option-picked",
              peer === button,
            );
          }
        } else if (picked.has(option.label)) {
          picked.delete(option.label);
          button.classList.remove("option-picked");
        } else {
          picked.add(option.label);
          button.classList.add("option-picked");
        }
        refresh();
      };
      optionButtons.push(button);
      card.body.appendChild(button);
    }
    other = el("input");
    other.placeholder = multi ? t("hitl.otherMulti") : t("hitl.other");
    other.className = "option-other";
    other.value = priorOther;
    other.oninput = () => {
      if (!multi && other.value.trim()) {
        picked.clear();
        for (const peer of optionButtons) peer.classList.remove("option-picked");
      }
      refresh();
    };
    other.onkeydown = (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      commit();
    };
    card.body.appendChild(other);

    if (index > 0) {
      card.actions.append(hitlButton(t("hitl.back"), false, () => {
        const values = collected();
        if (values.length) drafts[question.header] = values;
        index -= 1;
        step();
      }));
    }
    card.actions.append(
      hitlButton(t("hitl.skipAll"), false, () => {
        clearInlineHitl();
        send("answer", { id: msg.id, answers });
      }),
    );
    confirm = hitlButton(last ? t("hitl.confirm") : t("hitl.next"), true, commit);
    confirm.disabled = true;
    card.actions.appendChild(confirm);
    refresh();
  };
  step();
}

function showPlanInline(msg) {
  const plan = msg.plan || {};
  let feedback = "";
  const card = mountHitlCard(t("hitl.planTitle", { rev: plan.revision || 1 }));
  if (plan.goal) card.body.appendChild(el("div", "", plan.goal));
  const list = el("ol");
  for (const step of plan.steps || []) {
    const item = el("li");
    item.append(el("div", "", step.title));
    if (step.files && step.files.length) {
      item.append(el("div", "row-sub", step.files.join(", ")));
    }
    list.appendChild(item);
  }
  card.body.appendChild(list);
  for (const [label, items] of [[t("hitl.risks"), plan.risks], [t("hitl.outOfScope"), plan.out_of_scope]]) {
    if (items && items.length) {
      card.body.appendChild(el("div", "hint", `${label}：${items.join("；")}`));
    }
  }
  const input = el("textarea");
  input.rows = 3;
  input.placeholder = t("hitl.planFeedback");
  input.className = "plan-feedback";
  card.body.appendChild(input);

  const reviseBtn = hitlButton(t("hitl.revise"), false, () => {
    clearInlineHitl();
    send("plan_decision", {
      id: msg.id, approved: false, feedback: feedback || t("hitl.reviseDefault"),
    });
  });
  const approveBtn = hitlButton(t("hitl.approve"), true, () => {
    clearInlineHitl();
    send("plan_decision", { id: msg.id, approved: true });
  });
  card.actions.append(reviseBtn, approveBtn);

  const refresh = () => {
    feedback = input.value.trim();
    reviseBtn.textContent = feedback ? t("hitl.sendFeedback") : t("hitl.revise");
    reviseBtn.classList.toggle("primary-button", !!feedback);
    reviseBtn.classList.toggle("ghost-button", !feedback);
    approveBtn.classList.toggle("ghost-button", !!feedback);
    approveBtn.classList.toggle("primary-button", !feedback);
  };
  input.oninput = refresh;
  input.onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (input.value.trim()) reviseBtn.click();
    }
  };
  setTimeout(() => input.focus(), 30);
}

/* The window is an embedded WebView, where window.confirm and window.prompt
 * are unreliable — they can return undefined without ever appearing, which
 * silently swallows the action. These replace them with real modals. */
function askConfirm(title, detail, run) {
  openModal(title, (body) => {
    if (detail) body.appendChild(el("div", "hint", detail));
  }, [
    { label: "取消", run: () => {} },
    { label: "确认", primary: true, run },
  ]);
}

/* ── opt-in capabilities ───────────────────────────────────────────── */

/* Desktop and browser control are the only tools that act outside the
 * working directory, so they get a switch with the reason next to it rather
 * than a checkbox in a list. Turning one on rebuilds the tool registry on
 * the backend; while it is off the model is not told the tools exist. */
function renderCapabilities(capabilities) {
  const host = $("capability-list");
  if (!host) return;
  host.innerHTML = "";
  for (const cap of capabilities) {
    const row = el("div", "row cap-row");
    const main = el("div", "row-main");
    main.append(
      el("div", "row-title", cap.name),
      el("div", "row-sub cap-detail", cap.detail || ""),
    );
    if (cap.note) main.appendChild(el("div", "row-sub cap-note", cap.note));

    const toggle = el("button", `switch ${cap.enabled ? "switch-on" : ""}`);
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", cap.enabled ? "true" : "false");
    toggle.title = cap.enabled ? "点击关闭" : "点击开启";
    toggle.appendChild(el("span", "switch-knob"));
    toggle.onclick = () => {
      if (cap.enabled) {
        send("set_capability", { id: cap.id, enabled: false });
        return;
      }
      askConfirm(`开启「${cap.name}」？`,
        `${cap.detail}

开启后模型才会看到这些工具。随时可以关掉。`,
        () => send("set_capability", { id: cap.id, enabled: true }));
    };
    row.append(main, toggle);
    host.appendChild(row);
  }
}

/* ── per-account network route ─────────────────────────────────────── */

function fillProxyPresets(presets) {
  const list = $("proxy-presets");
  if (!list) return;
  list.innerHTML = "";
  for (const url of presets) {
    const option = document.createElement("option");
    option.value = url;
    list.appendChild(option);
  }
}

/* One machine often needs two answers: a proxy is what makes a foreign
 * endpoint reachable and what makes a domestic one slow. So the route is a
 * property of the account, not of the process. */
function proxyDialog(account) {
  let value = account.proxy || "";
  openModal(`${account.id} 的网络出口`, (body) => {
    body.appendChild(el("div", "hint",
      "留空＝跟随系统代理设置；「直连」＝忽略系统代理；" +
      "或直接填代理地址。改完立刻生效，保存后才会留到下次启动。"));

    const choices = el("div", "choice-list");
    const presets = [
      { value: "", label: "跟随系统", hint: "用 HTTP_PROXY / HTTPS_PROXY" },
      { value: "direct", label: "直连", hint: "国内模型走这个，避免绕远" },
    ];
    for (const preset of presets) {
      const button = el("button", "choice", preset.label);
      button.appendChild(el("span", "choice-hint", preset.hint));
      button.onclick = () => { closeModal(); apply(preset.value); };
      choices.appendChild(button);
    }
    body.appendChild(choices);

    const input = el("input");
    input.value = value;
    input.setAttribute("list", "proxy-presets");
    input.placeholder = "http://127.0.0.1:7897";
    input.className = "limit-input";
    input.style.marginTop = "12px";
    input.style.width = "100%";
    input.oninput = () => { value = input.value.trim(); };
    input.onkeydown = (event) => {
      if (event.key === "Enter") { closeModal(); apply(value); }
    };
    body.appendChild(input);
    setTimeout(() => input.focus(), 30);
  }, [
    { label: "取消", run: () => {} },
    { label: "应用", primary: true, run: () => apply(value) },
  ]);

  function apply(proxy) {
    send("set_account_proxy", { id: account.id, proxy });
  }
}

/* ── goal mode ─────────────────────────────────────────────────────── */

/* Between "limits accepted" and "goal typed" the composer means something
 * different, so it has to *look* different. Without the banner the next
 * thing typed silently becomes an unattended loop instead of one turn. */
function setGoalMode(on) {
  const banner = $("goal-banner");
  banner.classList.toggle("hidden", !on);
  $("goal-limits").textContent = on ? state.armedLimits || "" : "";
  $("prompt").classList.toggle("goal-mode", on);
  $("prompt").placeholder = on
    ? "写下自动迭代的目标 —— 越具体越好，它会照着这个反复干活…"
    : "说点什么…  （Enter 发送 · Shift+Enter 换行）";
}

/* The dialog only collects the caps. A goal worth leaving unattended is a
 * paragraph, and a one-line box in a modal quietly asks for one line. */
function heartbeatDialog() {
  const fields = [
    { key: "iterations", label: "最多轮数", value: "10", hint: "留空 = 不限轮数" },
    { key: "cost", label: "最多花费 ($)", value: "1.00", hint: "留空 = 不限花费" },
    { key: "minutes", label: "最多分钟", value: "30", hint: "留空 = 不限时间" },
  ];
  const inputs = {};
  openModal("自动迭代 · 硬性上限", (body) => {
    body.appendChild(el("div", "hint",
      "先定上限，再在输入框里写目标。三个上限至少要留一个 —— " +
      "全部留空等于让 agent 无限花钱，不允许。"));
    for (const field of fields) {
      const row = el("div", "limit-row");
      row.appendChild(el("label", "limit-label", field.label));
      const input = el("input", "limit-input");
      input.value = field.value;
      input.placeholder = field.hint;
      inputs[field.key] = input;
      row.appendChild(input);
      body.appendChild(row);
    }
  }, [
    { label: "取消", run: () => {} },
    { label: "开启目标模式", primary: true, run: () => {
        const args = {};
        for (const key of Object.keys(inputs)) args[key] = inputs[key].value.trim();
        send("start_heartbeat", args);
      } },
  ]);
}

/* ── inline form feedback ──────────────────────────────────────────── */

/* A toast is the wrong place for "your key was rejected": it disappears
 * while the form still holds the text that caused it, which reads as
 * success. This keeps the verdict pinned under the form. */
function showAccountResult(result) {
  const box = $("account-result");
  box.textContent = result.text || "";
  box.className = `form-result ${result.ok ? "ok" : "bad"}`;
  box.classList.toggle("hidden", !result.text);
  if (result.ok) {
    for (const id of ["acc-id", "acc-url", "acc-env"]) $(id).value = "";
  }
}

function toast(text, kind) {
  const node = el("div", `toast ${kind || ""}`, text);
  $("toast-stack").appendChild(node);
  setTimeout(() => node.remove(), TOAST_MS);
}

/* ── panel ─────────────────────────────────────────────────────────── */

/* How much room the topbar actually has depends on the panel, and CSS cannot
 * ask "is my later sibling visible?". So the two facts the stylesheet needs —
 * panel open, and how narrow the remaining column is — are published as
 * classes on <body>. Without this the usage meter and the cost were pushed
 * out over the panel at the default 1360px window size. */
function syncPanelWidth() {
  const open = !$("panel").classList.contains("hidden");
  const room = document.getElementById("main").getBoundingClientRect().width;
  document.body.classList.toggle("panel-open", open);
  document.body.classList.toggle("main-tight", room < 620);
  document.body.classList.toggle("main-cramped", room < 470);
}

function setCanvasDirty(dirty) {
  state.canvasDirty = !!dirty;
  const save = $("canvas-save");
  if (save) save.disabled = !state.canvasDirty || !state.canvasPath;
}

function previewCanvasPath() {
  const path = ($("canvas-path") && $("canvas-path").value.trim()) || "";
  const view = $("canvas-view");
  if (!view || !path) return;
  view.innerHTML = "";
  state.canvasPath = path;
  setCanvasDirty(false);
  const session = (state.status && state.status.session_id) || "";
  const url = workspaceFileUrl(path);
  if (!session || !url) {
    view.textContent = t("canvas.noSession");
    return;
  }
  if (IMAGE_SUFFIX.test(path)) {
    const img = document.createElement("img");
    img.src = url;
    img.alt = path;
    img.style.maxWidth = "100%";
    view.appendChild(img);
    view.appendChild(el("div", "hint", t("canvas.imageOnly")));
    return;
  }
  fetch(url).then((response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.text();
  }).then((text) => {
    mountCanvasEditor(view, path, text);
  }).catch((error) => {
    view.textContent = t("canvas.previewFail", { error: error.message || error });
  });
}

function mountCanvasEditor(view, path, text) {
  view.innerHTML = "";
  const isHtml = /\.html?$/i.test(path);
  const editor = document.createElement("textarea");
  editor.className = "canvas-editor";
  editor.value = text;
  editor.spellcheck = false;
  editor.oninput = () => {
    setCanvasDirty(true);
    if (isHtml && frame) {
      frame.srcdoc = editor.value;
    }
  };
  let frame = null;
  if (isHtml) {
    frame = document.createElement("iframe");
    frame.title = path;
    frame.className = "canvas-frame";
    frame.srcdoc = text;
    view.appendChild(el("div", "hint", t("canvas.htmlHint")));
    view.appendChild(frame);
  } else {
    view.appendChild(el("div", "hint", t("canvas.textHint")));
  }
  view.appendChild(editor);
}

function saveCanvasPath() {
  const path = state.canvasPath || (($("canvas-path") && $("canvas-path").value.trim()) || "");
  const view = $("canvas-view");
  const editor = view && view.querySelector(".canvas-editor");
  if (!path || !editor) {
    toast(t("canvas.nothing"), "warn");
    return;
  }
  send("save_canvas", { path, content: editor.value });
  setCanvasDirty(false);
}

function renderOnboarding(msg) {
  const host = $("onboarding");
  if (!host) return;
  if (!msg || msg.ready || !(msg.steps || []).length) {
    host.classList.add("hidden");
    host.innerHTML = "";
    return;
  }
  host.classList.remove("hidden");
  host.innerHTML = "";
  host.appendChild(el("div", "onboarding-title", t("onboard.title")));
  const list = el("ol", "onboarding-steps");
  for (const step of msg.steps) {
    list.appendChild(el("li", "", step.label || step.id));
  }
  host.appendChild(list);
  const go = el("button", "primary-button", t("onboard.goSettings"));
  go.onclick = () => openPanel("settings");
  host.appendChild(go);
}

function renderSearchHits(hits, query) {
  const host = $("file-search-hits");
  if (!host) return;
  host.innerHTML = "";
  if (!hits.length) {
    host.textContent = query ? `(0) ${query}` : "";
    return;
  }
  for (const hit of hits) {
    const row = el("button", "search-hit", `${hit.path}:${hit.line}  ${hit.text || ""}`);
    row.type = "button";
    row.onclick = () => {
      send("preview_path", { path: hit.path });
      openPanel("files");
    };
    host.appendChild(row);
  }
}

function renderEquityChart(points, symbol) {
  const host = $("market-chart");
  if (!host) return;
  host.innerHTML = "";
  if (!points || !points.length) return;
  const width = 520;
  const height = 180;
  const padL = 48;
  const padR = 12;
  const padT = 16;
  const padB = 24;
  const values = points.map((p) => Number(p.equity));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "kline-svg");
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  let d = "";
  points.forEach((point, index) => {
    const x = padL + (plotW * index) / Math.max(points.length - 1, 1);
    const y = padT + (1 - (Number(point.equity) - min) / span) * plotH;
    d += (index ? " L " : "M ") + `${x},${y}`;
  });
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "#3d9a5f");
  path.setAttribute("stroke-width", "2");
  svg.appendChild(path);
  const label = document.createElementNS(ns, "text");
  label.setAttribute("x", String(padL));
  label.setAttribute("y", String(height - 6));
  label.setAttribute("class", "kline-label");
  label.textContent = `${symbol || ""} equity ${values[0].toFixed(3)} → ${values[values.length - 1].toFixed(3)}`;
  svg.appendChild(label);
  host.appendChild(svg);
}

function renderMarketChart(bars, symbol) {
  const host = $("market-chart");
  if (!host) return;
  host.innerHTML = "";
  if (!bars || !bars.length) return;
  const width = 520;
  const height = 220;
  const padL = 48;
  const padR = 12;
  const padT = 16;
  const padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const highs = bars.map((b) => Number(b.high));
  const lows = bars.map((b) => Number(b.low));
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const span = max - min || 1;
  const y = (price) => padT + (1 - (price - min) / span) * plotH;
  const slot = plotW / bars.length;
  const bodyW = Math.max(2, Math.min(10, slot * 0.55));
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "kline-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${symbol || ""} K线`);
  const title = document.createElementNS(ns, "title");
  title.textContent = `${symbol || ""} · ${bars.length} 根`;
  svg.appendChild(title);
  // grid / labels
  for (let i = 0; i < 4; i += 1) {
    const price = min + (span * i) / 3;
    const yy = y(price);
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", String(padL));
    line.setAttribute("x2", String(width - padR));
    line.setAttribute("y1", String(yy));
    line.setAttribute("y2", String(yy));
    line.setAttribute("class", "kline-grid");
    svg.appendChild(line);
    const label = document.createElementNS(ns, "text");
    label.setAttribute("x", String(padL - 6));
    label.setAttribute("y", String(yy + 3));
    label.setAttribute("text-anchor", "end");
    label.setAttribute("class", "kline-label");
    label.textContent = price.toFixed(2);
    svg.appendChild(label);
  }
  bars.forEach((bar, index) => {
    const open = Number(bar.open);
    const close = Number(bar.close);
    const high = Number(bar.high);
    const low = Number(bar.low);
    const cx = padL + slot * index + slot / 2;
    const up = close >= open;
    const colour = up ? "#3d9a5f" : "#c04a3c";
    const wick = document.createElementNS(ns, "line");
    wick.setAttribute("x1", String(cx));
    wick.setAttribute("x2", String(cx));
    wick.setAttribute("y1", String(y(high)));
    wick.setAttribute("y2", String(y(low)));
    wick.setAttribute("stroke", colour);
    wick.setAttribute("stroke-width", "1");
    svg.appendChild(wick);
    const top = y(Math.max(open, close));
    const bottom = y(Math.min(open, close));
    const bodyH = Math.max(1, bottom - top);
    const rect = document.createElementNS(ns, "rect");
    rect.setAttribute("x", String(cx - bodyW / 2));
    rect.setAttribute("y", String(top));
    rect.setAttribute("width", String(bodyW));
    rect.setAttribute("height", String(bodyH));
    rect.setAttribute("fill", colour);
    rect.setAttribute("title", `${bar.day} O${open} H${high} L${low} C${close}`);
    svg.appendChild(rect);
  });
  const first = bars[0].day || "";
  const last = bars[bars.length - 1].day || "";
  const axis = document.createElementNS(ns, "text");
  axis.setAttribute("x", String(padL));
  axis.setAttribute("y", String(height - 8));
  axis.setAttribute("class", "kline-label");
  axis.textContent = `${first} → ${last}`;
  svg.appendChild(axis);
  host.appendChild(svg);
}

function openPanel(tab) {
  $("panel").classList.remove("hidden");
  syncPanelWidth();
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("active", button.dataset.tab === tab);
  }
  for (const page of document.querySelectorAll(".tab-page")) {
    page.classList.toggle("hidden", page.dataset.page !== tab);
  }
  if (tab === "context") send("refresh", {});
  if (tab === "skills") send("list_skills", {});
}

/* ── wiring ────────────────────────────────────────────────────────── */

function renderPromptQueue() {
  const host = $("prompt-queue");
  if (!host) return;
  host.innerHTML = "";
  if (!state.promptQueue.length) {
    host.classList.add("hidden");
    return;
  }
  host.classList.remove("hidden");
  host.appendChild(el("div", "prompt-queue-title",
    t("queue.title", { n: state.promptQueue.length })));
  state.promptQueue.forEach((item, index) => {
    const row = el("div", "queue-item");
    const main = el("div");
    main.style.flex = "1";
    const area = document.createElement("textarea");
    area.value = item.text;
    area.placeholder = t("queue.guidePh");
    area.oninput = () => {
      item.text = area.value;
      item.display = item.refs.length
        ? `${item.refs.map((r) => `@${r}`).join(" ")}${item.text ? `\n${item.text}` : ""}`
        : item.text;
    };
    main.appendChild(area);
    if (item.images.length) {
      const thumbs = el("div", "queue-thumbs");
      item.images.forEach((img) => {
        const node = document.createElement("img");
        node.src = img.dataUrl;
        node.alt = img.name || "";
        thumbs.appendChild(node);
      });
      main.appendChild(thumbs);
    }
    const meta = el("div", "queue-item-meta");
    const steer = el("button", "queue-steer", t("queue.steer"));
    steer.type = "button";
    steer.title = t("queue.steerTitle");
    steer.onclick = () => steerQueuedItem(index);
    const bump = el("button", "", t("queue.now"));
    bump.type = "button";
    bump.title = t("queue.nowTitle");
    bump.onclick = () => {
      if (index > 0) {
        const [picked] = state.promptQueue.splice(index, 1);
        state.promptQueue.unshift(picked);
        renderPromptQueue();
      }
      if (!state.busy) flushPromptQueue();
    };
    const remove = el("button", "danger", t("queue.remove"));
    remove.type = "button";
    remove.onclick = () => {
      state.promptQueue.splice(index, 1);
      renderPromptQueue();
    };
    if (state.busy) meta.append(steer);
    meta.append(bump, remove);
    row.append(main, meta);
    host.appendChild(row);
  });
}

function steerQueuedItem(index) {
  const item = state.promptQueue[index];
  if (!item) return;
  const text = (item.text || "").trim();
  if (!text && !(item.images || []).length) {
    toast(t("queue.guidePh"), "warn");
    return;
  }
  if (!state.busy) {
    // Idle: just send as a normal prompt.
    state.promptQueue.splice(index, 1);
    renderPromptQueue();
    dispatchPrompt(item.text, item.images, item.refs, item.display);
    return;
  }
  state.promptQueue.splice(index, 1);
  renderPromptQueue();
  const display = item.display || text;
  const userIndex = transcript().querySelectorAll(".entry.user").length;
  addUser(display, (item.images || []).map((img) => ({
    name: img.name,
    dataUrl: img.dataUrl,
  })), userIndex);
  send("steer", {
    text,
    refs: item.refs || [],
    images: (item.images || []).map((img) => ({
      mime: img.mime,
      name: img.name,
      data: img.data,
    })),
  });
  toast(t("queue.steered"), "ok");
}

function enqueuePrompt(text, images, refs) {
  const display = refs.length
    ? `${refs.map((r) => `@${r}`).join(" ")}${text ? `\n${text}` : ""}`
    : text;
  state.promptQueue.push({
    text,
    images: images.slice(),
    refs: refs.slice(),
    display,
  });
  renderPromptQueue();
  toast(t("queue.added"), "ok");
}

function flushPromptQueue() {
  if (state.busy || !state.promptQueue.length) return;
  const item = state.promptQueue.shift();
  renderPromptQueue();
  dispatchPrompt(item.text, item.images, item.refs, item.display);
}

function dispatchPrompt(text, images, refs, shown) {
  const display = shown != null ? shown : (refs.length
    ? `${refs.map((r) => `@${r}`).join(" ")}${text ? `\n${text}` : ""}`
    : text);
  const userIndex = transcript().querySelectorAll(".entry.user").length;
  addUser(display, images.map((item) => ({
    name: item.name,
    dataUrl: item.dataUrl,
  })), userIndex);
  // Optimistic: the server round-trip is short, but with no line here the
  // UI looks dead for a beat and people hit Send again or Interrupt.
  state.busy = true;
  $("send").textContent = t("composer.queue");
  $("interrupt").disabled = false;
  setActivity(t("activity.sending"), "pending");
  if (state.draftTimer) {
    clearTimeout(state.draftTimer);
    state.draftTimer = null;
  }
  const payload = {
    text,
    refs,
    images: images.map((item) => ({
      mime: item.mime,
      name: item.name,
      data: item.data,
    })),
  };
  if (!send("prompt", payload)) {
    // Local UI↔backend socket is down (not the model API). Keep the bubble
    // and resend automatically when the socket comes back.
    state.pendingPrompt = payload;
    setConnBanner("error", t("conn.retrying"));
    setActivity(t("conn.waitingSend"), "pending");
    toast(t("conn.offlineSend"), "warn");
  }
}

function requestContinueWork() {
  if (state.busy) {
    toast(t("toast.busy"), "warn");
    return;
  }
  if (!send("continue_work", {})) {
    setConnBanner("error", t("conn.retrying"));
    toast(t("conn.offlineSend"), "warn");
    return;
  }
  // Optimistic busy until status/turn_start arrives (same as Send).
  state.busy = true;
  $("send").textContent = t("composer.queue");
  $("interrupt").disabled = false;
  setActivity(t("activity.sending"), "pending");
  if (state.lastTodos && state.lastTodos.length) renderTodos(state.lastTodos);
}

function submitPrompt() {
  const box = $("prompt");
  const text = box.value.trim();
  const images = state.pendingImages.slice();
  const refs = state.refs.slice();
  if (!text && !images.length && !refs.length) return;
  if (state.draftTimer) {
    clearTimeout(state.draftTimer);
    state.draftTimer = null;
  }
  box.value = "";
  box.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  clearPendingImages();
  state.refs = [];
  renderRefChips();
  $("at-menu").classList.add("hidden");
  if (state.busy) {
    enqueuePrompt(text, images, refs);
    return;
  }
  dispatchPrompt(text, images, refs);
}

function requestScreenshot() {
  if (state.capturingScreen) return;
  if (panelAttachBucket().length >= ATTACH_MAX_COUNT) {
    toast(t("composer.screenshotFull"), "warn");
    return;
  }
  state.capturingScreen = true;
  const btn = $("screenshot-button");
  if (btn) btn.disabled = true;
  const codexBtn = $("codex-screenshot");
  if (codexBtn) codexBtn.disabled = true;
  const claudeBtn = $("claude-screenshot");
  if (claudeBtn) claudeBtn.disabled = true;
  toast(t("composer.screenshotBusy"), "ok");
  send("capture_screen", { hide_self: true, interactive: true });
  // Region select can take a while; keep the button locked until reply / timeout.
  setTimeout(() => {
    if (!state.capturingScreen) return;
    state.capturingScreen = false;
    if (btn) btn.disabled = false;
  }, 120000);
}

function setUiMode(mode) {
  state.uiMode = (mode === "codex" || mode === "claude") ? mode : "agent";
  const app = $("app");
  if (app) {
    app.classList.toggle("mode-codex", state.uiMode === "codex");
    app.classList.toggle("mode-claude", state.uiMode === "claude");
    app.classList.toggle("mode-agent", state.uiMode === "agent");
  }
  const agentMain = $("main");
  const codexMain = $("codex-main");
  const claudeMain = $("claude-main");
  if (agentMain) agentMain.classList.toggle("hidden", state.uiMode !== "agent");
  if (codexMain) codexMain.classList.toggle("hidden", state.uiMode !== "codex");
  if (claudeMain) claudeMain.classList.toggle("hidden", state.uiMode !== "claude");
  const agentBtn = $("mode-agent");
  const codexBtn = $("mode-codex");
  const claudeBtn = $("mode-claude");
  if (agentBtn) agentBtn.classList.toggle("active", state.uiMode === "agent");
  if (codexBtn) codexBtn.classList.toggle("active", state.uiMode === "codex");
  if (claudeBtn) claudeBtn.classList.toggle("active", state.uiMode === "claude");
  if (state.uiMode === "codex") {
    applyCodexStatus(state.codex);
    if (state.codex.sessions) renderSessions(state.codex.sessions);
    if (panelTranscriptIsEmpty("codex")) {
      const id = state.viewCodexSessionId
        || (state.codex && (state.codex.viewed_id || state.codex.panel_session_id))
        || "";
      if (id) {
        state.codexReplayPending = true;
        send("codex_open_session", { id });
      }
    }
    const box = $("codex-prompt");
    if (box) box.focus();
  } else if (state.uiMode === "claude") {
    applyClaudeStatus(state.claude);
    if (state.claude.sessions) renderSessions(state.claude.sessions);
    if (panelTranscriptIsEmpty("claude")) {
      const id = state.viewClaudeSessionId
        || (state.claude && (state.claude.viewed_id || state.claude.panel_session_id))
        || "";
      if (id) {
        state.claudeReplayPending = true;
        send("claude_open_session", { id });
      }
    }
    const box = $("claude-prompt");
    if (box) box.focus();
  } else {
    send("refresh", { transcript: false });
  }
}

function renderCodexProfileSelect() {
  const select = $("codex-profile-select");
  if (!select) return;
  const profiles = state.codex.profiles || [];
  const current = state.codex.selection || state.codex.profile_id || "default";
  const options = [`<option value="default">${escapeHtml(t("codex.homeDefault"))}</option>`];
  for (const p of profiles) {
    const bits = [];
    if (p.has_secret) bits.push("key");
    else if (p.env_key) bits.push(`$${p.env_key}`);
    if (p.proxy_label && p.proxy_label !== "跟随系统" && p.proxy_label !== "system") {
      bits.push(p.proxy_label);
    } else if (p.proxy === "direct") {
      bits.push("直连");
    }
    const mark = bits.length ? ` · ${bits.join(" · ")}` : "";
    options.push(
      `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}${escapeHtml(mark)}</option>`
    );
  }
  select.innerHTML = options.join("");
  select.value = current === "default" ? "default" : current;
  if (select.value !== current && current !== "default") {
    // Fall back if profile missing.
    if (profiles.length) select.value = profiles[0].id;
  }
  const tpl = $("codex-tpl-select");
  if (tpl) {
    const templates = state.codex.templates || [];
    const prev = tpl.value;
    tpl.innerHTML = templates.map((item) =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}</option>`
    ).join("") || '<option value="custom">Custom</option>';
    if (prev) tpl.value = prev;
  }
  renderCodexModelSelect();
  renderImportAccountSelect("codex");
  const hint = $("codex-default-hint");
  if (hint) {
    const isDefault = (state.codex.selection || state.codex.home_kind) === "default";
    if (isDefault) {
      hint.className = "conn-banner";
      hint.textContent = t("codex.defaultHint");
    } else {
      hint.className = "conn-banner hidden";
      hint.textContent = "";
    }
  }
}

function renderImportAccountSelect(panel) {
  const select = $(panel === "claude" ? "claude-import-account" : "codex-import-account");
  if (!select) return;
  const source = panel === "claude" ? state.claude : state.codex;
  const accounts = (source && source.agent_accounts) || [];
  const prev = select.value;
  if (!accounts.length) {
    select.innerHTML = `<option value="">${escapeHtml(t("codex.importEmpty"))}</option>`;
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = accounts.map((account) => {
    const mark = account.has_key ? " · key" : "";
    const label = `${account.id}${account.note ? ` · ${account.note}` : ""}${mark}`;
    return `<option value="${escapeHtml(account.id)}">${escapeHtml(label)}</option>`;
  }).join("");
  if (prev && accounts.some((item) => item.id === prev)) select.value = prev;
}

function renderCodexModelSelect() {
  const select = $("codex-model-select");
  if (!select) return;
  const models = state.codex.models || [];
  const current = state.codex.selected_model || state.codex.model || "";
  if (!models.length) {
    select.innerHTML = `<option value="">${escapeHtml(current || t("codex.modelPick"))}</option>`;
    if (current) {
      select.innerHTML = `<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`;
      select.value = current;
    }
    select.disabled = !current;
    renderCodexEffortSelect();
    return;
  }
  const seen = new Set();
  const options = [];
  for (const m of models) {
    const id = m.id || m.model || "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    options.push(
      `<option value="${escapeHtml(id)}">${escapeHtml(m.label || id)}</option>`
    );
  }
  if (current && !seen.has(current)) {
    options.unshift(`<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`);
  }
  select.innerHTML = options.join("") || `<option value="">${escapeHtml(t("codex.modelPick"))}</option>`;
  select.disabled = false;
  if (current) select.value = current;
  renderCodexEffortSelect();
}

function renderCodexEffortSelect() {
  const select = $("codex-effort-select");
  if (!select) return;
  const levels = state.codex.effort_levels || [];
  const current = state.codex.selected_effort || "";
  if (!levels.length) {
    select.innerHTML = `<option value="">${escapeHtml(t("composer.effort") || "effort")}</option>`;
    select.disabled = true;
    return;
  }
  select.innerHTML = levels.map((level) =>
    `<option value="${escapeHtml(level)}">effort: ${escapeHtml(level)}</option>`
  ).join("");
  select.disabled = state.codex.busy || levels.length <= 1;
  if (current && levels.includes(current)) select.value = current;
  else select.value = levels[levels.length - 1] || "";
}

function applyCodexStatus(msg) {
  if (!msg) return;
  const prevView = state.viewCodexSessionId;
  state.codex = { ...state.codex, ...msg };
  if (state.codex.viewed_id || state.codex.panel_session_id) {
    state.viewCodexSessionId = state.codex.viewed_id || state.codex.panel_session_id;
  }
  const label = t(`codex.state.${state.codex.state || "stopped"}`) || state.codex.state || "—";
  const statusText = $("codex-status-text");
  if (statusText) {
    const sel = state.codex.selection || state.codex.home_kind || "—";
    statusText.textContent = state.codex.error
      ? `${label} · ${state.codex.error}`
      : `${label} · ${sel}`;
  }
  renderCodexProfileSelect();
  renderPanelWorkspaceChips("codex");
  syncPanelModeSelect("codex");
  const modelStat = $("codex-model-stat");
  if (modelStat) {
    const model = state.codex.selected_model || state.codex.model;
    const effort = state.codex.selected_effort;
    const parts = [state.codex.model_provider, model, effort].filter(Boolean);
    modelStat.textContent = parts.length ? parts.join(" · ") : "—";
  }
  const modelSelect = $("codex-model-select");
  if (modelSelect) modelSelect.disabled = !!state.codex.busy;
  renderCodexEffortSelect();
  const interrupt = $("codex-interrupt");
  if (interrupt) interrupt.disabled = !state.codex.busy;
  const sendBtn = $("codex-send");
  if (sendBtn) sendBtn.disabled = state.codex.state === "starting";
  const banner = $("codex-conn-banner");
  if (banner) {
    if (state.codex.state === "error" && state.codex.error) {
      banner.className = "conn-banner error";
      banner.textContent = state.codex.error;
    } else if (state.codex.state === "starting") {
      banner.className = "conn-banner";
      banner.textContent = t("codex.state.starting");
    } else {
      banner.className = "conn-banner hidden";
      banner.textContent = "";
    }
  }
  if (state.codex.sessions && state.uiMode === "codex") {
    renderSessions(state.codex.sessions);
  }
  const nextView = state.viewCodexSessionId;
  // Replay when the view changed, the UI asked for it, or the panel is still
  // blank (status-without-transcript can set viewed_id before history arrives).
  if (
    Array.isArray(msg.transcript)
    && nextView
    && (
      nextView !== prevView
      || state.codexReplayPending
      || panelTranscriptIsEmpty("codex")
    )
  ) {
    state.codexReplayPending = false;
    replayPanelTranscript("codex", msg.transcript);
  }
}

function renderClaudeModelSelect() {
  const select = $("claude-model-select");
  if (!select) return;
  const models = state.claude.models || [];
  const current = state.claude.selected_model || state.claude.model || "";
  if (!models.length) {
    select.innerHTML = `<option value="">${escapeHtml(current || t("claude.modelPick"))}</option>`;
    if (current) {
      select.innerHTML = `<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`;
      select.value = current;
    }
    select.disabled = !current;
    renderClaudeEffortSelect();
    return;
  }
  const seen = new Set();
  const options = [];
  for (const m of models) {
    const id = m.id || m.model || "";
    if (!id || seen.has(id)) continue;
    seen.add(id);
    options.push(
      `<option value="${escapeHtml(id)}">${escapeHtml(m.label || id)}</option>`
    );
  }
  if (current && !seen.has(current)) {
    options.unshift(`<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`);
  }
  select.innerHTML = options.join("") || `<option value="">${escapeHtml(t("claude.modelPick"))}</option>`;
  select.disabled = !!state.claude.busy;
  if (current) select.value = current;
  renderClaudeEffortSelect();
}

function renderClaudeEffortSelect() {
  const select = $("claude-effort-select");
  if (!select) return;
  const levels = state.claude.effort_levels || [];
  const current = state.claude.selected_effort || "";
  if (!levels.length) {
    select.innerHTML = `<option value="">${escapeHtml(t("composer.effort") || "effort")}</option>`;
    select.disabled = true;
    return;
  }
  select.innerHTML = levels.map((level) =>
    `<option value="${escapeHtml(level)}">effort: ${escapeHtml(level)}</option>`
  ).join("");
  select.disabled = state.claude.busy || levels.length <= 1;
  if (current && levels.includes(current)) select.value = current;
  else select.value = levels[levels.length - 1] || "";
}

function applyClaudeStatus(msg) {
  if (!msg) return;
  const prevView = state.viewClaudeSessionId;
  const wasBusy = !!(state.claude && state.claude.busy);
  state.claude = { ...state.claude, ...msg };
  if (state.claude.viewed_id || state.claude.panel_session_id) {
    state.viewClaudeSessionId = state.claude.viewed_id || state.claude.panel_session_id;
  }
  // Drop a stuck "系统: status" pulse when the runtime reports idle.
  if (wasBusy && !state.claude.busy) clearPanelActivity("claude");
  else if (!state.claude.busy) {
    const dock = $("claude-activity-dock");
    const row = dock && dock.querySelector(".activity");
    if (row && /^系统[：:]/i.test(row.textContent || "")) clearPanelActivity("claude");
  }
  const label = t(`claude.state.${state.claude.state || "stopped"}`) || state.claude.state || "—";
  const statusText = $("claude-status-text");
  if (statusText) {
    const sel = state.claude.selection || state.claude.profile_id || "—";
    statusText.textContent = state.claude.error
      ? `${label} · ${state.claude.error}`
      : `${label} · ${sel}`;
  }
  renderClaudeProfileSelect();
  renderPanelWorkspaceChips("claude");
  syncPanelModeSelect("claude");
  const modelStat = $("claude-model-stat");
  if (modelStat) {
    const model = state.claude.selected_model || state.claude.model;
    const effort = state.claude.selected_effort;
    const parts = [
      state.claude.auth_mode === "login" ? "login" : "api",
      model,
      effort,
    ].filter(Boolean);
    modelStat.textContent = parts.length ? parts.join(" · ") : "—";
  }
  const modelSelect = $("claude-model-select");
  if (modelSelect) modelSelect.disabled = !!state.claude.busy;
  renderClaudeModelSelect();
  const interrupt = $("claude-interrupt");
  if (interrupt) interrupt.disabled = !state.claude.busy;
  const sendBtn = $("claude-send");
  if (sendBtn) sendBtn.disabled = state.claude.state === "starting";
  const banner = $("claude-conn-banner");
  if (banner) {
    if (state.claude.state === "error" && state.claude.error) {
      banner.className = "conn-banner error";
      banner.textContent = state.claude.error;
    } else if (state.claude.state === "starting") {
      banner.className = "conn-banner";
      banner.textContent = t("claude.state.starting");
    } else {
      banner.className = "conn-banner hidden";
      banner.textContent = "";
    }
  }
  if (state.claude.sessions && state.uiMode === "claude") {
    renderSessions(state.claude.sessions);
  }
  const nextView = state.viewClaudeSessionId;
  // Replay when the view changed, the UI asked for it, or the panel is still
  // blank (status-without-transcript can set viewed_id before history arrives).
  if (
    Array.isArray(msg.transcript)
    && nextView
    && (
      nextView !== prevView
      || state.claudeReplayPending
      || panelTranscriptIsEmpty("claude")
    )
  ) {
    state.claudeReplayPending = false;
    replayPanelTranscript("claude", msg.transcript);
  }
}

function panelAttachBucket() {
  if (state.uiMode === "codex") return state.codexAttachments;
  if (state.uiMode === "claude") return state.claudeAttachments;
  return state.pendingImages;
}

function renderPanelAttachStrip(mode) {
  const strip = $(mode === "claude" ? "claude-attach-strip" : "codex-attach-strip");
  if (!strip) return;
  const items = mode === "claude" ? state.claudeAttachments : state.codexAttachments;
  strip.innerHTML = "";
  if (!items.length) {
    strip.classList.add("hidden");
    return;
  }
  strip.classList.remove("hidden");
  items.forEach((image, index) => {
    const chip = el("div", "attach-chip");
    const img = el("img");
    img.src = image.dataUrl || image.data_url || "";
    img.alt = image.name || `img-${index + 1}`;
    chip.appendChild(img);
    const remove = el("button", "attach-remove", "×");
    remove.type = "button";
    remove.onclick = () => {
      items.splice(index, 1);
      renderPanelAttachStrip(mode);
    };
    chip.appendChild(remove);
    strip.appendChild(chip);
  });
}

function renderClaudeProfileSelect() {
  const select = $("claude-profile-select");
  if (!select) return;
  const profiles = state.claude.profiles || [];
  const current = state.claude.selection || state.claude.profile_id || "anthropic";
  select.innerHTML = profiles.map((p) => {
    const bits = [];
    if (p.auth_mode === "login") bits.push("login");
    else if (p.has_secret) bits.push("key");
    else if (p.env_key) bits.push(`$${p.env_key}`);
    if (p.proxy === "direct" || p.proxy_label === "直连" || p.proxy_label === "direct") {
      bits.push("直连");
    } else if (p.proxy_label && p.proxy_label !== "跟随系统" && p.proxy_label !== "system") {
      bits.push(p.proxy_label);
    }
    const mark = bits.length ? ` · ${bits.join(" · ")}` : "";
    return `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name || p.id)}${escapeHtml(mark)}</option>`;
  }).join("") || '<option value="anthropic">Anthropic · API Key</option><option value="login">Anthropic · 订阅登录</option>';
  select.value = current;
  const tpl = $("claude-tpl-select");
  if (tpl) {
    const templates = state.claude.templates || [];
    const prev = tpl.value;
    tpl.innerHTML = templates.map((item) =>
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}</option>`
    ).join("") || '<option value="anthropic">Anthropic</option>';
    if (prev) tpl.value = prev;
  }
  renderImportAccountSelect("claude");
  const loginBtn = $("claude-login");
  if (loginBtn) {
    const mode = state.claude.auth_mode || "";
    const selected = profiles.find((p) => p.id === select.value);
    const needsLogin = mode === "login" || (selected && selected.auth_mode === "login");
    loginBtn.classList.toggle("hidden", false);
    loginBtn.title = needsLogin
      ? "打开 Claude 订阅登录"
      : "也可登录订阅账号（会切换到登录 Profile）";
  }
}

function suggestNewProfileId(panel, base) {
  const profiles = ((panel === "claude" ? state.claude : state.codex).profiles) || [];
  const ids = new Set(profiles.map((item) => item.id));
  if (!ids.has(base)) return base;
  for (let index = 2; index < 100; index += 1) {
    const candidate = `${base}-${index}`;
    if (!ids.has(candidate)) return candidate;
  }
  return `${base}-${Date.now()}`;
}

function prepareNewProfileForm(panel) {
  const tplSelect = $(panel === "claude" ? "claude-tpl-select" : "codex-tpl-select");
  const idBox = $(panel === "claude" ? "claude-pf-id" : "codex-pf-id");
  const nameBox = $(panel === "claude" ? "claude-pf-name" : "codex-pf-name");
  const envBox = $(panel === "claude" ? "claude-pf-env" : "codex-pf-env");
  const tplId = (tplSelect && tplSelect.value) || (panel === "claude" ? "anthropic" : "kimi");
  const base = tplId === "custom" ? "profile" : tplId;
  const nextId = suggestNewProfileId(panel, base);
  if (idBox) idBox.value = nextId;
  if (nameBox) nameBox.value = nextId;
  if (envBox) envBox.value = "";
  if (panel === "claude") fillClaudeProfileFormFromTemplate();
  else fillCodexProfileFormFromTemplate();
  // fill* reloads selected profile; force the new id/name after.
  if (idBox) idBox.value = nextId;
  if (nameBox) nameBox.value = nextId;
  if (envBox) envBox.value = "";
  const templates = ((panel === "claude" ? state.claude : state.codex).templates) || [];
  const tpl = templates.find((item) => item.id === tplId) || {};
  const urlBox = $(panel === "claude" ? "claude-pf-url" : "codex-pf-url");
  const modelBox = $(panel === "claude" ? "claude-pf-model" : "codex-pf-model");
  if (urlBox) urlBox.value = tpl.base_url || "";
  if (modelBox) modelBox.value = tpl.model || "";
  if (envBox && tpl.env_key) envBox.placeholder = tpl.env_key;
}

function fillClaudeProfileFormFromTemplate() {
  const tplId = ($("claude-tpl-select") && $("claude-tpl-select").value) || "anthropic";
  const templates = state.claude.templates || [];
  const tpl = templates.find((item) => item.id === tplId) || {};
  const profiles = state.claude.profiles || [];
  const selectedId = ($("claude-profile-select") && $("claude-profile-select").value) || "";
  const selected = profiles.find((item) => item.id === selectedId);
  const idBox = $("claude-pf-id");
  const nameBox = $("claude-pf-name");
  const urlBox = $("claude-pf-url");
  const modelBox = $("claude-pf-model");
  const envBox = $("claude-pf-env");
  const proxyBox = $("claude-pf-proxy");
  if (selected && selected.id) {
    if (idBox) idBox.value = selected.id;
    if (nameBox) nameBox.value = selected.name || "";
    if (urlBox) urlBox.value = selected.base_url || "";
    if (modelBox) modelBox.value = selected.model || "";
    if (envBox) envBox.value = selected.env_key || "";
    if (proxyBox) proxyBox.value = selected.proxy || "";
    if ($("claude-tpl-select") && selected.template) {
      $("claude-tpl-select").value = selected.template;
    }
    return;
  }
  if (idBox && !idBox.value) idBox.value = tplId === "custom" ? "" : `${tplId}-1`;
  if (nameBox && !nameBox.value) nameBox.value = tpl.name || "";
  if (urlBox) urlBox.value = tpl.base_url || urlBox.value || "";
  if (modelBox) modelBox.value = tpl.model || modelBox.value || "";
  if (envBox && !envBox.value) envBox.value = tpl.env_key || "ANTHROPIC_API_KEY";
  if (proxyBox && !proxyBox.value) proxyBox.value = "";
}

function panelImagesPayload(mode) {
  const items = mode === "claude" ? state.claudeAttachments : state.codexAttachments;
  return items.map((image) => ({
    data_url: image.dataUrl || image.data_url || "",
    mime: image.mime || "image/png",
    name: image.name || "image.png",
    path: image.path || "",
  }));
}

function panelPasteImages(event, mode) {
  const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
  const imageFiles = files.filter((file) => ATTACH_MIME.has((file.type || "").toLowerCase()));
  if (!imageFiles.length) return;
  event.preventDefault();
  enqueuePanelImageFiles(imageFiles, mode);
}

function wirePanelDrop(host, mode) {
  if (!host) return;
  host.ondragenter = (event) => { event.preventDefault(); host.classList.add("drag-over"); };
  host.ondragover = (event) => { event.preventDefault(); host.classList.add("drag-over"); };
  host.ondragleave = (event) => {
    if (!host.contains(event.relatedTarget)) host.classList.remove("drag-over");
  };
  host.ondrop = (event) => {
    event.preventDefault();
    host.classList.remove("drag-over");
    enqueuePanelImageFiles(event.dataTransfer && event.dataTransfer.files, mode);
  };
}

function enqueuePanelImageFiles(fileList, mode) {
  const files = Array.from(fileList || []).filter((file) => ATTACH_MIME.has((file.type || "").toLowerCase()));
  if (!files.length) return;
  const bucket = mode === "claude" ? state.claudeAttachments : state.codexAttachments;
  files.forEach((file) => {
    if (bucket.length >= ATTACH_MAX_COUNT) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const base64 = dataUrl.includes(",") ? dataUrl.split(",")[1] : "";
      bucket.push({
        mime: (file.type || "image/png").toLowerCase(),
        name: file.name || "image.png",
        data: base64,
        dataUrl,
        data_url: dataUrl,
      });
      renderPanelAttachStrip(mode);
    };
    reader.readAsDataURL(file);
  });
}

function codexTranscript() {
  return $("codex-transcript");
}

function panelTranscript(mode) {
  return mode === "claude" ? claudeTranscript() : codexTranscript();
}

function panelTools(mode) {
  return mode === "claude" ? state.claudeTools : state.codexTools;
}

function setPanelActivity(mode, text, kind) {
  const dockId = mode === "claude" ? "claude-activity-dock" : "codex-activity-dock";
  const dock = $(dockId);
  if (!dock) return;
  if (!text || kind === "clear") {
    dock.innerHTML = "";
    dock.classList.add("hidden");
    return;
  }
  dock.classList.remove("hidden");
  let row = dock.querySelector(".activity");
  if (!row) {
    row = el("div", `activity ${kind || "busy"}`, text);
    dock.appendChild(row);
    return;
  }
  // Skip no-op updates — stream used to rewrite identical "回答中…" thousands of times.
  const nextKind = kind || "busy";
  if (row.textContent === text && row.className === `activity ${nextKind}`) return;
  row.className = `activity ${nextKind}`;
  row.textContent = text;
}

function clearPanelActivity(mode) {
  setPanelActivity(mode, "", "clear");
}

function addPanelToolEntry(mode, msg) {
  const host = panelTranscript(mode);
  const tools = panelTools(mode);
  if (!host || !msg || !msg.call_id) return;
  if (tools.has(msg.call_id)) return;
  const box = el("div", "entry tool running");
  const head = el("div", "tool-head");
  head.append(
    el("span", "tool-glyph", "⟳"),
    el("span", "tool-name", msg.headline || msg.name || "tool"),
    el("span", "tool-summary", ""),
    el("span", "tool-time", ""),
  );
  const body = el("div", "tool-body hidden");
  head.onclick = () => body.classList.toggle("hidden");
  box.append(head, body);
  tools.set(msg.call_id, { box, head, body, args: msg.args || {} });
  host.appendChild(box);
  host.scrollTop = host.scrollHeight;
}

function completePanelToolEntry(mode, msg) {
  const tools = panelTools(mode);
  if (!msg || !msg.call_id) return;
  const entry = tools.get(msg.call_id);
  if (!entry) return;
  tools.delete(msg.call_id);
  entry.box.classList.remove("running");
  entry.box.classList.add(msg.is_error ? "failed" : "ok");
  entry.head.children[0].textContent = msg.is_error ? "✗" : "✓";
  entry.head.children[2].textContent = msg.summary || msg.name || "";
  if (msg.duration) {
    entry.head.children[3].textContent = `${Number(msg.duration).toFixed(1)}s`;
  }
  entry.body.textContent = msg.content || "";
  // Match Agent: keep successful tool output collapsed; only expand failures.
  // Click the header to open details.
  if (msg.is_error) entry.body.classList.remove("hidden");
  else entry.body.classList.add("hidden");
  if (msg.content && !entry.head.querySelector(".copy-btn")) {
    const copy = el("button", "copy-btn", t("copy"));
    copy.type = "button";
    copy.title = t("copyTool");
    copy.onclick = (event) => {
      event.stopPropagation();
      copyText(msg.content, copy);
    };
    entry.head.appendChild(copy);
  }
  if (!msg.is_error) regroupPanelTools(entry.box);
  const host = panelTranscript(mode);
  if (host) host.scrollTop = host.scrollHeight;
}

/** Fold consecutive finished Codex/Claude tool rows (same idea as Agent regroupTools). */
function regroupPanelTools(box) {
  const previous = box.previousElementSibling;
  const existing = groupOf(previous);
  if (existing) {
    existing.querySelector(".tool-group-body").appendChild(box);
    countGroup(existing);
    return;
  }
  if (!previous || !previous.classList.contains("tool") ||
      previous.classList.contains("running") ||
      previous.classList.contains("failed")) {
    return;
  }
  const group = el("div", "entry tool-group");
  const head = el("div", "tool-group-head");
  head.append(el("span", "tool-glyph", "›"), el("span", "tool-group-label", ""));
  const body = el("div", "tool-group-body hidden");
  head.onclick = () => {
    body.classList.toggle("hidden");
    head.querySelector(".tool-glyph").textContent =
      body.classList.contains("hidden") ? "›" : "⌄";
  };
  group.append(head, body);
  previous.replaceWith(group);
  body.append(previous, box);
  countGroup(group);
}

function appendPanelThinking(mode, delta) {
  if (!delta) return;
  const host = panelTranscript(mode);
  if (!host) return;
  const boxKey = mode === "claude" ? "claudeThinking" : "codexThinking";
  const bufKey = mode === "claude" ? "claudeThinkingBuf" : "codexThinkingBuf";
  const frameKey = mode === "claude" ? "claudeThinkingFrame" : "codexThinkingFrame";
  state[bufKey] = (state[bufKey] || "") + delta;
  let box = state[boxKey];
  if (!box || !host.contains(box)) {
    box = el("div", "entry thinking collapsed");
    const head = el("div", "thinking-head");
    head.append(el("span", "thinking-glyph", "›"), el("span", "thinking-label", "thinking"));
    const body = el("div", "thinking-body");
    head.onclick = () => box.classList.toggle("collapsed");
    box.append(head, body);
    host.appendChild(box);
    state[boxKey] = box;
  }
  if (state[frameKey]) return;
  state[frameKey] = requestAnimationFrame(() => {
    state[frameKey] = 0;
    const live = state[boxKey];
    const body = live && live.querySelector(".thinking-body");
    if (body) body.textContent = state[bufKey] || "";
    if (host && atPanelBottom(mode)) host.scrollTop = host.scrollHeight;
  });
}

function atPanelBottom(mode) {
  const host = panelTranscript(mode);
  if (!host) return true;
  return host.scrollHeight - host.scrollTop - host.clientHeight < 80;
}

function addCodexEntry(kind, text) {
  const host = codexTranscript();
  if (!host || !text) return null;
  const node = el("div", `entry ${kind}`);
  node.innerHTML = kind === "assistant"
    ? renderMarkdown(text)
    : `<p>${escapeHtml(text)}</p>`;
  host.appendChild(node);
  host.scrollTop = host.scrollHeight;
  return node;
}

function appendCodexText(delta) {
  if (!delta) return;
  state.codexBuffer = (state.codexBuffer || "") + delta;
  const host = codexTranscript();
  if (!host) return;
  if (!state.codexStreaming || !host.contains(state.codexStreaming)) {
    const node = el("div", "entry assistant");
    node.textContent = state.codexBuffer;
    host.appendChild(node);
    state.codexStreaming = node;
  }
  if (state.codexStreamFrame) return;
  state.codexStreamFrame = requestAnimationFrame(() => {
    state.codexStreamFrame = 0;
    if (state.codexStreaming) {
      state.codexStreaming.textContent = state.codexBuffer;
      if (atPanelBottom("codex")) host.scrollTop = host.scrollHeight;
    }
  });
}

function finishCodexStream() {
  if (state.codexStreamFrame) {
    cancelAnimationFrame(state.codexStreamFrame);
    state.codexStreamFrame = 0;
  }
  if (state.codexStreaming && state.codexBuffer) {
    state.codexStreaming.innerHTML = renderMarkdown(state.codexBuffer);
  }
  state.codexStreaming = null;
  state.codexBuffer = "";
  state.codexThinking = null;
  state.codexThinkingBuf = "";
}

function submitCodexPrompt() {
  const box = $("codex-prompt");
  if (!box) return;
  const text = (box.value || "").trim();
  const images = panelImagesPayload("codex");
  if (!text && !images.length) return;
  addCodexEntry("user", text || `(${images.length} image)`);
  box.value = "";
  box.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  state.codexAttachments = [];
  renderPanelAttachStrip("codex");
  state.codex.busy = true;
  state.codexThinking = null;
  setPanelActivity("codex", t("activity.sending") || "发送中…", "pending");
  applyCodexStatus({ ...state.codex, busy: true });
  if (!send("codex_prompt", { text, images })) {
    toast(t("conn.offlineSend"), "warn");
  }
}

function showCodexPermissionInline(msg) {
  const host = codexTranscript();
  if (!host) return;
  const card = el("div", "entry hitl");
  const title = el("div", "hitl-title", t("codex.hitlTitle"));
  card.appendChild(title);
  if (msg.reason) card.appendChild(el("div", "hint", msg.reason));
  if (msg.detail) card.appendChild(el("div", "modal-code", String(msg.detail)));
  const actions = el("div", "hitl-actions");
  const finish = (decision) => {
    card.remove();
    send("codex_approve", { id: msg.id, decision });
  };
  actions.append(
    hitlButton(t("codex.decline"), false, () => finish("decline")),
    hitlButton(t("codex.acceptSession"), false, () => finish("acceptForSession")),
    hitlButton(t("codex.accept"), true, () => finish("accept")),
  );
  card.appendChild(actions);
  host.appendChild(card);
  host.scrollTop = host.scrollHeight;
}

function claudeTranscript() {
  return $("claude-transcript");
}

function addClaudeEntry(kind, text) {
  const host = claudeTranscript();
  if (!host || !text) return null;
  const node = el("div", `entry ${kind}`);
  node.innerHTML = kind === "assistant"
    ? renderMarkdown(text)
    : `<p>${escapeHtml(text)}</p>`;
  host.appendChild(node);
  host.scrollTop = host.scrollHeight;
  return node;
}

function appendClaudeText(delta) {
  if (!delta) return;
  state.claudeBuffer = (state.claudeBuffer || "") + delta;
  const host = claudeTranscript();
  if (!host) return;
  if (!state.claudeStreaming || !host.contains(state.claudeStreaming)) {
    const node = el("div", "entry assistant");
    node.textContent = state.claudeBuffer;
    host.appendChild(node);
    state.claudeStreaming = node;
  }
  if (state.claudeStreamFrame) return;
  state.claudeStreamFrame = requestAnimationFrame(() => {
    state.claudeStreamFrame = 0;
    if (state.claudeStreaming) {
      state.claudeStreaming.textContent = state.claudeBuffer;
      if (atPanelBottom("claude")) host.scrollTop = host.scrollHeight;
    }
  });
}

function finishClaudeStream() {
  if (state.claudeStreamFrame) {
    cancelAnimationFrame(state.claudeStreamFrame);
    state.claudeStreamFrame = 0;
  }
  if (state.claudeStreaming && state.claudeBuffer) {
    state.claudeStreaming.innerHTML = renderMarkdown(state.claudeBuffer);
  }
  state.claudeStreaming = null;
  state.claudeBuffer = "";
  state.claudeThinking = null;
  state.claudeThinkingBuf = "";
}

function submitClaudePrompt() {
  const box = $("claude-prompt");
  if (!box) return;
  const text = (box.value || "").trim();
  const images = panelImagesPayload("claude");
  if (!text && !images.length) return;
  addClaudeEntry("user", text || `(${images.length} image)`);
  box.value = "";
  box.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  state.claudeAttachments = [];
  renderPanelAttachStrip("claude");
  state.claude.busy = true;
  state.claudeThinking = null;
  setPanelActivity("claude", t("activity.sending") || "发送中…", "pending");
  applyClaudeStatus({ ...state.claude, busy: true });
  if (!send("claude_prompt", { text, images })) {
    toast(t("conn.offlineSend"), "warn");
  }
}

function showClaudePermissionInline(msg) {
  const host = claudeTranscript();
  if (!host) return;
  const card = el("div", "entry hitl");
  card.appendChild(el("div", "hitl-title", t("claude.hitlTitle")));
  if (msg.reason) card.appendChild(el("div", "hint", msg.reason));
  if (msg.detail) card.appendChild(el("div", "modal-code", String(msg.detail)));
  const actions = el("div", "hitl-actions");
  const finish = (decision) => {
    card.remove();
    send("claude_approve", { id: msg.id, decision });
  };
  actions.append(
    hitlButton(t("claude.decline"), false, () => finish("deny")),
    hitlButton(t("claude.accept"), true, () => finish("accept")),
  );
  card.appendChild(actions);
  host.appendChild(card);
  host.scrollTop = host.scrollHeight;
}

function fillCodexProfileFormFromTemplate() {
  const tplId = ($("codex-tpl-select") && $("codex-tpl-select").value) || "custom";
  const templates = state.codex.templates || [];
  const tpl = templates.find((item) => item.id === tplId) || {};
  const profiles = state.codex.profiles || [];
  const selectedId = ($("codex-profile-select") && $("codex-profile-select").value) || "";
  const selected = profiles.find((item) => item.id === selectedId);
  const idBox = $("codex-pf-id");
  const nameBox = $("codex-pf-name");
  const urlBox = $("codex-pf-url");
  const modelBox = $("codex-pf-model");
  const envBox = $("codex-pf-env");
  const proxyBox = $("codex-pf-proxy");
  if (selected && selected.id && selected.id !== "default") {
    if (idBox) idBox.value = selected.id;
    if (nameBox) nameBox.value = selected.name || "";
    if (urlBox) urlBox.value = selected.base_url || "";
    if (modelBox) modelBox.value = selected.model || "";
    if (envBox) envBox.value = selected.env_key || "";
    if (proxyBox) proxyBox.value = selected.proxy || "";
    if ($("codex-tpl-select") && selected.template) {
      $("codex-tpl-select").value = selected.template;
    }
    return;
  }
  if (idBox && !idBox.value) idBox.value = tplId === "custom" ? "" : `${tplId}-1`;
  if (nameBox && !nameBox.value) nameBox.value = tpl.name || "";
  if (urlBox) urlBox.value = tpl.base_url || urlBox.value;
  if (modelBox) modelBox.value = tpl.model || modelBox.value;
  if (envBox && !envBox.value) envBox.value = tpl.env_key || "";
  if (proxyBox && !proxyBox.value) {
    // Domestic APIs usually prefer direct; foreign ones can use Clash.
    proxyBox.value = (tplId === "kimi" || tplId === "glm") ? "direct" : "";
  }
}

function wire() {
  wireImageEditor();
  wireImageViewer();
  setUiMode("agent");
  const modeAgent = $("mode-agent");
  const modeCodex = $("mode-codex");
  const modeClaude = $("mode-claude");
  if (modeAgent) modeAgent.onclick = () => setUiMode("agent");
  if (modeCodex) {
    modeCodex.onclick = () => {
      setUiMode("codex");
      send("codex_start", {});
    };
  }
  if (modeClaude) {
    modeClaude.onclick = () => {
      setUiMode("claude");
      send("claude_start", {});
    };
  }
  const codexProfile = $("codex-profile-select");
  if (codexProfile) {
    codexProfile.onchange = () => {
      send("codex_set_profile", { selection: codexProfile.value || "default" });
    };
  }
  const codexModel = $("codex-model-select");
  if (codexModel) {
    codexModel.onchange = () => {
      const value = codexModel.value || "";
      if (!value) return;
      send("codex_set_model", { model: value });
    };
  }
  const codexEffort = $("codex-effort-select");
  if (codexEffort) {
    codexEffort.onchange = () => {
      send("codex_set_effort", { effort: codexEffort.value || "" });
    };
  }
  const profileEdit = $("codex-profile-edit");
  if (profileEdit) {
    profileEdit.onclick = () => {
      const form = $("codex-profile-form");
      if (!form) return;
      form.classList.toggle("hidden");
      if (!form.classList.contains("hidden")) {
        renderCodexProfileSelect();
        fillCodexProfileFormFromTemplate();
      }
    };
  }
  const tplSelect = $("codex-tpl-select");
  if (tplSelect) {
    tplSelect.onchange = () => {
      const idBox = $("codex-pf-id");
      const nameBox = $("codex-pf-name");
      if (idBox) idBox.value = "";
      if (nameBox) nameBox.value = "";
      fillCodexProfileFormFromTemplate();
    };
  }
  const pfSave = $("codex-pf-save");
  if (pfSave) {
    pfSave.onclick = () => {
      const tpl = ($("codex-tpl-select") && $("codex-tpl-select").value) || "custom";
      const keyOrEnv = (($("codex-pf-env") && $("codex-pf-env").value) || "").trim();
      const payload = {
        id: (($("codex-pf-id") && $("codex-pf-id").value) || "").trim(),
        name: (($("codex-pf-name") && $("codex-pf-name").value) || "").trim(),
        base_url: (($("codex-pf-url") && $("codex-pf-url").value) || "").trim(),
        model: (($("codex-pf-model") && $("codex-pf-model").value) || "").trim(),
        proxy: (($("codex-pf-proxy") && $("codex-pf-proxy").value) || "").trim(),
        template: tpl,
        make_active: true,
        activate: true,
      };
      if (/^[A-Z][A-Z0-9_]{2,}$/.test(keyOrEnv)) {
        payload.env_key = keyOrEnv;
      } else if (keyOrEnv) {
        payload.api_key = keyOrEnv;
        const templates = state.codex.templates || [];
        const titem = templates.find((item) => item.id === tpl);
        if (titem && titem.env_key) payload.env_key = titem.env_key;
      }
      send("codex_upsert_profile", payload);
    };
  }
  const pfDelete = $("codex-pf-delete");
  if (pfDelete) {
    pfDelete.onclick = () => {
      const id = (($("codex-pf-id") && $("codex-pf-id").value)
        || ($("codex-profile-select") && $("codex-profile-select").value)
        || "").trim();
      if (!id || id === "default") return;
      send("codex_delete_profile", { id });
    };
  }
  const pfNew = $("codex-pf-new");
  if (pfNew) pfNew.onclick = () => prepareNewProfileForm("codex");
  const pfImport = $("codex-pf-import");
  if (pfImport) {
    pfImport.onclick = () => {
      const accountId = (($("codex-import-account") && $("codex-import-account").value) || "").trim();
      if (!accountId) {
        toast(t("codex.importEmpty"), "warn");
        return;
      }
      send("codex_import_account", { account_id: accountId, activate: true });
    };
  }
  const codexMode = $("codex-mode-select");
  if (codexMode) {
    codexMode.onchange = (event) => send("codex_set_mode", { mode: event.target.value });
  }
  const codexStart = $("codex-start");
  if (codexStart) codexStart.onclick = () => send("codex_start", {});
  const codexStop = $("codex-stop");
  if (codexStop) codexStop.onclick = () => send("codex_stop", {});
  const codexSend = $("codex-send");
  if (codexSend) codexSend.onclick = submitCodexPrompt;
  const codexInterrupt = $("codex-interrupt");
  if (codexInterrupt) {
    codexInterrupt.onclick = () => send("codex_interrupt", {});
  }
  const codexShot = $("codex-screenshot");
  if (codexShot) codexShot.onclick = requestScreenshot;
  const codexBox = $("codex-prompt");
  if (codexBox) {
    codexBox.onkeydown = (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submitCodexPrompt();
      }
    };
    codexBox.oninput = () => {
      codexBox.style.height = "auto";
      codexBox.style.height = Math.min(codexBox.scrollHeight, COMPOSER_MAX_HEIGHT_PX) + "px";
    };
    codexBox.onpaste = (event) => panelPasteImages(event, "codex");
    wirePanelDrop($("codex-composer"), "codex");
    codexBox.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  }
  const claudeProfile = $("claude-profile-select");
  if (claudeProfile) {
    claudeProfile.onchange = () => {
      send("claude_set_profile", { selection: claudeProfile.value || "anthropic" });
    };
  }
  const claudeModel = $("claude-model-select");
  if (claudeModel) {
    claudeModel.onchange = () => {
      const value = claudeModel.value || "";
      if (!value) return;
      send("claude_set_model", { model: value });
    };
  }
  const claudeEffort = $("claude-effort-select");
  if (claudeEffort) {
    claudeEffort.onchange = () => {
      send("claude_set_effort", { effort: claudeEffort.value || "" });
    };
  }
  const claudeProfileEdit = $("claude-profile-edit");
  if (claudeProfileEdit) {
    claudeProfileEdit.onclick = () => {
      const form = $("claude-profile-form");
      if (!form) return;
      form.classList.toggle("hidden");
      if (!form.classList.contains("hidden")) {
        renderClaudeProfileSelect();
        fillClaudeProfileFormFromTemplate();
      }
    };
  }
  const claudeTpl = $("claude-tpl-select");
  if (claudeTpl) {
    claudeTpl.onchange = () => {
      const idBox = $("claude-pf-id");
      const nameBox = $("claude-pf-name");
      if (idBox) idBox.value = "";
      if (nameBox) nameBox.value = "";
      fillClaudeProfileFormFromTemplate();
    };
  }
  const claudePfSave = $("claude-pf-save");
  if (claudePfSave) {
    claudePfSave.onclick = () => {
      const tpl = ($("claude-tpl-select") && $("claude-tpl-select").value) || "anthropic";
      const keyOrEnv = (($("claude-pf-env") && $("claude-pf-env").value) || "").trim();
      const payload = {
        id: (($("claude-pf-id") && $("claude-pf-id").value) || "").trim(),
        name: (($("claude-pf-name") && $("claude-pf-name").value) || "").trim(),
        base_url: (($("claude-pf-url") && $("claude-pf-url").value) || "").trim(),
        model: (($("claude-pf-model") && $("claude-pf-model").value) || "").trim(),
        proxy: (($("claude-pf-proxy") && $("claude-pf-proxy").value) || "").trim(),
        template: tpl,
        make_active: true,
        activate: true,
      };
      if (/^[A-Z][A-Z0-9_]{2,}$/.test(keyOrEnv)) {
        payload.env_key = keyOrEnv;
      } else if (keyOrEnv) {
        payload.api_key = keyOrEnv;
        payload.env_key = "ANTHROPIC_API_KEY";
      }
      send("claude_upsert_profile", payload);
    };
  }
  const claudePfDelete = $("claude-pf-delete");
  if (claudePfDelete) {
    claudePfDelete.onclick = () => {
      const id = (($("claude-pf-id") && $("claude-pf-id").value)
        || ($("claude-profile-select") && $("claude-profile-select").value)
        || "").trim();
      if (!id) return;
      send("claude_delete_profile", { id });
    };
  }
  const claudePfNew = $("claude-pf-new");
  if (claudePfNew) claudePfNew.onclick = () => prepareNewProfileForm("claude");
  const claudePfImport = $("claude-pf-import");
  if (claudePfImport) {
    claudePfImport.onclick = () => {
      const accountId = (($("claude-import-account") && $("claude-import-account").value) || "").trim();
      if (!accountId) {
        toast(t("codex.importEmpty"), "warn");
        return;
      }
      send("claude_import_account", { account_id: accountId, activate: true });
    };
  }
  const claudeMode = $("claude-mode-select");
  if (claudeMode) {
    claudeMode.onchange = (event) => send("claude_set_mode", { mode: event.target.value });
  }
  const claudeLogin = $("claude-login");
  if (claudeLogin) {
    claudeLogin.onclick = () => {
      const sel = ($("claude-profile-select") && $("claude-profile-select").value) || "";
      send("claude_login", { selection: sel });
    };
  }
  const claudeStart = $("claude-start");
  if (claudeStart) claudeStart.onclick = () => send("claude_start", {});
  const claudeStop = $("claude-stop");
  if (claudeStop) claudeStop.onclick = () => send("claude_stop", {});
  const claudeSend = $("claude-send");
  if (claudeSend) claudeSend.onclick = submitClaudePrompt;
  const claudeInterrupt = $("claude-interrupt");
  if (claudeInterrupt) claudeInterrupt.onclick = () => send("claude_interrupt", {});
  const claudeShot = $("claude-screenshot");
  if (claudeShot) claudeShot.onclick = requestScreenshot;
  const claudeBox = $("claude-prompt");
  if (claudeBox) {
    claudeBox.onkeydown = (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submitClaudePrompt();
      }
    };
    claudeBox.oninput = () => {
      claudeBox.style.height = "auto";
      claudeBox.style.height = Math.min(claudeBox.scrollHeight, COMPOSER_MAX_HEIGHT_PX) + "px";
    };
    claudeBox.onpaste = (event) => panelPasteImages(event, "claude");
    wirePanelDrop($("claude-composer"), "claude");
    claudeBox.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  }
  $("send").onclick = submitPrompt;
  $("interrupt").onclick = () => {
    setActivity(t("activity.interrupting"), "busy");
    send("interrupt", {});
  };
  const shotBtn = $("screenshot-button");
  if (shotBtn) shotBtn.onclick = requestScreenshot;
  $("plan-badge").onclick = () => {
    if (state.status && state.status.plan_mode) {
      send("set_plan_mode", { enabled: false });
    }
  };
  const exploreBadgeBtn = $("explore-badge");
  if (exploreBadgeBtn) {
    exploreBadgeBtn.onclick = () => {
      if (state.status && state.status.explore_mode) {
        send("set_explore_mode", { enabled: false });
      }
    };
  }
  const classify = $("auto-classify");
  if (classify) {
    classify.onchange = () => send("set_auto_classify", { enabled: classify.checked });
  }
  const autoApply = $("auto-apply-edits");
  if (autoApply) {
    autoApply.onchange = () => send("set_auto_apply_edits", { enabled: autoApply.checked });
  }
  const languageSelect = $("language-select");
  if (languageSelect) {
    languageSelect.onchange = () => {
      const code = languageSelect.value || "auto";
      if (typeof setLanguagePreference === "function") setLanguagePreference(code);
      send("set_language", { language: code });
    };
  }
  $("new-session").onclick = () => {
    const prefix = panelSessionPrefix();
    if (prefix === "codex") {
      state.viewCodexSessionId = "";
      state.codexReplayPending = true;
      clearPanelTranscriptDom("codex");
      send("codex_new_session", {});
      return;
    }
    if (prefix === "claude") {
      state.viewClaudeSessionId = "";
      state.claudeReplayPending = true;
      clearPanelTranscriptDom("claude");
      send("claude_new_session", {});
      return;
    }
    flushDraftNow();
    abandonLiveStreamView();
    state.pinBottomOnReady = true;
    send("new_session", {});
  };
  $("goal-cancel").onclick = () => send("stop_heartbeat", {});
  $("open-settings").onclick = () => {
    openPanel("settings");
    send("list_rules", {});
    send("list_memories", {});
    send("list_quest", {});
  };
  $("open-context").onclick = () => openPanel("context");
  const newWindow = $("new-window");
  if (newWindow) newWindow.onclick = () => send("new_window", {});
  $("close-panel").onclick = () => {
    $("panel").classList.add("hidden");
    syncPanelWidth();
  };
  addEventListener("resize", syncPanelWidth);
  syncPanelWidth();
  $("save-config").onclick = () => send("save_config", {});
  const saveRule = $("save-rule");
  if (saveRule) {
    saveRule.onclick = () => send("save_rule", {
      scope: $("rule-scope").value,
      name: $("rule-name").value,
      body: $("rule-body").value,
    });
  }
  const addMemory = $("add-memory");
  if (addMemory) {
    addMemory.onclick = () => {
      const text = ($("memory-input").value || "").trim();
      if (!text) return;
      send("add_memory", { text, pinned: true });
      $("memory-input").value = "";
    };
  }
  const startQuest = $("start-quest");
  if (startQuest) {
    startQuest.onclick = () => {
      const goal = ($("quest-goal").value || "").trim();
      const steps = ($("quest-steps").value || "").split("|").map((s) => s.trim()).filter(Boolean);
      send("start_quest", { goal, steps });
    };
  }
  const resumeQuest = $("resume-quest");
  if (resumeQuest) resumeQuest.onclick = () => send("resume_quest", {});
  const clearQuest = $("clear-quest");
  if (clearQuest) clearQuest.onclick = () => send("clear_quest", {});
  const fileOpen = $("file-open-external");
  if (fileOpen) {
    fileOpen.onclick = () => {
      if (state.previewPath) send("open_path", { path: state.previewPath });
    };
  }

  for (const button of document.querySelectorAll(".tab")) {
    button.onclick = () => {
      openPanel(button.dataset.tab);
      if (button.dataset.tab === "files") {
        send("list_tree", { path: state.fileTreePath || "" });
        renderEditDiffPanel(state.pendingEdits || []);
      }
      if (button.dataset.tab === "settings") {
        send("list_rules", {});
        send("list_memories", {});
        send("list_quest", {});
      }
    };
  }

  const box = $("prompt");
  box.onkeydown = (event) => {
    // Ctrl/Cmd+Enter sends. Plain Enter inserts a newline — matching Cursor
    // and every multi-line composer people already have muscle memory for.
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      submitPrompt();
    }
    if (event.key === "Escape") $("at-menu").classList.add("hidden");
  };
  document.addEventListener("keydown", (event) => {
    if (
      (event.key === "S" || event.key === "s") &&
      event.shiftKey &&
      (event.ctrlKey || event.metaKey) &&
      !event.altKey
    ) {
      event.preventDefault();
      requestScreenshot();
    }
  });
  box.oninput = () => {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, COMPOSER_MAX_HEIGHT_PX) + "px";
    scheduleDraftSave();
    maybeOpenAtMenu();
  };
  box.onpaste = (event) => {
    const files = Array.from((event.clipboardData && event.clipboardData.files) || []);
    const imageFiles = files.filter((file) => ATTACH_MIME.has((file.type || "").toLowerCase()));
    if (!imageFiles.length) return;
    event.preventDefault();
    enqueueImageFiles(imageFiles);
  };
  const composer = $("composer");
  composer.ondragenter = (event) => {
    event.preventDefault();
    composer.classList.add("drag-over");
  };
  composer.ondragover = (event) => {
    event.preventDefault();
    composer.classList.add("drag-over");
  };
  composer.ondragleave = (event) => {
    if (!composer.contains(event.relatedTarget)) composer.classList.remove("drag-over");
  };
  composer.ondrop = (event) => {
    event.preventDefault();
    composer.classList.remove("drag-over");
    enqueueImageFiles(event.dataTransfer && event.dataTransfer.files);
  };
  // Start tall enough to look like a real chat box, not a single-line field.
  box.style.height = `${COMPOSER_MIN_HEIGHT_PX}px`;
  addEventListener("beforeunload", flushDraftNow);

  $("model-select").onchange = (event) => {
    if (state.busy) {
      syncModelControls(state.status || {});
      toast(t("composer.modelLocked"), "warn");
      return;
    }
    if (event.target.value) {
      syncVisionBadge(event.target.value.trim());
      send("set_model", { spec: event.target.value.trim() });
    }
  };
  $("effort-select").onchange = (event) => {
    if (state.busy) {
      syncModelControls(state.status || {});
      toast(t("composer.modelLocked"), "warn");
      return;
    }
    send("set_effort", { effort: event.target.value });
  };
  const contextSelect = $("context-select");
  if (contextSelect) {
    contextSelect.onchange = (event) => {
      if (event.target.value) send("set_context", { tokens: Number(event.target.value) });
    };
  }
  const archiveToggle = $("archive-toggle");
  if (archiveToggle) {
    archiveToggle.onclick = () => {
      const prefix = panelSessionPrefix();
      if (prefix) send(`${prefix}_toggle_archived`, {});
      else send("toggle_archived", {});
    };
  }
  $("mode-select").onchange = (event) => send("set_mode", { mode: event.target.value });

  $("account-form").onsubmit = (event) => {
    event.preventDefault();
    send("add_account", {
      id: $("acc-id").value.trim(),
      base_url: $("acc-url").value.trim(),
      env: $("acc-env").value.trim(),
      proxy: $("acc-proxy").value.trim(),
    });
    $("acc-id").value = $("acc-url").value = $("acc-env").value = "";
    $("acc-proxy").value = "";
  };
  $("fetch-models").onclick = () =>
    send("list_account_models", { id: $("catalogue-account").value });

  $("browse-skill-path").onclick = () => send("add_skill_path", { path: "" });
  $("add-skill-path").onclick = () => {
    const value = $("skill-path-input").value.trim();
    if (value) { send("add_skill_path", { path: value }); $("skill-path-input").value = ""; }
  };
  $("reload-skills").onclick = () => send("reload_skills", {});
  const learnBtn = $("learn-skills");
  if (learnBtn) {
    learnBtn.onclick = () => {
      const box = $("learn-results");
      if (box) box.textContent = "扫描中…";
      send("learn_skills", {});
    };
  }
  const marketBtn = $("market-quote");
  if (marketBtn) {
    marketBtn.onclick = () => {
      const symbol = ($("market-symbol") && $("market-symbol").value.trim()) || "";
      send("market_quote", { symbol });
    };
  }
  const historyBtn = $("market-history");
  if (historyBtn) {
    historyBtn.onclick = () => {
      const symbol = ($("market-symbol") && $("market-symbol").value.trim()) || "";
      send("market_history", { symbol, count: 30 });
    };
  }
  const backtestBtn = $("market-backtest");
  if (backtestBtn) {
    backtestBtn.onclick = () => {
      const symbol = ($("market-symbol") && $("market-symbol").value.trim()) || "";
      send("market_backtest", { symbol, fast: 5, slow: 20, count: 120 });
    };
  }
  const alertAdd = $("market-alert-add");
  if (alertAdd) {
    alertAdd.onclick = () => {
      const symbol = ($("market-symbol") && $("market-symbol").value.trim()) || "";
      const price = ($("market-alert-price") && $("market-alert-price").value) || "";
      const op = ($("market-alert-op") && $("market-alert-op").value) || ">=";
      send("market_alert_add", { symbol, price, op });
    };
  }
  const alertList = $("market-alert-list");
  if (alertList) {
    alertList.onclick = () => send("market_alert_list", {});
  }
  const paperBtn = $("paper-status");
  if (paperBtn) {
    paperBtn.onclick = () => send("paper_status", {});
  }
  const fileSearchBtn = $("file-search-btn");
  if (fileSearchBtn) {
    fileSearchBtn.onclick = () => {
      const query = ($("file-search") && $("file-search").value.trim()) || "";
      const ext = ($("file-ext-filter") && $("file-ext-filter").value) || "";
      send("search_content", { query, glob: ext });
    };
  }
  const canvasOpen = $("canvas-open");
  if (canvasOpen) {
    canvasOpen.onclick = () => previewCanvasPath();
  }
  const canvasSave = $("canvas-save");
  if (canvasSave) {
    canvasSave.onclick = () => saveCanvasPath();
  }
  const canvasReveal = $("canvas-reveal");
  if (canvasReveal) {
    canvasReveal.onclick = () => {
      const path = ($("canvas-path") && $("canvas-path").value.trim()) || "";
      if (path) send("open_path", { path, mode: "reveal" });
    };
  }

  $("heartbeat-button").onclick = () => {
    const status = state.status;
    if (status && (status.heartbeat || status.heartbeat_armed)) {
      send("stop_heartbeat", {});
      return;
    }
    heartbeatDialog();
  };

  document.onkeydown = (event) => {
    if (event.key === "Escape" && !$("modal-backdrop").classList.contains("hidden")) {
      closeModal();
    }
  };
}

wire();
connect();
