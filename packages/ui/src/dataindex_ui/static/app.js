// DataIndex local chat. Every value that comes from documents, the model or the
// history file is inserted with textContent, never as HTML.
"use strict";

const state = {
  status: null,
  conversations: [],
  documents: [],
  selectedDocs: new Set(),
  currentId: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);

function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.title) node.title = options.title;
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) node.setAttribute(name, value);
  }
  if (options.onClick) node.addEventListener("click", options.onClick);
  for (const child of children) if (child) node.append(child);
  return node;
}

// ---------------------------------------------------------------------------
// HTTP helpers

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  if (response.status === 401) {
    showToast("Your session expired. Open the link printed by dataindex-ui again.", true);
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(await errorDetail(response));
  if (response.status === 204) return null;
  return response.json();
}

async function errorDetail(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail.length) {
      return body.detail.map((item) => item.msg || String(item)).join("; ");
    }
  } catch (_) {
    // Not JSON; fall through to the status text.
  }
  return `Request failed (${response.status} ${response.statusText})`;
}

function jsonBody(value) {
  return { headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
}

let toastTimer = null;
function showToast(message, bad = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("bad", bad);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, bad ? 7000 : 3500);
}

// ---------------------------------------------------------------------------
// Sidebar: conversations

async function loadConversations() {
  const data = await api("/api/conversations");
  state.conversations = data.conversations;
  renderConversations();
}

function renderConversations() {
  const list = $("conversations");
  list.replaceChildren();
  for (const conversation of state.conversations) {
    const item = el("li", { className: conversation.id === state.currentId ? "active" : "" });
    item.append(
      el("button", {
        className: "item-title",
        text: conversation.title,
        title: conversation.title,
        attrs: { type: "button" },
        onClick: () => openConversation(conversation.id),
      }),
      el("button", {
        className: "icon",
        text: "\u270E",
        title: "Rename",
        attrs: { type: "button", "aria-label": "Rename chat" },
        onClick: () => editTitle(item, conversation),
      }),
      el("button", {
        className: "icon",
        text: "\u2715",
        title: "Delete",
        attrs: { type: "button", "aria-label": "Delete chat" },
        onClick: () => confirmInRow(item, "Delete this chat?", "Delete", renderConversations,
          () => deleteConversation(conversation)),
      }),
    );
    list.append(item);
  }
  $("no-conversations").hidden = state.conversations.length > 0;
}

// Native prompt() and confirm() are unavailable in some embedded browsers, so
// rename and delete are confirmed inside the sidebar row instead.
function confirmInRow(item, question, actionLabel, restore, action) {
  const cancel = el("button", {
    className: "row-button",
    text: "Cancel",
    attrs: { type: "button" },
    onClick: restore,
  });
  const confirm = el("button", {
    className: "row-button danger",
    text: actionLabel,
    attrs: { type: "button" },
    onClick: action,
  });
  item.replaceChildren(el("span", { className: "row-question", text: question }), confirm, cancel);
  item.addEventListener("keydown", (event) => { if (event.key === "Escape") restore(); });
  cancel.focus();
}

function editTitle(item, conversation) {
  const input = el("input", {
    className: "row-input",
    attrs: { type: "text", maxlength: "200", "aria-label": "Chat title" },
  });
  input.value = conversation.title;
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== conversation.title) renameConversation(conversation, title);
    else renderConversations();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); finish(true); }
    if (event.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  item.replaceChildren(input);
  input.focus();
  input.select();
}

async function openConversation(id) {
  try {
    const conversation = await api(`/api/conversations/${encodeURIComponent(id)}`);
    state.currentId = conversation.id;
    history.replaceState(null, "", `#${conversation.id}`);
    $("chat-title").textContent = conversation.title;
    const messages = $("messages");
    messages.replaceChildren();
    for (const message of conversation.messages) messages.append(renderMessage(message));
    if (!conversation.messages.length) messages.append(welcome());
    renderConversations();
    scrollToEnd();
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
    newChat();
  }
}

function newChat() {
  state.currentId = null;
  history.replaceState(null, "", "#");
  $("chat-title").textContent = "New chat";
  $("messages").replaceChildren(welcome());
  renderConversations();
  $("question").focus();
}

function welcome() {
  return el("div", { className: "welcome" }, [
    el("h2", { text: "Ask about your documents" }),
    el("p", {
      text: "Answers cite the exact sections they come from. Chats are saved on this computer.",
    }),
  ]);
}

async function renameConversation(conversation, title) {
  try {
    const updated = await api(
      `/api/conversations/${encodeURIComponent(conversation.id)}`,
      { method: "PATCH", ...jsonBody({ title }) },
    );
    if (updated.id === state.currentId) $("chat-title").textContent = updated.title;
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
  }
  await loadConversations();
}

async function deleteConversation(conversation) {
  try {
    await api(`/api/conversations/${encodeURIComponent(conversation.id)}`, { method: "DELETE" });
    if (conversation.id === state.currentId) newChat();
    await loadConversations();
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
  }
}

// ---------------------------------------------------------------------------
// Sidebar: documents

async function loadDocuments() {
  try {
    const data = await api("/api/documents");
    state.documents = data.documents;
    const known = new Set(state.documents.map((doc) => doc.doc_id));
    for (const id of [...state.selectedDocs]) if (!known.has(id)) state.selectedDocs.delete(id);
    renderDocuments();
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
  }
}

function renderDocuments() {
  const list = $("documents");
  list.replaceChildren();
  for (const doc of state.documents) {
    const box = el("input", { attrs: { type: "checkbox" } });
    box.checked = state.selectedDocs.has(doc.doc_id);
    box.addEventListener("change", () => {
      if (box.checked) state.selectedDocs.add(doc.doc_id);
      else state.selectedDocs.delete(doc.doc_id);
    });
    const where = doc.connection && doc.connection.url
      ? `${doc.connection.url}${doc.connection.schema ? ` (schema ${doc.connection.schema})` : ""}`
      : doc.doc_id;
    const label = el("label", { className: "doc-label", title: `${doc.name} (${where})` }, [
      box,
      el("span", { className: "doc-name", text: doc.name }),
      doc.status !== "completed" ? el("span", { className: "badge", text: doc.status }) : null,
    ]);
    const item = el("li", {}, [label]);
    item.append(el("button", {
      className: "icon",
      text: "\u2715",
      title: "Remove from DataIndex",
      attrs: { type: "button", "aria-label": `Remove ${doc.name}` },
      onClick: () => confirmInRow(item, "Remove this document? Chats are kept.", "Remove",
        renderDocuments, () => removeDocument(doc)),
    }));
    list.append(item);
  }
  $("no-documents").hidden = state.documents.length > 0;
}

async function uploadFiles(files) {
  for (const file of files) {
    showToast(`Indexing ${file.name}\u2026`);
    try {
      const result = await api(`/api/documents?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      const doc = result.document;
      const notes = result.warnings.length ? `\n${result.warnings.join("\n")}` : "";
      showToast(`${doc.name}: ${doc.change}, ${doc.node_count} sections indexed.${notes}`);
    } catch (error) {
      if (error.message !== "unauthorized") showToast(`${file.name}: ${error.message}`, true);
    }
  }
  await loadDocuments();
}

async function removeDocument(doc) {
  try {
    await api(`/api/documents/${encodeURIComponent(doc.doc_id)}`, { method: "DELETE" });
    state.selectedDocs.delete(doc.doc_id);
    await loadDocuments();
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
  }
}

// The approved tables, views and queries use the same JSON as `dataindex submit-db --config`.
function approvedConfig(text) {
  let config;
  try {
    config = JSON.parse(text);
  } catch (_) {
    throw new Error("The approved tables, views and queries are not valid JSON.");
  }
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new Error("The approved tables, views and queries must be a JSON object.");
  }
  return { allowed_columns: config.allowed_columns, queries: config.queries };
}

function optionalField(id) {
  const value = $(id).value.trim();
  return value ? value : undefined;
}

function showConnectError(message) {
  const box = $("connect-error");
  box.textContent = message || "";
  box.hidden = !message;
}

function openConnectDialog() {
  showConnectError("");
  $("connect-dialog").showModal();
}

async function connectDatabase(event) {
  event.preventDefault();
  const submit = $("connect-submit");
  let body;
  try {
    body = {
      url: $("connect-url").value.trim(),
      password_env: optionalField("connect-password-env"),
      schema: optionalField("connect-schema"),
      name: optionalField("connect-name"),
      ...approvedConfig($("connect-config").value),
    };
  } catch (error) {
    showConnectError(error.message);
    return;
  }
  showConnectError("");
  submit.disabled = true;
  submit.textContent = "Connecting\u2026";
  try {
    const result = await api("/api/databases", { method: "POST", ...jsonBody(body) });
    const doc = result.document;
    $("connect-dialog").close();
    $("connect-form").reset();
    showToast(`${doc.name}: ${doc.change}, ${doc.node_count} schema items indexed.`);
    await loadDocuments();
  } catch (error) {
    if (error.message !== "unauthorized") showConnectError(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = "Connect";
  }
}

// ---------------------------------------------------------------------------
// Messages

function renderMessage(message) {
  if (message.role === "user") {
    const wrapper = el("div", { className: "message user" }, [
      el("div", { className: "bubble", text: message.content }),
    ]);
    if (message.doc_ids && message.doc_ids.length) {
      wrapper.append(el("div", { className: "meta", text: `Limited to: ${docNames(message.doc_ids)}` }));
    }
    return wrapper;
  }
  if (message.role === "error") {
    return el("div", { className: "message error" }, [
      el("div", { className: "bubble", text: message.content }),
    ]);
  }
  return renderAnswer(message);
}

function docNames(ids) {
  const names = new Map(state.documents.map((doc) => [doc.doc_id, doc.name]));
  return ids.map((id) => names.get(id) || id).join(", ");
}

function renderAnswer(message) {
  const answer = message.answer || { text: message.content, citations: [], warnings: [] };
  const key = `m${message.id}`;
  const bubble = el("div", { className: "bubble" });
  if (answer.abstained) {
    const text = answer.text || "No supported answer was found.";
    bubble.append(el("div", { className: "abstained", text }));
    if (answer.reason && !text.includes(answer.reason)) {
      bubble.append(el("div", { className: "meta", text: `Reason: ${answer.reason}` }));
    }
  } else {
    appendWithMarkers(bubble, answer.text || "", key);
  }

  if (answer.warnings && answer.warnings.length) {
    bubble.append(el("ul", { className: "warnings" }, answer.warnings.map((text) => el("li", { text }))));
  }

  if (answer.citations && answer.citations.length) {
    const list = el("div", { className: "citations" });
    for (const citation of answer.citations) list.append(renderCitation(citation, key));
    bubble.append(list);
  }

  const meta = [];
  if (answer.citations) meta.push(`${answer.citations.length} source${answer.citations.length === 1 ? "" : "s"}`);
  if (answer.tokens_used) meta.push(`${answer.tokens_used} tokens`);
  meta.push(formatTime(message.created_at));
  return el("div", { className: "message assistant" }, [
    bubble,
    el("div", { className: "meta", text: meta.join(" \u00b7 ") }),
  ]);
}

function appendWithMarkers(parent, text, key) {
  const pattern = /\[(\d+)\]/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parent.append(document.createTextNode(text.slice(last, match.index)));
    const number = match[1];
    const link = el("a", { text: `[${number}]`, attrs: { href: `#${key}-c${number}` } });
    link.addEventListener("click", (event) => {
      event.preventDefault();
      focusCitation(`${key}-c${number}`);
    });
    parent.append(el("span", { className: "marker" }, [link]));
    last = match.index + match[0].length;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

function focusCitation(id) {
  const card = document.getElementById(id);
  if (!card) return;
  card.open = true;
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1200);
}

function renderCitation(citation, key) {
  const details = el("details", { className: "citation", attrs: { id: `${key}-c${citation.number}` } });
  const summary = el("summary", {}, [
    el("span", { className: "num", text: `[${citation.number}]` }),
    document.createTextNode(`${citation.document} \u2014 ${citation.position}`),
  ]);
  const excerpt = citation.excerpt + (citation.excerpt_truncated ? " \u2026" : "");
  const facts = [
    ["Source", citation.source_id],
    ["Version", citation.source_version],
    ["Operation", citation.operation],
    ["Locator", `${citation.locator.scheme || ""} ${JSON.stringify(citation.locator.value ?? citation.locator)}`],
    ["Evidence", citation.evidence_id],
  ];
  const list = el("dl");
  for (const [name, value] of facts) {
    list.append(el("dt", { text: name }), el("dd", { text: String(value ?? "") }));
  }
  details.append(summary, el("div", { className: "body" }, [el("blockquote", { text: excerpt }), list]));
  return details;
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function scrollToEnd() {
  const messages = $("messages");
  messages.scrollTop = messages.scrollHeight;
}

// ---------------------------------------------------------------------------
// Asking a question: the reply arrives as server-sent events

async function ask(question) {
  if (!state.currentId) {
    const created = await api("/api/conversations", { method: "POST", ...jsonBody({}) });
    state.currentId = created.id;
    history.replaceState(null, "", `#${created.id}`);
    $("messages").replaceChildren();
  }
  const conversationId = state.currentId;
  const docIds = [...state.selectedDocs];
  const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ question, doc_ids: docIds.length ? docIds : null }),
  });
  if (!response.ok) throw new Error(await errorDetail(response));

  const thinking = el("div", { className: "message assistant thinking" }, [
    el("div", { className: "bubble", text: "Searching your documents\u2026" }),
  ]);
  const visible = () => state.currentId === conversationId;
  let placed = false;

  await readEvents(response, (name, data) => {
    if (name === "message") {
      if (!visible()) return;
      thinking.remove();
      $("messages").append(renderMessage(data));
      if (data.role === "user") {
        $("messages").append(thinking);
        placed = true;
      }
      scrollToEnd();
    } else if (name === "status") {
      if (visible() && placed) {
        thinking.firstChild.textContent = `Searching your documents\u2026 ${data.elapsed}s`;
      }
    } else if (name === "error") {
      thinking.remove();
      showToast(data.detail, true);
    } else if (name === "done") {
      if (visible()) $("chat-title").textContent = data.conversation.title;
    }
  });
  thinking.remove();
  await loadConversations();
}

async function readEvents(response, handle) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let name = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) handle(name, JSON.parse(data.join("\n")));
    }
  }
}

function setBusy(busy) {
  state.busy = busy;
  $("send").disabled = busy || !(state.status && state.status.chat_enabled);
}

async function onSubmit(event) {
  event.preventDefault();
  const input = $("question");
  const question = input.value.trim();
  if (!question || state.busy) return;
  if (state.status && question.length > state.status.limits.question_chars) {
    showToast(`Questions may be at most ${state.status.limits.question_chars} characters.`, true);
    return;
  }
  setBusy(true);
  try {
    await ask(question);
    input.value = "";
  } catch (error) {
    if (error.message !== "unauthorized") showToast(error.message, true);
  } finally {
    setBusy(false);
    input.focus();
  }
}

// ---------------------------------------------------------------------------
// Start-up

async function start() {
  $("new-chat").addEventListener("click", newChat);
  $("composer").addEventListener("submit", onSubmit);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  const upload = $("upload");
  upload.addEventListener("change", async () => {
    const files = [...upload.files];
    upload.value = "";
    await uploadFiles(files);
  });
  $("connect").addEventListener("click", openConnectDialog);
  $("connect-cancel").addEventListener("click", () => $("connect-dialog").close());
  $("connect-form").addEventListener("submit", connectDatabase);

  try {
    state.status = await api("/api/status");
  } catch (_) {
    return;
  }
  upload.accept = state.status.limits.upload_suffixes.join(",");
  $("question").maxLength = state.status.limits.question_chars;
  $("model-info").textContent = state.status.chat_enabled
    ? `Model: ${state.status.model} \u00b7 dataindex-ui ${state.status.version}`
    : "No chat model configured. Restart with --model NAME or set DATAINDEX_MODEL.";
  setBusy(false);

  await Promise.all([loadConversations(), loadDocuments()]);
  const requested = decodeURIComponent(location.hash.slice(1));
  if (requested && state.conversations.some((item) => item.id === requested)) {
    await openConversation(requested);
  } else {
    newChat();
  }
}

start();
