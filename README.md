# FiftyOne ComfyUI Plugin

Embed a full [ComfyUI](https://github.com/comfyanonymous/ComfyUI) instance inside the FiftyOne sample modal. Run any workflow against the current sample and save outputs back to your dataset — as group slices, new samples, fields, heatmaps, classifications, detections, segmentation masks, or 3D models.

---

## What this plugin does

Open a sample in FiftyOne. Click the **ComfyUI** tab. You get a real ComfyUI instance in an iframe — the same UI you'd hit at `localhost:8188`, just wired into your dataset:

- **Your sample is already in ComfyUI's input directory** as `fo_current_sample.png`. Drop a `LoadImage` node, point it at that filename, and you're ready.
- **Outputs go back to FiftyOne automatically** when you use any of the bundled `FO_Save*` nodes. No download/upload, no copy-paste.
- **Right-click any image-producing node** → "Save Image to FiftyOne" — for one-off saves without modifying the workflow.
- **19 starter workflow templates** ship with the plugin, including object detection (Grounding DINO) and segmentation (SAM3).
- **Bundled detection + segmentation packs** — `ComfyUI-Grounding` and `ComfyUI-SAM3` are vendored and installed automatically.

---

## Requirements

- **FiftyOne** ≥ 0.25
- **Python** ≥ 3.9
- **ComfyUI** — see install instructions below
- **CUDA-capable GPU** (≥ 6 GB VRAM) — only for the bundled detection / segmentation pipelines. Other workflows work on CPU.
- **ffmpeg** on `$PATH` — only if you want to encode raw video frames via `FO_SaveVideo`.

First-time `pip install -r requirements.txt` pulls ~2-4 GB of ML dependencies (`transformers`, `huggingface_hub`, etc.). Allow a few minutes.

---

## Installation

### 1. Install ComfyUI

The simplest path is the official `comfy-cli` tool:

```bash
pip install comfy-cli
comfy install
```

This drops ComfyUI at `~/comfy/ComfyUI` — exactly where the plugin looks by default. You don't need to launch ComfyUI yourself; the plugin will start it on demand.

If you already have ComfyUI installed somewhere else, that's fine — you'll point the plugin at it via the **Settings** button on first open (see [Configuration](#configuration)).

### 2. Install the plugin

From this repo:

```bash
fiftyone plugins download <path-to-this-repo> --plugin-names "@harpreetsahota/comfyui-plugin"
```

Or symlink it into your FiftyOne plugins directory:

```bash
ln -s "$(pwd)/comfyui-plugin" "$(fiftyone config plugins_dir)/comfyui-plugin"
```

### 3. Build the React panel

```bash
cd comfyui-plugin
npm install
npm run build
```

This produces `dist/index.umd.js`, which FiftyOne loads as the panel's UI.

### 4. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 5. Launch FiftyOne

```bash
fiftyone app launch
```

Open any sample. The **ComfyUI** tab appears in the modal.

---

## Using the plugin

> **Building workflows works exactly like standard ComfyUI.** All the same nodes, same drag-and-drop graph editor, same Queue button, same right-click menus. The plugin doesn't reinvent ComfyUI — it embeds it. The only differences are:
>
> 1. Your current sample is already available as `fo_current_sample.png` in the LoadImage dropdown.
> 2. The `FO_Save*` nodes (under the **FiftyOne/IO** category) ship outputs back to your dataset.

The flow:

1. Open a sample modal.
2. Click the **ComfyUI** tab. (First time: ~10-30 seconds while ComfyUI spins up. Subsequent opens are instant.)
3. Either pick a starter from the **Load template…** dropdown, or build a workflow yourself.
4. Click **Queue** (ComfyUI's run button).
5. Output lands in your dataset automatically (if you used an `FO_Save*` node) or via right-click (see below).

---

## FiftyOne I/O nodes

All seven save nodes live under **FiftyOne/IO** in ComfyUI's node browser. Drop them into any workflow.

| Node | Inputs | Output type | Widget options | Lands in FiftyOne as |
|---|---|---|---|---|
| **`FO_SaveImage`** | `image: IMAGE` | image | `save_mode`, `name`, `labels` | new sample · group slice |
| **`FO_SaveVideo`** | `video: ANY` (frames / file path / VHS_FILENAMES) | H.264 MP4 | `fps`, `save_mode`, `name`, `labels` | new sample · group slice |
| **`FO_SaveText`** | `text: STRING` | text | `name` | string field on current sample |
| **`FO_SaveDepth`** | `depth: IMAGE` (depth map as 3-channel image) | depth (PNG) | `name` | `fo.Heatmap` field on current sample |
| **`FO_SaveDetections`** | `boxes`, `pred_labels`, `scores`, `masks`, `image` (all optional) | detections | `field`, `labels` (fallback class names) | `fo.Detections` field on current sample |
| **`FO_SaveSegmentation`** | `mask: MASK` | segmentation | `field`, `mask_targets` | `fo.Segmentation` field on current sample |
| **`FO_Save3D`** | `model: ANY` (filepath / File3D / MESH tensor) | 3D model (.glb / .ply / .obj / .stl / .fbx / .pcd) | `save_mode`, `name`, `labels` | new sample · group slice (`media_type="3d"`) |

There's also **`FO_LoadImage`** (also under FiftyOne/IO) — a thin alias for the built-in `LoadImage`, listed here for discoverability.

### Widget reference

- **`save_mode`** (`FO_SaveImage` / `FO_SaveVideo` / `FO_Save3D`) — `new_sample` or `group_slice`. Defaults to `new_sample`.
- **`name`** — slice name (when `save_mode=group_slice`), or field name (text/depth/detections/segmentation).
- **`labels`** — see [Copy labels](#copy-labels-optional) below. On `FO_SaveDetections` it's *fallback class names*, used only when the upstream model doesn't provide them.
- **`field`** (`FO_SaveDetections` / `FO_SaveSegmentation`) — the FiftyOne field name to write to.
- **`mask_targets`** (`FO_SaveSegmentation`) — optional class-index → label mapping. JSON or `key=value,key=value`.

### Polymorphic inputs

`FO_SaveDetections` accepts the output shape of either bundled detector pack on the same sockets:

- **ComfyUI-Grounding**: `BBOX` lists, `STRING` labels, `FLOAT` scores, `MASK` per-instance masks.
- **ComfyUI-SAM3**: `STRING`-JSON boxes / scores, `MASK` per-instance masks.

You can also connect just `masks` (e.g. SAM2 output) — bboxes are auto-derived via tight enclosure.

`FO_SaveVideo` accepts an `IMAGE` batch (frames), a filepath string, a `VHS_FILENAMES` tuple, or any dict containing a `path`/`filename` key.

`FO_Save3D` accepts a filepath string, a `File3D`-style wrapper with a `.save_to(path)` method, or a `MESH` tensor (delegated to ComfyUI's built-in `save_glb` for serialization — same path the native `SaveGLB` node uses).

---

## Save destinations

### `new_sample`

Creates a brand-new `fo.Sample` with `tags=["comfy_output"]` and a `source_sample_id` linking back to the source. In a grouped dataset, the new sample gets its own fresh group on a slice matching its media type.

### `group_slice`

Creates a new sample in the **same group** as the source, on a slice named after your `name` widget. If your dataset is flat, the first such save converts it to a grouped dataset (and a yellow banner asks you to refresh the browser to pick up the new group structure).

### `fo.Heatmap` field — `FO_SaveDepth`

Writes the depth PNG and attaches it to the current sample as a `fo.Heatmap` field. Refresh the browser to see it render.

### `fo.Detections` / `fo.Segmentation` field

`FO_SaveDetections` writes per-instance boxes (with optional labels, scores, and per-instance masks cropped to bbox) onto the current sample. Pixel-space xyxy is auto-converted to FiftyOne's normalized rxywh.

`FO_SaveSegmentation` writes a single semantic mask as `fo.Segmentation`. Stored on disk via `mask_path`. Multi-instance masks are argmaxed to an indexed map.

### String field / `fo.Classification` — `FO_SaveText`

Writes the text to a string field on the current sample (default), or wraps it as `fo.Classification(label=text)` if you pick that destination from the right-click dialog.

---

## Right-click saves

Two extra entry points appear when you right-click a node in the iframe:

- **"Save Image to FiftyOne"** — appears on any node with an `IMAGE` output. Pops a dialog to pick destination (new sample / group slice), name, and `Copy labels`. One-shot save; the workflow itself is unchanged.
- **"Save Text to FiftyOne"** — appears on any node with a `STRING` output. Same idea for text.
- **"Save 3D to FiftyOne"** — appears on any node with a 3D output socket (`MESH`, `FILE_3D_*`) or that has previously emitted a 3D file. Auto-saves to a new sample.
- **"Convert to Save Image to FiftyOne"** — appears on a native `SaveImage` node. Replaces it with `FO_SaveImage` at the same position with the IMAGE wire reconnected. Future runs auto-save through the FiftyOne pipeline.

---

## Copy labels (optional)

`FO_SaveImage`, `FO_SaveVideo`, `FO_Save3D`, and the right-click dialog all expose a **Copy labels** picker — a multi-select pill widget listing every `fo.Label` field on the source sample with a non-`None` value. The selected fields are deep-copied onto the new sample.

The picker only shows fields that are actually populated on the current sample.

---

## Multi-input workflows (grouped datasets)

If your sample is in a grouped dataset, every image-typed slice gets its own file in ComfyUI's input directory:

- `fo_current_sample.png` — follows the **active modal slice** (updates when you click slice tabs).
- `fo_current_sample_<slice_name>.png` — one per slice (e.g. `fo_current_sample_close_up.png`). Static — these don't move with pagination.

Drop multiple `LoadImage` nodes and pick a different per-slice file in each.

When you switch the active slice tab in the modal, `fo_current_sample.png` updates and any `LoadImage` referencing it refreshes automatically.

---

## Templates

The plugin ships **19 starter templates** covering image editing, generation, processing, analysis, video, 3D, detection, and segmentation. Pick from the **Load template…** dropdown in the toolbar.

Save your own: build a workflow, click **Save Template**, give it a name. Saved templates appear in the dropdown alongside the built-ins.

Templates are JSON files in `comfyui_extension/workflows/`. They also surface in ComfyUI's native **Workflow Templates** browser under the `fiftyone_bridge` group.

---

## Adding your own FiftyOne I/O nodes

Want a `FO_SaveKeypoints` node that ships keypoints as `fo.Keypoints`? Or a `FO_SaveEmbedding` that lands a float vector on a sample? Here's the recipe.

For a **simple save node** (one output type, one destination — like `FO_SaveImage`), you touch three files:

### 1. Define the node — [`comfyui_extension/nodes.py`](comfyui-plugin/comfyui_extension/nodes.py)

Add a class following the `FO_SaveImage` pattern:

```python
class FO_SaveYourThing:
    """Save your-thing to FiftyOne as <whatever>."""

    CATEGORY = "FiftyOne/IO"
    FUNCTION = "execute"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    DESCRIPTION = "Short description shown in ComfyUI's node browser."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"data": ("YOUR_TYPE",)},
            "optional": {
                "name": ("STRING", {"default": "your_thing"}),
                # ... whatever widgets you need
            },
        }

    def execute(self, data, name="your_thing", **_):
        # Do whatever serialization you need (write a file, etc.).
        # Then dispatch to the FiftyOne side via PromptServer:
        PromptServer.instance.send_sync("fiftyone.save_output", {
            "type": "your_thing",          # this becomes output_type on the operator
            "save_mode": "field",          # or "new_sample" / "group_slice" / etc.
            "name": name,
            # ... any extra payload keys you need
        })
        return {}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")  # always re-runs
```

### 2. Register the node — [`comfyui_extension/__init__.py`](comfyui-plugin/comfyui_extension/__init__.py)

Add it to both mappings:

```python
from .nodes import FO_SaveYourThing  # add the import

NODE_CLASS_MAPPINGS = {
    # ... existing entries
    "FO_SaveYourThing": FO_SaveYourThing,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    # ... existing entries
    "FO_SaveYourThing": "Save Your Thing to FiftyOne",
}
```

### 3. Handle the output type on the FiftyOne side — [`__init__.py`](comfyui-plugin/__init__.py)

In `SaveComfyOutput.execute()`, route your `output_type` to a save helper. Look for the `if output_type in (...)` block (around line 1589) and add a branch:

```python
elif output_type == "your_thing":
    self._save_your_thing(dataset, sample_id, field_name, ctx.params)
```

Then implement `_save_your_thing()` as a method on `SaveComfyOutput`. The simplest pattern is to follow `_save_text` (lines ~2034) for a single-field save, or `_save_segmentation` (lines ~1952) if your save involves a media file plus mask metadata.

### When you also need React panel awareness

If your save type needs special UI behavior on the React side (a custom banner, a dialog destination, anything beyond the default auto-save), you also touch:

- **[`src/types.ts`](comfyui-plugin/src/types.ts)** — add `"your_thing"` to the `ComfyOutputType` union. If your dialog should show your save destinations to the user, add an entry to `SAVE_OPTIONS`. If your bridge dispatches extra payload fields, declare them on `OutputExtras`.
- **[`src/ComfyUIPanel.tsx`](comfyui-plugin/src/ComfyUIPanel.tsx)** — `executeSave` already forwards everything the operator needs; you'd only edit this if you want a post-save banner (search for `setShowDepthSavedBanner` for the pattern) or some new UX behavior.
- **[`comfyui_extension/js/fiftyone_bridge.js`](comfyui-plugin/comfyui_extension/js/fiftyone_bridge.js)** — if your node needs custom inline widgets or extra payload extraction in the `OUTPUT_READY` synthesis (see the `extras` block ~line 261), add it here.

For most additions only steps 1–3 are necessary.

### After your changes

```bash
cd comfyui-plugin
python -m py_compile __init__.py comfyui_extension/__init__.py comfyui_extension/nodes.py
npm run build  # rebuild the React panel if you touched src/
```

Then restart FiftyOne and reopen the sample modal. Your new node appears under **FiftyOne/IO** in ComfyUI's node browser.

---

## Configuration

Click **Settings** in the panel toolbar:

- **ComfyUI Path** — absolute path to your ComfyUI install (where `main.py` lives). Default: `~/comfy/ComfyUI`. Tilde-expanded automatically.
- **Port** — port to run ComfyUI on. Default: `8188`.

Settings persist in FiftyOne's execution store. After changing them, click **Save & Restart**.

If ComfyUI is already running externally on the configured port, the plugin detects it via PID file (`~/.fiftyone/comfyui_plugin/.comfyui.pid`) and reuses it instead of spawning a duplicate.

---

## Bundled extras

The plugin vendors trimmed copies of two custom-node packs under `vendor/`. They're auto-symlinked into ComfyUI's `custom_nodes/` directory at panel startup:

- **`ComfyUI-Grounding`** — GroundingDINO, MM-GroundingDINO, OWLv2, Florence-2, YOLO-World, plus SAM2 segmentation. ([upstream](https://github.com/harpreetsahota204/ComfyUI-Grounding))
- **`ComfyUI-SAM3`** — SAM3 segmentation: text, click, and box-based, plus four interactive collector nodes that let you click directly on a node-rendered canvas. ([upstream](https://github.com/harpreetsahota204/ComfyUI-SAM3))

If you'd rather use your own build of either pack, drop a real (non-symlink) directory at `ComfyUI/custom_nodes/ComfyUI-Grounding` or `ComfyUI/custom_nodes/ComfyUI-SAM3`. The plugin detects the conflict and skips its symlink, keeping yours.

---

## Saving generation metadata

Every save automatically captures the workflow's parameters as fields on the new sample (`comfy_workflow_name`, `comfy_prompt`, `comfy_negative_prompt`, `comfy_seed`, `comfy_steps`, `comfy_cfg`, `comfy_sampler`, `comfy_scheduler`, `comfy_denoise`, `comfy_model`, `comfy_node_title`, `comfy_prompt_id`, `comfy_workflow_json`).

The extraction is heuristic — it works with arbitrary workflows by scanning for known input-key patterns. Some fields may be empty for exotic workflows; that's fine.

---

## Troubleshooting

**The panel shows "Starting ComfyUI…" forever.**
First-time spawn can take 30+ seconds while ComfyUI loads. If it stalls longer, check the FiftyOne terminal (ComfyUI's stdout is piped there). Common causes: missing dependencies, a custom node failing to import, port already in use.

**The depth heatmap / mask doesn't appear after saving.**
Refresh the browser. FiftyOne's heatmap and mask renderers cache aggressively and won't pick up the new field on the same page load. The plugin shows a yellow banner reminding you of this after each such save.

**The new group slice doesn't appear in the slice tabs after saving.**
Same fix: refresh the browser, close the modal, reopen it. Only the first flat→grouped transition needs this.

**`xt is not a function` / `sample not attached` in the console.**
Pre-existing FiftyOne worker errors. Don't break the save flow; ignore.

**ComfyUI deprecation warnings (`scripts/ui.js`, etc.).**
From ComfyUI Manager and other custom nodes, not from this plugin.

---

## Debug logs

The plugin emits debug logs in three places:

- **Browser console** — `[fo-panel]`, `[fo-bridge]`, `[fo-host]`.
- **FiftyOne terminal** — `[comfyui-plugin]`, `[FO_*]` from the operator save flow.
- **ComfyUI terminal** — node `execute()` prints, vendored pack startup banners.

When debugging save flows, you usually want all three open.

---

## License

MIT. See repo root.
