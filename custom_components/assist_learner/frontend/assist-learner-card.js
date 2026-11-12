// Helpmeet: Assist Learner dashboard card. Served and loaded by the integration itself.

const CARD_TYPE = "assist-learner-card";
const DOMAIN = "assist_learner";
const DEFAULT_ENTITY = "sensor.assist_learner_status";

const findStatusEntity = (hass) =>
  Object.values(hass?.entities ?? {}).find(
    (e) => e.platform === DOMAIN && e.entity_id.startsWith("sensor."),
  )?.entity_id ?? DEFAULT_ENTITY;

const STYLES = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  button { font: inherit; color: inherit; }
  .header {
    display: flex; align-items: center; gap: 12px; width: 100%;
    padding: 16px; background: none; border: none; cursor: pointer; text-align: left;
  }
  .header:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
  .header ha-icon.main { color: var(--state-icon-color, var(--primary-color)); }
  .title { flex: 1; font-size: 1.1em; font-weight: 500; }
  .stats { display: flex; gap: 20px; }
  .stat { display: flex; flex-direction: column; align-items: flex-end; line-height: 1.2; }
  .stat .value { font-size: 1.5em; font-weight: 500; }
  .stat .label { font-size: 0.8em; color: var(--secondary-text-color); }
  .stat.waiting .value { color: var(--primary-color); }
  .stat.waiting.zero .value { color: var(--primary-text-color); }
  .chevron { color: var(--secondary-text-color); transition: transform 0.2s; }
  :host([expanded]) .chevron { transform: rotate(180deg); }
  .body { display: none; border-top: 1px solid var(--divider-color); }
  :host([expanded]) .body { display: block; }
  .message { padding: 16px; color: var(--secondary-text-color); }
  .message.error { color: var(--error-color); }
  .message:empty { display: none; }
  .bulk {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    padding: 8px 16px; border-bottom: 1px solid var(--divider-color);
  }
  .bulk[hidden] { display: none; }
  .bulk label { flex: 1; display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .list { max-height: var(--assist-learner-list-height, 480px); overflow-y: auto; }
  .row {
    display: flex; gap: 12px; padding: 12px 16px;
    border-bottom: 1px solid var(--divider-color);
  }
  .row:last-child { border-bottom: none; }
  .row.busy { opacity: 0.5; }
  .row .content { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
  .heard { font-weight: 500; overflow-wrap: anywhere; }
  .seen { font-weight: normal; color: var(--secondary-text-color); font-size: 0.85em; }
  .detail { font-size: 0.85em; color: var(--secondary-text-color); overflow-wrap: anywhere; }
  .detail b { font-weight: 500; color: var(--primary-text-color); }
  .notes { font-size: 0.85em; color: var(--warning-color, #f4b400); }
  .notes:empty { display: none; }
  input[type="checkbox"] {
    width: 18px; height: 18px; margin: 2px 0 0; accent-color: var(--primary-color); cursor: pointer;
  }
  input.sentence {
    width: 100%; box-sizing: border-box; padding: 6px 8px; font: inherit;
    color: var(--primary-text-color);
    background: var(--input-fill-color, var(--secondary-background-color));
    border: 1px solid var(--input-idle-line-color, var(--divider-color)); border-radius: 4px;
  }
  input.sentence:focus { outline: none; border-color: var(--primary-color); }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn {
    padding: 4px 12px; border-radius: 16px; cursor: pointer; font-size: 0.9em;
    border: 1px solid var(--divider-color); background: none;
  }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .btn.approve {
    background: var(--primary-color); color: var(--text-primary-color, #fff);
    border-color: var(--primary-color);
  }
  .btn.reject { color: var(--error-color); }
`;

class AssistLearnerCard extends HTMLElement {
  constructor() {
    super();
    this._shadow = this.attachShadow({ mode: "open" });
    this._expanded = false;
    this._snapshot = null;
    this._error = null;
    this._notice = null;
    this._unsub = null;
    this._subscribing = false;
    this._subscribeFailed = false;
    this._rows = new Map();
    this._selected = new Set();
    this._edits = new Map();
    this._busy = new Set();
  }

  static getStubConfig(hass) {
    return { entity: findStatusEntity(hass) };
  }

  static getConfigForm() {
    const labels = {
      entity: "Assist Learner status sensor",
      title: "Title",
      expanded: "Start expanded",
    };
    return {
      schema: [
        { name: "entity", selector: { entity: { filter: { integration: DOMAIN, domain: "sensor" } } } },
        { name: "title", selector: { text: {} } },
        { name: "expanded", selector: { boolean: {} } },
      ],
      computeLabel: (schema) => labels[schema.name],
    };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
    if (!this._els) this._build();
    this._els.title.textContent = config.title || "Assist Learner";
    this._setExpanded(Boolean(config.expanded));
  }

  set hass(hass) {
    this._hass = hass;
    this._renderCounts();
    if (this._expanded) this._subscribe();
  }

  connectedCallback() {
    if (this._expanded) this._subscribe();
  }

  disconnectedCallback() {
    this._unsubscribe();
  }

  getCardSize() {
    return this._expanded ? 3 + 2 * (this._snapshot?.pending.length ?? 0) : 2;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  get _entityId() {
    return this._config?.entity || findStatusEntity(this._hass);
  }

  _build() {
    this._shadow.innerHTML = `
      <style>${STYLES}</style>
      <ha-card>
        <button class="header" type="button" aria-expanded="false">
          <ha-icon class="main" icon="mdi:head-lightbulb-outline"></ha-icon>
          <span class="title"></span>
          <span class="stats">
            <span class="stat waiting"><span class="value">–</span><span class="label">to review</span></span>
            <span class="stat captured"><span class="value">–</span><span class="label">captured</span></span>
          </span>
          <ha-icon class="chevron" icon="mdi:chevron-down"></ha-icon>
        </button>
        <div class="body">
          <div class="bulk" hidden>
            <label><input type="checkbox" class="select-all" /><span class="selected-label">Select all</span></label>
            <button class="btn approve" type="button" data-decision="approve">Approve</button>
            <button class="btn" type="button" data-decision="defer" title="Hide until heard again">Later</button>
            <button class="btn reject" type="button" data-decision="reject" title="Never propose again">Reject</button>
          </div>
          <div class="message"></div>
          <div class="list"></div>
        </div>
      </ha-card>
    `;
    const $ = (selector) => this._shadow.querySelector(selector);
    this._els = {
      header: $(".header"),
      title: $(".title"),
      waiting: $(".stat.waiting"),
      captured: $(".stat.captured"),
      bulk: $(".bulk"),
      selectAll: $(".select-all"),
      selectedLabel: $(".selected-label"),
      bulkButtons: [...this._shadow.querySelectorAll(".bulk .btn")],
      message: $(".message"),
      list: $(".list"),
    };
    this._els.header.addEventListener("click", () => this._setExpanded(!this._expanded));
    this._els.selectAll.addEventListener("change", (ev) => {
      const pending = this._snapshot?.pending ?? [];
      this._selected = ev.target.checked ? new Set(pending.map((p) => p.id)) : new Set();
      this._renderList();
    });
    for (const button of this._els.bulkButtons) {
      button.addEventListener("click", () => this._reviewSelected(button.dataset.decision));
    }
  }

  _setExpanded(expanded) {
    this._expanded = expanded;
    this.toggleAttribute("expanded", expanded);
    this._els.header.setAttribute("aria-expanded", String(expanded));
    if (expanded) {
      this._subscribeFailed = false;
      this._subscribe();
    } else {
      this._unsubscribe();
    }
    this._renderList();
  }

  async _subscribe() {
    if (this._unsub || this._subscribing || this._subscribeFailed) return;
    if (!this._hass || !this.isConnected) return;
    this._subscribing = true;
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (snapshot) => this._onSnapshot(snapshot),
        { type: `${DOMAIN}/subscribe` },
      );
      this._error = null;
    } catch (err) {
      this._subscribeFailed = true;
      this._error =
        err?.code === "unauthorized"
          ? "Only administrators can review learned commands."
          : err?.code === "unknown_command"
            ? "Restart Home Assistant to finish installing Assist Learner."
            : `Couldn't load commands to review: ${err?.message ?? err}`;
    } finally {
      this._subscribing = false;
    }
    if (this._unsub && (!this._expanded || !this.isConnected)) this._unsubscribe();
    this._renderList();
  }

  _unsubscribe() {
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
    this._snapshot = null;
    this._renderCounts();
  }

  _onSnapshot(snapshot) {
    this._snapshot = snapshot;
    const ids = new Set(snapshot.pending.map((p) => p.id));
    for (const id of [...this._selected]) if (!ids.has(id)) this._selected.delete(id);
    for (const id of [...this._edits.keys()]) if (!ids.has(id)) this._edits.delete(id);
    this._renderCounts();
    this._renderList();
  }

  _renderCounts() {
    if (!this._els) return;
    const counts = this._snapshot?.loaded
      ? this._snapshot.counts
      : this._hass?.states[this._entityId]?.attributes ?? {};
    const show = (el, value) => {
      el.querySelector(".value").textContent = value ?? "–";
      el.classList.toggle("zero", !value);
    };
    show(this._els.waiting, counts.proposed);
    show(this._els.captured, counts.captured_total);
  }

  _renderList() {
    if (!this._els || !this._expanded) return;
    const { list, message, bulk } = this._els;
    const snapshot = this._snapshot;
    const pending = snapshot?.loaded ? snapshot.pending : [];

    message.classList.toggle("error", Boolean(this._error));
    if (this._error) message.textContent = this._error;
    else if (!snapshot) message.textContent = "Loading…";
    else if (!snapshot.loaded) message.textContent = "Assist Learner isn't loaded.";
    else if (this._notice) message.textContent = this._notice;
    else if (!pending.length) {
      message.textContent = "Nothing to review. New commands show up here after you've said them a couple of times.";
    } else message.textContent = "";

    const ids = new Set(pending.map((p) => p.id));
    for (const [id, row] of this._rows) {
      if (!ids.has(id)) {
        row.el.remove();
        this._rows.delete(id);
      }
    }
    pending.forEach((item, index) => {
      let row = this._rows.get(item.id);
      if (!row) {
        row = this._createRow(item.id);
        this._rows.set(item.id, row);
      }
      this._updateRow(row, item);
      if (list.children[index] !== row.el) list.insertBefore(row.el, list.children[index] ?? null);
    });

    bulk.hidden = pending.length < 2;
    const selected = pending.filter((p) => this._selected.has(p.id)).length;
    this._els.selectAll.checked = selected > 0 && selected === pending.length;
    this._els.selectAll.indeterminate = selected > 0 && selected < pending.length;
    this._els.selectedLabel.textContent = selected ? `${selected} selected` : "Select all";
    for (const button of this._els.bulkButtons) {
      button.disabled = !selected || this._busy.size > 0;
    }
  }

  _createRow(id) {
    const el = document.createElement("div");
    el.className = "row";
    el.innerHTML = `
      <input type="checkbox" class="select" aria-label="Select" />
      <div class="content">
        <div class="heard"><span class="text"></span> <span class="seen"></span></div>
        <input class="sentence" type="text" aria-label="Sentence to learn" spellcheck="false" />
        <div class="detail"><b>Does:</b> <span class="action"></span></div>
        <div class="detail"><b>Affected:</b> <span class="entities"></span></div>
        <div class="notes"></div>
        <div class="actions">
          <button class="btn approve" type="button" data-decision="approve">Approve</button>
          <button class="btn" type="button" data-decision="defer" title="Hide until heard again">Later</button>
          <button class="btn reject" type="button" data-decision="reject" title="Never propose again">Reject</button>
        </div>
      </div>
    `;
    const $ = (selector) => el.querySelector(selector);
    const row = {
      el,
      checkbox: $(".select"),
      heard: $(".heard .text"),
      seen: $(".seen"),
      input: $(".sentence"),
      action: $(".action"),
      entities: $(".entities"),
      notes: $(".notes"),
      buttons: [...el.querySelectorAll(".actions .btn")],
    };
    row.checkbox.addEventListener("change", () => {
      if (row.checkbox.checked) this._selected.add(id);
      else this._selected.delete(id);
      this._renderList();
    });
    row.input.addEventListener("input", () => this._edits.set(id, row.input.value));
    row.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") this._review([id], "approve");
    });
    for (const button of row.buttons) {
      button.addEventListener("click", () => this._review([id], button.dataset.decision));
    }
    return row;
  }

  _updateRow(row, item) {
    row.heard.textContent = `“${item.heard}”`;
    row.seen.textContent = `heard ${item.count}×`;
    if (!this._edits.has(item.id) && this._shadow.activeElement !== row.input) {
      row.input.value = item.sentence;
    }
    row.action.textContent = item.action;
    row.entities.textContent = item.entities;
    row.notes.textContent = item.notes;
    row.checkbox.checked = this._selected.has(item.id);
    const busy = this._busy.has(item.id);
    row.el.classList.toggle("busy", busy);
    row.input.disabled = busy;
    for (const button of row.buttons) button.disabled = busy;
  }

  _reviewSelected(decision) {
    const ids = (this._snapshot?.pending ?? [])
      .map((p) => p.id)
      .filter((id) => this._selected.has(id));
    if (!ids.length) return;
    if (
      decision === "reject" &&
      !window.confirm(`Reject ${ids.length} commands? They'll never be proposed again.`)
    ) {
      return;
    }
    this._review(ids, decision);
  }

  async _review(ids, decision) {
    const byId = new Map((this._snapshot?.pending ?? []).map((p) => [p.id, p]));
    const decisions = ids.map((id) => {
      const item = { candidate_id: id, decision };
      const edited = this._edits.get(id)?.trim();
      if (decision === "approve" && edited && edited !== byId.get(id)?.sentence) {
        item.sentence = edited;
      }
      return item;
    });
    for (const id of ids) this._busy.add(id);
    this._error = null;
    this._notice = null;
    this._renderList();
    try {
      const { failed } = await this._hass.callWS({ type: `${DOMAIN}/review`, decisions });
      if (failed.length) {
        this._notice = `${failed.length} command${failed.length > 1 ? "s were" : " was"} already handled elsewhere.`;
      }
    } catch (err) {
      this._error = `Couldn't save: ${err?.message ?? err}`;
    } finally {
      for (const id of ids) this._busy.delete(id);
      this._renderList();
    }
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, AssistLearnerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Assist Learner",
    description: "Commands waiting for review and captured so far. Expand to approve, defer, or reject them.",
    preview: true,
    documentationURL: "https://github.com/Helpmeeters/assist-learner#dashboard-card",
  });
}
