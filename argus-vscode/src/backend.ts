import * as vscode from 'vscode';

// Talks to the local Argus governance backend (FastAPI, default :8000).
// The realtime "tokens without / with Argus" figures come straight from
// GET /v1/agents -> org.tokens_used (governed) and org.tokens_saved (avoided).
declare function fetch(input: any, init?: any): Promise<any>;

export interface TokenStats {
  online: boolean;
  withArgus: number; // tokens actually spent through the pipeline
  withoutArgus: number; // what it would have cost with no caching/dedup
  saved: number; // tokens avoided by Argus
  savedPct: number; // 0..1
  cacheHitRate: number; // 0..1
}

const OFFLINE: TokenStats = {
  online: false,
  withArgus: 0,
  withoutArgus: 0,
  saved: 0,
  savedPct: 0,
  cacheHitRate: 0,
};

export function apiBase(): string {
  return vscode.workspace
    .getConfiguration('argus')
    .get<string>('apiBase', 'http://localhost:8000')
    .replace(/\/+$/, '');
}

/** Poll the org-level token counters. Never throws — returns an offline stat on failure. */
export async function fetchTokenStats(): Promise<TokenStats> {
  try {
    const res = await fetch(`${apiBase()}/v1/agents`, { method: 'GET' });
    if (!res.ok) return OFFLINE;
    const data = await res.json();
    const org = data?.org ?? {};
    const withArgus = Number(org.tokens_used ?? 0);
    const saved = Number(org.tokens_saved ?? 0);
    const withoutArgus = withArgus + saved;
    const savedPct = withoutArgus > 0 ? saved / withoutArgus : 0;
    return {
      online: true,
      withArgus,
      withoutArgus,
      saved,
      savedPct,
      cacheHitRate: Number(org.cache_hit_rate ?? 0),
    };
  } catch {
    return OFFLINE;
  }
}

/** Quick liveness probe against /v1/health. */
export async function ping(): Promise<boolean> {
  try {
    const res = await fetch(`${apiBase()}/v1/health`, { method: 'GET' });
    return !!res.ok;
  } catch {
    return false;
  }
}
