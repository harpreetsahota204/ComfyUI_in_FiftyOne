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

export type ComfyOutputType = "image" | "text" | "depth" | "video";

export type SaveDestination =
  | "group_slice"
  | "string_field"
  | "classification"
  | "heatmap"
  | "new_sample";

export interface SliceInfo {
  name: string;
  mediaType: string;
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
}

export const SAVE_OPTIONS: Record<ComfyOutputType, { value: SaveDestination; label: string }[]> = {
  image: [
    { value: "group_slice", label: "Group Slice" },
    { value: "new_sample", label: "New Sample" },
  ],
  text: [
    { value: "string_field", label: "String Field" },
    { value: "classification", label: "Classification" },
  ],
  depth: [
    { value: "heatmap", label: "Heatmap" },
  ],
  video: [
    { value: "group_slice", label: "Group Slice" },
    { value: "new_sample", label: "New Sample" },
  ],
};
