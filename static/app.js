(() => {
  const $ = (id) => document.getElementById(id);
  const app = document.querySelector(".app");
  const chat = $("chat");
  const form = $("chat-form");
  const input = $("input");
  const sendBtn = $("send-btn");
  const newBtn = $("new-btn");
  const menuBtn = $("menu-btn");
  const backdrop = $("backdrop");
  const historyEl = $("history");
  const titleEl = $("title");

  const NEW_TITLE = "New conversation";
  let conversations = [];
  let currentId = null;
  let busy = false;

  /* ------------------------------------------------------------ API */
  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
    return data;
  }

  /* -------------------------------------------------------- Helpers */
  // SQLite timestamps are UTC ISO strings; Date parses them to local time.
  const fmtTime = (iso) =>
    new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  function groupLabel(iso) {
    const d = new Date(iso);
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const days = Math.round((startOfDay(new Date()) - startOfDay(d)) / 864e5);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return "Previous 7 days";
    return "Older";
  }

  const fmtNum = (n) => Number(n).toLocaleString();

  // Returns a tokens option only when usage was recorded for the message.
  const tokensOf = (input, output) =>
    input == null && output == null ? null : { input: input ?? 0, output: output ?? 0 };

  function renderTotals(totals) {
    const el = $("totals");
    el.textContent =
      totals && (totals.input_tokens || totals.output_tokens)
        ? `${fmtNum(totals.input_tokens)} in · ${fmtNum(totals.output_tokens)} out total`
        : "";
  }

  const icon = (path) =>
    `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
  const ICON_EDIT = icon('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>');
  const ICON_TRASH = icon('<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/>');

  /* -------------------------------------------------------- Sidebar */
  function renderHistory() {
    historyEl.innerHTML = "";
    if (!conversations.length) {
      historyEl.innerHTML = '<div class="history-empty">No conversations yet.<br>Start chatting to see them here.</div>';
      return;
    }

    let lastGroup = null;
    for (const c of conversations) {
      const group = groupLabel(c.updated_at);
      if (group !== lastGroup) {
        const label = document.createElement("div");
        label.className = "group-label";
        label.textContent = group;
        historyEl.appendChild(label);
        lastGroup = group;
      }

      const row = document.createElement("div");
      row.className = "conv" + (c.id === currentId ? " active" : "");

      const open = document.createElement("button");
      open.className = "conv-title";
      open.type = "button";
      open.textContent = c.title; // textContent: safe against HTML injection
      open.title = c.title;
      open.addEventListener("click", () => openConversation(c.id));

      const actions = document.createElement("div");
      actions.className = "conv-actions";

      const rename = document.createElement("button");
      rename.className = "icon-btn";
      rename.type = "button";
      rename.title = "Rename";
      rename.setAttribute("aria-label", "Rename conversation");
      rename.innerHTML = ICON_EDIT;
      rename.addEventListener("click", () => renameConversation(c));

      const del = document.createElement("button");
      del.className = "icon-btn danger";
      del.type = "button";
      del.title = "Delete";
      del.setAttribute("aria-label", "Delete conversation");
      del.innerHTML = ICON_TRASH;
      del.addEventListener("click", () => deleteConversation(c));

      actions.append(rename, del);
      row.append(open, actions);
      historyEl.appendChild(row);
    }
  }

  async function refreshHistory() {
    try {
      conversations = (await api("/api/conversations")).conversations;
    } catch (err) {
      console.error(err);
    }
    renderHistory();
  }

  function setSidebar(open) {
    app.classList.toggle("sidebar-open", open);
  }

  /* ----------------------------------------------------------- Chat */
  function showWelcome() {
    chat.innerHTML = "";
    chat.appendChild($("welcome-tpl").content.cloneNode(true));
    chat.querySelectorAll(".chip").forEach((chip) =>
      chip.addEventListener("click", () => send(chip.textContent))
    );
  }

  function clearWelcome() {
    const w = chat.querySelector(".welcome");
    if (w) w.remove();
  }

  function addMessage(role, text, opts = {}) {
    clearWelcome();
    const row = document.createElement("div");
    row.className = `message ${role === "user" ? "user" : "bot"}`;

    if (role !== "user") {
      const av = document.createElement("div");
      av.className = "avatar avatar-bot";
      av.textContent = "AI";
      row.appendChild(av);
    }

    const body = document.createElement("div");
    body.className = "message-body";

    const bubble = document.createElement("div");
    bubble.className = "bubble" + (opts.error ? " error" : "");
    if (opts.typing) {
      bubble.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
    } else {
      bubble.textContent = text;
    }
    body.appendChild(bubble);

    if (opts.time || opts.tokens) {
      const meta = document.createElement("div");
      meta.className = "meta";
      if (opts.time) {
        const t = document.createElement("span");
        t.className = "time";
        t.textContent = fmtTime(opts.time);
        meta.appendChild(t);
      }
      if (opts.tokens) {
        const tk = document.createElement("span");
        tk.className = "tokens";
        tk.textContent = `${fmtNum(opts.tokens.input)} in · ${fmtNum(opts.tokens.output)} out`;
        tk.title = "Input tokens (full context sent) · Output tokens (this reply)";
        meta.appendChild(tk);
      }
      body.appendChild(meta);
    }

    row.appendChild(body);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
    input.disabled = state;
    if (!state && window.matchMedia("(min-width: 761px)").matches) input.focus();
  }

  function autoresize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  function newChat() {
    if (busy) return;
    currentId = null;
    titleEl.textContent = NEW_TITLE;
    renderTotals(null);
    showWelcome();
    renderHistory();
    setSidebar(false);
    input.focus();
  }

  async function openConversation(id) {
    if (busy || id === currentId) return setSidebar(false);
    try {
      const data = await api(`/api/conversations/${id}`);
      currentId = id;
      titleEl.textContent = data.conversation.title;
      chat.innerHTML = "";
      data.messages.forEach((m) =>
        addMessage(m.role, m.content, {
          time: m.created_at,
          tokens: m.role === "assistant" ? tokensOf(m.input_tokens, m.output_tokens) : null,
        })
      );
      renderTotals(data.totals);
      chat.scrollTop = chat.scrollHeight;
      renderHistory();
      setSidebar(false);
    } catch (err) {
      alert(err.message);
      refreshHistory();
    }
  }

  async function renameConversation(c) {
    const title = prompt("Rename conversation", c.title);
    if (title === null || !title.trim() || title.trim() === c.title) return;
    try {
      const data = await api(`/api/conversations/${c.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      if (c.id === currentId) titleEl.textContent = data.conversation.title;
      await refreshHistory();
    } catch (err) {
      alert(err.message);
    }
  }

  async function deleteConversation(c) {
    if (busy) return;
    if (!confirm(`Delete "${c.title}"? This cannot be undone.`)) return;
    try {
      await api(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (c.id === currentId) newChat();
      await refreshHistory();
    } catch (err) {
      alert(err.message);
    }
  }

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;

    addMessage("user", text, { time: new Date().toISOString() });
    input.value = "";
    autoresize();
    setBusy(true);
    const typingRow = addMessage("bot", "", { typing: true });

    try {
      const data = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ conversation_id: currentId, message: text }),
      });
      typingRow.remove();
      currentId = data.conversation.id;
      titleEl.textContent = data.conversation.title;
      addMessage("assistant", data.reply, {
        time: data.conversation.updated_at,
        tokens: tokensOf(data.usage.input_tokens, data.usage.output_tokens),
      });
      renderTotals(data.totals);
      await refreshHistory();
    } catch (err) {
      typingRow.remove();
      addMessage("assistant", `Sorry, something went wrong: ${err.message}`, { error: true });
    } finally {
      setBusy(false);
    }
  }

  /* --------------------------------------------------------- Events */
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send(input.value);
    }
  });
  input.addEventListener("input", autoresize);
  newBtn.addEventListener("click", newChat);
  menuBtn.addEventListener("click", () => setSidebar(!app.classList.contains("sidebar-open")));
  backdrop.addEventListener("click", () => setSidebar(false));

  /* ----------------------------------------------------------- Init */
  showWelcome();
  refreshHistory();
  input.focus();
})();
