// Canonical message-type constants shared between the React panel and the
// ComfyUI bridge extension (comfyui_extension/js/fiftyone_bridge.js).
// Keep both copies in sync — they cannot share code because the bridge
// runs as a standalone browser script inside the ComfyUI iframe.
export const MSG = {
  BRIDGE_READY: "fiftyone_bridge_ready",
  LOAD_WORKFLOW: "fiftyone_load_workflow",
  GET_WORKFLOW: "fiftyone_get_workflow",
  WORKFLOW_DATA: "fiftyone_workflow_data",
  OUTPUT_READY: "fiftyone_output_ready",
  OUTPUT_AVAILABLE: "fiftyone_output_available",
  SAMPLE_CHANGED: "fiftyone_sample_changed",
  SLICE_INFO: "fiftyone_slice_info",
} as const;
