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
const SLICE_NODE_CLASSES = new Set(["FO_SaveImage", "FO_SaveVideo", "FO_Save3D"]);

// Map node class → expected media type for filtering available slices.
// FiftyOne lumps every 3D asset format (.glb/.ply/.obj/.stl/.fbx/.pcd)
// under a single ``"3d"`` media type, so all 3D save destinations share
// one slice pool here.
const NODE_MEDIA_TYPE = {
  FO_SaveImage: "image",
  FO_SaveVideo: "video",
  FO_Save3D: "3d",
};

// ComfyUI output socket types that indicate a node produces 3D data —
// used by the right-click "Save 3D to FiftyOne" detector to decide
// whether to show that menu item on a node.  Both legacy (``MESH``) and
// modern V3 names (``FILE_3D_*``) are listed.  Terminal save nodes
// (e.g. ``SaveGLB``) have no output sockets — they're detected via the
// ``_last3DOutputs`` cache instead.
const THREE_D_OUTPUT_TYPES = new Set([
  "MESH",
  "VOXEL",
  "FILE_3D",
  "FILE_3D_ANY",
  "FILE_3D_GLB",
  "FILE_3D_GLTF",
  "FILE_3D_OBJ",
  "FILE_3D_FBX",
  "FILE_3D_STL",
  "FILE_3D_USDZ",
]);

// Cache of the latest 3D file each node has emitted via ComfyUI's
// ``executed`` event (output["3d"] = [{filename, subfolder, type}]).
// Right-click "Save 3D to FiftyOne" pulls from here because terminal
// 3D save nodes (SaveGLB, third-party SaveOBJ / SavePLY / etc.)
// don't have an output socket — they only emit a UI dict.
//   Map<string nodeId, {filename, subfolder}>
const _last3DOutputs = new Map();

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
      // Order matches FO_SaveImage.INPUT_TYPES: save_mode, name, labels.
      // Keeping all three keeps round-trip serialize/deserialize stable.
      widgets_values: ["new_sample", "comfy_output", ""],
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

    // We forward `output.images` to the parent's right-click "Save to
    // FiftyOne" UI for the image flow.  ``output["3d"]`` lands in our
    // local cache so the right-click "Save 3D to FiftyOne" handler can
    // pick up filenames written by terminal save nodes (SaveGLB, etc.)
    // that don't have output sockets.  Videos / text / depth / etc.
    // intentionally rely on their corresponding FO_Save* nodes — the
    // right-click flow has no way to know how to encode/store them.
    api.addEventListener("executed", (event) => {
      const output = event.detail?.output;
      const nodeId = event.detail?.node;
      _DBG(
        "executed event: nodeId=", nodeId,
        "hasOutput=", !!output,
        "images=", output?.images?.length,
        "3d=", output?.["3d"]?.length,
      );
      if (!output) return;

      if (output.images && output.images.length > 0) {
        window.parent.postMessage(
          {
            type: MSG.OUTPUT_AVAILABLE,
            outputs: output.images.map((img) => ({
              filename: img.filename,
              subfolder: img.subfolder || "",
            })),
            nodeId: nodeId,
            promptId: lastPromptId,
          },
          "*"
        );
      }

      if (output["3d"] && output["3d"].length > 0) {
        // Cache the most recent 3D file for this node — the right-click
        // "Save 3D to FiftyOne" handler reads from here.  We only keep
        // the most recent (overwrite) since the menu pulls one file per
        // click anyway.
        const item = output["3d"][0];
        _last3DOutputs.set(String(nodeId), {
          filename: item.filename,
          subfolder: item.subfolder || "",
        });
        _DBG("executed: cached 3D output for node", nodeId, "→", item.filename);
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
        _DBG("SAMPLE_CHANGED received from parent — refreshing LoadImage previews + SAM3 collector canvases + clearing prompts");
        refreshLoadImagePreviews();
        refreshSAM3CollectorImages();
        clearStalePromptsOnSampleChange();
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
      const titleMap = {
        image: "FO_SaveImage",
        video: "FO_SaveVideo",
        text: "FO_SaveText",
        depth: "FO_SaveDepth",
        detections: "FO_SaveDetections",
        segmentation: "FO_SaveSegmentation",
        "3d": "FO_Save3D",
      };
      // Detection / segmentation nodes emit a richer payload (boxes JSON,
      // mask filenames, fallback labels, etc.).  These extras ride along
      // unchanged in the OUTPUT_READY message and are forwarded to the
      // operator as ctx.params keys with the same name.
      const extras =
        outputType === "detections"
          ? {
              field: data.field || "detections",
              imageHeight: data.image_height || 0,
              imageWidth: data.image_width || 0,
              boxesJson: data.boxes_json || "",
              predLabelsJson: data.pred_labels_json || "",
              scoresJson: data.scores_json || "",
              masksFilename: data.masks_filename || "",
              fallbackLabels: data.fallback_labels || "",
            }
          : outputType === "segmentation"
          ? {
              field: data.field || "segmentation",
              maskTargets: data.mask_targets || "",
            }
          : {};

      // Single structured log line — bypasses the browser console's "…"
      // truncation on multi-key objects, but still gives the type + key
      // sizes (boxesJson can be MB-sized so we don't dump its contents).
      if (outputType === "detections") {
        _DBG(
          "fiftyone.save_output → OUTPUT_READY (detections):",
          "field=", extras.field,
          "boxesJson_len=", (extras.boxesJson || "").length,
          "labelsJson_len=", (extras.predLabelsJson || "").length,
          "scoresJson_len=", (extras.scoresJson || "").length,
          "masksFilename=", extras.masksFilename || "(empty)",
          "fallbackLabels=", extras.fallbackLabels || "(empty)",
          "imageHW=", `${extras.imageHeight}x${extras.imageWidth}`,
        );
      } else if (outputType === "segmentation") {
        _DBG(
          "fiftyone.save_output → OUTPUT_READY (segmentation):",
          "field=", extras.field,
          "maskTargets=", extras.maskTargets || "(empty)",
          "filename=", data.filename || "(empty)",
        );
      } else {
        _DBG(
          "fiftyone.save_output → OUTPUT_READY:",
          "type=", outputType,
          "saveMode=", saveMode,
          "filename=", data.filename || "(empty)",
        );
      }

      window.parent.postMessage(
        {
          type: MSG.OUTPUT_READY,
          outputType,
          nodeTitle: titleMap[outputType] || "FO_SaveImage",
          // nodeId is supplied by the right-click flows (saveToFiftyOne /
          // saveTextToFiftyOne) where we know exactly which node the
          // user clicked.  Auto-save events don't carry it through —
          // the `executed` listener has the node id but plumbing it
          // here would require keeping a per-promptId map. Skip until
          // there's a real consumer.
          promptId: lastPromptId,
          workflowName: cachedWorkflowName,
          filename: data.filename || "",
          subfolder: data.subfolder || "",
          textValue: data.text || null,
          autoSave: true,
          saveMode: saveMode,
          // For detection / segmentation nodes the "name" we ship to the
          // panel is the destination field (extras.field).  Other nodes
          // continue to use ``data.name`` (the slice / file / heatmap name).
          fieldName: extras.field || data.name || "comfy_output",
          copyLabels: data.copy_labels || "",
          extras,
        },
        "*"
      );
    });

    // ComfyUI loads this extension asynchronously and only invokes
    // ``setup()`` once the app + graph are already initialized, so it's
    // safe to announce readiness to the parent panel immediately.
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
    // The four save-node families have mutually exclusive widget setups,
    // so dispatch via if/else.  FO_SaveDetections and FO_SaveSegmentation
    // both expose a free-text "field" picker; only Detections additionally
    // gets the multi-pill labels widget.
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
    } else if (node.comfyClass === "FO_SaveDetections") {
      _DBG("nodeCreated: FO_SaveDetections, wiring widgets node=", node.id);
      setupDetectionsFieldWidget(node);
      setupPillsWidget(node, "labels", {
        label: "Class fallback",
        placeholder: "Type a class name, press Enter…",
        emptyText: "(no fallback — upstream labels will be used)",
        accent: "#ffb454",
      });
    } else if (node.comfyClass === "FO_SaveSegmentation") {
      _DBG("nodeCreated: FO_SaveSegmentation, wiring widget node=", node.id);
      setupSegmentationFieldWidget(node);
    }
  },

  getNodeMenuItems(node) {
    if (window.parent === window) return [];

    const items = [];
    const hasImage = nodeHasImageOutput(node);
    const hasString = nodeHasStringOutput(node);
    const has3D = nodeHas3DOutput(node);
    _DBG(
      "getNodeMenuItems: node=", node.comfyClass || node.type,
      "id=", node.id, "hasImage=", hasImage, "hasString=", hasString, "has3D=", has3D,
    );

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

    if (has3D) {
      items.push({
        content: "Save 3D to FiftyOne",
        callback: () => {
          _DBG("context menu: 'Save 3D to FiftyOne' clicked, node=", node.type, "id=", node.id);
          saveToFiftyOne3D(node);
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
  // Only ever called from setupSliceWidget — i.e. for SLICE_NODE_CLASSES.
  // FO_SaveDepth attaches its own ``_foGetSuggestions`` directly in
  // setupDepthWidget and never round-trips through here.
  if (SLICE_NODE_CLASSES.has(node.comfyClass)) {
    // SLICE_NODE_CLASSES and NODE_MEDIA_TYPE are kept in lock-step; every
    // entry here has a known expected media type.
    const expectedMedia = NODE_MEDIA_TYPE[node.comfyClass];
    return availableSlices
      .filter((s) => s.mediaType === expectedMedia)
      .map((s) => s.name);
  }
  return [];
}

// Delay before an inline picker's blur commits, so a click landing on one of
// the picker's own elements (which fires blur first) can cancel the commit.
const BLUR_COMMIT_MS = 200;

// Compute the on-screen position of a node's widget. Returns null if the
// canvas isn't available; ``scale`` is the graph zoom (callers derive widths).
function widgetScreenRect(node, widget) {
  const canvas = app.canvas;
  const canvasEl = canvas.canvas;
  if (!canvasEl) return null;
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
  return { screenX, screenY, scale };
}

// Build a removable pill element. ``onRemove`` runs when the user clicks it.
// Shared by the labels picker (Set-backed) and the free-text pill picker
// (Array-backed) — they differ only in selection storage and accent color.
function makePillEl(name, accent, onRemove) {
  const pill = document.createElement("span");
  pill.style.cssText =
    `background:${accent};color:#1a1a2e;padding:2px 6px;` +
    `border-radius:10px;font-size:11px;cursor:pointer;` +
    `display:inline-flex;align-items:center;gap:4px;`;
  pill.textContent = name;
  const x = document.createElement("span");
  x.textContent = "×";
  x.style.cssText = "font-weight:bold;font-size:14px;line-height:1;";
  pill.appendChild(x);
  pill.addEventListener("mousedown", (e) => {
    e.preventDefault();
    onRemove();
  });
  return pill;
}

function showInlineInput(node, widget, suggestions) {
  _DBG("showInlineInput: ENTER node=", node.id, "widget=", widget.name, "suggestions=", suggestions, "last_y=", widget.last_y);

  if (_activeInlineInput) {
    _DBG("showInlineInput: removing previous active input");
    _activeInlineInput.remove();
    _activeInlineInput = null;
  }

  const r = widgetScreenRect(node, widget);
  if (!r) {
    _DBG("showInlineInput: ABORT — no canvas element");
    return;
  }
  const { screenX, screenY, scale } = r;
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
  input.addEventListener("blur", () => { blurTimeout = setTimeout(commit, BLUR_COMMIT_MS); });
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

// Detection / segmentation save nodes share a single "field" name widget
// that lands on the active sample.  No autocomplete suggestions today —
// the user free-types the field name (e.g. "detections", "segmentation").
// If we ever want to pre-populate from existing dataset fields, this is
// the spot: replace the empty array with one sourced from SLICE_INFO.

function _setupFreeTextFieldWidget(node, setupFlag, debugName) {
  const w = node.widgets?.find((x) => x.name === "field");
  if (!w || node[setupFlag]) return;
  node[setupFlag] = true;

  w.label = "Field";
  w._foGetSuggestions = () => [];
  _installInlineOnClick(node, w);
  _DBG(`${debugName}: done for node`, node.id);
}

function setupDetectionsFieldWidget(node) {
  _setupFreeTextFieldWidget(node, "_foDetFieldSetup", "setupDetectionsFieldWidget");
}

function setupSegmentationFieldWidget(node) {
  _setupFreeTextFieldWidget(node, "_foSegFieldSetup", "setupSegmentationFieldWidget");
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

  const r = widgetScreenRect(node, widget);
  if (!r) return;
  const { screenX, screenY, scale } = r;
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
      pillsRow.appendChild(makePillEl(name, "#4ecca3", () => {
        selected.delete(name);
        renderAll();
      }));
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
    blurTimer = setTimeout(commit, BLUR_COMMIT_MS);
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

// ---------------------------------------------------------------------------
// Free-text multi-pill picker — like showInlineLabelsPicker but the pills
// are arbitrary strings the user types in (Enter commits the typed token
// to a new pill).  Used by FO_SaveDetections / FO_SaveSegmentation to let
// the user provide fallback class names when the upstream model doesn't
// emit per-detection labels.
//
// Optional `suggestions` are displayed below the input as a one-click
// list; clicking a suggestion adds it as a pill (same behavior as Enter).
// ---------------------------------------------------------------------------

function setupPillsWidget(node, widgetName, opts = {}) {
  const w = node.widgets?.find((x) => x.name === widgetName);
  if (!w) {
    _DBG("setupPillsWidget: widget not found", widgetName, "on node", node.id);
    return;
  }
  const flag = `_foPillsSetup_${widgetName}`;
  if (node[flag]) return;
  node[flag] = true;

  if (opts.label) w.label = opts.label;
  w.onClick = function () {
    _DBG("pills widget click: node=", node.id, "widget=", widgetName, "value=", w.value);
    showInlinePillPicker(node, w, opts);
  };
  _DBG("setupPillsWidget: done for", widgetName, "on node", node.id);
}

function showInlinePillPicker(node, widget, opts = {}) {
  if (_activeInlineInput) {
    _activeInlineInput.remove();
    _activeInlineInput = null;
  }

  const r = widgetScreenRect(node, widget);
  if (!r) return;
  const { screenX, screenY, scale } = r;
  const sw = Math.max(node.size[0] * scale, 240);
  const accent = opts.accent || "#ffb454";

  const initial = String(widget.value || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  const pills = [...initial];

  const container = document.createElement("div");
  container.style.cssText =
    `position:fixed;left:${screenX}px;top:${screenY}px;` +
    `width:${sw}px;z-index:10000;` +
    `background:#1a1a2e;border:2px solid ${accent};border-radius:4px;` +
    `font-family:sans-serif;color:#eee;font-size:12px;` +
    `box-shadow:0 4px 16px rgba(0,0,0,0.5);`;

  const pillsRow = document.createElement("div");
  pillsRow.style.cssText =
    `display:flex;flex-wrap:wrap;gap:4px;padding:6px;min-height:24px;` +
    `border-bottom:1px solid #2a2a4a;`;

  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = opts.placeholder || "Type a name and press Enter…";
  input.style.cssText =
    `width:100%;box-sizing:border-box;padding:6px 8px;` +
    `background:#12121e;color:#eee;border:none;` +
    `outline:none;font-family:inherit;font-size:inherit;`;

  const suggestions = Array.isArray(opts.suggestions) ? opts.suggestions : [];
  const list = document.createElement("div");
  list.style.cssText = `max-height:180px;overflow-y:auto;border-top:1px solid #2a2a4a;`;

  function renderPills() {
    pillsRow.innerHTML = "";
    if (pills.length === 0) {
      const empty = document.createElement("span");
      empty.textContent = opts.emptyText || "(no labels — upstream values will be used)";
      empty.style.cssText = "color:#808098;font-style:italic;padding:2px 4px;";
      pillsRow.appendChild(empty);
      return;
    }
    pills.forEach((name, idx) => {
      pillsRow.appendChild(makePillEl(name, accent, () => {
        pills.splice(idx, 1);
        renderAll();
      }));
    });
  }

  function renderList() {
    list.innerHTML = "";
    if (suggestions.length === 0) return;
    const q = input.value.toLowerCase();
    const matches = suggestions.filter(
      (n) => !pills.includes(n) && (!q || n.toLowerCase().includes(q))
    );
    if (matches.length === 0) return;
    matches.forEach((name) => {
      const row = document.createElement("div");
      row.style.cssText = `padding:6px 10px;cursor:pointer;color:#eee;`;
      row.textContent = name;
      row.addEventListener("mouseenter", () => { row.style.background = "#222244"; });
      row.addEventListener("mouseleave", () => { row.style.background = "transparent"; });
      row.addEventListener("mousedown", (e) => {
        e.preventDefault();
        pills.push(name);
        input.value = "";
        renderAll();
      });
      list.appendChild(row);
    });
  }

  function renderAll() { renderPills(); renderList(); }

  function commit() {
    if (!container.parentNode) return;
    widget.value = pills.length === 0 ? "" : pills.join(",");
    _DBG("pills picker commit: value=", widget.value);
    container.remove();
    _activeInlineInput = null;
    app.graph.setDirtyCanvas(true, true);
  }

  let blurTimer = null;
  input.addEventListener("input", renderList);
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      commit();
      return;
    }
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const tok = input.value.trim();
      if (tok && !pills.includes(tok)) {
        pills.push(tok);
        input.value = "";
        renderAll();
      } else if (!tok) {
        commit();
      }
      return;
    }
    if (e.key === "Backspace" && !input.value && pills.length > 0) {
      pills.pop();
      renderAll();
    }
  });
  input.addEventListener("blur", () => { blurTimer = setTimeout(commit, BLUR_COMMIT_MS); });
  input.addEventListener("focus", () => {
    if (blurTimer) { clearTimeout(blurTimer); blurTimer = null; }
  });

  container.appendChild(pillsRow);
  container.appendChild(input);
  if (suggestions.length > 0) container.appendChild(list);
  document.body.appendChild(container);
  _activeInlineInput = container;

  renderAll();
  try { input.focus(); } catch (_) { /* ignore */ }
  _DBG("showInlinePillPicker: opened, pills=", pills.slice());
}

function updateAllSaveNodeSliceWidgets() {
  const nodes = app.graph?._nodes || [];
  for (const node of nodes) {
    if (SLICE_NODE_CLASSES.has(node.comfyClass)) {
      setupSliceWidget(node);
      setupLabelsWidget(node);
    } else if (node.comfyClass === "FO_SaveDepth") {
      setupDepthWidget(node);
    } else if (node.comfyClass === "FO_SaveDetections") {
      setupDetectionsFieldWidget(node);
      setupPillsWidget(node, "labels", {
        label: "Class fallback",
        placeholder: "Type a class name, press Enter…",
        emptyText: "(no fallback — upstream labels will be used)",
        accent: "#ffb454",
      });
    } else if (node.comfyClass === "FO_SaveSegmentation") {
      setupSegmentationFieldWidget(node);
    }
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
// Node-type set for "any node that loads an image from ComfyUI's input dir
// via an `image` widget whose value is a filename string." Both the
// built-in `LoadImage` and our `FO_LoadImage` subclass (registered under
// FiftyOne/IO for discoverability) have identical widget semantics, so we
// treat them interchangeably for refresh / scope-of-prompt-clear logic.
//
// If a future custom node also wraps `LoadImage` (or has compatible
// widgets), add its `node.type` here.
const LOAD_IMAGE_NODE_TYPES = new Set([
  "LoadImage",     // built-in ComfyUI
  "FO_LoadImage",  // our subclass under FiftyOne/IO
]);

function refreshLoadImagePreviews() {
  try {
    const nodes = app.graph._nodes || [];
    const ts = Date.now();

    let loadImageCount = 0;
    let matchedCount = 0;
    const skippedIds = [];

    for (const node of nodes) {
      if (!LOAD_IMAGE_NODE_TYPES.has(node.type)) continue;
      loadImageCount++;

      const widget = node.widgets?.find((w) => w.name === "image");
      if (!widget || widget.value !== "fo_current_sample.png") {
        skippedIds.push(`${node.id}(${widget?.value ?? "?"})`);
        continue;
      }
      matchedCount++;

      // Forcing the LoadImage preview to re-fetch when the FILENAME is
      // unchanged but the on-disk BYTES have changed (sample paginated /
      // slice switched) requires bouncing widget.value through a different
      // string and back.  ComfyUI's LoadImage widget keys its internal
      // preview cache on the value, so a no-op assignment to the same
      // string is ignored.  Calling widget.callback alone is not enough.
      // The mid-sequence value (``...?_=ts``) is never used as a URL — it
      // exists only to make the value-change detector fire.
      node.imgs = null;
      widget.value = `fo_current_sample.png?_=${ts}`;
      widget.value = "fo_current_sample.png";
      if (widget.callback) {
        widget.callback(widget.value, app.graph, node);
      }
    }

    _DBG(
      "refreshLoadImagePreviews: total=", nodes.length,
      "LoadImage=", loadImageCount,
      "refreshed=", matchedCount,
      ...(skippedIds.length ? ["skipped=", skippedIds] : []),
    );
    app.graph.setDirtyCanvas(true, true);
  } catch (e) {
    console.warn("[fiftyone-bridge] preview refresh error:", e);
  }
}

// ---------------------------------------------------------------------------
// SAM3 interactive-collector canvas refresh
// ---------------------------------------------------------------------------
//
// The four SAM3 collector nodes (Point / BBox / MultiRegion / Interactive)
// render an image on a DOM canvas where the user clicks to drop points or
// drag boxes.  Their canvas image is only updated by their ``onExecuted``
// handler — i.e. AFTER the workflow runs — so paginating samples in FiftyOne
// would otherwise leave the canvas frozen on the previous image while
// LoadImage already shows the new one.  Clicks then land on the wrong
// coordinate space.
//
// On every SAMPLE_CHANGED we walk the graph: for any SAM3 collector whose
// ``image`` input traces back through a single hop to a LoadImage pointed at
// ``fo_current_sample.png`` (the active-slice file the panel rewrites on
// pagination), we fetch that file via ComfyUI's /view endpoint and swap the
// collector's ``canvasWidget.image`` in place.  Existing user-placed
// points/boxes are intentionally KEPT — we don't second-guess workflow state.
// If the user wants a clean canvas, they have a "Clear All" button.

const SAM3_COLLECTOR_CLASSES = new Set([
  "SAM3PointCollector",
  "SAM3BBoxCollector",
  "SAM3MultiRegionCollector",
  "SAM3InteractiveCollector",
]);

function _findUpstreamImageNode(node, inputName = "image") {
  const input = node.inputs?.find((i) => i.name === inputName);
  if (!input || input.link == null) return null;
  const link = app.graph.links?.[input.link];
  if (!link) return null;
  return app.graph.getNodeById?.(link.origin_id) ?? null;
}

function refreshSAM3CollectorImages() {
  try {
    const nodes = app.graph?._nodes || [];
    let collectorCount = 0;
    let refreshedCount = 0;

    for (const node of nodes) {
      if (!SAM3_COLLECTOR_CLASSES.has(node.comfyClass)) continue;
      collectorCount++;

      // Only refresh collectors backed (one hop) by a LoadImage-equivalent
      // node (LoadImage or FO_LoadImage) pointed at the active-sample file.
      // Other upstreams (resize / preprocess / per-slice files) are
      // intentionally left alone — they either don't change on pagination,
      // or have post-processing we can't replay without queueing the
      // workflow.
      const upstream = _findUpstreamImageNode(node, "image");
      if (!upstream || !LOAD_IMAGE_NODE_TYPES.has(upstream.type)) {
        _DBG("  SAM3 collector id=", node.id, "class=", node.comfyClass,
             "→ skipping (upstream is", upstream?.type ?? "<none>", ")");
        continue;
      }
      const fileWidget = upstream.widgets?.find((w) => w.name === "image");
      const filename = fileWidget?.value;
      if (filename !== "fo_current_sample.png") {
        _DBG("  SAM3 collector id=", node.id, "→ skipping (LoadImage widget=", filename, ")");
        continue;
      }
      if (!node.canvasWidget) {
        _DBG("  SAM3 collector id=", node.id, "→ skipping (no canvasWidget yet)");
        continue;
      }

      const ts = Date.now();
      const url = `/view?filename=${encodeURIComponent(filename)}&type=input&_=${ts}`;
      const img = new Image();
      img.onload = () => {
        try {
          const cw = node.canvasWidget;
          if (!cw || !cw.canvas) return;

          cw.image = img;
          cw.canvas.width = img.width;
          cw.canvas.height = img.height;

          // Resize node + container to match new aspect — mirrors the
          // sizing math each SAM3 widget runs in its own onExecuted
          // (we can't share that code; it's per-widget closures).
          const nodeWidth = node.size?.[0] || 400;
          const availableWidth = nodeWidth - 20;
          const aspectRatio = img.height / img.width;
          const newWidgetHeight = Math.round(availableWidth * aspectRatio);

          node._isResizing = true;
          cw.widgetHeight = newWidgetHeight;
          if (cw.container) cw.container.style.height = newWidgetHeight + "px";
          if (typeof node.setSize === "function") {
            node.setSize([nodeWidth, newWidgetHeight + 80]);
          }
          setTimeout(() => { node._isResizing = false; }, 50);

          if (typeof node.redrawCanvas === "function") {
            node.redrawCanvas();
          }
          app.graph.setDirtyCanvas(true, true);
          _DBG("  SAM3 collector id=", node.id, "class=", node.comfyClass,
               "→ refreshed (", img.width, "x", img.height, ")");
        } catch (e) {
          console.warn("[fiftyone-bridge] SAM3 refresh inner error:", e);
        }
      };
      img.onerror = (e) => {
        console.warn("[fiftyone-bridge] SAM3 refresh image load failed for", url, e);
      };
      img.src = url;
      refreshedCount++;
    }

    _DBG("refreshSAM3CollectorImages: collectors=", collectorCount, "refreshing=", refreshedCount);
  } catch (e) {
    console.warn("[fiftyone-bridge] SAM3 refresh error:", e);
  }
}

// ---------------------------------------------------------------------------
// Prompt clearing on sample-pagination
// ---------------------------------------------------------------------------
//
// Two categories of node hold "prompt-shaped" state that's meaningful only
// for the sample on the canvas at the time it was entered:
//
//   1. SAM3 interactive collectors (point / bbox / multi-region / interactive) —
//      the user clicks on the canvas to drop points/boxes whose pixel
//      coordinates only make sense for the image currently shown.
//   2. Text-prompt nodes (SAM3Grounding, GroundingDetector) — the user
//      types a noun phrase like "person, dog" that targets the current
//      image's contents.
//
// When the user paginates to a different sample, both categories should
// reset so the new sample starts fresh. Otherwise the user would have
// to manually clear before queueing — easy to forget, and the stale
// prompt state often produces confusing results.
//
// SAM3 collector clearing is scoped to collectors whose upstream is a
// LoadImage pointed at ``fo_current_sample.png`` (matches refreshSAM3-
// CollectorImages — collectors pinned to per-slice files don't follow
// pagination, so their prompts shouldn't either). Text-prompt nodes are
// cleared unconditionally — they don't have a "current sample" tether
// we can use.
//
// If a future use case wants persistent prompts across pagination,
// we'd add an opt-out widget on the node or a panel-level toggle.
// ---------------------------------------------------------------------------

const SAM3_PROMPT_CLEAR_HANDLERS = {
  SAM3PointCollector: (node) => {
    const cw = node.canvasWidget;
    if (!cw) return false;
    cw.positivePoints = [];
    cw.negativePoints = [];
    cw.hoveredPoint = null;
    if (typeof node.updatePoints === "function") node.updatePoints();
    if (typeof node.redrawCanvas === "function") node.redrawCanvas();
    return true;
  },
  SAM3BBoxCollector: (node) => {
    const cw = node.canvasWidget;
    if (!cw) return false;
    cw.positiveBBoxes = [];
    cw.negativeBBoxes = [];
    cw.hoveredBBox = null;
    cw.currentBBox = null;
    if (typeof node.updateBBoxes === "function") node.updateBBoxes();
    if (typeof node.redrawCanvas === "function") node.redrawCanvas();
    return true;
  },
  SAM3MultiRegionCollector: (node) => {
    if (typeof node.clearAllPrompts !== "function") return false;
    node.clearAllPrompts();
    return true;
  },
  SAM3InteractiveCollector: (node) => {
    if (typeof node.clearAllPrompts !== "function") return false;
    node.clearAllPrompts();
    return true;
  },
};

// Maps comfyClass → widget name to clear. Each entry zeroes a single
// STRING widget on the node. Keep this list short — only widgets that
// represent "what to find / segment" in the current image should appear.
const TEXT_PROMPT_TARGETS = {
  SAM3Grounding: "text_prompt",
  GroundingDetector: "prompt",
};

function clearStalePromptsOnSampleChange() {
  try {
    const nodes = app.graph?._nodes || [];
    let collectorReset = 0;
    let textReset = 0;

    for (const node of nodes) {
      const handler = SAM3_PROMPT_CLEAR_HANDLERS[node.comfyClass];
      if (handler) {
        // Only reset collectors whose upstream is a LoadImage-equivalent
        // pointed at the active-sample file, mirroring
        // refreshSAM3CollectorImages (so per-slice-pinned collectors
        // keep their state).
        const upstream = _findUpstreamImageNode(node, "image");
        const fileWidget = upstream?.widgets?.find((w) => w.name === "image");
        const filename = fileWidget?.value;
        if (
          upstream &&
          LOAD_IMAGE_NODE_TYPES.has(upstream.type) &&
          filename === "fo_current_sample.png"
        ) {
          if (handler(node)) {
            collectorReset++;
            _DBG("  cleared prompts on", node.comfyClass, "id=", node.id);
          }
        }
        continue;
      }

      const textWidgetName = TEXT_PROMPT_TARGETS[node.comfyClass];
      if (textWidgetName) {
        const w = node.widgets?.find((x) => x.name === textWidgetName);
        if (w && w.value) {
          w.value = "";
          textReset++;
          _DBG(
            "  cleared", textWidgetName, "on", node.comfyClass, "id=", node.id,
          );
        }
      }
    }

    if (collectorReset || textReset) {
      app.graph.setDirtyCanvas(true, true);
    }
    _DBG(
      "clearStalePromptsOnSampleChange: collectorReset=", collectorReset,
      "textReset=", textReset,
    );
  } catch (e) {
    console.warn("[fiftyone-bridge] prompt-clear error:", e);
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

function nodeHas3DOutput(node) {
  // Has a 3D output socket type — covers MESH / VOXEL / FILE_3D_* etc.
  if (node.outputs?.some((o) => THREE_D_OUTPUT_TYPES.has(o.type))) {
    return true;
  }
  // ...or has previously emitted a 3D file (terminal save node like
  // SaveGLB / pack-native SaveOBJ etc., which have no output sockets).
  return _last3DOutputs.has(String(node.id));
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

// Right-click "Save 3D to FiftyOne" — pulls the latest cached 3D file
// emitted by this node (populated by the ``executed`` event listener)
// and ships it to the FiftyOne panel as a new sample.  Uses the same
// auto-save path FO_Save3D's node-based flow does, defaulting to
// ``save_mode="new_sample"`` for a zero-click experience.  Users who
// want group_slice / labels should drop the FO_Save3D node into their
// workflow instead — the right-click is a quick path.
function saveToFiftyOne3D(node) {
  _DBG("saveToFiftyOne3D: node=", node.type, "id=", node.id);
  const cached = _last3DOutputs.get(String(node.id));
  if (!cached) {
    _DBG(
      "saveToFiftyOne3D: ABORTED — no cached 3D output for node",
      node.id,
      "(node may not have run yet — queue the workflow first)",
    );
    return;
  }

  _DBG("saveToFiftyOne3D: sending OUTPUT_READY, filename=", cached.filename);
  window.parent.postMessage(
    {
      type: MSG.OUTPUT_READY,
      outputType: "3d",
      nodeTitle: node.title || node.type,
      nodeId: node.id,
      promptId: lastPromptId,
      workflowName: cachedWorkflowName || getWorkflowName(),
      filename: cached.filename,
      subfolder: cached.subfolder,
      autoSave: true,
      saveMode: "new_sample",
      fieldName: "comfy_output",
      copyLabels: "",
    },
    "*"
  );
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
