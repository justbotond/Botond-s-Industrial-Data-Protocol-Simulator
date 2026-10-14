/* Botond's Industrial Data Protocol Simulator - web UI (no build step, plain JS) */
"use strict";

// ====================================================================== helpers
const $ = (sel, root = document) => root.querySelector(sel);
const fmtInt = n => (n ?? 0).toLocaleString("en-US");

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "html") e.innerHTML = v;
    else if (k in e && typeof v !== "string") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opt);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* plain text error */ }
  if (!r.ok) throw new Error((data && data.error) || text || `${r.status} ${r.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isErr ? 7000 : 3000);
}

function fmtValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number" && !Number.isInteger(v)) return v.toFixed(4);
  return String(v);
}
function fmtUptime(s) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}
const localHost = h => (!h || h === "0.0.0.0" ? "127.0.0.1" : h);

// ====================================================================== schema
const DATA_TYPES = ["float32", "float64", "int16", "uint16", "int32", "uint32", "bool"];
const GENERATORS = ["random", "random_walk", "sine", "ramp", "square", "counter", "constant"];
const MB_AREAS = ["holding_registers", "input_registers", "coils", "discrete_inputs"];
const BAC_TYPES = ["analog-input", "analog-value", "binary-input", "binary-value"];
const REG_WIDTH = { int16: 1, uint16: 1, bool: 1, int32: 2, uint32: 2, float32: 2, float64: 4 };

const TAG_COMMON = [
  { k: "name", label: "Name prefix", type: "text", def: "Tag", w: 120 },
  { k: "count", label: "Count", type: "int", def: 100 },
  { k: "data_type", label: "Data type", type: "select", options: DATA_TYPES, def: "float32" },
  { k: "generator", label: "Generator", type: "select", options: GENERATORS, def: "random" },
  { k: "min", label: "Min", type: "num", def: 0 },
  { k: "max", label: "Max", type: "num", def: 100 },
  { k: "period_s", label: "Period s", type: "num", def: 60, title: "sine / ramp / square period" },
  { k: "step", label: "Step", type: "num", def: 1, title: "random_walk / counter step" },
];

const KIND = {
  modbus: {
    label: "Modbus TCP server", short: "Modbus",
    fields: [
      { k: "name", label: "Name", type: "text" },
      { k: "host", label: "Bind address", type: "text", hint: "0.0.0.0 = all interfaces" },
      { k: "port", label: "TCP port", type: "int", hint: "standard is 502" },
      { k: "update_interval_ms", label: "Update interval (ms)", type: "int" },
      { k: "word_order", label: "Word order (32/64-bit)", type: "select", options: ["big", "little"], hint: "big = ABCD, little = CDAB" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: [...TAG_COMMON,
      { k: "area", label: "Area", type: "select", options: MB_AREAS, def: "holding_registers" },
      { k: "start_address", label: "Start addr", type: "int", def: "", hint: "blank = auto", optional: true }],
    make: name => ({ name, enabled: true, host: "0.0.0.0", port: freePort("modbus", 1502), update_interval_ms: 1000, word_order: "big",
      tags: [{ name: "HR_Float", count: 100, data_type: "float32", generator: "random", min: 0, max: 100, area: "holding_registers" }] }),
    endpoint: c => `modbus://${localHost(c.host)}:${c.port}`,
  },
  opcua: {
    label: "OPC UA server", short: "OPC UA",
    fields: [
      { k: "name", label: "Name", type: "text", hint: "also the endpoint path" },
      { k: "host", label: "Bind address", type: "text" },
      { k: "port", label: "TCP port", type: "int", hint: "standard is 4840" },
      { k: "update_interval_ms", label: "Update interval (ms)", type: "int" },
      { k: "namespace", label: "Namespace URI", type: "text" },
      { k: "folder_size", label: "Tags per folder", type: "int", hint: "large groups are split into sub-folders" },
      { k: "writable", label: "Clients may write values", type: "bool" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: TAG_COMMON,
    make: name => ({ name, enabled: true, host: "0.0.0.0", port: freePort("opcua", 4840), update_interval_ms: 1000,
      namespace: "urn:botond:simulator", writable: false, folder_size: 1000,
      tags: [{ name: "Random", count: 1000, data_type: "float64", generator: "random", min: 0, max: 100 }] }),
    endpoint: c => `opc.tcp://${localHost(c.host)}:${c.port}/${c.name}`,
  },
  opcda: {
    label: "OPC DA server", short: "OPC DA",
    fields: [
      { k: "name", label: "Name", type: "text", hint: "label in this tool and for COM auto-start" },
      { k: "prog_id", label: "ProgID", type: "text", hint: "what OPC DA clients connect to" },
      { k: "update_interval_ms", label: "Update interval (ms)", type: "int" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: TAG_COMMON,
    make: name => ({ name, enabled: true, prog_id: "BIDPS.OpcDaSimulator.1", update_interval_ms: 1000,
      tags: [{ name: "Random", count: 1000, data_type: "float64", generator: "random", min: 0, max: 100 }] }),
    endpoint: c => `opcda://localhost/${c.prog_id}`,
  },
  mqtt_broker: {
    label: "MQTT broker (internal)", short: "Broker",
    fields: [
      { k: "name", label: "Name", type: "text" },
      { k: "host", label: "Bind address", type: "text" },
      { k: "port", label: "TCP port", type: "int", hint: "standard is 1883" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: null,
    make: name => ({ name, enabled: true, host: "0.0.0.0", port: freePort("mqtt_broker", 1883) }),
    endpoint: c => `mqtt://${localHost(c.host)}:${c.port}`,
  },
  mqtt: {
    label: "MQTT publisher", short: "Publisher",
    fields: [
      { k: "name", label: "Name", type: "text" },
      { k: "broker_host", label: "Broker host", type: "text", hint: "127.0.0.1 = internal broker" },
      { k: "broker_port", label: "Broker port", type: "int" },
      { k: "update_interval_ms", label: "Publish interval (ms)", type: "int" },
      { k: "topic_prefix", label: "Topic prefix", type: "text" },
      { k: "mode", label: "Mode", type: "select", options: ["per_tag", "batch"], hint: "per_tag = one topic per tag" },
      { k: "batch_size", label: "Tags per batch message", type: "int" },
      { k: "payload_format", label: "Payload (per_tag)", type: "select", options: ["json", "plain"] },
      { k: "qos", label: "QoS", type: "select", options: [0, 1] },
      { k: "username", label: "Username", type: "text", optional: true },
      { k: "password", label: "Password", type: "password", optional: true },
      { k: "retain", label: "Retain", type: "bool" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: TAG_COMMON,
    make: name => ({ name, enabled: true, broker_host: "127.0.0.1", broker_port: firstBrokerPort(), update_interval_ms: 1000,
      topic_prefix: `sim/${name}`, mode: "per_tag", batch_size: 1000, payload_format: "json", qos: 0, retain: false,
      tags: [{ name: "Temp", count: 100, data_type: "float32", generator: "random_walk", min: 20, max: 80, step: 0.5 }] }),
    endpoint: c => `mqtt://${c.broker_host}:${c.broker_port} → ${c.topic_prefix || "sim/" + c.name}/#`,
  },
  bacnet: {
    label: "BACnet/IP device", short: "BACnet",
    fields: [
      { k: "name", label: "Device name", type: "text" },
      { k: "ip_address", label: "IP address / prefix", type: "text", hint: "auto, or e.g. 192.168.1.20/24" },
      { k: "port", label: "UDP port", type: "int", hint: "standard is 47808 (0xBAC0)" },
      { k: "device_instance", label: "Device instance", type: "int", hint: "0 … 4194302, unique on the network" },
      { k: "update_interval_ms", label: "Update interval (ms)", type: "int" },
      { k: "enabled", label: "Include in “Start all”", type: "bool" },
    ],
    tagCols: [
      { k: "name", label: "Name prefix", type: "text", def: "AI" },
      { k: "count", label: "Count", type: "int", def: 50 },
      { k: "object_type", label: "Object type", type: "select", options: BAC_TYPES, def: "analog-input" },
      { k: "start_instance", label: "First instance", type: "int", def: "", optional: true },
      ...TAG_COMMON.slice(3)],
    make: name => ({ name, enabled: true, ip_address: "auto", port: freePort("bacnet", 47808),
      device_instance: freeDeviceInstance(), update_interval_ms: 1000,
      tags: [{ name: "AI", count: 50, object_type: "analog-input", generator: "sine", min: 0, max: 100, period_s: 120 }] }),
    endpoint: c => `bacnet://${c.ip_address === "auto" ? (S.meta?.primary_ip || "host") : c.ip_address.split("/")[0]}:${c.port} device ${c.device_instance}`,
  },
};

// ---------------------------------------------------------------- presets / suggestions
const t = (name, count, extra) => ({ name, count, generator: "random", min: 0, max: 100, ...extra });
const PRESETS = {
  modbus: [
    { name: "Energy meter", desc: "≈120 input registers (float32 V, A, kW, kWh…) + 20 holding setpoints",
      tags: [t("Meas", 60, { data_type: "float32", area: "input_registers", generator: "random_walk", min: 0, max: 400, step: 2 }),
             t("Setpoint", 20, { data_type: "uint16", area: "holding_registers", generator: "constant", min: 0, max: 100 })] },
    { name: "Small PLC", desc: "512 coils, 512 discrete inputs, 256 input + 512 holding registers (int16)",
      tags: [t("Q", 512, { data_type: "bool", area: "coils", generator: "square", period_s: 20 }),
             t("I", 512, { data_type: "bool", area: "discrete_inputs", generator: "square", period_s: 15 }),
             t("AI", 256, { data_type: "int16", area: "input_registers", generator: "sine", min: 0, max: 27648, period_s: 120 }),
             t("MW", 512, { data_type: "int16", area: "holding_registers", generator: "random_walk", min: 0, max: 1000, step: 5 })] },
    { name: "Medium PLC", desc: "2,000 coils/inputs, 2,000 float32 inputs, 4,000 int16 holding",
      tags: [t("Q", 2000, { data_type: "bool", area: "coils", generator: "square", period_s: 20 }),
             t("I", 2000, { data_type: "bool", area: "discrete_inputs", generator: "square", period_s: 30 }),
             t("AI", 2000, { data_type: "float32", area: "input_registers", generator: "sine", period_s: 120 }),
             t("MW", 4000, { data_type: "int16", area: "holding_registers", generator: "random_walk", min: 0, max: 1000, step: 5 })] },
    { name: "Classic 5-digit map (x0001–x9999)", desc: "Fills the classic Modicon ranges: 9,999 coils/inputs, 9,999 IR, 4,999 float32 HR",
      tags: [t("C", 9999, { data_type: "bool", area: "coils", generator: "square", period_s: 30 }),
             t("DI", 9999, { data_type: "bool", area: "discrete_inputs", generator: "square", period_s: 45 }),
             t("IR", 9999, { data_type: "int16", area: "input_registers", generator: "random", min: 0, max: 10000 }),
             t("HR", 4999, { data_type: "float32", area: "holding_registers" })] },
    { name: "Stress test – full address space", desc: "65,536 coils + 65,536 inputs + 32,768 float32 IR + 32,768 float32 HR",
      tags: [t("C", 65536, { data_type: "bool", area: "coils", generator: "square", period_s: 30 }),
             t("DI", 65536, { data_type: "bool", area: "discrete_inputs", generator: "square", period_s: 45 }),
             t("IR", 32768, { data_type: "float32", area: "input_registers" }),
             t("HR", 32768, { data_type: "float32", area: "holding_registers" })] },
  ],
  opcua: [
    { name: "1,000 tags", desc: "Small server: 1,000 random doubles", tags: [t("Random", 1000, { data_type: "float64" })] },
    { name: "Mixed plant (≈5,500)", desc: "Analogs, counters, states",
      tags: [t("Temp", 2000, { data_type: "float32", generator: "random_walk", min: 20, max: 90, step: 0.3 }),
             t("Flow", 2000, { data_type: "float64", generator: "sine", period_s: 300 }),
             t("Count", 500, { data_type: "uint32", generator: "counter", min: 0, max: 1000000, step: 1 }),
             t("Running", 1000, { data_type: "bool", generator: "square", period_s: 60 })] },
    { name: "40,000 tags @ 500 ms", desc: "The load test from session 1", tags: [t("Random", 40000, { data_type: "float64" })] },
  ],
  opcda: [
    { name: "1,000 tags", desc: "1,000 random doubles", tags: [t("Random", 1000, { data_type: "float64" })] },
    { name: "Mixed (≈3,000)", desc: "Analogs, counters, states",
      tags: [t("Temp", 1500, { data_type: "float32", generator: "random_walk", min: 20, max: 90, step: 0.3 }),
             t("Count", 500, { data_type: "int32", generator: "counter", min: 0, max: 1000000 }),
             t("Running", 1000, { data_type: "bool", generator: "square", period_s: 60 })] },
  ],
  mqtt: [
    { name: "100 sensors", desc: "100 topics, JSON payloads", tags: [t("Temp", 100, { data_type: "float32", generator: "random_walk", min: 20, max: 80, step: 0.5 })] },
    { name: "1,000 sensors + states", desc: "1,000 analogs + 100 booleans",
      tags: [t("Temp", 1000, { data_type: "float32", generator: "random_walk", min: 20, max: 80, step: 0.5 }),
             t("Status", 100, { data_type: "bool", generator: "square", period_s: 30 })] },
    { name: "10,000 tags (use batch mode)", desc: "Switch mode to batch for high counts",
      tags: [t("Value", 10000, { data_type: "float64" })] },
  ],
  bacnet: [
    { name: "AHU / small controller", desc: "20 AI, 10 BI, 20 AV, 10 BV",
      tags: [t("AI", 20, { object_type: "analog-input", generator: "random_walk", min: 15, max: 30, step: 0.2 }),
             t("BI", 10, { object_type: "binary-input", generator: "square", period_s: 120 }),
             t("AV", 20, { object_type: "analog-value", generator: "constant", min: 21, max: 21 }),
             t("BV", 10, { object_type: "binary-value", generator: "square", period_s: 300 })] },
    { name: "Building supervisory (≈1,000)", desc: "500 AI, 200 BI, 200 AV, 100 BV",
      tags: [t("AI", 500, { object_type: "analog-input", generator: "random_walk", min: 15, max: 30, step: 0.2 }),
             t("BI", 200, { object_type: "binary-input", generator: "square", period_s: 120 }),
             t("AV", 200, { object_type: "analog-value", generator: "sine", period_s: 600 }),
             t("BV", 100, { object_type: "binary-value", generator: "square", period_s: 300 })] },
    { name: "Stress: 5,000 analog values", desc: "5,000 AV at 1 s; builds in ~6 s",
      tags: [t("AV", 5000, { object_type: "analog-value" })] },
  ],
};

// ====================================================================== state
const S = {
  cfg: null, meta: null, status: {}, page: "overview", tabs: {}, dirty: false,
  logs: [], logsSince: 0, collapsed: {}, client: {}, timers: {},
};

function allEntries() {
  const out = [];
  for (const kind of Object.keys(KIND)) for (const c of S.cfg[kind] || []) out.push([kind, c]);
  return out;
}
function freePort(kind, base) {
  const used = new Set(allEntries().filter(([k]) => (k === "bacnet") === (kind === "bacnet")).map(([, c]) => +c.port));
  let p = base; while (used.has(p)) p++; return p;
}
function freeDeviceInstance() {
  const used = new Set((S.cfg.bacnet || []).map(c => +c.device_instance));
  let i = 1001; while (used.has(i)) i++; return i;
}
function firstBrokerPort() { return (S.cfg.mqtt_broker || [])[0]?.port || 1883; }
function uniqueName(prefix) {
  const names = new Set(allEntries().map(([, c]) => c.name));
  let i = 1; while (names.has(prefix + i)) i++; return prefix + i;
}
function isRunning(name) {
  const st = S.status[name]?.state; return st === "running" || st === "starting" || st === "stopping";
}
function markDirty() { S.dirty = true; $("#dirtyFlag").hidden = false; }

async function saveConfig(quiet = false) {
  await api("PUT", "/api/config", S.cfg);
  S.dirty = false; $("#dirtyFlag").hidden = true;
  if (!quiet) toast("Configuration saved to config.yaml");
}

// ====================================================================== instance actions
async function act(name, action) {
  try {
    if (action !== "stop" && S.dirty) await saveConfig(true);
    await api("POST", `/api/instances/${encodeURIComponent(name)}/${action}`);
    await refreshStatus();
  } catch (e) { toast(e.message, true); }
}

function tagTotal(c) { return (c.tags || []).reduce((a, g) => a + (+g.count || 0), 0); }

function modbusUsage(c) {
  const used = { holding_registers: 0, input_registers: 0, coils: 0, discrete_inputs: 0 };
  for (const g of c.tags || []) {
    const area = g.area || "holding_registers";
    const w = area === "coils" || area === "discrete_inputs" ? 1 : REG_WIDTH[g.data_type || "float32"];
    const start = g.start_address === "" || g.start_address == null ? used[area] : +g.start_address;
    used[area] = Math.max(used[area], start + w * (+g.count || 0));
  }
  return used;
}

// ====================================================================== rendering: generic bits
function fieldInput(def, obj, onChange, disabled) {
  const v = obj[def.k];
  const set = val => { if (def.optional && (val === "" || val === null)) delete obj[def.k]; else obj[def.k] = val; markDirty(); onChange && onChange(); };
  if (def.type === "bool") {
    return el("label", { class: "field check" },
      el("input", { type: "checkbox", checked: !!v, disabled, onchange: e => set(e.target.checked) }), def.label);
  }
  let input;
  if (def.type === "select") {
    input = el("select", { disabled, onchange: e => set(typeof def.options[0] === "number" ? +e.target.value : e.target.value) },
      def.options.map(o => el("option", { value: o, selected: String(o) === String(v ?? def.def) }, String(o))));
  } else {
    const numeric = def.type === "int" || def.type === "num";
    input = el("input", {
      type: numeric ? "number" : def.type === "password" ? "password" : "text", value: v ?? "", disabled,
      step: def.type === "num" ? "any" : undefined, placeholder: def.optional ? (def.hint || "") : "",
      onchange: e => {
        const raw = e.target.value;
        set(numeric ? (raw === "" ? (def.optional ? "" : 0) : def.type === "int" ? parseInt(raw, 10) : parseFloat(raw)) : raw);
      },
    });
  }
  return el("label", { class: "field", title: def.title || "" }, def.label, input, def.hint && !def.optional ? el("span", { class: "hint" }, def.hint) : null);
}

function tagTable(kind, c, onChange) {
  const cols = KIND[kind].tagCols;
  const tbody = el("tbody");
  const redraw = () => {
    tbody.replaceChildren(...(c.tags || []).map((g, i) => el("tr", {},
      cols.map(col => {
        const lock = kind === "modbus" && col.k === "data_type" && (g.area === "coils" || g.area === "discrete_inputs");
        const f = fieldInput(col, g, () => { onChange(); if (col.k === "area") redraw(); }, lock);
        const input = f.querySelector("input,select");
        if (lock) input.value = "bool";
        if (col.w) input.style.width = col.w + "px";
        return el("td", {}, input);
      }),
      el("td", {}, el("button", { class: "btn-link", title: "Remove group", onclick: () => { c.tags.splice(i, 1); markDirty(); redraw(); onChange(); } }, "✕")),
    )));
  };
  redraw();
  return el("div", {},
    el("div", { class: "table-wrap", style: "max-height:none" },
      el("table", {}, el("thead", {}, el("tr", {}, cols.map(col => el("th", { title: col.title || col.hint || "" }, col.label)), el("th"))), tbody)),
    el("div", { class: "row", style: "margin-top:8px" },
      el("button", { class: "btn btn-sm", onclick: () => {
        const g = {}; for (const col of cols) if (col.def !== "" && col.def !== undefined) g[col.k] = col.def;
        g.name = (cols[0].def || "Tag") + ((c.tags || []).length + 1) + "_";
        (c.tags = c.tags || []).push(g); markDirty(); redraw(); onChange();
      } }, "+ Add tag group")));
}

function presetPicker(kind, c, after) {
  const list = PRESETS[kind];
  if (!list) return null;
  return el("details", { style: "margin-top:12px" },
    el("summary", { class: "muted", style: "cursor:pointer" }, "Suggested tag layouts…"),
    el("div", { class: "preset-list", style: "margin-top:8px" }, list.map(p =>
      el("div", { class: "preset", onclick: () => {
        if (!confirm(`Replace the tag groups of '${c.name}' with “${p.name}”?`)) return;
        c.tags = JSON.parse(JSON.stringify(p.tags)); markDirty(); after();
      } }, el("b", {}, p.name), el("span", {}, p.desc)))));
}

// ---------------------------------------------------------------- instance card
function statsLine(kind, st) {
  const s = st?.stats || {};
  const parts = [];
  const add = (label, v) => { if (v !== undefined && v !== null) parts.push(el("span", {}, label + " ", el("b", {}, v))); };
  if (st?.uptime_s != null) add("up", fmtUptime(st.uptime_s));
  if (s.phase && s.phase !== "running") add("phase", s.phase);
  if (s.tags != null) add("tags", fmtInt(s.tags));
  if (s.cycles != null) add("cycles", fmtInt(s.cycles));
  if (s.last_cycle_ms != null) add("cycle", `${s.last_cycle_ms} / ${s.interval_ms} ms`);
  if (s.overruns) add("late", fmtInt(s.overruns));
  if (s.clients != null) add("clients", s.clients);
  if (s.subscriptions != null) add("subscriptions", s.subscriptions);
  if (s.requests != null) add("requests", fmtInt(s.requests));
  if (s.messages_sent != null) add("sent", fmtInt(s.messages_sent));
  if (s.connected === false) add("broker", "not connected");
  if (s.messages_in != null) add("in", fmtInt(s.messages_in));
  if (s.messages_out != null) add("out", fmtInt(s.messages_out));
  if (s.dropped) add("dropped", fmtInt(s.dropped));
  if (s.address) add("address", s.address);
  return parts;
}

function controlButtons(name) {
  const st = S.status[name]?.state || "stopped";
  const running = st === "running" || st === "starting";
  return [
    running ? null : el("button", { class: "btn btn-sm btn-go", onclick: () => act(name, "start") }, "Start"),
    running ? el("button", { class: "btn btn-sm", onclick: () => act(name, "restart") }, "Restart") : null,
    running ? el("button", { class: "btn btn-sm btn-stop", onclick: () => act(name, "stop") }, "Stop") : null,
  ];
}

function updateStatusViews() {
  document.querySelectorAll("[data-status]").forEach(node => {
    const name = node.dataset.status;
    const st = S.status[name];
    const state = st?.state || "stopped";
    const role = node.dataset.role;
    if (role === "dot") node.className = "dot " + state;
    else if (role === "state") {
      node.textContent = state + (st?.config_changed ? " · restart to apply changes" : "");
      node.className = "badge" + (state === "error" ? " err" : st?.config_changed ? " warn" : "");
    } else if (role === "stats") node.replaceChildren(...statsLine(null, st));
    else if (role === "buttons") {
      if (node.dataset.last === state) return; // rebuild only on change, so clicks are never lost
      node.dataset.last = state;
      node.replaceChildren(...controlButtons(name).filter(Boolean));
    }
    else if (role === "error") { node.textContent = st?.error || ""; node.hidden = !st?.error; }
    else if (role === "lock") node.querySelectorAll('input[data-name-field]').forEach(i => (i.disabled = isRunning(name)));
  });
  const ov = $("#ovSummary");
  if (ov) {
    const all = allEntries();
    ov.textContent = `${all.length} configured instance(s), ${all.filter(([, c]) => isRunning(c.name)).length} running. Configure each protocol on its page, then start the services you need individually or use “Start all enabled”.`;
  }
  for (const [kind, page] of Object.entries(PAGE_KINDS)) {
    const names = page.flatMap(k => (S.cfg[k] || []).map(c => c.name));
    const running = names.filter(n => isRunning(n)).length;
    const elc = document.querySelector(`[data-count="${kind}"]`);
    if (elc) elc.textContent = names.length ? `${running}/${names.length}` : "";
  }
}

function statusBits(name) {
  return {
    dot: el("span", { class: "dot", "data-status": name, "data-role": "dot" }),
    state: el("span", { class: "badge", "data-status": name, "data-role": "state" }),
    stats: el("div", { class: "stats-row", "data-status": name, "data-role": "stats" }),
    buttons: el("div", { class: "row", "data-status": name, "data-role": "buttons" }),
    error: el("div", { class: "error-text", "data-status": name, "data-role": "error", hidden: true }),
  };
}

function instanceCard(kind, c, rerender) {
  const name = c.name;
  const b = statusBits(name);
  const key = kind + ":" + name;
  const card = el("div", { class: "card" + (S.collapsed[key] ? " collapsed" : ""), "data-status": name, "data-role": "lock" });
  const summary = el("span", { class: "muted", style: "font-size:12px" });
  const updSummary = () => {
    const n = tagTotal(c);
    let txt = KIND[kind].endpoint(c) + (KIND[kind].tagCols ? ` · ${fmtInt(n)} tags` : "");
    if (c.update_interval_ms) txt += ` · ${c.update_interval_ms} ms`;
    summary.textContent = txt;
    if (kind === "modbus") usage.replaceChildren(...modbusUsageView(c));
  };
  const usage = el("div", { class: "stats-row", style: "margin-top:8px" });

  const fields = el("div", { class: "form-grid" }, KIND[kind].fields.map(def => {
    const f = fieldInput(def, c, () => { updSummary(); if (def.k === "name") { renameKey(key, kind, c); } });
    if (def.k === "name") f.querySelector("input").dataset.nameField = "1";
    return f;
  }));

  const body = el("div", { class: "card-body" }, fields,
    KIND[kind].tagCols ? [el("h3", {}, "Tag groups"), tagTable(kind, c, updSummary), usage,
      presetPicker(kind, c, rerender)] : null,
    b.error);

  const head = el("div", { class: "card-head" },
    el("div", { class: "title" }, b.dot,
      el("div", { style: "min-width:0" },
        el("div", { class: "row" }, el("b", {}, name), el("span", { class: "badge" }, KIND[kind].label), b.state),
        summary)),
    el("div", { class: "row" }, b.buttons,
      KIND[kind].tagCols ? el("a", { class: "btn btn-sm", href: `/api/tagmap/${encodeURIComponent(name)}`, title: "Download the tag list with addresses / node ids / topics as CSV" }, "Tag list CSV") : null,
      el("button", { class: "btn btn-sm", title: "Duplicate", onclick: () => {
        const copy = JSON.parse(JSON.stringify(c)); copy.name = uniqueName(c.name.replace(/\d+$/, "") || kind);
        if ("port" in copy) copy.port = freePort(kind, +c.port + 1);
        if (kind === "bacnet") copy.device_instance = freeDeviceInstance();
        if (kind === "opcda") copy.prog_id = c.prog_id.replace(/(\.\d+)?$/, "") + "." + copy.name;
        S.cfg[kind].push(copy); markDirty(); rerender();
      } }, "Duplicate"),
      el("button", { class: "btn btn-sm btn-stop", title: "Remove from configuration", onclick: async () => {
        if (!confirm(`Remove '${name}' from the configuration?` + (isRunning(name) ? " It will be stopped." : ""))) return;
        S.cfg[kind] = S.cfg[kind].filter(x => x !== c);
        try { await saveConfig(true); } catch (e) { toast(e.message, true); }
        rerender();
      } }, "Remove"),
      el("button", { class: "btn-link", title: "Collapse / expand", onclick: () => { S.collapsed[key] = !S.collapsed[key]; card.classList.toggle("collapsed"); } }, "▾")),
    el("div", { style: "flex-basis:100%" }, b.stats));
  card.append(head, body);
  updSummary();
  return card;
}

function renameKey() { /* names are keys on the server only after saving; nothing to do client side */ }

function modbusUsageView(c) {
  const u = modbusUsage(c);
  return Object.entries(u).map(([area, n]) => el("span", {}, area.replace("_", " ") + " ",
    el("b", { style: n > 65536 ? "color:var(--stop)" : "" }, `${fmtInt(n)} / 65,536`)));
}

// ====================================================================== pages
const PAGE_KINDS = { modbus: ["modbus"], opcua: ["opcua"], opcda: ["opcda"], mqtt: ["mqtt_broker", "mqtt"], bacnet: ["bacnet"] };
const PAGE_INFO = {
  modbus: { title: "Modbus TCP", desc: "Modbus TCP servers (slaves). Each server listens on its own TCP port and exposes coils, discrete inputs, input registers and holding registers. Supports function codes 1, 2, 3, 4, 5, 6, 15, 16 and answers any unit id." },
  opcua: { title: "OPC UA", desc: "OPC UA servers with anonymous access and no security. Every tag is a variable with a string NodeId: ns=2;s=<group>.<tag>." },
  opcda: { title: "OPC DA", desc: "Classic OPC Data Access 2.0 servers (Windows COM/DCOM). Clients connect by ProgID instead of a port." },
  mqtt: { title: "MQTT", desc: "An internal MQTT 3.1.1 broker plus data publishers. Publishers can also target an external broker. Use the test client to subscribe and watch the data." },
  bacnet: { title: "BACnet/IP", desc: "BACnet/IP devices on UDP. Each device needs its own UDP port and a unique device instance number. Tags become analog/binary input/value objects (present-value)." },
};

function render() {
  clearInterval(S.timers.client); S.timers.client = null;
  document.querySelectorAll(".nav-item").forEach(a => a.classList.toggle("active", a.dataset.page === S.page));
  const main = $("#main");
  main.replaceChildren();
  if (S.page === "overview") renderOverview(main);
  else if (S.page === "logs") renderLogs(main);
  else renderProtocol(main, S.page);
  updateStatusViews();
}

function renderOverview(main) {
  const entries = allEntries();
  const running = entries.filter(([, c]) => isRunning(c.name)).length;
  main.append(el("div", { class: "page-head" }, el("div", {},
    el("h1", {}, "Overview"),
    el("p", { class: "muted", id: "ovSummary" }, `${entries.length} configured instance(s), ${running} running. Configure each protocol on its page, then start the services you need individually or use “Start all enabled”.`))));
  if (!entries.length) {
    main.append(el("div", { class: "empty" }, "Nothing configured yet. Pick a protocol on the left and add a server."));
    return;
  }
  for (const [page, kinds] of Object.entries(PAGE_KINDS)) {
    const list = kinds.flatMap(k => (S.cfg[k] || []).map(c => [k, c]));
    if (!list.length) continue;
    main.append(el("h3", {}, PAGE_INFO[page].title));
    main.append(el("div", { class: "grid-cards", style: "margin-bottom:8px" }, list.map(([k, c]) => {
      const b = statusBits(c.name);
      return el("div", { class: "card" },
        el("div", { class: "card-head" }, el("div", { class: "title" }, b.dot, el("b", {}, c.name), el("span", { class: "badge" }, KIND[k].short)), b.buttons),
        el("div", { class: "card-body" },
          el("div", { class: "mono muted", style: "margin-bottom:6px;word-break:break-all" }, KIND[k].endpoint(c)),
          el("div", { class: "row", style: "margin-bottom:6px" }, b.state), b.stats, b.error));
    })));
  }
}

function renderProtocol(main, page) {
  const info = PAGE_INFO[page];
  const kinds = PAGE_KINDS[page];
  const tab = S.tabs[page] || "servers";
  const rerender = () => render();
  main.append(el("div", { class: "page-head" },
    el("div", {}, el("h1", {}, info.title), el("p", { class: "muted" }, info.desc)),
    el("div", { class: "row" }, kinds.map(k => el("button", { class: "btn btn-accent", onclick: () => {
      const prefix = { modbus: "modbus", opcua: "opcua", opcda: "opcda", mqtt_broker: "broker", mqtt: "mqtt", bacnet: "bacnet" }[k];
      S.cfg[k] = S.cfg[k] || []; S.cfg[k].push(KIND[k].make(uniqueName(prefix))); markDirty(); S.tabs[page] = "servers"; rerender();
    } }, "+ Add " + KIND[k].label)),
      el("button", { class: "btn btn-go", onclick: () => startKinds(kinds) }, "Start all"),
      el("button", { class: "btn btn-stop", onclick: () => stopKinds(kinds) }, "Stop all"))));

  const tabNames = { servers: page === "mqtt" ? "Broker & publishers" : "Servers", client: "Test client", guide: "Guide" };
  main.append(el("div", { class: "tabs" }, Object.entries(tabNames).map(([k, label]) =>
    el("div", { class: "tab" + (tab === k ? " active" : ""), onclick: () => { S.tabs[page] = k; rerender(); } }, label))));

  if (tab === "servers") {
    if (page === "opcda") main.append(opcdaNotice());
    const list = kinds.flatMap(k => (S.cfg[k] || []).map(c => [k, c]));
    if (!list.length) main.append(el("div", { class: "empty" }, `No ${info.title} instances yet — use the “Add” button above.`));
    for (const k of kinds) {
      const items = S.cfg[k] || [];
      if (page === "mqtt" && items.length) main.append(el("h3", {}, k === "mqtt_broker" ? "Internal brokers" : "Publishers (data simulators)"));
      for (const c of items) main.append(instanceCard(k, c, rerender));
    }
  } else if (tab === "client") {
    CLIENTS[page](main);
  } else {
    main.append(GUIDES[page]());
  }
}

async function startKinds(kinds) {
  if (S.dirty) await saveConfig(true).catch(e => toast(e.message, true));
  for (const k of kinds) {
    try {
      const r = await api("POST", `/api/start_all?kind=${k}`);
      if (!r.ok) toast(Object.entries(r.errors).map(([n, e]) => `${n}: ${e}`).join("\n"), true);
    } catch (e) { toast(e.message, true); }
  }
  refreshStatus();
}
async function stopKinds(kinds) {
  for (const k of kinds) await api("POST", `/api/stop_all?kind=${k}`).catch(e => toast(e.message, true));
  refreshStatus();
}

// ---------------------------------------------------------------- logs page
function renderLogs(main) {
  const st = S.client.logs = S.client.logs || { source: "", level: "INFO", follow: true };
  const box = el("div", { class: "logbox", id: "logbox" });
  const sources = ["", ...allEntries().map(([, c]) => c.name)];
  main.append(el("div", { class: "page-head" }, el("div", {}, el("h1", {}, "Logs"),
    el("p", { class: "muted" }, "Messages from the manager and every simulator process (last 1,000)."))),
  el("div", { class: "row", style: "margin-bottom:8px" },
    el("label", { class: "field", style: "width:200px" }, "Instance",
      el("select", { onchange: e => { st.source = e.target.value; drawLogs(); } }, sources.map(s => el("option", { value: s, selected: s === st.source }, s || "All")))),
    el("label", { class: "field", style: "width:140px" }, "Minimum level",
      el("select", { onchange: e => { st.level = e.target.value; drawLogs(); } }, ["DEBUG", "INFO", "WARNING", "ERROR"].map(s => el("option", { selected: s === st.level }, s)))),
    el("label", { class: "field check" }, el("input", { type: "checkbox", checked: st.follow, onchange: e => (st.follow = e.target.checked) }), "Follow"),
    el("span", { class: "spacer" }),
    el("button", { class: "btn btn-sm", onclick: () => { S.logs = []; drawLogs(); } }, "Clear view")),
  box);
  drawLogs();
}
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
function drawLogs() {
  const box = $("#logbox"); if (!box) return;
  const st = S.client.logs;
  const rows = S.logs.filter(r => LEVELS[r.level] >= LEVELS[st.level] &&
    (!st.source || r.source === st.source || r.message.startsWith(st.source + ":") || r.message.includes(`'${st.source}'`)));
  box.replaceChildren(...rows.slice(-1000).map(r => el("div", { class: "logline " + r.level },
    el("span", { class: "t" }, new Date(r.time * 1000).toLocaleTimeString() + " "),
    el("span", { class: "s" }, r.source.padEnd(11) + " "),
    el("span", { class: "m" }, r.message))));
  if (st.follow) box.scrollTop = box.scrollHeight;
}

// ====================================================================== test clients
function clientState(page, init) { return (S.client[page] = S.client[page] || init()); }

function autoRefresh(st, fn) {
  clearInterval(S.timers.client); S.timers.client = null;
  if (st.auto) S.timers.client = setInterval(fn, Math.max(250, +st.every || 1000));
}

function targetPicker(kind, onPick) {
  const items = S.cfg[kind] || [];
  if (!items.length) return null;
  return el("label", { class: "field" }, "Fill from local instance",
    el("select", { onchange: e => { const c = items.find(x => x.name === e.target.value); if (c) onPick(c); e.target.selectedIndex = 0; } },
      el("option", { value: "" }, "— choose —"), items.map(c => el("option", { value: c.name }, c.name))));
}

function bindInput(st, key, attrs = {}) {
  const numeric = attrs.type === "number";
  return el("input", { ...attrs, value: st[key] ?? "", onchange: e => (st[key] = numeric ? +e.target.value : e.target.value) });
}
function bindSelect(st, key, options) {
  return el("select", { onchange: e => (st[key] = e.target.value) }, options.map(o => el("option", { value: o, selected: String(o) === String(st[key]) }, String(o))));
}
function f(label, input, hint) { return el("label", { class: "field" }, label, input, hint ? el("span", { class: "hint" }, hint) : null); }
function autoControls(st, run) {
  return [
    el("label", { class: "field check" }, el("input", { type: "checkbox", checked: !!st.auto, onchange: e => { st.auto = e.target.checked; autoRefresh(st, run); } }), "Auto refresh"),
    el("label", { class: "field", style: "width:110px" }, "every (ms)", el("input", { type: "number", value: st.every || 1000, min: 250, onchange: e => { st.every = +e.target.value; autoRefresh(st, run); } })),
  ];
}

// ---------------------------------------------------------------- Modbus client
function modbusClient(main) {
  const st = clientState("modbus", () => {
    const c = (S.cfg.modbus || [])[0];
    return { host: "127.0.0.1", port: c?.port || 502, unit: 1, area: "holding_registers", address: 0, count: 20,
      data_type: "float32", word_order: c?.word_order || "big", every: 1000 };
  });
  const result = el("div");
  const meta = el("div", { class: "muted", style: "font-size:12px;margin:6px 0" });
  let prev = {};
  const run = async () => {
    try {
      const r = await api("POST", "/api/client/modbus/read", st);
      const offset = { coils: 1, discrete_inputs: 10001, input_registers: 30001, holding_registers: 40001 }[st.area];
      const rows = r.rows.map(x => {
        const changed = prev[x.address] !== undefined && prev[x.address] !== x.value;
        return el("tr", { class: changed ? "flash" : "" },
          el("td", { class: "num" }, x.address), el("td", { class: "num" }, x.address < 9999 ? offset + x.address : (offset - 1) * 10 + x.address + 1),
          el("td", { class: "mono" }, x.raw.map(w => st.area.includes("coil") || st.area.includes("discrete") ? w : "0x" + w.toString(16).padStart(4, "0")).join(" ")),
          el("td", { class: "val" }, fmtValue(x.value)));
      });
      prev = Object.fromEntries(r.rows.map(x => [x.address, x.value]));
      result.replaceChildren(el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Address (0-based)"), el("th", {}, "Modicon"), el("th", {}, "Raw"), el("th", {}, "Value"))),
        el("tbody", {}, rows))));
      meta.textContent = `Read ${r.rows.length} value(s) from ${st.host}:${st.port} at ${new Date(r.time * 1000).toLocaleTimeString()}`;
    } catch (e) { meta.textContent = ""; result.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  const fill = c => { st.host = localHost(c.host); st.port = c.port; st.word_order = c.word_order || "big"; render(); };
  const groupsView = el("div");
  const showGroups = () => {
    const c = (S.cfg.modbus || []).find(x => +x.port === +st.port);
    if (!c) { groupsView.replaceChildren(); return; }
    const next = { holding_registers: 0, input_registers: 0, coils: 0, discrete_inputs: 0 };
    const rows = (c.tags || []).map(g => {
      const area = g.area || "holding_registers";
      const bits = area === "coils" || area === "discrete_inputs";
      const w = bits ? 1 : REG_WIDTH[g.data_type || "float32"];
      const start = g.start_address === "" || g.start_address == null ? next[area] : +g.start_address;
      next[area] = Math.max(next[area], start + w * g.count);
      return el("tr", { class: "clickable", title: "Click to read this group", onclick: () => {
        Object.assign(st, { area, address: start, data_type: bits ? "bool" : g.data_type || "float32", count: Math.min(g.count, 100) }); render(); run();
      } }, el("td", {}, g.name), el("td", {}, area), el("td", { class: "num" }, `${start} … ${start + w * g.count - 1}`), el("td", {}, bits ? "bool" : g.data_type), el("td", { class: "num" }, fmtInt(g.count)));
    });
    groupsView.replaceChildren(el("h3", {}, `Address map of '${c.name}' (click a row to read it)`),
      el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, ["Group", "Area", "Addresses", "Type", "Tags"].map(h => el("th", {}, h)))), el("tbody", {}, rows))));
  };
  main.append(el("div", { class: "card" }, el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      targetPicker("modbus", fill),
      f("Host", bindInput(st, "host")), f("Port", bindInput(st, "port", { type: "number" })),
      f("Unit id", bindInput(st, "unit", { type: "number" })),
      f("Area", bindSelect(st, "area", MB_AREAS)), f("Start address (0-based)", bindInput(st, "address", { type: "number" })),
      f("Number of values", bindInput(st, "count", { type: "number" }), "max 2,000"),
      f("Data type", bindSelect(st, "data_type", DATA_TYPES)), f("Word order", bindSelect(st, "word_order", ["big", "little"]))),
    el("div", { class: "row", style: "margin-top:10px" }, el("button", { class: "btn btn-accent", onclick: run }, "Read"), ...autoControls(st, run)),
    meta, result, groupsView)));
  showGroups();
  autoRefresh(st, run);
}

// ---------------------------------------------------------------- OPC UA client
function opcuaClient(main) {
  const st = clientState("opcua", () => {
    const c = (S.cfg.opcua || [])[0];
    return { endpoint: c ? KIND.opcua.endpoint(c) : "opc.tcp://127.0.0.1:4840", path: [], offset: 0, every: 1000 };
  });
  const out = el("div");
  const meta = el("div", { class: "muted", style: "font-size:12px;margin:6px 0" });
  let prev = {};
  const browse = async () => {
    const cur = st.path[st.path.length - 1];
    try {
      const r = await api("POST", "/api/client/opcua/browse", { endpoint: st.endpoint, node_id: cur?.id, offset: st.offset, limit: 200 });
      const crumbs = el("div", { class: "crumbs" },
        el("a", { onclick: () => { st.path = []; st.offset = 0; browse(); } }, "Objects"),
        st.path.map((p, i) => [" / ", el("a", { onclick: () => { st.path = st.path.slice(0, i + 1); st.offset = 0; browse(); } }, p.name)]));
      const rows = r.items.map(it => {
        const isVar = it.node_class === "Variable";
        const changed = isVar && prev[it.node_id] !== undefined && prev[it.node_id] !== it.value;
        return el("tr", { class: (isVar ? "" : "clickable ") + (changed ? "flash" : ""), onclick: isVar ? null : () => { st.path.push({ id: it.node_id, name: it.name }); st.offset = 0; browse(); } },
          el("td", {}, isVar ? "" : "📁 ", it.name), el("td", { class: "mono" }, it.node_id), el("td", {}, it.node_class),
          el("td", { class: "val" }, isVar ? fmtValue(it.value) : ""), el("td", {}, it.data_type || ""), el("td", {}, it.status || ""),
          el("td", { class: "mono" }, it.source_timestamp ? new Date(it.source_timestamp).toLocaleTimeString() : ""));
      });
      prev = Object.fromEntries(r.items.map(i => [i.node_id, i.value]));
      const pager = r.total > 200 ? el("div", { class: "row", style: "margin-top:6px" },
        el("button", { class: "btn btn-sm", disabled: st.offset === 0, onclick: () => { st.offset = Math.max(0, st.offset - 200); browse(); } }, "‹ Prev"),
        el("span", { class: "muted" }, `${st.offset + 1}–${Math.min(st.offset + 200, r.total)} of ${fmtInt(r.total)}`),
        el("button", { class: "btn btn-sm", disabled: st.offset + 200 >= r.total, onclick: () => { st.offset += 200; browse(); } }, "Next ›")) : null;
      out.replaceChildren(crumbs, el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Browse name", "NodeId", "Class", "Value", "Type", "Status", "Source time"].map(h => el("th", {}, h)))),
        el("tbody", {}, rows))), pager);
      meta.textContent = `${fmtInt(r.total)} child node(s) · ${new Date().toLocaleTimeString()}`;
    } catch (e) { meta.textContent = ""; out.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  main.append(el("div", { class: "card" }, el("div", { class: "card-body" },
    el("div", { class: "form-grid", style: "grid-template-columns: 220px 1fr" },
      targetPicker("opcua", c => { st.endpoint = KIND.opcua.endpoint(c); st.path = []; st.offset = 0; render(); browse(); }) || el("span"),
      f("Endpoint URL", bindInput(st, "endpoint"), "Security None, anonymous")),
    el("div", { class: "row", style: "margin-top:10px" },
      el("button", { class: "btn btn-accent", onclick: () => { st.path = []; st.offset = 0; browse(); } }, "Connect & browse"),
      el("button", { class: "btn", onclick: browse }, "Refresh"),
      ...autoControls(st, browse),
      el("button", { class: "btn btn-sm", onclick: () => api("POST", "/api/client/opcua/disconnect", { endpoint: st.endpoint }).then(() => toast("Disconnected")) }, "Disconnect")),
    meta, out)));
  autoRefresh(st, browse);
}

// ---------------------------------------------------------------- MQTT client
function mqttClient(main) {
  const st = clientState("mqtt", () => {
    const b = (S.cfg.mqtt_broker || [])[0];
    return { host: "127.0.0.1", port: b?.port || 1883, filter: "sim/#", pubTopic: "test/hello", pubPayload: '{"value": 42}', qos: 0, view: "", username: "", password: "" };
  });
  const statusLine = el("div", { class: "stats-row", style: "margin:8px 0" });
  const chips = el("div", { class: "row" });
  const latest = el("div");
  const recent = el("div");
  let prev = {};
  const draw = s => {
    statusLine.replaceChildren(
      el("span", {}, "state ", el("b", { style: s.connected ? "color:var(--go)" : "" }, s.connected ? "connected to " + s.broker : "disconnected")),
      el("span", {}, "messages ", el("b", {}, fmtInt(s.count))), el("span", {}, "rate ", el("b", {}, s.rate + "/s")),
      el("span", {}, "topics ", el("b", {}, fmtInt(s.topics))));
    chips.replaceChildren(...s.filters.map(flt => el("span", { class: "badge" }, flt, " ",
      el("button", { class: "btn-link", onclick: () => api("POST", "/api/client/mqtt/unsubscribe", { filter: flt }).then(draw).catch(e => toast(e.message, true)) }, "✕"))));
    latest.replaceChildren(el("h3", {}, `Last value per topic (${fmtInt(s.latest.length)} shown)`), el("div", { class: "table-wrap", style: "max-height:420px" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Topic", "Payload", "QoS", "Retain", "Received"].map(h => el("th", {}, h)))),
      el("tbody", {}, s.latest.map(m => {
        const changed = prev[m.topic] !== undefined && prev[m.topic] !== m.payload;
        return el("tr", { class: changed ? "flash" : "" }, el("td", { class: "mono" }, m.topic), el("td", { class: "val", style: "white-space:normal;max-width:520px" }, m.payload),
          el("td", {}, m.qos), el("td", {}, m.retain ? "yes" : ""), el("td", {}, new Date(m.time * 1000).toLocaleTimeString()));
      })))));
    prev = Object.fromEntries(s.latest.map(m => [m.topic, m.payload]));
    recent.replaceChildren(el("h3", {}, "Most recent messages"), el("div", { class: "logbox", style: "height:220px" },
      s.recent.map(m => el("div", { class: "logline" }, el("span", { class: "t" }, new Date(m.time * 1000).toLocaleTimeString() + " "), el("span", { class: "s" }, m.topic + " "), el("span", { class: "m" }, m.payload)))));
  };
  const poll = () => api("GET", `/api/client/mqtt/state?filter=${encodeURIComponent(st.view || "")}`).then(draw).catch(() => {});
  const call = (op, b) => api("POST", `/api/client/mqtt/${op}`, b).then(draw).catch(e => toast(e.message, true));
  main.append(el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Connection")), el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      targetPicker("mqtt_broker", c => { st.host = localHost(c.host); st.port = c.port; render(); }),
      f("Broker host", bindInput(st, "host")), f("Port", bindInput(st, "port", { type: "number" })),
      f("Username", bindInput(st, "username")), f("Password", bindInput(st, "password", { type: "password" }))),
    el("div", { class: "row", style: "margin-top:10px" },
      el("button", { class: "btn btn-accent", onclick: () => call("connect", { host: st.host, port: st.port, username: st.username, password: st.password, filters: st.filter ? [st.filter] : [] }) }, "Connect & subscribe"),
      el("button", { class: "btn", onclick: () => call("disconnect", {}) }, "Disconnect"),
      el("button", { class: "btn btn-sm", onclick: () => call("clear", {}) }, "Clear messages")),
    statusLine)),
  el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Subscribe & publish")), el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      f("Topic filter", bindInput(st, "filter"), "+ and # wildcards"),
      el("div", { class: "row", style: "align-items:flex-end" }, el("button", { class: "btn", onclick: () => call("subscribe", { filter: st.filter, qos: st.qos }) }, "Subscribe"))),
    el("div", { style: "margin:8px 0" }, chips),
    el("div", { class: "form-grid" },
      f("Publish topic", bindInput(st, "pubTopic")), f("Payload", bindInput(st, "pubPayload")),
      f("QoS", bindSelect(st, "qos", [0, 1])),
      el("div", { class: "row", style: "align-items:flex-end" }, el("button", { class: "btn", onclick: () => call("publish", { topic: st.pubTopic, payload: st.pubPayload, qos: +st.qos }) }, "Publish"))))),
  el("div", { class: "card" }, el("div", { class: "card-body" },
    el("label", { class: "field", style: "max-width:360px" }, "Show topics containing", el("input", { value: st.view, oninput: e => { st.view = e.target.value; poll(); } })),
    latest, recent)));
  poll();
  clearInterval(S.timers.client);
  S.timers.client = setInterval(poll, 1000);
}

// ---------------------------------------------------------------- BACnet client
function bacnetClient(main) {
  const st = clientState("bacnet", () => {
    const c = (S.cfg.bacnet || [])[0];
    const ip = c && c.ip_address !== "auto" ? c.ip_address.split("/")[0] : S.meta?.primary_ip || "127.0.0.1";
    return { client_port: 47899, low: "", high: "", whois_to: "", address: c ? `${ip}:${c.port}` : "", object_type: "analog-input", start: 0, count: 20, every: 2000 };
  });
  const devs = el("div");
  const out = el("div");
  const meta = el("div", { class: "muted", style: "font-size:12px;margin:6px 0" });
  let prev = {};
  const whois = async () => {
    devs.replaceChildren(el("div", { class: "muted" }, "Sending Who-Is…"));
    try {
      const r = await api("POST", "/api/client/bacnet/whois", { low: st.low, high: st.high, address: st.whois_to, client_port: st.client_port, timeout: 3 });
      devs.replaceChildren(r.devices.length ? el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Device instance", "Address", "Max APDU", "Vendor id"].map(h => el("th", {}, h)))),
        el("tbody", {}, r.devices.map(d => el("tr", { class: "clickable", title: "Use this device", onclick: () => { st.address = d.address; render(); } },
          el("td", { class: "num" }, d.device_instance), el("td", { class: "mono" }, d.address), el("td", { class: "num" }, d.max_apdu), el("td", { class: "num" }, d.vendor_id)))))) :
        el("div", { class: "muted" }, "No I-Am answers. Windows often does not deliver broadcasts to devices on the same PC — enter the device's ip:port in “Send to” for a directed Who-Is, or read it directly below."));
    } catch (e) { devs.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  const read = async () => {
    try {
      const r = await api("POST", "/api/client/bacnet/read_range", { address: st.address, object_type: st.object_type, start: st.start, count: st.count, client_port: st.client_port });
      out.replaceChildren(el("div", { class: "table-wrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Object", "Object name", "present-value"].map(h => el("th", {}, h)))),
        el("tbody", {}, r.rows.map(x => el("tr", { class: prev[x.object] !== undefined && prev[x.object] !== x.value ? "flash" : "" },
          el("td", { class: "mono" }, x.object), el("td", {}, x.name || ""), el("td", { class: "val" }, x.error ? el("span", { class: "error-text" }, x.error) : fmtValue(x.value))))))));
      prev = Object.fromEntries(r.rows.map(x => [x.object, x.value]));
      meta.textContent = `Read ${r.rows.length} object(s) at ${new Date(r.time * 1000).toLocaleTimeString()}`;
    } catch (e) { meta.textContent = ""; out.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  main.append(el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Discover devices (Who-Is)")), el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      f("Client UDP port", bindInput(st, "client_port", { type: "number" }), "must differ from simulated devices"),
      f("Send to", bindInput(st, "whois_to", { placeholder: "blank = broadcast" }), "ip[:port] for a directed Who-Is"),
      f("Instance low", bindInput(st, "low", { type: "number" })), f("Instance high", bindInput(st, "high", { type: "number" })),
      el("div", { class: "row", style: "align-items:flex-end" }, el("button", { class: "btn btn-accent", onclick: whois }, "Who-Is"))),
    el("div", { style: "margin-top:10px" }, devs))),
  el("div", { class: "card" }, el("div", { class: "card-head" }, el("h2", {}, "Read objects")), el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      targetPicker("bacnet", c => { const ip = c.ip_address !== "auto" ? c.ip_address.split("/")[0] : S.meta?.primary_ip; st.address = `${ip}:${c.port}`; render(); }),
      f("Device address", bindInput(st, "address"), "ip:port"),
      f("Object type", bindSelect(st, "object_type", BAC_TYPES)),
      f("First instance", bindInput(st, "start", { type: "number" })), f("Count", bindInput(st, "count", { type: "number" }), "max 500")),
    el("div", { class: "row", style: "margin-top:10px" }, el("button", { class: "btn btn-accent", onclick: read }, "Read present-value"), ...autoControls(st, read)),
    meta, out)));
  autoRefresh(st, read);
}

// ---------------------------------------------------------------- OPC DA client
function opcdaNotice() {
  const s = S.meta?.opcda;
  if (!s) return el("span");
  if (s.ready) return el("div", { class: "notice" }, "OPC DA prerequisites found: ", el("b", {}, `${s.python_bits}-bit OPC Core Components`), " and comtypes. Servers register themselves for the current user when started (machine-wide when this tool runs as Administrator).");
  return el("div", { class: "notice warn" }, el("b", {}, "OPC DA is not ready on this machine."), el("ul", {}, (s.missing || [s.message]).map(m => el("li", {}, m))),
    el("div", { style: "margin-top:6px" }, "You can still start the servers below — they register with Windows and clients can find them — but connections from other processes fail until you install the ", el("b", {}, "OPC Core Components Redistributable (x64)"), " from the OPC Foundation (opcfoundation.org → Developer Tools → Classic). Then restart this tool."));
}

function opcdaClient(main) {
  const st = clientState("opcda", () => ({ host: "", prog_id: (S.cfg.opcda || [])[0]?.prog_id || "", path: "", items: [], every: 1000 }));
  const servers = el("div");
  const out = el("div");
  const meta = el("div", { class: "muted", style: "font-size:12px;margin:6px 0" });
  const listServers = async () => {
    try {
      const r = await api("POST", "/api/client/opcda/servers", { host: st.host });
      servers.replaceChildren(el("div", { class: "row" }, r.servers.length ? r.servers.map(s => el("button", { class: "btn btn-sm", onclick: () => { st.prog_id = s; render(); } }, s)) : el("span", { class: "muted" }, "No OPC DA servers registered.")));
    } catch (e) { servers.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  let prev = {};
  const browse = async () => {
    try {
      const r = await api("POST", "/api/client/opcda/browse", { host: st.host, prog_id: st.prog_id, path: st.path });
      const parts = st.path ? st.path.split(".") : [];
      out.replaceChildren(
        el("div", { class: "crumbs" }, el("a", { onclick: () => { st.path = ""; browse(); } }, "Root"),
          parts.map((p, i) => [" / ", el("a", { onclick: () => { st.path = parts.slice(0, i + 1).join("."); browse(); } }, p)])),
        el("div", { class: "table-wrap" }, el("table", {},
          el("thead", {}, el("tr", {}, ["Name", "Item ID", "Value", "Quality", "Timestamp"].map(h => el("th", {}, h)))),
          el("tbody", {}, [
            ...r.branches.map(b => el("tr", { class: "clickable", onclick: () => { st.path = st.path ? st.path + "." + b : b; browse(); } }, el("td", {}, "📁 " + b), el("td"), el("td"), el("td"), el("td"))),
            ...r.items.map(it => el("tr", { class: prev[it.item_id] !== undefined && prev[it.item_id] !== it.value ? "flash" : "" },
              el("td", {}, it.name), el("td", { class: "mono" }, it.item_id), el("td", { class: "val" }, fmtValue(it.value)), el("td", {}, it.quality ?? ""), el("td", {}, it.timestamp ? new Date(it.timestamp).toLocaleTimeString() : ""))),
          ]))));
      prev = Object.fromEntries(r.items.map(i => [i.item_id, i.value]));
      meta.textContent = `${r.branches.length} branch(es), ${r.items.length} item(s)` + (r.truncated ? " (first 200 items shown)" : "") + ` · ${new Date().toLocaleTimeString()}`;
    } catch (e) { meta.textContent = ""; out.replaceChildren(el("div", { class: "error-text" }, e.message)); }
  };
  main.append(opcdaNotice(), el("div", { class: "card" }, el("div", { class: "card-body" },
    el("div", { class: "form-grid" },
      f("Host", bindInput(st, "host", { placeholder: "blank = this PC" })), f("Server ProgID", bindInput(st, "prog_id")),
      el("div", { class: "row", style: "align-items:flex-end" }, el("button", { class: "btn", onclick: listServers }, "List servers"))),
    el("div", { style: "margin-top:8px" }, servers),
    el("div", { class: "row", style: "margin-top:10px" }, el("button", { class: "btn btn-accent", onclick: () => { st.path = ""; browse(); } }, "Connect & browse"), ...autoControls(st, browse),
      el("button", { class: "btn btn-sm", onclick: () => api("POST", "/api/client/opcda/disconnect", {}).then(() => toast("Disconnected")) }, "Disconnect")),
    meta, out)));
  autoRefresh(st, browse);
}

const CLIENTS = { modbus: modbusClient, opcua: opcuaClient, mqtt: mqttClient, bacnet: bacnetClient, opcda: opcdaClient };

// ====================================================================== guides
const guide = (...blocks) => el("div", { class: "card" }, el("div", { class: "card-body", style: "max-width:900px" }, ...blocks));
const GUIDES = {
  modbus: () => guide(
    el("h3", {}, "Data model and limits"),
    el("div", { class: "table-wrap", style: "max-height:none;margin-bottom:10px" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Table", "Access", "Classic number", "Max addresses", "Max per read request"].map(h => el("th", {}, h)))),
      el("tbody", {},
        [["Coils", "read/write bits (FC 1, 5, 15)", "0xxxx / 00001+", "65,536", "2,000 bits"],
         ["Discrete inputs", "read-only bits (FC 2)", "1xxxx / 10001+", "65,536", "2,000 bits"],
         ["Input registers", "read-only 16-bit words (FC 4)", "3xxxx / 30001+", "65,536", "125 registers"],
         ["Holding registers", "read/write 16-bit words (FC 3, 6, 16)", "4xxxx / 40001+", "65,536", "125 read / 123 write"]]
          .map(r => el("tr", {}, r.map(c => el("td", {}, c))))))),
    el("p", {}, "A float32 or 32-bit integer takes 2 registers and a float64 takes 4, so one table holds at most 32,768 float32 values. Classic 5-digit Modicon addressing (40001–49999) only reaches the first 9,999 addresses; 6-digit addressing (400001–465536) covers all of them. Addresses in this tool are 0-based protocol addresses (Modicon number = offset + address)."),
    el("h3", {}, "Suggested sizes"),
    el("ul", {},
      el("li", {}, el("b", {}, "Energy meter / drive: "), "50–300 registers, mostly input registers with float32 measurements, a few holding registers for setpoints, usually no coils."),
      el("li", {}, el("b", {}, "Small PLC: "), "a few hundred coils and discrete inputs, 256–1,000 int16 registers."),
      el("li", {}, el("b", {}, "Medium PLC / RTU: "), "2,000–4,000 bits per table and 2,000–10,000 registers."),
      el("li", {}, el("b", {}, "Protocol gateway / historian load test: "), "use the full 65,536 addresses per table; scale beyond that with more servers on more ports.")),
    el("p", {}, "Clients poll in blocks of at most 125 registers, so 10,000 float32 tags need ≈160 requests per scan. The simulator serves thousands of requests per second; the client side is usually the bottleneck. Use “Suggested tag layouts” on a server to apply one of these."),
    el("h3", {}, "Byte and word order"),
    el("p", {}, "Each 16-bit register is big-endian (Modbus standard). For 32/64-bit values, “big” sends the high word first (ABCD), “little” sends the low word first (CDAB) — match what your client expects.")),
  opcua: () => guide(
    el("h3", {}, "Connecting"),
    el("p", {}, "Endpoint ", el("code", {}, "opc.tcp://<pc>:<port>/<name>"), ", security policy None, anonymous user. NodeIds are string ids ", el("code", {}, "ns=2;s=<group>.<tag>"), " — download the tag list CSV from the server card for all of them."),
    el("h3", {}, "Scale"),
    el("p", {}, "Measured on this PC: 40,000 doubles at 500 ms. Startup builds the address space at about 2,500–4,000 nodes per second. Each server runs in its own process, so several servers on different ports use separate CPU cores. Many active client subscriptions add load; the card shows late cycles if the update rate cannot be held.")),
  opcda: () => guide(
    el("h3", {}, "How OPC DA differs"),
    el("p", {}, "OPC DA is Windows COM/DCOM. Clients find a server by ProgID (and host for remote access) instead of a port. Remote clients also need DCOM permissions configured with dcomcnfg."),
    el("h3", {}, "Prerequisites"),
    el("ul", {},
      el("li", {}, "OPC Core Components Redistributable from the OPC Foundation for 64-bit (this Python is 64-bit). 32-bit OPC clients additionally need the 32-bit components, which are already present on this PC."),
      el("li", {}, "Starting a server registers its ProgID and CLSID for the current user (no Administrator rights needed). Run this tool as Administrator to register machine-wide — needed for clients that run elevated or as a service."),
      el("li", {}, "If a client connects while the server is stopped, Windows starts it in the background from the saved configuration; it exits 30 s after the last client disconnects.")),
    el("h3", {}, "Item IDs"),
    el("p", {}, el("code", {}, "<group>.<tag>"), ", e.g. ", el("code", {}, "Random.Random0001"), ". Browse is hierarchical (group → tags). Supported: OPC DA 2.05 — IOPCServer, IOPCCommon, IOPCBrowseServerAddressSpace, IOPCItemProperties, IOPCItemMgt, IOPCGroupStateMgt, IOPCSyncIO, IOPCAsyncIO2 with IOPCDataCallback, IOPCShutdown.")),
  mqtt: () => guide(
    el("h3", {}, "Internal broker"),
    el("p", {}, "A small MQTT 3.1.1 broker built into this tool: QoS 0/1, retained messages, + and # wildcards, no authentication. Any external client can connect to it, e.g. ", el("code", {}, "mosquitto_sub -h <pc> -p 1883 -t \"sim/#\" -v"), "."),
    el("h3", {}, "Publisher modes"),
    el("ul", {},
      el("li", {}, el("b", {}, "per_tag: "), el("code", {}, "<prefix>/<group>/<tag>"), " with ", el("code", {}, '{"value": 42.1, "timestamp": "…"}'), " or a plain value. Best up to a few thousand tags per second."),
      el("li", {}, el("b", {}, "batch: "), el("code", {}, "<prefix>/<group>/batch/<n>"), " with ", el("code", {}, '{"timestamp": "…", "values": {"Tag0": 1.2, …}}'), " — use for 10,000+ tags.")),
    el("p", {}, "Publishers can point at an external broker (Mosquitto, EMQX, HiveMQ…) by changing broker host/port.")),
  bacnet: () => guide(
    el("h3", {}, "Addressing"),
    el("p", {}, "BACnet/IP uses UDP 47808. Only one program per PC can own a UDP port, so additional simulated devices on the same PC need 47809, 47810, …; many BACnet clients only talk to 47808, so for those run one device per PC/IP or use the first device. The IP prefix (e.g. /24) is needed for broadcasts (Who-Is / I-Am)."),
    el("h3", {}, "Objects"),
    el("p", {}, "Each tag becomes an analog-input, analog-value, binary-input or binary-value object with object-name = tag name and a live present-value. Instance numbers are allocated per object type starting at 0 unless you set “First instance”."),
    el("h3", {}, "Scale"),
    el("p", {}, "Real controllers hold from tens to a few thousand objects. This simulator builds about 800 objects per second and updates 5,000 objects in ≈0.2 s; keep large devices at 1 s or slower updates.")),
};

// ====================================================================== polling & boot
async function refreshStatus() {
  try { S.status = await api("GET", "/api/status"); updateStatusViews(); } catch { /* server restarting */ }
}
async function pollLogs() {
  try {
    const rows = await api("GET", `/api/logs?since=${S.logsSince}`);
    if (rows.length) {
      S.logsSince = rows[rows.length - 1].id;
      S.logs.push(...rows); if (S.logs.length > 2000) S.logs.splice(0, S.logs.length - 2000);
      if (S.page === "logs") drawLogs();
      for (const r of rows) if (r.level === "ERROR" && r.source !== "manager") toast(r.message.split("\n")[0], true);
    }
  } catch { /* ignore */ }
}

async function boot() {
  [S.meta, S.cfg] = await Promise.all([api("GET", "/api/meta"), api("GET", "/api/config")]);
  for (const k of Object.keys(KIND)) S.cfg[k] = S.cfg[k] || [];
  $("#hostinfo").textContent = `${S.meta.hostname} · ${S.meta.primary_ip} · Python ${S.meta.python} · ${S.meta.config_path}`;
  $("#nav").addEventListener("click", e => {
    const a = e.target.closest(".nav-item"); if (!a) return;
    S.page = a.dataset.page; location.hash = S.page; render();
  });
  $("#btnSave").onclick = () => saveConfig().catch(e => toast(e.message, true));
  $("#btnStartAll").onclick = async () => {
    try {
      if (S.dirty) await saveConfig(true);
      const r = await api("POST", "/api/start_all");
      if (!r.ok) toast(Object.entries(r.errors).map(([n, e]) => `${n}: ${e}`).join("\n"), true);
    } catch (e) { toast(e.message, true); }
    refreshStatus();
  };
  $("#btnStopAll").onclick = () => api("POST", "/api/stop_all").then(refreshStatus).catch(e => toast(e.message, true));
  window.addEventListener("beforeunload", e => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
  const h = location.hash.slice(1);
  if (h && (PAGE_KINDS[h] || h === "logs" || h === "overview")) S.page = h;
  await refreshStatus();
  render();
  setInterval(refreshStatus, 1000);
  setInterval(pollLogs, 1500);
  pollLogs();
}
boot().catch(e => { document.body.innerHTML = `<pre style="padding:20px">Failed to start UI: ${e.message}</pre>`; });
