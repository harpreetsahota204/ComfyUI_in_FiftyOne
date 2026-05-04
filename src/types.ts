export interface TemplateInfo {
  id: string;
  name: string;
  description: string;
  file: string;
  input_types: string[];
  output_type: string;
  category?: string;
}

export type ServerStatus =
  | "starting"
  | "ready"
  | "not_running"
  | "not_found"
  | "error"
  | "timeout"
  | "stopped"
  | "";

export type ComfyOutputType =
  | "image"
  | "text"
  | "depth"
  | "video"
  | "detections"
  | "segmentation";

export type SaveDestination =
  | "group_slice"
  | "string_field"
  | "classification"
  | "heatmap"
  | "new_sample"
  | "field";

export interface SliceInfo {
  name: string;
  mediaType: string;
}

/**
 * Optional extras carried for richer save payloads (detections /
 * segmentation).  Forwarded verbatim to ``SaveComfyOutput`` operator
 * params with snake_case keys (the React panel converts).
 */
export interface OutputExtras {
  field?: string;
  imageHeight?: number;
  imageWidth?: number;
  boxesJson?: string;
  predLabelsJson?: string;
  scoresJson?: string;
  masksFilename?: string;
  fallbackLabels?: string;
  maskTargets?: string;
}

export interface OutputReadyPayload {
  outputType: ComfyOutputType;
  nodeTitle: string;
  nodeId: number | null;
  promptId: string | null;
  workflowName?: string;
  filename?: string;
  subfolder?: string;
  textValue?: string;
  imageDataBase64?: string;
  autoSave?: boolean;
  saveMode?: SaveDestination;
  fieldName?: string;
  /** Comma-separated list of label-field names to copy from the source
   *  sample onto the new sample.  ``""`` means copy nothing. */
  copyLabels?: string;
  extras?: OutputExtras;
}

/**
 * Save-destination options shown in the SaveDialog.
 *
 * The dialog only opens for the right-click flows in the iframe bridge,
 * which are wired exclusively for image-producing nodes (`saveToFiftyOne`)
 * and string-producing nodes (`saveTextToFiftyOne`). All other output
 * types (depth / video / detections / segmentation) flow through the
 * `autoSave=true` path that bypasses the dialog entirely, so they don't
 * need entries here.
 */
export const SAVE_OPTIONS: Partial<
  Record<ComfyOutputType, { value: SaveDestination; label: string }[]>
> = {
  image: [
    { value: "group_slice", label: "Group Slice" },
    { value: "new_sample", label: "New Sample" },
  ],
  text: [
    { value: "string_field", label: "String Field" },
    { value: "classification", label: "Classification" },
  ],
};
