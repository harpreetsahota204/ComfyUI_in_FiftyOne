# FiftyOne ComfyUI Plugin

<div align="center">
<p align="center">

<!-- prettier-ignore -->
<img src="https://user-images.githubusercontent.com/25985824/106288517-2422e000-6216-11eb-871d-26ad2e7b1e59.png" height="55px"> &nbsp;
<img src="https://user-images.githubusercontent.com/25985824/106288518-24bb7680-6216-11eb-8f10-60052c519586.png" height="50px">

**The open-source tool for building high-quality datasets and computer vision
models**

---

<!-- prettier-ignore -->
<a href="https://voxel51.com/fiftyone?utm_source=harpreet-gh">Website</a> •
<a href="https://docs.voxel51.com?utm_source=harpreet-gh">Docs</a> •
<a href="https://colab.research.google.com/github/voxel51/fiftyone-examples/blob/master/examples/quickstart.ipynb?utm_source=harpreet-gh">Try it Now</a> •
<a href="https://docs.voxel51.com/getting_started_guides/index.html?utm_source=harpreet-gh">Getting Started Guides</a> •
<a href="https://docs.voxel51.com/tutorials/index.html?utm_source=harpreet-gh">Tutorials</a> •
<a href="https://voxel51.com/blog/?utm_source=harpreet-gh">Blog</a> •
<a href="https://discord.gg/fiftyone-community?utm_source=harpreet-gh">Community</a>

[![Discord](https://img.shields.io/badge/Discord-7289DA?logo=discord&logoColor=white)](https://discord.gg/fiftyone-community)
[![Hugging Face](https://img.shields.io/badge/Hugging_Face-purple?style=flat&logo=huggingface)](https://huggingface.co/Voxel51)
[![Voxel51 Blog](https://img.shields.io/badge/Voxel51_Blog-ff6d04?style=flat)](https://voxel51.com/blog)
[![Newsletter](https://img.shields.io/badge/Newsletter-BE5B25?logo=mail.ru&logoColor=white)](https://share.hsforms.com/1zpJ60ggaQtOoVeBqIZdaaA2ykyk)
[![LinkedIn](https://img.shields.io/badge/In-white?style=flat&label=Linked&labelColor=blue)](https://www.linkedin.com/company/voxel51)
[![Twitter](https://img.shields.io/badge/Twitter-000000?logo=x&logoColor=white)](https://x.com/voxel51)
[![Medium](https://img.shields.io/badge/Medium-12100E?logo=medium&logoColor=white)](https://medium.com/voxel51)

</p>
</div>


Run [ComfyUI](https://github.com/comfyanonymous/ComfyUI) right inside the FiftyOne sample modal. Your current sample is auto-loaded into ComfyUI, you run any workflow against it, and outputs save straight back to your dataset — as new samples, group slices, or label fields. No exporting, no copy-paste.

---

## Quickstart

**You'll need:** FiftyOne ≥ 0.25, Python ≥ 3.9, and a working [ComfyUI](https://docs.comfy.org/) install. Most workflows run on CPU; a CUDA GPU is only needed for the optional detection/segmentation packs.

### 1. Install ComfyUI

```bash
pip install comfy-cli
comfy install
```

Installs to `~/comfy/ComfyUI` (the plugin's default path). Already have ComfyUI elsewhere? You'll point the plugin at it via **Settings** later.

> **Models and nodes are managed by ComfyUI, not this plugin.** Install checkpoints, LoRAs, and custom node packs with **ComfyUI Manager** or the [ComfyUI docs](https://docs.comfy.org/). A workflow won't run until the models and nodes it uses are present.

### 2. Start ComfyUI (and leave it running)

```bash
comfy launch
```

Serves on `localhost:8188` — the port the plugin expects.

### 3. Install the plugin

```bash
fiftyone plugins download https://github.com/harpreetsahota204/ComfyUI_in_FiftyOne
```

The panel ships pre-built and needs no extra dependencies.

### 4. Launch FiftyOne

```bash
fiftyone app launch
```

Open a sample, click the **ComfyUI** tab — you're in.

---

## Using it

It's standard ComfyUI — same nodes, same graph editor, same **Queue** button — with two additions:

1. **Your current sample is already loaded** as `fo_current_sample.png`. Point any `LoadImage` node at it.
2. **The `FO_Save*` nodes** (under **FiftyOne/IO**) send outputs back to your dataset.

**Typical flow:** open a sample → **ComfyUI** tab → pick a **Load template…** or build a graph → **Queue** → the output lands in your dataset automatically.

Prefer not to add a save node? **Right-click any node** with an image, text, or 3D output → **"Save … to FiftyOne"** for a one-off save that leaves your workflow untouched.

---

## Saving outputs

Drop any of these **FiftyOne/IO** nodes into a workflow:

| Node | Saves to FiftyOne as |
|---|---|
| `FO_SaveImage` | new sample or group slice |
| `FO_SaveVideo` | new sample or group slice (H.264 MP4) |
| `FO_Save3D` | new sample or group slice (3D model) |
| `FO_SaveText` | string field (or `fo.Classification`) |
| `FO_SaveDepth` | `fo.Heatmap` field |
| `FO_SaveDetections` | `fo.Detections` field |
| `FO_SaveSegmentation` | `fo.Segmentation` field |

Key widgets:

- **`save_mode`** — `new_sample` or `group_slice` (image / video / 3D nodes).
- **`name`** — the group-slice name, or the target field name.
- **`field`** — target field for detections / segmentation.
- **Copy labels** — a picker to deep-copy existing label fields from the source sample onto the new one (only populated fields are listed).

`FO_SaveDetections` and `FO_SaveSegmentation` accept the output of the optional detection/segmentation packs directly, and `FO_SaveDetections` can derive boxes from masks alone. Every save also records the generation parameters (prompt, seed, sampler, steps, CFG, model, …) and the workflow graph as fields on the new sample.

> Heatmaps, masks, and brand-new group slices won't render until you refresh the browser — the plugin pops a reminder banner when this applies.

---

## Reload the workflow that generated a sample

Open a sample that ComfyUI produced and the panel **auto-loads the exact graph that made it** — seed, sampler, steps, prompts, and a `LoadImage` pointing at the original source image. It's pinned at the top of the **Load template…** dropdown as **✨ Workflow that generated this sample** and selected by default, so you can always return to it after experimenting.

It reads the workflow embedded in the image's PNG metadata (so it even works for ComfyUI images imported from elsewhere), and falls back to a copy saved on the sample. Plain, non-ComfyUI samples just show the starter workflow.

---

## Grouped datasets (multi-input)

In a grouped dataset, each image slice is available in ComfyUI's input directory:

- `fo_current_sample.png` — follows the active slice tab (updates as you switch tabs).
- `fo_current_sample_<slice>.png` — one static file per slice.

Use multiple `LoadImage` nodes to feed different slices into a single workflow.

---

## Configuration

Click **Settings** in the panel toolbar to set the **ComfyUI Path** (default `~/comfy/ComfyUI`) and **Port** (default `8188`), then **Save & Restart**. If a ComfyUI server is already running on that port, the plugin reuses it.

---

## Troubleshooting

- **Panel stuck on "Starting ComfyUI…" or "refused to connect"** — make sure ComfyUI is running (`comfy launch`) on the port set in **Settings**.
- **A heatmap, mask, or new group slice doesn't appear after saving** — refresh the browser; FiftyOne caches these per page load (the plugin shows a reminder banner).
- **ComfyUI deprecation warnings** — come from ComfyUI or other custom nodes, not this plugin.

---

## License

MIT. See repo root.
