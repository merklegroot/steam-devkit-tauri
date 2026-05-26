import { invoke } from "@tauri-apps/api/core";

export const API_PORT = 32100;

export async function getApiBaseUrl(): Promise<string> {
  try {
    return await invoke<string>("api_base_url");
  } catch {
    return `http://127.0.0.1:${API_PORT}`;
  }
}

export type DevkitState =
  | "devkit_init"
  | "devkit_registering"
  | "devkit_init_failed"
  | "devkit_release"
  | "devkit_not_registered"
  | "devkit_online";

export interface DevkitInfo {
  name: string;
  full_name: string;
  state: DevkitState;
  address: string | null;
  http_port: number;
  added_by_ip: boolean;
  has_mdns_service: boolean;
  ssh_connectivity: boolean | null;
  http_connectivity: boolean | null;
  limited_connectivity: boolean | null;
  guest_lan: boolean;
  is_steamos: boolean;
  steamos_status: Record<string, unknown>;
  steam_client_status: string | null;
  steam_configuration: string | null;
  os_name: string | null;
  os_version: string | null;
  user_password_is_set: boolean | null;
  cef_debugging_enabled: boolean | null;
  machine_login: string | null;
}

export interface TaskStatus {
  key: string;
  done: boolean;
  ok?: boolean;
  error?: string;
  result?: unknown;
}

async function apiFetch<T>(
  base: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error ?? `Request failed (${response.status})`);
  }
  return data as T;
}

export async function waitForBackend(base: string, attempts = 40): Promise<void> {
  for (let i = 0; i < attempts; i++) {
    try {
      await apiFetch<{ ok: boolean }>(base, "/api/health");
      return;
    } catch {
      await new Promise((r) => setTimeout(r, 300));
    }
  }
  throw new Error("Python backend did not start. Run: npm run setup:python");
}

export async function listDevkits(base: string): Promise<DevkitInfo[]> {
  const data = await apiFetch<{ devkits: DevkitInfo[] }>(base, "/api/devkits");
  return data.devkits;
}

export async function getSelectedDevkit(
  base: string,
): Promise<DevkitInfo | null> {
  const data = await apiFetch<{ devkit: DevkitInfo | null }>(base, "/api/selected");
  return data.devkit;
}

export async function selectDevkit(
  base: string,
  name: string | null,
): Promise<DevkitInfo | null> {
  const data = await apiFetch<{ devkit: DevkitInfo | null }>(base, "/api/selected", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  return data.devkit;
}

export async function connectByIp(
  base: string,
  address: string,
  port?: number,
): Promise<DevkitInfo> {
  const data = await apiFetch<{ devkit: DevkitInfo }>(base, "/api/devkits/connect", {
    method: "POST",
    body: JSON.stringify({ address, port }),
  });
  return data.devkit;
}

export async function registerDevkit(base: string, name: string): Promise<string> {
  const data = await apiFetch<{ task: string }>(
    base,
    `/api/devkits/${encodeURIComponent(name)}/register`,
    { method: "POST", body: "{}" },
  );
  return data.task;
}

export async function forgetDevkit(base: string, name: string): Promise<void> {
  await apiFetch(base, `/api/devkits/${encodeURIComponent(name)}/forget`, {
    method: "POST",
    body: "{}",
  });
}

export async function retryDevkit(base: string, name: string): Promise<DevkitInfo> {
  const data = await apiFetch<{ devkit: DevkitInfo }>(
    base,
    `/api/devkits/${encodeURIComponent(name)}/retry`,
    { method: "POST", body: "{}" },
  );
  return data.devkit;
}

export async function refreshStatus(base: string, name: string): Promise<string> {
  const data = await apiFetch<{ task: string }>(
    base,
    `/api/devkits/${encodeURIComponent(name)}/refresh-status`,
    { method: "POST", body: "{}" },
  );
  return data.task;
}

export async function runAction(
  base: string,
  name: string,
  action: string,
  body: Record<string, unknown> = {},
): Promise<string> {
  const data = await apiFetch<{ task: string }>(
    base,
    `/api/devkits/${encodeURIComponent(name)}/${action}`,
    { method: "POST", body: JSON.stringify(body) },
  );
  return data.task;
}

export async function pollTask(
  base: string,
  taskKey: string,
  onUpdate?: (status: TaskStatus) => void,
): Promise<TaskStatus> {
  for (;;) {
    const status = await apiFetch<TaskStatus>(
      base,
      `/api/tasks/${encodeURIComponent(taskKey)}`,
    );
    onUpdate?.(status);
    if (status.done) {
      if (status.ok === false) {
        throw new Error(status.error ?? "Task failed");
      }
      return status;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
}

export async function listGames(
  base: string,
  name: string,
): Promise<unknown[]> {
  const data = await apiFetch<{ games: unknown[] }>(
    base,
    `/api/devkits/${encodeURIComponent(name)}/games`,
  );
  return data.games;
}
