import { useCallback, useEffect, useMemo, useRef } from "react";
import { usePanelEvent } from "@fiftyone/operators";

/**
 * URIs for each panel method, read from ``props.schema.view`` at runtime.
 * FiftyOne populates these as ``"pluginName/panelName#methodName"``.
 */
export interface PanelMethodUris {
  initialize: string;
  start_server: string;
  stop_server: string;
  load_template: string;
  save_template: string;
  get_templates: string;
  update_config: string;
  get_group_slices: string;
  inject_slice: string;
  trigger_reload: string;
}

export function usePluginClient(uris: PanelMethodUris) {
  const handleEvent = usePanelEvent();
  // Refs are updated in effects (not during render) so concurrent /
  // StrictMode double-invocation doesn't observe a half-updated ref.
  // The ``call`` callback below memoizes with deps ``[]`` and reads
  // these refs lazily, so the latest values are always used.
  const handleEventRef = useRef(handleEvent);
  useEffect(() => {
    handleEventRef.current = handleEvent;
  }, [handleEvent]);
  const urisRef = useRef(uris);
  useEffect(() => {
    urisRef.current = uris;
  }, [uris]);

  const call = useCallback(
    <T = Record<string, any>>(
      method: keyof PanelMethodUris,
      params: Record<string, any> = {},
      fallback?: T,
    ): Promise<T> =>
      new Promise((resolve) => {
        handleEventRef.current(method, {
          operator: urisRef.current[method],
          params,
          callback: (result: any) => {
            resolve(result?.result ?? fallback ?? ({} as T));
          },
        });
      }),
    []
  );

  return useMemo(
    () => ({
      initialize: (params: { filepath?: string } = {}) =>
        call<Record<string, any>>("initialize", params, { server_status: "unknown" }),
      startServer: () =>
        call<Record<string, any>>("start_server", {}, { server_status: "unknown" }),
      stopServer: () =>
        call<Record<string, any>>("stop_server", {}, { server_status: "unknown" }),
      loadTemplate: (templateId: string, sampleFilename?: string, filepath?: string) =>
        call<{ workflow?: any; error?: string }>("load_template", {
          template_id: templateId,
          sample_filename: sampleFilename || "",
          filepath: filepath || "",
        }),
      saveTemplate: (name: string, workflow: any) =>
        call<{ status?: string; template_id?: string; error?: string }>("save_template", {
          name,
          workflow,
        }),
      getTemplates: (filepath?: string) =>
        call<{ templates?: any[]; default?: string | null }>(
          "get_templates",
          { filepath: filepath || "" },
          { templates: [], default: null },
        ),
      updateConfig: (config: Record<string, any>) =>
        call<{ status: string }>("update_config", config, { status: "unknown" }),
      getGroupSlices: () =>
        call<{
          slices: { name: string; media_type: string }[];
          heatmap_fields: string[];
          label_fields: string[];
          dataset_is_grouped: boolean;
        }>("get_group_slices", {}, {
          slices: [],
          heatmap_fields: [],
          label_fields: [],
          dataset_is_grouped: false,
        }),
      injectSlice: (sliceName: string) =>
        call<{ sample_filename?: string; filepath?: string; error?: string }>(
          "inject_slice",
          { slice_name: sliceName },
        ),
      triggerReload: () =>
        call<{ status: string }>("trigger_reload", {}, { status: "ok" }),
    }),
    [call]
  );
}
