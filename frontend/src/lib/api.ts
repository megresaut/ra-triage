// TODO(Agent 2): typed fetch client built on top of `fetch` and the types
// generated into ./types.ts. For the scaffold, we expose only the health URL
// constant so the Queue page can render a backend connectivity indicator.

export const BACKEND_BASE_URL = "http://127.0.0.1:5000";

export interface HealthResponse {
  status: string;
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch(`${BACKEND_BASE_URL}/health`, { signal });
  if (!response.ok) {
    throw new Error(`backend unhealthy: HTTP ${response.status}`);
  }
  return (await response.json()) as HealthResponse;
}
