export type ApiError = Error & { status?: number };

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export function getToken(): string | null {
  return localStorage.getItem("ptmh_token");
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem("ptmh_token", token);
  else localStorage.removeItem("ptmh_token");
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type") && options.body) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    const error = new Error(await response.text()) as ApiError;
    error.status = response.status;
    throw error;
  }
  return response.json() as Promise<T>;
}

export function formatBytes(value: number): string {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let current = value;
  for (const unit of units) {
    if (current < 1024 || unit === units[units.length - 1]) return `${current.toFixed(1)} ${unit}`;
    current /= 1024;
  }
  return `${current.toFixed(1)} PB`;
}

export function formatSpeed(value: number): string {
  return `${formatBytes(value)}/s`;
}

