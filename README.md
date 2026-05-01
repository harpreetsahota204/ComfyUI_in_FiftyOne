# FiftyOne ComfyUI Plugin

Embed a full [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instance inside the FiftyOne sample modal. Run any workflow against the current sample, and save outputs back to your dataset as group slices, new samples, fields, heatmaps, or classifications — with one click.

---

## What this plugin does

When you open a sample in FiftyOne, a new **ComfyUI** tab appears in the modal. That tab hosts a real ComfyUI instance in an embedded iframe — the same UI you'd get at `localhost:8188` — but wired into your dataset:

- The current sample's image is automatically copied into ComfyUI's input directory and exposed as `fo_current_sample.png`. Drop a `LoadImage` node, point it at that filename, and you're ready to go.
- Every group slice on the sample is also exposed as `fo_current_sample_<slice>.png` for multi-input workflows.
- The plugin ships custom **save nodes** (`Save Image to FiftyOne`, `Save Video to FiftyOne`, `Save Text to FiftyOne`, `Save Depth to FiftyOne`) that send outputs back to FiftyOne with no further clicks.
- A right-click menu also lets you save outputs from any image-producing ComfyUI node directly to the dataset, or convert a native `SaveImage` node into a `FO_SaveImage` in one action.
- Saves can land as a **new group slice**, a **new sample**, a **`fo.Heatmap`** field, a **`fo.Classification`** field, or a **string field** — depending on output type and your choice.
- Optionally copy any subset of the source sample's `fo.Label` fields onto the new sample.
- 17 starter workflow templates ship with the plugin; you can save your own.

---

## Requirements

- **FiftyOne** ≥ 0.25
- **Python** ≥ 3.9 (matching your FiftyOne install)
- **ComfyUI** — installed and reachable on disk. The plugin spawns it on demand and reuses an existing process if one is running.
- **ffmpeg** on `$PATH` — only needed if you'll save raw video frames via `FO_SaveVideo`. Saving existing video files (e.g. from `VHS_VideoCombine`) works without it.

---

## Installation

### 1. Get ComfyUI running on your machine

If you don't already have it:

```bash
git clone https://github.com/comfyanonymous/ComfyUI
cd ComfyUI
pip install -r requirements.txt
```

Note the absolute path to your `ComfyUI` directory — you'll point the plugin at it in step 3.

### 2. Register the plugin with FiftyOne

From this repo's root:

```bash
fiftyone plugins download <path-to-this-repo> --plugin-names "@harpreetsahota/comfyui-plugin"
```

Or symlink/copy `comfyui-plugin/` into your FiftyOne plugins directory:

```bash
ln -s "$(pwd)/comfyui-plugin" "$(fiftyone config plugins_dir)/comfyui-plugin"
```

### 3. Build the React panel

```bash
cd comfyui-plugin
npm install
npm run build
```

This produces `dist/index.umd.js`, which FiftyOne loads as the panel's frontend.

### 4. Tell the plugin where ComfyUI lives

By default the plugin looks for ComfyUI at `~/comfy/ComfyUI`. If yours is elsewhere, you'll set it the first time you open the panel (via the **Settings** button — see *Configuration* below).

### 5. Launch FiftyOne

```bash
fiftyone app launch
```

When you open a sample modal, the **ComfyUI** tab appears alongside the existing tabs.

---

## Quick start

1. Open a sample in the modal (any image, video, or 3D sample).
2. Click the **ComfyUI** tab.
3. The plugin spawns ComfyUI on first use (takes ~10-30 seconds while models load). The panel shows a spinner; subsequent opens reuse the same process and are instant.
4. ComfyUI's iframe loads with a starter workflow: `LoadImage("fo_current_sample.png") → PreviewImage` plus a pre-wired `FO_SaveImage`.
5. Either:
   - **Use a template**: click the *Load template…* dropdown in the toolbar and pick from 17 built-in workflows.
   - **Build your own**: drag in nodes as usual, leave a `LoadImage` pointed at `fo_current_sample.png` for the current image input.
6. Click **Queue** (ComfyUI's run button) to run the workflow.
7. When outputs arrive they're saved back to FiftyOne automatically (if you used an `FO_Save*` node) or via right-click (see below).

---

## Saving outputs

There are three ways to get a workflow's output into FiftyOne. Pick whichever fits your style:

### A. `FO_Save*` nodes (recommended)

Drop one of these into your workflow:

| Node | Saves as | Destination options |
|---|---|---|
| **`FO_SaveImage`** | image | new sample · group slice |
| **`FO_SaveVideo`** | H.264 MP4 | new sample · group slice |
| **`FO_SaveText`** | string field on the current sample | string field · classification |
| **`FO_SaveDepth`** | `fo.Heatmap` field on the current sample | (heatmap field name) |

When the workflow finishes, the save fires automatically — no dialog, no click. The node has widgets for:

- **`save_mode`** (image/video only) — `new_sample` or `group_slice`. Defaults to `new_sample`.
- **`name`** — slice name (when `group_slice`) or field name (text / depth).
- **`labels`** — optional **Copy labels** picker (see below). Defaults to none.

### B. Right-click on any node with image output → "Save Image to FiftyOne"

Right-click any ComfyUI node that has an `IMAGE` output and pick **Save Image to FiftyOne**. A dialog pops up letting you pick:

- Save destination (new sample / group slice).
- A name for the slice/file.
- Which `fo.Label` fields to copy from the source sample (multi-select pill picker).

This is a **one-shot save** — it takes whatever's currently displayed by the node and ships it to FiftyOne. The workflow itself is unchanged.

### C. Right-click a native ComfyUI `SaveImage` → "Convert to Save Image to FiftyOne"

If you've loaded an existing ComfyUI workflow that uses the native `SaveImage` node, right-click it and pick **Convert to Save Image to FiftyOne**. The node is replaced with `FO_SaveImage` at the same position with the IMAGE input wire reconnected. Future runs auto-save through the FiftyOne pipeline.

---

## Save destinations explained

### Group slice

Saves create a new `fo.Sample` in the same group as the source, on a slice named after your `name` widget (or the value you typed in the Save dialog).

If your dataset is **flat** (no group field), the first `group_slice` save converts it to a grouped dataset. The original sample becomes the `original` slice, and the save lands on a new slice. **You'll see a yellow banner asking you to refresh the browser and reopen the sample modal once the workflow finishes** — FiftyOne's modal does not pick up the new group structure mid-flight.

### New sample

Saves create a brand-new `fo.Sample` with `tags=["comfy_output"]` and a `source_sample_id` field linking back to the source. In a grouped dataset, the new sample gets its own fresh group on a slice that matches its media type (image vs. video).

### Heatmap (depth saves)

`FO_SaveDepth` writes the depth map as a PNG and attaches it to the current sample as a `fo.Heatmap` field with `map_path` pointing at the file. **You'll see a banner asking you to refresh the browser** to see the heatmap render — FiftyOne's heatmap layer caches aggressively and won't pick up the new field without a refresh.

### String field / Classification (text saves)

`FO_SaveText` writes the text to a field on the current sample. Either as a plain `StringField` (default) or wrapped in `fo.Classification(label=text)` if you pick that destination from the dialog.

---

## Copy labels (optional)

Both the `FO_SaveImage` / `FO_SaveVideo` nodes and the right-click save dialog have a **Copy labels** picker — a pill multi-select listing every `fo.Label` field on the source sample with a non-`None` value. Click rows to add pills, click `×` on a pill to remove. Empty (default) = copy nothing.

When you save, those label fields are deep-copied onto the new sample (using `copy.deepcopy`, the same pattern as `fiftyone-image-edit-panel`). Helpful if you want generated outputs to inherit the source's annotations.

The picker only shows fields that are *actually populated on the sample you're viewing* — empty fields are filtered out server-side.

---

## Templates

The plugin ships 17 starter templates covering common workflows: image edit (Qwen), pose-/canny-/depth-to-image (Z-Image-Turbo), upscale, captioning (Gemini), inpainting, outpainting, depth (Lotus), 3D (Hunyuan3D), image-to-video (Wan), plus simple image-processing recipes (blur, sharpen, brightness, hue/saturation, film grain).

Pick one from the **Load template…** dropdown in the toolbar; the workflow loads with the current sample's image already wired into the right input slots. Run as-is or modify before queueing.

To save your own template: build the workflow you want, click **Save Template** in the toolbar, give it a name. Saved templates appear in the dropdown alongside the built-ins on the next panel load. Templates are stored as workflow JSON in `comfyui-plugin/templates/`.

---

## Multi-input workflows (grouped datasets)

If your sample is part of a grouped dataset, every image-typed slice gets its own file in ComfyUI's input directory:

- `fo_current_sample.png` — follows the active modal slice (updates when you click slice tabs).
- `fo_current_sample_<slice_name>.png` — one per group slice (e.g. `fo_current_sample_close_up.png`).

Drop multiple `LoadImage` nodes and pick a different per-slice file in each one's dropdown. You can also use the `FO_LoadImage` node (under `FiftyOne/IO`) — it's a thin wrapper around the built-in `LoadImage` that's just there for discoverability.

When you switch the active slice tab in the modal, `fo_current_sample.png` updates and any `LoadImage` referencing it refreshes its preview automatically.

---

## Slice handling at save time

Saves always target the slice you're **currently viewing** in the modal — not the original-slice sample. If you're on the `qwen_edit` slice and run a depth workflow, the heatmap lands on `qwen_edit`'s sample, not the original.

This is true for all save modes (image/video to slice, image/video to new sample, text, depth). The active slice is read from FiftyOne's Recoil `modalGroupSlice` atom and passed explicitly to the save operator, so it stays correct even when you paginate through slices quickly.

---

## Configuration

Click the **Settings** button in the panel toolbar to configure:

- **ComfyUI Path** — absolute path to your ComfyUI install (where `main.py` lives). Defaults to `~/comfy/ComfyUI`.
- **Port** — port to run ComfyUI on. Defaults to `8188`.

Settings are persisted in FiftyOne's execution store (`~/.fiftyone/comfyui_plugin/`). After changing them, click **Save & Restart** to apply.

The plugin's PID file lives at `~/.fiftyone/comfyui_plugin/.comfyui.pid`. If ComfyUI is already running externally on the configured port, the plugin will detect it and reuse it instead of spawning a duplicate.

---

## Saving generation metadata

Every save automatically captures and stores the workflow's generation parameters as fields on the new sample:

- `comfy_workflow_name` — the template/workflow name.
- `comfy_prompt` / `comfy_negative_prompt` — extracted heuristically from any node with a string input named `text`/`prompt`/`positive`/etc., classified by the node class type.
- `comfy_seed` / `comfy_steps` / `comfy_cfg` / `comfy_sampler` / `comfy_scheduler` / `comfy_denoise` — extracted from any KSampler-style node.
- `comfy_model` — the checkpoint or unet name.
- `comfy_node_title` / `comfy_prompt_id` — for traceability.
- `comfy_workflow_json` — the full API workflow JSON, stringified.

The extraction is heuristic, not workflow-specific — it works with arbitrary workflows (Qwen, Flux, SDXL, custom, etc.) by scanning for known input-key patterns. Some fields may be empty for exotic workflows; that's fine.

---

## Troubleshooting

**The panel shows "Starting ComfyUI…" forever.**
First-time spawn can take 30+ seconds while ComfyUI loads its frontend. If it stalls longer, check the FiftyOne terminal — ComfyUI's stdout is piped there. Common causes: missing dependencies, a custom node failing to import, port already in use.

**The depth heatmap doesn't appear after saving.**
Refresh the browser. FiftyOne's heatmap renderer caches aggressively and won't pick up the new field on the same page load. The plugin shows a yellow banner reminding you of this after each depth save.

**The new group slice doesn't appear in the slice tabs after saving.**
Same fix: refresh the browser, close the sample modal, then reopen it. Banner appears the first time this happens, with the same instructions. After the dataset is grouped you don't need to refresh again — only the flat→grouped transition is sticky.

**`xt is not a function` / `sample not attached` errors in the console.**
These come from FiftyOne's bundled worker code, not from this plugin. They fire on sample modal open and don't break the save flow. Ignore unless you're chasing them in FiftyOne itself.

**`Cannot perform Construct on a detached ArrayBuffer` at `HeatmapOverlay`.**
Same — FiftyOne's heatmap renderer worker. Doesn't affect saves; the heatmap is on the sample, just not currently rendering. Refresh the browser.

**ComfyUI deprecation warnings (`scripts/ui.js`, `groupNode.js`, etc.).**
Coming from ComfyUI-Manager and other custom nodes — not from this plugin. We import only `scripts/app.js` and `scripts/api.js`, both still supported.

---

## Plugin layout

```
comfyui-plugin/
├── fiftyone.yml                    # Plugin manifest
├── package.json                    # JS build config
├── __init__.py                     # Python: panel + operators (1500 lines)
├── comfyui_extension/              # ComfyUI custom nodes (Python + JS)
│   ├── __init__.py                 # Node registration
│   ├── nodes.py                    # FO_SaveImage, FO_SaveVideo, FO_SaveText, FO_SaveDepth, FO_LoadImage
│   └── js/
│       └── fiftyone_bridge.js      # Iframe-side bridge: postMessage protocol, custom widgets, right-click menu
├── src/                            # React panel (TypeScript)
│   ├── ComfyUIPanel.tsx            # Main panel component
│   ├── SaveDialog.tsx              # Right-click save dialog
│   ├── dialogHost.tsx              # Separate React root for the save dialog
│   ├── hooks/usePluginClient.ts    # Panel-method client
│   └── …
├── templates/                      # Starter workflow JSON files + manifest
└── dist/                           # Built bundle (generated by `npm run build`)
```

---

## Development

After a code change:

- **Python changes** (anything in `__init__.py`, `comfyui_extension/`): just reload FiftyOne and reopen the sample. The Python code reloads on each panel-method call.
- **JS / React changes**: run `npm run build` (or `npm run dev` for watch mode), then refresh the browser to pick up the new bundle.
- **ComfyUI bridge JS changes** (`comfyui_extension/js/fiftyone_bridge.js`): refresh the FiftyOne page (the iframe reloads, picking up the new bridge). No build step needed for the bridge.

The plugin emits verbose debug logs in two places:

- **Browser console** — anything prefixed `[fo-panel]`, `[fo-bridge]`, or `[fo-host]`.
- **FiftyOne terminal** — anything prefixed `[comfyui-plugin]`.

When debugging save flows, check both — the React side and the Python operator each log distinct phases.

---

## License

MIT. See repo root.
