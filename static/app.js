"use strict";

/* ===================================================================== *
 * SQLite Browser - frontend
 * Edits are staged on the server inside an open transaction the moment the
 * user makes them, so the global "Write Changes" / "Revert" buttons map
 * directly to COMMIT / ROLLBACK. The browser keeps no separate edit buffer.
 * ===================================================================== */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  dbOpen: false,
  structure: { tables: [], views: [], indexes: [] },
  selectedObject: null,      // sidebar/structure selection
  browse: {
    table: null, offset: 0, limit: 1000,
    orderBy: null, desc: false, filters: {}, exact: {},
    columns: [], total: 0, hasRowid: true,
    filterOpts: null,   // cached distinct values per column for dropdowns
  },
  lastQuery: null,           // last SELECT executed in SQL tab
};

/* ----------------------------- API helper ---------------------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch { data = { ok: false, error: "Bad response" }; }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}
const jsonPost = (path, body, method = "POST") =>
  api(path, { method, headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body || {}) });

/* ------------------------------ UI utils ----------------------------- */
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast " + kind;
  setTimeout(() => t.classList.add("hidden"), 3200);
}
function setDirty(dirty) {
  $("#dirty-badge").classList.toggle("hidden", !dirty);
  $("#btn-write").disabled = !dirty;
  $("#btn-revert").disabled = !dirty;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function cellText(v) {
  if (v === null || v === undefined) return { text: "(null)", cls: "null" };
  return { text: String(v), cls: "" };
}

/* ------------------------------- Modal ------------------------------- */
function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-overlay").classList.remove("hidden");
}
function closeModal() { $("#modal-overlay").classList.add("hidden"); }
$("#modal-overlay").addEventListener("click", (e) => {
  if (e.target.id === "modal-overlay") closeModal();
});
function confirmModal(title, message, onYes, danger = true) {
  openModal(`
    <h3>${escapeHtml(title)}</h3>
    <p>${message}</p>
    <div class="modal-actions">
      <button id="m-cancel">Cancel</button>
      <button id="m-ok" class="${danger ? "danger" : "primary"}">Confirm</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => { closeModal(); await onYes(); };
}

/* ===================================================================== *
 * Tabs
 * ===================================================================== */
$$(".tab").forEach((tab) => {
  tab.onclick = () => {
    $$(".tab").forEach((t) => t.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#panel-" + tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "backups") loadBackups();
  };
});

/* ===================================================================== *
 * Open / new / upload database
 * ===================================================================== */
$("#btn-open").onclick = async () => {
  openModal(`<h3>Open Database</h3>
    <div class="field"><label>Upload a .db file</label>
      <input type="file" id="db-upload" accept=".db,.sqlite,.sqlite3,.db3"></div>
    <h4>Or pick a file on this machine</h4>
    <div id="db-list" class="muted">Scanning…</div>
    <div class="modal-actions"><button id="m-cancel">Close</button></div>`);
  $("#m-cancel").onclick = closeModal;
  $("#db-upload").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api("/api/upload", { method: "POST", body: fd });
      closeModal(); onDbOpened(r);
    } catch (err) { toast(err.message, "error"); }
  };
  try {
    const r = await api("/api/databases");
    const list = $("#db-list");
    if (!r.databases.length) { list.textContent = "No .db files found nearby."; return; }
    list.className = "";
    list.innerHTML = r.databases.map((d) => `
      <div class="dblist-item" data-path="${escapeHtml(d.path)}">
        <span>${escapeHtml(d.name)}</span>
        <span class="meta">${(d.size/1024).toFixed(0)} KB · ${d.modified}</span>
      </div>`).join("");
    $$("#db-list .dblist-item").forEach((el) => {
      el.onclick = async () => {
        try {
          const res = await jsonPost("/api/open", { path: el.dataset.path });
          closeModal(); onDbOpened(res);
        } catch (err) { toast(err.message, "error"); }
      };
    });
  } catch (err) { $("#db-list").textContent = err.message; }
};

$("#btn-new-db").onclick = () => {
  openModal(`<h3>New Database</h3>
    <div class="field"><label>File name</label>
      <input type="text" id="new-db-name" placeholder="mydata.db"></div>
    <div class="modal-actions">
      <button id="m-cancel">Cancel</button>
      <button id="m-ok" class="primary">Create</button></div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const name = $("#new-db-name").value.trim();
    if (!name) return;
    try {
      const r = await jsonPost("/api/create_db", { name });
      closeModal(); onDbOpened(r);
    } catch (err) { toast(err.message, "error"); }
  };
};

function onDbOpened(r) {
  state.dbOpen = true;
  $("#current-db").textContent = r.path;
  $("#current-db").title = r.path;
  toast(`Opened ${r.name}`, "ok");
  setDirty(false);
  refreshStructure();
}

/* ===================================================================== *
 * Structure: sidebar tree + detail
 * ===================================================================== */
$("#btn-refresh").onclick = () => refreshStructure();

async function refreshStructure() {
  if (!state.dbOpen) return;
  const r = await api("/api/structure");
  state.structure = { tables: r.tables, views: r.views, indexes: r.indexes };
  setDirty(r.dirty);
  renderTree();
  populateTableSelectors();
  if (state.selectedObject) renderStructureDetail(state.selectedObject);
}

function renderTree() {
  const s = state.structure;
  const tree = $("#tree");
  let html = "";
  const group = (title, items, renderItem) => {
    html += `<div class="tree-group">${title} (${items.length})</div>`;
    items.forEach((it) => { html += renderItem(it); });
  };
  group("Tables", s.tables, (t) => `
    <div class="tree-item ${state.selectedObject===t.name?'active':''}" data-name="${escapeHtml(t.name)}" data-kind="table">
      <span>📋 ${escapeHtml(t.name)}</span><span class="count">${t.rows ?? "?"}</span></div>`);
  group("Views", s.views, (v) => `
    <div class="tree-item" data-name="${escapeHtml(v.name)}" data-kind="view">
      <span>👁 ${escapeHtml(v.name)}</span></div>`);
  group("Indexes", s.indexes, (i) => `
    <div class="tree-item" data-name="${escapeHtml(i.name)}" data-kind="index">
      <span>🔑 ${escapeHtml(i.name)}</span></div>`);
  tree.innerHTML = html;
  $$("#tree .tree-item").forEach((el) => {
    el.onclick = () => selectObject(el.dataset.name, el.dataset.kind);
  });
}

function selectObject(name, kind) {
  state.selectedObject = name;
  renderTree();
  $("#structure-target").textContent = `${kind}: ${name}`;
  $("#btn-edit-table").disabled = kind !== "table";
  if (kind === "index") {
    renderIndexDetail(name);
  } else {
    renderStructureDetail(name);
  }
  // Convenience: clicking a table also points Browse at it (and reloads it).
  if (kind === "table" || kind === "view") {
    setBrowseTable(name);
  }
}

async function renderStructureDetail(name) {
  try {
    const r = await api(`/api/table/${encodeURIComponent(name)}/info`);
    const info = r.info;
    let html = `<h4>Columns — ${escapeHtml(name)} (${info.rows ?? "?"} rows)</h4>
      <table><thead><tr><th>#</th><th>Name</th><th>Type</th><th>Not Null</th>
      <th>Default</th><th>PK</th></tr></thead><tbody>`;
    info.columns.forEach((c) => {
      html += `<tr><td>${c.cid}</td><td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.type)}</td>
        <td>${c.notnull ? "✓" : ""}</td><td>${c.default ?? ""}</td>
        <td>${c.pk ? "✓" : ""}</td></tr>`;
    });
    html += "</tbody></table>";
    if (info.foreign_keys.length) {
      html += "<h4>Foreign Keys</h4><table><thead><tr><th>Column</th><th>References</th></tr></thead><tbody>";
      info.foreign_keys.forEach((f) => {
        html += `<tr><td>${escapeHtml(f.from)}</td><td>${escapeHtml(f.table)}(${escapeHtml(f.to)})</td></tr>`;
      });
      html += "</tbody></table>";
    }
    if (info.indexes.length) {
      html += "<h4>Indexes</h4><table><thead><tr><th>Name</th><th>Unique</th><th>Columns</th></tr></thead><tbody>";
      info.indexes.forEach((i) => {
        html += `<tr><td>${escapeHtml(i.name)}</td><td>${i.unique?"✓":""}</td><td>${i.columns.map(escapeHtml).join(", ")}</td></tr>`;
      });
      html += "</tbody></table>";
    }
    $("#structure-detail").innerHTML = html;
  } catch (err) { $("#structure-detail").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
}

function renderIndexDetail(name) {
  $("#structure-detail").innerHTML =
    `<h4>Index: ${escapeHtml(name)}</h4>
     <button id="drop-index-btn" class="danger">Drop Index</button>`;
  $("#btn-edit-table").disabled = true;
  $("#drop-index-btn").onclick = () =>
    confirmModal("Drop index", `Drop index <b>${escapeHtml(name)}</b>?`, async () => {
      await jsonPost(`/api/table/${encodeURIComponent(name)}`,
        { op: "drop_index", index_name: name }, "PATCH");
      toast("Index dropped (staged)", "ok"); refreshStructure();
    });
}

/* --------------------------- New / Edit table ------------------------ */
const TYPES = ["INTEGER", "TEXT", "REAL", "NUMERIC", "BLOB"];
function colRow(c = {}) {
  const typeOpts = TYPES.map((t) =>
    `<option ${c.type===t?"selected":""}>${t}</option>`).join("");
  return `<tr>
    <td><input type="text" class="c-name" value="${escapeHtml(c.name||"")}"></td>
    <td><select class="c-type">${typeOpts}</select></td>
    <td style="text-align:center"><input type="checkbox" class="c-pk" ${c.pk?"checked":""}></td>
    <td style="text-align:center"><input type="checkbox" class="c-nn" ${c.notnull?"checked":""}></td>
    <td><input type="text" class="c-def" value="${escapeHtml(c.default??"")}"></td>
    <td><button class="c-del danger">✕</button></td></tr>`;
}
function collectColumns(scope) {
  return $$(`${scope} tbody tr`).map((tr) => ({
    name: tr.querySelector(".c-name").value.trim(),
    type: tr.querySelector(".c-type").value,
    pk: tr.querySelector(".c-pk").checked,
    notnull: tr.querySelector(".c-nn").checked,
    default: tr.querySelector(".c-def").value.trim() || null,
  })).filter((c) => c.name);
}

$("#btn-new-table").onclick = () => {
  openModal(`<h3>New Table</h3>
    <div class="field"><label>Table name</label><input type="text" id="nt-name"></div>
    <table class="col-editor" id="nt-cols"><thead><tr>
      <th>Name</th><th>Type</th><th>PK</th><th>Not Null</th><th>Default</th><th></th>
      </tr></thead><tbody>${colRow({name:"id",type:"INTEGER",pk:true})}</tbody></table>
    <button id="nt-add">+ Add column</button>
    <div class="modal-actions"><button id="m-cancel">Cancel</button>
      <button id="m-ok" class="primary">Create Table</button></div>`);
  wireColEditor("#nt-cols", "#nt-add");
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const name = $("#nt-name").value.trim();
    const columns = collectColumns("#nt-cols");
    if (!name || !columns.length) { toast("Need a name and a column", "error"); return; }
    try {
      await jsonPost("/api/table", { name, columns });
      closeModal(); toast("Table created (staged)", "ok"); refreshStructure();
    } catch (err) { toast(err.message, "error"); }
  };
};

function wireColEditor(tableSel, addSel) {
  const bind = () => $$(`${tableSel} .c-del`).forEach((b) =>
    b.onclick = () => { b.closest("tr").remove(); });
  bind();
  $(addSel).onclick = () => {
    $(`${tableSel} tbody`).insertAdjacentHTML("beforeend", colRow());
    bind();
  };
}

$("#btn-edit-table").onclick = async () => {
  const name = state.selectedObject;
  if (!name) return;
  const r = await api(`/api/table/${encodeURIComponent(name)}/info`);
  const info = r.info;
  const colsHtml = info.columns.map((c) => `
    <tr data-orig="${escapeHtml(c.name)}">
      <td><input type="text" class="ec-name" value="${escapeHtml(c.name)}"></td>
      <td>${escapeHtml(c.type)}</td>
      <td>${c.pk?"PK":""} ${c.notnull?"NN":""}</td>
      <td><button class="ec-drop danger">Drop</button></td></tr>`).join("");
  openModal(`<h3>Edit Table: ${escapeHtml(name)}</h3>
    <div class="field"><label>Rename table</label>
      <div style="display:flex;gap:6px">
        <input type="text" id="et-rename" value="${escapeHtml(name)}">
        <button id="et-rename-btn">Rename</button></div></div>
    <h4>Columns</h4>
    <table class="col-editor"><thead><tr><th>Name</th><th>Type</th><th>Flags</th><th></th></tr></thead>
      <tbody>${colsHtml}</tbody></table>
    <h4>Add column</h4>
    <table class="col-editor" id="et-newcol"><tbody>${colRow()}</tbody></table>
    <button id="et-add-btn">Add Column</button>
    <h4>Create index</h4>
    <div class="field"><input type="text" id="et-idx-name" placeholder="index name"></div>
    <div class="field"><input type="text" id="et-idx-cols" placeholder="col1, col2"></div>
    <label><input type="checkbox" id="et-idx-unique"> Unique</label>
    <button id="et-idx-btn">Create Index</button>
    <div class="modal-actions"><button id="m-cancel">Close</button></div>`);
  $("#m-cancel").onclick = closeModal;

  $("#et-rename-btn").onclick = async () => {
    const nn = $("#et-rename").value.trim();
    if (!nn || nn === name) return;
    await patch(name, { op: "rename_table", new_name: nn });
    closeModal(); state.selectedObject = nn; refreshStructure();
  };
  $$(".ec-name").forEach((inp) => {
    inp.onchange = async () => {
      const orig = inp.closest("tr").dataset.orig;
      if (inp.value.trim() && inp.value.trim() !== orig)
        await patch(name, { op: "rename_column", old: orig, new: inp.value.trim() });
    };
  });
  $$(".ec-drop").forEach((b) => {
    b.onclick = () => {
      const col = b.closest("tr").dataset.orig;
      confirmModal("Drop column", `Drop column <b>${escapeHtml(col)}</b>? A backup is taken first.`,
        async () => { await patch(name, { op: "drop_column", column: col }); closeModal();
          $("#btn-edit-table").click(); });
    };
  });
  $("#et-add-btn").onclick = async () => {
    const cols = collectColumns("#et-newcol");
    if (!cols.length) return;
    await patch(name, { op: "add_column", column: cols[0] });
    closeModal(); $("#btn-edit-table") && refreshStructure();
  };
  $("#et-idx-btn").onclick = async () => {
    const iname = $("#et-idx-name").value.trim();
    const cols = $("#et-idx-cols").value.split(",").map((s) => s.trim()).filter(Boolean);
    if (!iname || !cols.length) return;
    await patch(name, { op: "create_index", index_name: iname, columns: cols,
      unique: $("#et-idx-unique").checked });
    closeModal(); refreshStructure();
  };
};

async function patch(name, body) {
  try {
    await jsonPost(`/api/table/${encodeURIComponent(name)}`, body, "PATCH");
    toast("Done (staged)", "ok"); refreshStructure();
  } catch (err) { toast(err.message, "error"); }
}

$("#btn-drop-table").onclick = () => {
  const name = state.selectedObject;
  if (!name) { toast("Select a table or view first", "error"); return; }
  confirmModal("Drop object", `Drop <b>${escapeHtml(name)}</b>? A backup is taken first.`,
    async () => {
      try {
        await api(`/api/table/${encodeURIComponent(name)}`, { method: "DELETE" });
        toast("Dropped (staged)", "ok");
        state.selectedObject = null; $("#structure-detail").innerHTML = "";
        refreshStructure();
      } catch (err) { toast(err.message, "error"); }
    });
};

/* ===================================================================== *
 * Browse data
 * ===================================================================== */
function populateTableSelectors() {
  const names = [...state.structure.tables.map((t) => t.name),
                 ...state.structure.views.map((v) => v.name)];
  const tableNames = state.structure.tables.map((t) => t.name);
  fillSelect($("#browse-table"), names);
  fillSelect($("#export-table"), names);
  if (names.length && !state.browse.table) {
    state.browse.table = names[0];
    $("#browse-table").value = names[0];
    loadBrowse();
  }
}
function fillSelect(sel, items) {
  const cur = sel.value;
  sel.innerHTML = items.map((n) => `<option>${escapeHtml(n)}</option>`).join("");
  if (items.includes(cur)) sel.value = cur;
}

// Point Browse Data at a table/view and reload it (resets paging/sort/filters).
// Shared by the dropdown and the structure-tree click, since assigning a
// <select>'s .value does not fire its change handler.
function setBrowseTable(name) {
  state.browse = { ...state.browse, table: name,
    offset: 0, orderBy: null, desc: false, filters: {}, exact: {}, filterOpts: null };
  $("#browse-table").value = name;
  loadBrowse();
}
$("#browse-table").onchange = () => setBrowseTable($("#browse-table").value);
$("#page-size").onchange = () => {
  state.browse.limit = parseInt($("#page-size").value, 10);
  state.browse.offset = 0; loadBrowse();
};
$("#page-first").onclick = () => { state.browse.offset = 0; loadBrowse(); };
$("#page-prev").onclick = () => {
  state.browse.offset = Math.max(0, state.browse.offset - state.browse.limit); loadBrowse(); };
$("#page-next").onclick = () => {
  if (state.browse.offset + state.browse.limit < state.browse.total) {
    state.browse.offset += state.browse.limit; loadBrowse(); } };
$("#page-last").onclick = () => {
  const b = state.browse;
  b.offset = Math.max(0, Math.floor((b.total - 1) / b.limit) * b.limit); loadBrowse(); };

// Tables larger than this skip distinct-value dropdowns (scanning every column
// would be slow); they fall back to free-text filters instead.
const FILTER_MAX = 50000;

// Guards against out-of-order renders: if a newer loadBrowse() starts while an
// older one is awaiting the network, the older one must not overwrite the grid.
let browseLoadSeq = 0;
async function loadBrowse() {
  const b = state.browse;
  const seq = ++browseLoadSeq;
  if (!b.table) { $("#browse-grid").innerHTML = ""; return; }
  const params = new URLSearchParams({ limit: b.limit, offset: b.offset });
  if (b.orderBy) { params.set("order_by", b.orderBy); if (b.desc) params.set("desc", "1"); }
  Object.entries(b.filters).forEach(([k, v]) => { if (v) params.set("f_" + k, v); });
  Object.entries(b.exact).forEach(([k, v]) => { if (v !== "" && v != null) params.set("e_" + k, v); });
  try {
    const r = await api(`/api/table/${encodeURIComponent(b.table)}/rows?${params}`);
    if (seq !== browseLoadSeq) return;  // superseded by a newer load
    b.columns = r.columns; b.total = r.total; b.hasRowid = r.has_rowid;
    // Cache dropdown options once per table (null=unloaded, false=use text).
    if (b.filterOpts === null) {
      if (r.total <= FILTER_MAX) {
        try { b.filterOpts = (await api(
          `/api/table/${encodeURIComponent(b.table)}/filteroptions?cap=300`)).options; }
        catch { b.filterOpts = false; }
      } else { b.filterOpts = false; }
    }
    if (seq !== browseLoadSeq) return;  // superseded while loading filter options
    renderBrowseGrid(r);
    updatePager();
  } catch (err) {
    if (seq !== browseLoadSeq) return;
    $("#browse-grid").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`;
  }
}

function updatePager() {
  const b = state.browse;
  const from = b.total ? b.offset + 1 : 0;
  const to = Math.min(b.offset + b.limit, b.total);
  $("#page-info").textContent = `${from}–${to} of ${b.total}`;
  $("#browse-count").textContent = `${b.total} rows`;
}

function renderBrowseGrid(r) {
  const b = state.browse;
  const editable = b.hasRowid && state.structure.tables.some((t) => t.name === b.table);
  let html = `<table class="grid"><thead><tr>`;
  if (editable) html += `<th class="check-col"><input type="checkbox" id="chk-all"></th>`;
  r.columns.forEach((c) => {
    const sorted = b.orderBy === c ? "sorted " + (b.desc ? "desc" : "") : "";
    html += `<th class="${sorted}" data-col="${escapeHtml(c)}">${escapeHtml(c)}</th>`;
  });
  html += `</tr><tr class="filter-row">`;
  if (editable) html += `<td></td>`;
  r.columns.forEach((c) => {
    const opt = b.filterOpts && b.filterOpts[c];
    if (opt) {
      const active = b.exact[c];
      let o = `<option value="">(all)</option>`;
      let hasActive = false;
      opt.values.forEach((v) => {
        const s = v === null ? "" : String(v);
        const sel = (active != null && s === String(active)) ? "selected" : "";
        if (sel) hasActive = true;
        o += `<option value="${escapeHtml(s)}" ${sel}>${escapeHtml(s)}</option>`;
      });
      if (active != null && active !== "" && !hasActive)
        o += `<option value="${escapeHtml(active)}" selected>${escapeHtml(active)}</option>`;
      if (opt.truncated) o += `<option value="" disabled>… more (refine via SQL)</option>`;
      html += `<td><select class="filter-select" data-col="${escapeHtml(c)}">${o}</select></td>`;
    } else {
      html += `<td><input class="filter-text" data-col="${escapeHtml(c)}" value="${escapeHtml(b.filters[c]||"")}" placeholder="filter"></td>`;
    }
  });
  html += `</tr></thead><tbody>`;
  r.rows.forEach((row) => {
    html += `<tr data-rowid="${row.rowid ?? ""}">`;
    if (editable) html += `<td class="check-col"><input type="checkbox" class="row-chk"></td>`;
    row.values.forEach((v, i) => {
      const { text, cls } = cellText(v);
      const ed = editable ? "editable" : "";
      html += `<td class="${cls} ${ed}" data-col="${escapeHtml(r.columns[i])}">${escapeHtml(text)}</td>`;
    });
    html += `</tr>`;
  });
  html += `</tbody></table>`;
  $("#browse-grid").innerHTML = html;

  // Sorting
  $$("#browse-grid thead th[data-col]").forEach((th) => {
    th.onclick = () => {
      const col = th.dataset.col;
      if (b.orderBy === col) b.desc = !b.desc; else { b.orderBy = col; b.desc = false; }
      b.offset = 0; loadBrowse();
    };
  });
  // Filters: dropdowns (exact match) where available, else free text (substring)
  $$("#browse-grid .filter-select").forEach((sel) => {
    sel.onchange = () => {
      const v = sel.value;
      if (v === "") delete b.exact[sel.dataset.col]; else b.exact[sel.dataset.col] = v;
      b.offset = 0; loadBrowse();
    };
  });
  $$("#browse-grid .filter-text").forEach((inp) => {
    inp.onchange = () => { b.filters[inp.dataset.col] = inp.value; b.offset = 0; loadBrowse(); };
  });
  // Select-all
  const chkAll = $("#chk-all");
  if (chkAll) chkAll.onclick = () =>
    $$("#browse-grid .row-chk").forEach((c) => {
      c.checked = chkAll.checked; c.closest("tr").classList.toggle("selected", chkAll.checked); });
  $$("#browse-grid .row-chk").forEach((c) =>
    c.onclick = () => c.closest("tr").classList.toggle("selected", c.checked));

  // Inline editing
  if (editable) {
    $$("#browse-grid td.editable").forEach((td) => {
      td.ondblclick = () => beginCellEdit(td);
    });
  }
}

function beginCellEdit(td) {
  const tr = td.closest("tr");
  const rowid = tr.dataset.rowid;
  const col = td.dataset.col;
  const wasNull = td.classList.contains("null");
  const original = wasNull ? "" : td.textContent;
  td.contentEditable = "true";
  td.classList.remove("null");
  if (wasNull) td.textContent = "";
  td.focus();
  document.execCommand && document.getSelection().selectAllChildren(td);

  const finish = async (commit) => {
    td.contentEditable = "false";
    td.removeEventListener("blur", onBlur);
    td.removeEventListener("keydown", onKey);
    const val = td.textContent;
    if (!commit || val === original) {
      if (wasNull && val === "") { td.textContent = "(null)"; td.classList.add("null"); }
      else td.textContent = val;
      return;
    }
    try {
      await jsonPost(`/api/table/${encodeURIComponent(state.browse.table)}/changes`,
        { updates: [{ rowid: numId(rowid), values: { [col]: val } }] });
      tr.classList.add("row-edited");
      setDirty(true);
    } catch (err) { toast(err.message, "error"); td.textContent = original; }
  };
  const onBlur = () => finish(true);
  const onKey = (e) => {
    if (e.key === "Enter") { e.preventDefault(); td.blur(); }
    if (e.key === "Escape") { td.textContent = original; td.blur(); }
  };
  td.addEventListener("blur", onBlur);
  td.addEventListener("keydown", onKey);
}
function numId(s) { const n = Number(s); return Number.isNaN(n) ? s : n; }

$("#btn-add-row").onclick = async () => {
  if (!state.browse.table) return;
  try {
    await jsonPost(`/api/table/${encodeURIComponent(state.browse.table)}/changes`,
      { inserts: [{}] });
    setDirty(true); toast("Row added (staged) — double-click cells to edit", "ok");
    // Jump to the last page so the new row is visible.
    const b = state.browse; b.total += 1;
    b.offset = Math.max(0, Math.floor((b.total - 1) / b.limit) * b.limit);
    loadBrowse();
  } catch (err) { toast(err.message, "error"); }
};

$("#btn-del-rows").onclick = () => {
  const ids = $$("#browse-grid .row-chk:checked")
    .map((c) => numId(c.closest("tr").dataset.rowid));
  if (!ids.length) { toast("Select rows first", "error"); return; }
  confirmModal("Delete rows", `Delete <b>${ids.length}</b> row(s)? Staged until you Write Changes.`,
    async () => {
      try {
        await jsonPost(`/api/table/${encodeURIComponent(state.browse.table)}/changes`,
          { deletes: ids });
        setDirty(true); toast(`${ids.length} row(s) deleted (staged)`, "ok"); loadBrowse();
      } catch (err) { toast(err.message, "error"); }
    });
};

$("#btn-bulk-edit").onclick = () => {
  const b = state.browse;
  if (!b.table || !b.columns.length) return;
  const opts = b.columns.map((c) => `<option>${escapeHtml(c)}</option>`).join("");
  const fcount = Object.values(b.filters).filter(Boolean).length + Object.keys(b.exact).length;
  openModal(`<h3>Bulk Edit Column</h3>
    <p class="muted">Sets a column for ${fcount ? "all <b>filtered</b>" : "<b>all</b>"} rows
      in <b>${escapeHtml(b.table)}</b>.</p>
    <div class="field"><label>Column</label><select id="be-col">${opts}</select></div>
    <div class="field"><label>New value</label><input type="text" id="be-val"></div>
    <div class="modal-actions"><button id="m-cancel">Cancel</button>
      <button id="m-ok" class="primary">Apply</button></div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    try {
      const r = await jsonPost(`/api/table/${encodeURIComponent(b.table)}/bulk_update`,
        { column: $("#be-col").value, value: $("#be-val").value,
          filters: b.filters, exact_filters: b.exact });
      closeModal(); setDirty(true); toast(`${r.updated} row(s) updated (staged)`, "ok"); loadBrowse();
    } catch (err) { toast(err.message, "error"); }
  };
};

/* ===================================================================== *
 * Write / revert / backup
 * ===================================================================== */
$("#btn-write").onclick = async () => {
  try { await jsonPost("/api/write", {}); setDirty(false);
    state.browse.filterOpts = null;
    toast("Changes written (auto-backed up)", "ok"); refreshStructure(); loadBrowse();
  } catch (err) { toast(err.message, "error"); }
};
$("#btn-revert").onclick = () => {
  confirmModal("Revert", "Discard all staged changes?", async () => {
    await jsonPost("/api/revert", {}); setDirty(false);
    state.browse.filterOpts = null;
    toast("Reverted", "ok"); refreshStructure(); loadBrowse();
  });
};
$("#btn-backup").onclick = async () => {
  try { const r = await jsonPost("/api/backup", {}); toast(`Backup: ${r.backup}`, "ok"); }
  catch (err) { toast(err.message, "error"); }
};

/* ===================================================================== *
 * Execute SQL
 * ===================================================================== */
async function runSql() {
  const sql = $("#sql-input").value.trim();
  if (!sql) return;
  const status = $("#sql-status");
  try {
    const r = await api("/api/sql", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql }) });
    setDirty(r.dirty);
    const res = r.result;
    if (res.type === "select") {
      state.lastQuery = sql;
      const shown = renderSqlGrid(res);
      status.className = "sql-status ok";
      status.textContent = shown < res.rowcount
        ? `${res.rowcount} row(s) — showing first ${shown}. Add a LIMIT, or use Import/Export to save the full result.`
        : `${res.rowcount} row(s).`;
    } else {
      $("#sql-grid").innerHTML = "";
      status.className = "sql-status ok";
      status.textContent = `Statement OK. ${res.rowcount >= 0 ? res.rowcount + " row(s) affected." : ""}` +
        (r.dirty ? " (staged — Write Changes to commit)" : "");
      refreshStructure();
    }
    loadHistory();
  } catch (err) {
    status.className = "sql-status error"; status.textContent = err.message;
    $("#sql-grid").innerHTML = "";
  }
}
// Rendering tens of thousands of <tr> in one shot freezes the browser, so the
// grid is capped. The full result is still available via export.
const SQL_RENDER_CAP = 2000;
function renderSqlGrid(res) {
  const limit = Math.min(res.rows.length, SQL_RENDER_CAP);
  let html = `<table class="grid"><thead><tr>`;
  res.columns.forEach((c) => html += `<th>${escapeHtml(c)}</th>`);
  html += `</tr></thead><tbody>`;
  for (let i = 0; i < limit; i++) {
    html += "<tr>";
    res.rows[i].forEach((v) => { const { text, cls } = cellText(v);
      html += `<td class="${cls}">${escapeHtml(text)}</td>`; });
    html += "</tr>";
  }
  html += "</tbody></table>";
  $("#sql-grid").innerHTML = html;
  return limit;
}
$("#btn-run-sql").onclick = runSql;
$("#sql-input").addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); runSql(); }
});

async function loadHistory() {
  try {
    const r = await api("/api/history");
    $("#history-select").innerHTML = `<option value="">— history —</option>` +
      r.history.map((h, i) =>
        `<option value="${i}">${escapeHtml(h.sql.slice(0, 60))}</option>`).join("");
    state._history = r.history;
  } catch {}
}
$("#history-select").onchange = (e) => {
  const h = (state._history || [])[e.target.value];
  if (h) $("#sql-input").value = h.sql;
};
async function loadSaved() {
  try {
    const r = await api("/api/saved");
    state._saved = r.saved;
    $("#saved-select").innerHTML = `<option value="">— saved —</option>` +
      r.saved.map((s) => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join("");
  } catch {}
}
$("#saved-select").onchange = (e) => {
  const s = (state._saved || []).find((x) => String(x.id) === e.target.value);
  if (s) $("#sql-input").value = s.sql;
};
$("#btn-save-query").onclick = () => {
  const sql = $("#sql-input").value.trim();
  if (!sql) return;
  openModal(`<h3>Save Query</h3>
    <div class="field"><label>Name</label><input type="text" id="sq-name"></div>
    <div class="modal-actions"><button id="m-cancel">Cancel</button>
      <button id="m-ok" class="primary">Save</button></div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-ok").onclick = async () => {
    const name = $("#sq-name").value.trim(); if (!name) return;
    await jsonPost("/api/saved", { name, sql });
    closeModal(); toast("Saved", "ok"); loadSaved();
  };
};
$("#btn-del-saved").onclick = async () => {
  const id = $("#saved-select").value; if (!id) return;
  await api(`/api/saved/${id}`, { method: "DELETE" });
  toast("Deleted", "ok"); loadSaved();
};

/* ===================================================================== *
 * Import / export
 * ===================================================================== */
$("#btn-import").onclick = async () => {
  const file = $("#import-file").files[0];
  const table = $("#import-table").value.trim();
  if (!file || !table) { toast("Pick a file and table name", "error"); return; }
  const fd = new FormData();
  fd.append("file", file); fd.append("table", table);
  fd.append("mode", $("#import-mode").value);
  $("#import-status").textContent = "Importing…";
  try {
    const r = await api("/api/import", { method: "POST", body: fd });
    $("#import-status").textContent = `Imported ${r.rows} rows into ${table}.`;
    state.browse.filterOpts = null;
    toast(`Imported ${r.rows} rows`, "ok"); refreshStructure();
  } catch (err) { $("#import-status").textContent = ""; toast(err.message, "error"); }
};
$("#btn-export-table").onclick = () => {
  const table = $("#export-table").value;
  if (!table) return;
  window.location = `/api/export?table=${encodeURIComponent(table)}&format=${$("#export-format").value}`;
};
$("#btn-export-query").onclick = () => {
  if (!state.lastQuery) { toast("Run a SELECT in the SQL tab first", "error"); return; }
  window.location = `/api/export?query=${encodeURIComponent(state.lastQuery)}&format=${$("#export-format").value}`;
};
$("#btn-download-db").onclick = () => { window.location = "/api/download"; };

/* ===================================================================== *
 * Backups
 * ===================================================================== */
$("#btn-make-backup").onclick = async () => {
  try { const r = await jsonPost("/api/backup", {}); toast(`Backup: ${r.backup}`, "ok"); loadBackups(); }
  catch (err) { toast(err.message, "error"); }
};
async function loadBackups() {
  if (!state.dbOpen) return;
  try {
    const r = await api("/api/backups");
    if (!r.backups.length) { $("#backups-list").innerHTML = `<p class="muted">No backups yet.</p>`; return; }
    $("#backups-list").innerHTML = `<table><thead><tr><th>Backup</th><th>Size</th><th>When</th><th></th></tr></thead><tbody>` +
      r.backups.map((b) => `<tr><td>${escapeHtml(b.name)}</td>
        <td>${(b.size/1024).toFixed(0)} KB</td><td>${b.modified}</td>
        <td><button class="restore-btn danger" data-name="${escapeHtml(b.name)}">Restore</button></td></tr>`).join("") +
      `</tbody></table>`;
    $$(".restore-btn").forEach((btn) => {
      btn.onclick = () => confirmModal("Restore backup",
        `Replace the current database with <b>${escapeHtml(btn.dataset.name)}</b>? This overwrites the live file.`,
        async () => {
          await jsonPost("/api/restore", { name: btn.dataset.name });
          state.browse.filterOpts = null;
          toast("Restored", "ok"); setDirty(false); refreshStructure(); loadBrowse();
        });
    });
  } catch (err) { $("#backups-list").innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
}

/* ===================================================================== *
 * Boot
 * ===================================================================== */
(async function boot() {
  try {
    const s = await api("/api/status");
    if (s.open) onDbOpened(s);
  } catch {}
  loadHistory(); loadSaved();
})();
