/**
 * Minimal type stubs for @fiftyone/* packages.
 *
 * These packages are provided as UMD globals by the FiftyOne App at runtime
 * and are externalized by Vite at build time.  The stubs here exist solely
 * to keep TypeScript happy during development.
 */

declare module "@fiftyone/operators" {
  export function usePanelEvent(): (
    eventName: string,
    options: {
      operator: string;
      params: Record<string, any>;
      callback: (result: any) => void;
    }
  ) => void;

  export interface OperatorExecutor {
    execute: (params: Record<string, any>) => Promise<any>;
    isLoading: boolean;
  }

  export function useOperatorExecutor(uri: string): OperatorExecutor;
}

declare module "@fiftyone/state" {
  /** Recoil atom: name of the slice currently active in the modal viewer. */
  export const modalGroupSlice: any;
}

declare module "recoil" {
  /** Loadable variant — does NOT suspend the component while the atom loads. */
  export interface RecoilLoadable<T> {
    state: "hasValue" | "loading" | "hasError";
    contents: T;
  }
  export function useRecoilValueLoadable<T>(atom: any): RecoilLoadable<T>;
}

declare module "@fiftyone/plugins" {
  export enum PluginComponentType {
    Component = "Component",
  }

  export function registerComponent(opts: {
    name: string;
    component: React.ComponentType<any>;
    type: PluginComponentType;
    activator?: (ctx: any) => boolean;
  }): void;
}
