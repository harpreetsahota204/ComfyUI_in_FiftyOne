import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Canonical message-type constants — keep in sync with
// src/messageTypes.ts in the React panel.
const MSG = {
  BRIDGE_READY: "fiftyone_bridge_ready",
  LOAD_WORKFLOW: "fiftyone_load_workflow",
  GET_WORKFLOW: "fiftyone_get_workflow",
  WORKFLOW_DATA: "fiftyone_workflow_data",
  OUTPUT_READY: "fiftyone_output_ready",
  OUTPUT_AVAILABLE: "fiftyone_output_available",
  SAMPLE_CHANGED: "fiftyone_sample_changed",
  SLICE_INFO: "fiftyone_slice_info",
};

const _DBG = (...args) => console.log("%c[fo-bridge]", "color:#4ecca3;font-weight:bold", ...args);

let lastPromptId = null;
let cachedWorkflowName = "";
let workflowLoadedFromParent = false;
let availableSlices = [];        // [{name, mediaType}]
let availableHeatmapFields = []; // [string]
let availableLabelFields = [];   // [string] — fo.Label fields with non-None value on the current sample

function getWorkflowName() {
  try {
    const title = document.title || "";
    const cleaned = title.replace(/\s*-\s*ComfyUI$/, "").replace(/^\*/, "").trim();
    if (cleaned && cleaned !== "ComfyUI") return cleaned;
  } catch (_) {}
  return "";
}

// Node classes that get the dynamic slice name widget
const SLICE_NODE_CLASSES = new Set(["FO_SaveImage", "FO_SaveVideo"]);

// Map node class → expected media type for filtering available slices
const NODE_MEDIA_TYPE = { FO_SaveImage: "image", FO_SaveVideo: "video" };

const STARTER_WORKFLOW = {
  last_node_id: 3,
  last_link_id: 1,
  nodes: [
    {
      id: 1,
      type: "LoadImage",
      pos: [50, 200],
      size: [315, 314],
      flags: {},
      order: 0,
      mode: 0,
      inputs: [],
      outputs: [
        { name: "IMAGE", type: "IMAGE", links: [1], slot_index: 0 },
        { name: "MASK", type: "MASK", links: null, slot_index: 1 },
      ],
      properties: { "Node name for S&R": "LoadImage" },
      widgets_values: ["fo_current_sample.png", "image"],
    },
    {
      id: 2,
      type: "PreviewImage",
      pos: [550, 200],
      size: [210, 246],
      flags: {},
      order: 1,
      mode: 0,
      inputs: [{ name: "images", type: "IMAGE", link: 1 }],
      outputs: [],
      properties: { "Node name for S&R": "PreviewImage" },
    },
    {
      id: 3,
      type: "FO_SaveImage",
      pos: [550, 500],
      size: [300, 120],
      flags: {},
      order: 2,
      mode: 0,
      inputs: [{ name: "image", type: "IMAGE", link: null }],
      outputs: [],
      properties: { "Node name for S&R": "FO_SaveImage" },
      widgets_values: ["new_sample", "comfy_output"],
    },
  ],
  links: [[1, 1, 0, 2, 0, "IMAGE"]],
  groups: [
    {
      title: "FiftyOne \u2014 Build your workflow between Load and Save",
      bounding: [30, 130, 850, 540],
      color: "#3f789e",
      font_size: 24,
    },
  ],
  config: {},
  extra: { ds: { scale: 1, offset: [0, 0] } },
  version: 0.4,
};

app.registerExtension({
  name: "fiftyone.bridge",

  async setup() {
    if (window.parent === window) {
      _DBG("setup: top-level window, skipping bridge");
      return;
    }
    _DBG("setup: running inside iframe, initializing bridge");

    api.addEventListener("execution_start", (event) => {
      lastPromptId = event.detail?.prompt_id || null;
      cachedWorkflowName = getWorkflowName();
      _DBG("execution_start, promptId=", lastPromptId, "workflowName=", cachedWorkflowName);
    });

    // We forward only `output.images` to the parent's right-click "Save to
    // FiftyOne" UI.  Videos and other media types intentionally rely on
    // their corresponding FO_Save* nodes (FO_SaveVideo, FO_SaveText, …)
    // because the right-click flow has no way to know how to encode/store
    // them — and the user explicitly preferred node-based saving for
    // non-image outputs.
    api.addEventListener("executed", (event) => {
      const output = event.detail?.output;
      const nodeId = event.detail?.node;
      _DBG("executed event: nodeId=", nodeId, "hasOutput=", !!output, "images=", output?.images?.length);
      if (!output) return;

      if (output.images && output.images.length > 0) {
        window.parent.postMessage(
          {
            type: MSG.OUTPUT_AVAILABLE,
            outputs: output.images.map((img) => ({
              filename: img.filename,
              subfolder: img.subfolder || "",
              imgType: img.type || "output",
            })),
            nodeId: nodeId,
            promptId: lastPromptId,
          },
          "*"
        );
      }
    });

    window.addEventListener("message", (event) => {
      if (event.data?.type === MSG.LOAD_WORKFLOW) {
        workflowLoadedFromParent = true;
        _DBG("LOAD_WORKFLOW received from parent, workflowLoadedFromParent=true");
        try {
          app.loadGraphData(event.data.workflow);
          _DBG("LOAD_WORKFLOW: loadGraphData() succeeded");
        } catch (e) {
          console.warn("[fiftyone-bridge] failed to load workflow:", e);
        }
      }

      if (event.data?.type === MSG.SAMPLE_CHANGED) {
        _DBG("SAMPLE_CHANGED received from parent — calling refreshLoadImagePreviews()");
        refreshLoadImagePreviews();
      }

      if (event.data?.type === MSG.GET_WORKFLOW) {
        _DBG("GET_WORKFLOW received from parent");
        try {
          const workflow = app.graph.serialize();
          window.parent.postMessage(
            { type: MSG.WORKFLOW_DATA, workflow },
            "*"
          );
        } catch (e) {
          console.warn("[fiftyone-bridge] failed to serialize workflow:", e);
        }
      }

      if (event.data?.type === MSG.SLICE_INFO) {
        const raw = event.data.slices || [];
        availableSlices = raw.map((s) =>
          typeof s === "string" ? { name: s, mediaType: "image" } : s
        );
        availableHeatmapFields = event.data.heatmapFields || [];
        availableLabelFields = event.data.labelFields || [];
        _DBG("SLICE_INFO received, slices=", availableSlices, "heatmapFields=", availableHeatmapFields, "labelFields=", availableLabelFields);
        updateAllSaveNodeSliceWidgets();
      }
    });

    api.addEventListener("fiftyone.save_output", (event) => {
      const data = event.detail;
      const saveMode = data.save_mode || "new_sample";
      const outputType = data.type || "image";
      const titleMap = { image: "FO_SaveImage", video: "FO_SaveVideo", text: "FO_SaveText", depth: "FO_SaveDepth" };
      window.parent.postMessage(
        {
          type: MSG.OUTPUT_READY,
          outputType,
          nodeTitle: titleMap[outputType] || "FO_SaveImage",
          nodeId: null,
          promptId: lastPromptId,
          workflowName: cachedWorkflowName,
          filename: data.filename || "",
          subfolder: data.subfolder || "",
          textValue: data.text || null,
          autoSave: true,
          saveMode: saveMode,
          fieldName: data.name || "comfy_output",
          copyLabels: data.copy_labels || "",
        },
        "*"
      );
    });

    // By the time extension setup() runs, the app and graph are already
    // initialized.  The WS `status` event may have fired before we
    // registered our listener, so we send BRIDGE_READY directly from here.
    _DBG("setup: sending BRIDGE_READY to parent immediately");
    window.parent.postMessage({ type: MSG.BRIDGE_READY }, "*");

    // Load starter workflow if the parent hasn't sent one yet.
    // A short delay lets any LOAD_WORKFLOW from the parent arrive first.
    setTimeout(() => {
      _DBG("setup: starter check — workflowLoadedFromParent=", workflowLoadedFromParent);
      if (!workflowLoadedFromParent) {
        _DBG("setup: loading STARTER_WORKFLOW via loadGraphData()");
        try {
          app.loadGraphData(STARTER_WORKFLOW);
          _DBG("setup: STARTER_WORKFLOW loaded successfully");
        } catch (e) {
          console.warn("[fiftyone-bridge] failed to load starter:", e);
        }
      } else {
        _DBG("setup: skipping starter — workflow already loaded from parent");
      }
    }, 500);

  },

  nodeCreated(node) {
    // SLICE_NODE_CLASSES (FO_SaveImage / FO_SaveVideo) and FO_SaveDepth
    // are mutually exclusive — keep them as if/else so the dispatch is
    // explicit and we don't accidentally run both setups on the same node.
    if (SLICE_NODE_CLASSES.has(node.comfyClass)) {
      const saveModeWidget = node.widgets?.find((w) => w.name === "save_mode");
      if (saveModeWidget && !["new_sample", "group_slice"].includes(saveModeWidget.value)) {
        _DBG("nodeCreated: fixing stale save_mode value:", saveModeWidget.value, "→ new_sample");
        saveModeWidget.value = "new_sample";
      }
      setupSliceWidget(node);
      setupLabelsWidget(node);
    } else if (node.comfyClass === "FO_SaveDepth") {
      setupDepthWidget(node);
    }
  },

  getNodeMenuItems(node) {
    if (window.parent === window) return [];

    const items = [];
    const hasImage = nodeHasImageOutput(node);
    const hasString = nodeHasStringOutput(node);
    _DBG("getNodeMenuItems: node=", node.comfyClass || node.type, "id=", node.id, "hasImage=", hasImage, "hasString=", hasString);

    if (hasImage) {
      items.push({
        content: "Save Image to FiftyOne",
        callback: () => {
          _DBG("context menu: 'Save Image to FiftyOne' clicked, node=", node.type, "id=", node.id);
          saveToFiftyOne(node);
        },
      });
    }

    if (hasString) {
      items.push({
        content: "Save Text to FiftyOne",
        callback: () => {
          _DBG("context menu: 'Save Text to FiftyOne' clicked, node=", node.type, "id=", node.id);
          saveTextToFiftyOne(node);
        },
      });
    }

    // One-click: replace a native SaveImage node with FO_SaveImage so
    // future runs auto-save into FiftyOne.  This is a permanent graph
    // mutation (in contrast to the "Save Image to FiftyOne" item above,
    // which is a one-shot save of the current displayed image).
    if (node.comfyClass === "SaveImage") {
      items.push({
        content: "Convert to Save Image to FiftyOne",
        callback: () => {
          _DBG("context menu: 'Convert to Save Image to FiftyOne' clicked, node=", node.type, "id=", node.id);
          convertToFOSaveImage(node);
        },
      });
    }

    return items;
  },
});

// ---------------------------------------------------------------------------
// Inline editable input — click a "name" widget to get an <input> with
// <datalist> suggestions positioned right over the widget on the canvas.
// Users can type a custom name or pick from the suggestion list.
// ---------------------------------------------------------------------------

const MIN_NODE_WIDTH = 300;
let _activeInlineInput = null;

function resizeNode(node) {
  const computed = node.computeSize();
  node.setSize([Math.max(computed[0], MIN_NODE_WIDTH), computed[1]]);
  app.graph.setDirtyCanvas(true, true);
}

function getSuggestionsForNode(node) {
  if (SLICE_NODE_CLASSES.has(node.comfyClass)) {
    // SLICE_NODE_CLASSES and NODE_MEDIA_TYPE are kept in lock-step; every
    // entry here has a known expected media type.
    const expectedMedia = NODE_MEDIA_TYPE[node.comfyClass];
    return availableSlices
      .filter((s) => s.mediaType === expectedMedia)
      .map((s) => s.name);
  }
  if (node.comfyClass === "FO_SaveDepth") {
    return availableHeatmapFields;
  }
  return [];
}

function showInlineInput(node, widget, suggestions) {
  _DBG("showInlineInput: ENTER node=", node.id, "widget=", widget.name, "suggestions=", suggestions, "last_y=", widget.last_y);

  if (_activeInlineInput) {
    _DBG("showInlineInput: removing previous active input");
    _activeInlineInput.remove();
    _activeInlineInput = null;
  }

  const canvas = app.canvas;
  const canvasEl = canvas.canvas;
  if (!canvasEl) {
    _DBG("showInlineInput: ABORT — no canvas element");
    return;
  }
  const rect = canvasEl.getBoundingClientRect();
  const scale = canvas.ds?.scale || 1;

  const widgetY = widget.last_y !== undefined ? widget.last_y : 0;
  const gx = node.pos[0];
  const gy = node.pos[1] + widgetY;

  const convertFn = canvas.ds?.convertOffsetToCanvas;
  let screenX, screenY;
  if (convertFn) {
    const [cx, cy] = convertFn.call(canvas.ds, [gx, gy]);
    screenX = cx + rect.left;
    screenY = cy + rect.top;
  } else {
    screenX = gx * scale + rect.left;
    screenY = gy * scale + rect.top;
  }
  const sw = node.size[0] * scale;
  const sh = (LiteGraph.NODE_WIDGET_HEIGHT || 20) * scale;

  _DBG("showInlineInput: position screenX=", screenX, "screenY=", screenY, "sw=", sw, "sh=", sh, "scale=", scale);

  const container = document.createElement("div");
  container.style.cssText =
    `position:fixed;left:${screenX}px;top:${screenY}px;` +
    `width:${sw}px;height:${sh}px;z-index:10000;`;

  const listId = `fo-inline-${node.id}-${Date.now()}`;
  const input = document.createElement("input");
  input.type = "text";
  const origValue = widget.value || "";
  input.value = "";
  input.placeholder = origValue || "type or select...";
  input.setAttribute("list", listId);
  const fs = Math.max(10, Math.round(12 * scale));
  input.style.cssText =
    `width:100%;height:100%;box-sizing:border-box;` +
    `font-size:${fs}px;padding:2px 6px;` +
    `border:2px solid #4ecca3;border-radius:2px;` +
    `background:#1a1a2e;color:#eee;outline:none;font-family:sans-serif;`;

  const datalist = document.createElement("datalist");
  datalist.id = listId;
  for (const s of suggestions) {
    const opt = document.createElement("option");
    opt.value = s;
    datalist.appendChild(opt);
  }

  container.appendChild(input);
  container.appendChild(datalist);
  document.body.appendChild(container);
  _activeInlineInput = container;

  try {
    input.focus();
    input.select();
    _DBG("showInlineInput: input focused and selected, activeElement=", document.activeElement?.tagName);
  } catch (e) {
    _DBG("showInlineInput: focus error:", e);
  }

  const commit = () => {
    if (!container.parentNode) return;
    const val = input.value.trim();
    _DBG("showInlineInput commit: val=", val, "origValue=", origValue);
    widget.value = val || origValue;
    container.remove();
    _activeInlineInput = null;
    app.graph.setDirtyCanvas(true, true);
  };

  let blurTimeout = null;
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") {
      container.remove();
      _activeInlineInput = null;
      app.graph.setDirtyCanvas(true, true);
    }
  });
  input.addEventListener("blur", () => { blurTimeout = setTimeout(commit, 200); });
  input.addEventListener("focus", () => { if (blurTimeout) clearTimeout(blurTimeout); });

  _DBG("showInlineInput: DONE — container added to DOM, listId=", listId);
}

// ---------------------------------------------------------------------------
// Dynamic slice widget for FO_Save* nodes
//
// When save_mode is "new_sample" the name widget is removed from the widgets
// array so it is completely invisible.  Clicking the name widget opens an
// inline input with datalist suggestions (see showInlineInput above).
// ---------------------------------------------------------------------------

function _installInlineOnClick(node, widget) {
  widget.onClick = function () {
    _DBG("widget.onClick intercepted: node=", node.id, "class=", node.comfyClass, "widget=", widget.name, "suggestions=", widget._foGetSuggestions?.()?.length);
    showInlineInput(node, widget, widget._foGetSuggestions ? widget._foGetSuggestions() : []);
  };
  _DBG("_installInlineOnClick: overrode onClick for widget", widget.name, "on node", node.id);
}

function setupSliceWidget(node) {
  const nameWidget = node.widgets?.find((w) => w.name === "name");
  if (!nameWidget) return;
  if (node._foSliceSetup) return;
  node._foSliceSetup = true;

  const saveModeWidget = node.widgets?.find((w) => w.name === "save_mode");
  nameWidget.label = "Slice name";

  nameWidget._foGetSuggestions = () => getSuggestionsForNode(node);
  _installInlineOnClick(node, nameWidget);

  const syncVisibility = () => {
    const showName = saveModeWidget?.value === "group_slice";
    const idx = node.widgets.indexOf(nameWidget);
    _DBG("syncVisibility: node=", node.id, "save_mode=", saveModeWidget?.value, "showName=", showName, "inArray=", idx >= 0);

    if (showName && idx < 0) {
      node.widgets.push(nameWidget);
    } else if (!showName && idx >= 0) {
      node.widgets.splice(idx, 1);
    }
    resizeNode(node);
  };

  syncVisibility();
  setTimeout(syncVisibility, 100);

  if (saveModeWidget) {
    const origCallback = saveModeWidget.callback;
    saveModeWidget.callback = (value, ...rest) => {
      if (origCallback) origCallback(value, ...rest);
      syncVisibility();
    };
  }

  const origOnConfigure = node.onConfigure;
  node.onConfigure = function (info) {
    if (origOnConfigure) origOnConfigure.call(this, info);
    setTimeout(syncVisibility, 100);
  };

  _DBG("setupSliceWidget: done for node", node.id, "slices=", availableSlices.length);
}

function setupDepthWidget(node) {
  const nameWidget = node.widgets?.find((w) => w.name === "name");
  if (!nameWidget || node._foDepthSetup) return;
  node._foDepthSetup = true;

  nameWidget._foGetSuggestions = () => availableHeatmapFields;
  _installInlineOnClick(node, nameWidget);
  _DBG("setupDepthWidget: done for node", node.id, "heatmapFields=", availableHeatmapFields.length);
}

// ---------------------------------------------------------------------------
// "Copy labels" widget — multi-select with pills, opens on click.
//
// The widget value is a comma-separated string of label-field names
// ("" = none).  The picker is constrained to the available list (no
// free text), and only fields with a non-None value on the current
// sample appear (filtered server-side, see _get_sample_label_fields).
// ---------------------------------------------------------------------------

function setupLabelsWidget(node) {
  const w = node.widgets?.find((x) => x.name === "labels");
  if (!w || node._foLabelsSetup) return;
  node._foLabelsSetup = true;

  w.label = "Copy labels";
  w.onClick = function () {
    _DBG("labels widget click: node=", node.id, "value=", w.value, "available=", availableLabelFields.length);
    showInlineLabelsPicker(node, w, availableLabelFields);
  };
  _DBG("setupLabelsWidget: done for node", node.id, "labelFields=", availableLabelFields.length);
}

function showInlineLabelsPicker(node, widget, available) {
  if (_activeInlineInput) {
    _activeInlineInput.remove();
    _activeInlineInput = null;
  }

  const canvas = app.canvas;
  const canvasEl = canvas.canvas;
  if (!canvasEl) return;
  const rect = canvasEl.getBoundingClientRect();
  const scale = canvas.ds?.scale || 1;

  const widgetY = widget.last_y !== undefined ? widget.last_y : 0;
  const gx = node.pos[0];
  const gy = node.pos[1] + widgetY;

  const convertFn = canvas.ds?.convertOffsetToCanvas;
  let screenX, screenY;
  if (convertFn) {
    const [cx, cy] = convertFn.call(canvas.ds, [gx, gy]);
    screenX = cx + rect.left;
    screenY = cy + rect.top;
  } else {
    screenX = gx * scale + rect.left;
    screenY = gy * scale + rect.top;
  }
  const sw = Math.max(node.size[0] * scale, 240);

  // Parse current widget value into a set, keeping only known fields
  const initial = String(widget.value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const selected = new Set(initial.filter((n) => available.includes(n)));

  const container = document.createElement("div");
  container.style.cssText =
    `position:fixed;left:${screenX}px;top:${screenY}px;` +
    `width:${sw}px;z-index:10000;` +
    `background:#1a1a2e;border:2px solid #4ecca3;border-radius:4px;` +
    `font-family:sans-serif;color:#eee;font-size:12px;` +
    `box-shadow:0 4px 16px rgba(0,0,0,0.5);`;

  const pillsRow = document.createElement("div");
  pillsRow.style.cssText =
    `display:flex;flex-wrap:wrap;gap:4px;padding:6px;min-height:24px;` +
    `border-bottom:1px solid #2a2a4a;`;

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = available.length === 0 ? "No labels on this sample" : "Type to filter…";
  input.disabled = available.length === 0;
  input.style.cssText =
    `width:100%;box-sizing:border-box;padding:6px 8px;` +
    `background:#12121e;color:#eee;border:none;border-bottom:1px solid #2a2a4a;` +
    `outline:none;font-family:inherit;font-size:inherit;`;

  const list = document.createElement("div");
  list.style.cssText = `max-height:200px;overflow-y:auto;`;

  function renderPills() {
    pillsRow.innerHTML = "";
    if (selected.size === 0) {
      const empty = document.createElement("span");
      empty.textContent = available.length === 0 ? "(no labels available)" : "(none selected)";
      empty.style.cssText = "color:#808098;font-style:italic;padding:2px 4px;";
      pillsRow.appendChild(empty);
      return;
    }
    selected.forEach((name) => {
      const pill = document.createElement("span");
      pill.style.cssText =
        `background:#4ecca3;color:#1a1a2e;padding:2px 6px;` +
        `border-radius:10px;font-size:11px;cursor:pointer;` +
        `display:inline-flex;align-items:center;gap:4px;`;
      pill.textContent = name;
      const x = document.createElement("span");
      x.textContent = "×";
      x.style.cssText = "font-weight:bold;font-size:14px;line-height:1;";
      pill.appendChild(x);
      pill.addEventListener("mousedown", (e) => {
        e.preventDefault();
        selected.delete(name);
        renderAll();
      });
      pillsRow.appendChild(pill);
    });
  }

  function renderList() {
    list.innerHTML = "";
    const q = input.value.toLowerCase();
    const matches = available.filter((n) => !q || n.toLowerCase().includes(q));
    if (matches.length === 0) {
      const empty = document.createElement("div");
      empty.textContent = available.length === 0 ? "" : "(no matches)";
      empty.style.cssText = "padding:8px;color:#808098;font-style:italic;";
      list.appendChild(empty);
      return;
    }
    matches.forEach((name) => {
      const row = document.createElement("div");
      const isSel = selected.has(name);
      row.style.cssText =
        `padding:6px 10px;cursor:pointer;display:flex;align-items:center;gap:8px;` +
        (isSel ? `background:#1e3a2e;color:#4ecca3;` : `color:#eee;`);
      const check = document.createElement("span");
      check.textContent = isSel ? "✓" : "";
      check.style.cssText = "width:12px;display:inline-block;";
      const label = document.createElement("span");
      label.textContent = name;
      row.appendChild(check);
      row.appendChild(label);
      row.addEventListener("mouseenter", () => {
        if (!isSel) row.style.background = "#222244";
      });
      row.addEventListener("mouseleave", () => {
        if (!isSel) row.style.background = "transparent";
      });
      row.addEventListener("mousedown", (e) => {
        e.preventDefault(); // keep input focused
        if (selected.has(name)) selected.delete(name);
        else selected.add(name);
        renderAll();
      });
      list.appendChild(row);
    });
  }

  function renderAll() {
    renderPills();
    renderList();
  }

  function commit() {
    if (!container.parentNode) return;
    const arr = Array.from(selected);
    widget.value = arr.length === 0 ? "" : arr.join(",");
    _DBG("labels picker commit: value=", widget.value);
    container.remove();
    _activeInlineInput = null;
    app.graph.setDirtyCanvas(true, true);
  }

  let blurTimer = null;
  input.addEventListener("input", renderList);
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape" || e.key === "Enter") {
      e.preventDefault();
      commit();
    }
  });
  input.addEventListener("blur", () => {
    blurTimer = setTimeout(commit, 200);
  });
  input.addEventListener("focus", () => {
    if (blurTimer) {
      clearTimeout(blurTimer);
      blurTimer = null;
    }
  });

  container.appendChild(pillsRow);
  container.appendChild(input);
  container.appendChild(list);
  document.body.appendChild(container);
  _activeInlineInput = container;

  renderAll();
  try {
    input.focus();
  } catch (_) {
    /* ignore */
  }
  _DBG("showInlineLabelsPicker: opened, selected=", Array.from(selected));
}

function updateAllSaveNodeSliceWidgets() {
  const nodes = app.graph?._nodes || [];
  for (const node of nodes) {
    if (SLICE_NODE_CLASSES.has(node.comfyClass)) {
      setupSliceWidget(node);
      setupLabelsWidget(node);
    }
    if (node.comfyClass === "FO_SaveDepth") setupDepthWidget(node);
  }
  app.graph.setDirtyCanvas(true, true);
}

// ---------------------------------------------------------------------------
// LoadImage preview refresh
// ---------------------------------------------------------------------------

/**
 * Force all LoadImage nodes referencing fo_current_sample.png to re-fetch
 * their preview from the server, without reloading the entire graph (which
 * would create a new workflow tab in ComfyUI).
 */
function refreshLoadImagePreviews() {
  try {
    const nodes = app.graph._nodes || [];
    const ts = Date.now();
    _DBG("refreshLoadImagePreviews: total nodes=", nodes.length);

    let loadImageCount = 0;
    let matchedCount = 0;

    for (const node of nodes) {
      if (node.type !== "LoadImage") continue;
      loadImageCount++;

      const widget = node.widgets?.find((w) => w.name === "image");
      _DBG("  LoadImage node id=", node.id, "widget.value=", widget?.value, "hasCallback=", !!widget?.callback, "hasImgs=", !!node.imgs, "imgCount=", node.imgs?.length);

      if (!widget || widget.value !== "fo_current_sample.png") {
        _DBG("  → skipping (value mismatch)");
        continue;
      }
      matchedCount++;

      node.imgs = null;

      widget.value = `fo_current_sample.png?_=${ts}`;
      widget.value = "fo_current_sample.png";
      if (widget.callback) {
        _DBG("  → calling widget.callback()");
        widget.callback(widget.value, app.graph, node);
      } else {
        _DBG("  → WARNING: no widget.callback found!");
      }
    }

    _DBG("refreshLoadImagePreviews: loadImageNodes=", loadImageCount, "matched=", matchedCount);
    app.graph.setDirtyCanvas(true, true);
  } catch (e) {
    console.warn("[fiftyone-bridge] preview refresh error:", e);
  }
}

function nodeHasImageOutput(node) {
  return (
    node.imgs?.length > 0 ||
    node.outputs?.some((o) => o.type === "IMAGE")
  );
}

function nodeHasStringOutput(node) {
  return node.outputs?.some((o) => o.type === "STRING");
}

function saveTextToFiftyOne(node) {
  const textWidget = node.widgets?.find(
    (w) => w.type === "customtext" || w.type === "text" || w.name === "text"
  );
  const textValue = textWidget?.value || node.widgets_values?.[0] || "";

  window.parent.postMessage(
    {
      type: MSG.OUTPUT_READY,
      outputType: "text",
      nodeTitle: node.title || node.type,
      nodeId: node.id,
      promptId: lastPromptId,
      workflowName: cachedWorkflowName || getWorkflowName(),
      textValue: String(textValue),
    },
    "*"
  );
}

async function saveToFiftyOne(node) {
  _DBG("saveToFiftyOne: node=", node.type, "id=", node.id, "imgs=", node.imgs?.length, "img[0].src=", node.imgs?.[0]?.src?.substring(0, 80));
  const img = node.imgs?.[0];
  if (!img) {
    _DBG("saveToFiftyOne: ABORTED — no img on node");
    return;
  }

  let base64 = null;

  if (img.src && img.src.includes("/view?")) {
    try {
      const resp = await fetch(img.src);
      const blob = await resp.blob();
      base64 = await blobToBase64(blob);
    } catch (e) {
      console.warn(
        "[fiftyone-bridge] failed to fetch full-res, falling back to canvas",
        e
      );
    }
  }

  if (!base64) {
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth || img.width;
    canvas.height = img.naturalHeight || img.height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const dataUrl = canvas.toDataURL("image/png");
    base64 = dataUrl.split(",")[1];
  }

  _DBG("saveToFiftyOne: sending OUTPUT_READY, base64 length=", base64?.length, "nodeTitle=", node.title || node.type);
  window.parent.postMessage(
    {
      type: MSG.OUTPUT_READY,
      outputType: "image",
      imageDataBase64: base64,
      nodeTitle: node.title || node.type,
      nodeId: node.id,
      promptId: lastPromptId,
      workflowName: cachedWorkflowName || getWorkflowName(),
    },
    "*"
  );
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const dataUrl = reader.result;
      resolve(dataUrl.split(",")[1]);
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// ---------------------------------------------------------------------------
// One-click: replace a native ComfyUI ``SaveImage`` node with our
// ``FO_SaveImage``.  Same canvas position, same IMAGE input wire,
// default widget values (save_mode=new_sample, name="", labels="").
// All other widgets (filename_prefix, etc.) are intentionally dropped —
// the goal is a fast path for converting existing workflows, not
// faithful migration.
// ---------------------------------------------------------------------------

function convertToFOSaveImage(node) {
  // Find the IMAGE input wire feeding the old node so we can reconnect
  // the new one to the same source.  SaveImage has a single IMAGE
  // input (named "images") at slot 0.
  const imgInput = node.inputs?.find(
    (i) => i.type === "IMAGE" && i.link != null
  );
  let srcNode = null;
  let srcSlot = null;
  if (imgInput) {
    const link = app.graph.links?.[imgInput.link];
    if (link) {
      srcNode = app.graph.getNodeById(link.origin_id);
      srcSlot = link.origin_slot;
    }
  }
  _DBG("convertToFOSaveImage: src node=", srcNode?.id, "slot=", srcSlot);

  // Capture position before removing the node — graph.remove() may
  // clear node.pos, and we want the new node in the same spot.
  const pos = node.pos.slice();
  const oldId = node.id;

  // Remove the old node — this also disconnects its wires.
  app.graph.remove(node);

  // Create the replacement.  LiteGraph.createNode returns null if the
  // type is not registered; degrade gracefully if our extension somehow
  // didn't load.
  const fresh = LiteGraph.createNode("FO_SaveImage");
  if (!fresh) {
    console.warn("[fiftyone-bridge] convertToFOSaveImage: FO_SaveImage type not registered");
    return;
  }
  fresh.pos = pos;
  app.graph.add(fresh);

  // Reconnect the IMAGE input.  FO_SaveImage's IMAGE input is slot 0.
  if (srcNode && srcSlot != null) {
    try {
      srcNode.connect(srcSlot, fresh, 0);
    } catch (e) {
      console.warn("[fiftyone-bridge] convertToFOSaveImage: connect failed", e);
    }
  }

  // ComfyUI's nodeCreated hook should fire automatically when a node is
  // added to the graph, but call our setups directly as belt-and-
  // suspenders.  The _foSliceSetup / _foLabelsSetup flags prevent
  // double-init if the hook does fire.
  setupSliceWidget(fresh);
  setupLabelsWidget(fresh);

  app.graph.setDirtyCanvas(true, true);
  _DBG("convertToFOSaveImage: replaced SaveImage", oldId, "→ FO_SaveImage", fresh.id);
}
