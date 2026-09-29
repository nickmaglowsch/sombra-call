// Sombra overlay client. Meeting content is untrusted: it is only ever set via
// textContent / value, never parsed as HTML.
"use strict";

(function () {
  const token = new URLSearchParams(location.search).get("token") || "";
  const cardsEl = document.getElementById("cards");
  const emptyEl = document.getElementById("empty");
  const statusEl = document.getElementById("status");

  let ws = null;
  let cards = [];
  let editing = null; // {id, draft} while the inline editor is open
  let retryMs = 250;
  // Shortcuts act on the newest suggestion. When that card changes, ignore keys for a
  // moment so a keypress meant for the previous card cannot resolve one not yet read.
  const ARM_DELAY_MS = 300;
  let topId = null;
  let armedAt = 0;

  function setStatus(state, text) {
    statusEl.dataset.state = state;
    statusEl.textContent = text;
  }

  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
    ws.onopen = () => {
      retryMs = 250;
      setStatus("online", "conectado");
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state") {
        cards = msg.cards;
        render();
      } else if (msg.type === "error") {
        setStatus("error", msg.message);
      }
    };
    ws.onclose = () => {
      setStatus("offline", "desconectado, tentando de novo…");
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 4000);
    };
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function act(card, kind, text) {
    const msg = { type: "action", suggestion_id: card.suggestion_id, kind };
    if (text !== undefined) msg.text = text;
    if (editing && editing.id === card.id) editing = null;
    send(msg);
  }

  function dismiss(card) {
    send({ type: "dismiss", card_id: card.id });
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, key, onClick, primary) {
    const b = el("button", primary ? "primary" : "", `${label} (${key})`);
    b.type = "button";
    b.addEventListener("click", onClick);
    return b;
  }

  function frames(card) {
    const box = el("div", "frames");
    box.append(el("span", "label", "frames enviados:"));
    for (const id of card.frames) {
      const img = document.createElement("img");
      img.alt = id;
      img.title = id;
      img.src = `/frames/${encodeURIComponent(id)}?token=${encodeURIComponent(token)}`;
      img.addEventListener("error", () => img.replaceWith(el("span", "frame-id", id)));
      box.append(img);
    }
    return box;
  }

  function startEdit(card) {
    editing = { id: card.id, draft: card.text };
    render();
  }

  function saveEdit(card) {
    const text = editing ? editing.draft : card.text;
    if (!text.trim()) return;
    act(card, "edit", text);
  }

  function renderSuggestion(card, node) {
    node.append(el("div", "label", "sugestão"));
    node.append(el("div", "excerpt", card.excerpt));
    if (editing && editing.id === card.id) {
      const area = el("textarea");
      area.value = editing.draft;
      area.addEventListener("input", () => { editing.draft = area.value; });
      area.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
          ev.preventDefault();
          saveEdit(card);
        } else if (ev.key === "Escape") {
          ev.preventDefault();
          editing = null;
          render();
        }
      });
      node.append(area);
      if (card.frames.length) node.append(frames(card));
      const row = el("div", "buttons");
      row.append(
        button("Aprovar edição", "⌘↵", () => saveEdit(card), true),
        button("Cancelar", "Esc", () => { editing = null; render(); }),
      );
      node.append(row);
      requestAnimationFrame(() => {
        if (document.activeElement !== area) {
          area.focus();
          area.setSelectionRange(area.value.length, area.value.length);
        }
      });
      return;
    }
    node.append(el("div", "answer", card.text));
    if (card.frames.length) node.append(frames(card));
    const row = el("div", "buttons");
    row.append(
      button("Aprovar", "A", () => act(card, "approve"), true),
      button("Editar", "E", () => startEdit(card)),
      button("Descartar", "D", () => act(card, "discard")),
      button("Não era comigo", "N", () => act(card, "not_for_me")),
    );
    node.append(row);
  }

  function renderSearching(card, node) {
    const label = el("div", "label");
    label.append(el("span", "spinner"), document.createTextNode("buscando contexto…"));
    node.append(label, el("div", "excerpt", card.excerpt));
    const row = el("div", "buttons");
    row.append(button("Ocultar", "X", () => dismiss(card)));
    node.append(row);
  }

  function renderFailure(card, node) {
    node.append(el("div", "label", "falha: responda manualmente"));
    node.append(el("div", "excerpt", card.excerpt));
    if (card.reason) node.append(el("div", "reason", card.reason));
    const row = el("div", "buttons");
    row.append(button("Ok", "X", () => dismiss(card)));
    node.append(row);
  }

  function render() {
    if (editing && !cards.some((c) => c.id === editing.id)) editing = null;
    const top = cards.find((c) => c.kind === "suggestion");
    const newTopId = top ? top.id : null;
    if (newTopId !== topId) {
      topId = newTopId;
      armedAt = performance.now() + ARM_DELAY_MS;
    }
    const nodes = cards.map((card) => {
      const node = el("section", `card ${card.kind}${top && card.id === top.id ? " top" : ""}`);
      node.dataset.cardId = card.id;
      if (card.kind === "suggestion") renderSuggestion(card, node);
      else if (card.kind === "failure") renderFailure(card, node);
      else renderSearching(card, node);
      return node;
    });
    // Keep the editor's DOM node alive across pushes so typing is not interrupted.
    const active = document.activeElement;
    if (active && active.tagName === "TEXTAREA" && editing) {
      const keep = active.closest(".card");
      const idx = nodes.findIndex((n) => n.dataset.cardId === editing.id);
      if (keep && idx >= 0) nodes[idx] = keep;
    }
    cardsEl.replaceChildren(...nodes);
    emptyEl.hidden = cards.length > 0;
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.repeat) return; // holding a key must not work through the queue
    if (ev.target && ev.target.tagName === "TEXTAREA") return;
    if (performance.now() < armedAt) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const top = cards.find((c) => c.kind === "suggestion");
    const key = ev.key.toLowerCase();
    if (key === "x") {
      const info = cards.find((c) => c.kind !== "suggestion");
      if (info) dismiss(info);
      return;
    }
    if (!top) return;
    const handlers = {
      a: () => act(top, "approve"),
      e: () => startEdit(top),
      d: () => act(top, "discard"),
      n: () => act(top, "not_for_me"),
    };
    if (handlers[key]) {
      ev.preventDefault();
      handlers[key]();
    }
  });

  render();
  connect();
})();
