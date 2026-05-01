import { registerComponent, PluginComponentType } from "@fiftyone/plugins";
import ComfyUIPanel from "./ComfyUIPanel";

// The ``name`` must match the ``component`` kwarg in the Python
// ``ComfyUIPanel.render()`` → ``types.View(component="ComfyUIPanel", ...)``.
// composite_view renders use PluginComponentType.Component (type 3).
registerComponent({
  name: "ComfyUIPanel",
  component: ComfyUIPanel,
  type: PluginComponentType.Component,
});
