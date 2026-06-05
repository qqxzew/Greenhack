import * as vscode from 'vscode';
import { PROVIDERS, ProviderId } from './providers';

// API keys live in VS Code SecretStorage (OS keychain), never in settings or globalState.
const keyName = (p: ProviderId) => `argus.apiKey.${p}`;

export function getApiKey(ctx: vscode.ExtensionContext, p: ProviderId): Thenable<string | undefined> {
  return ctx.secrets.get(keyName(p));
}

export function setApiKey(ctx: vscode.ExtensionContext, p: ProviderId, value: string): Thenable<void> {
  return ctx.secrets.store(keyName(p), value);
}

export function clearApiKey(ctx: vscode.ExtensionContext, p: ProviderId): Thenable<void> {
  return ctx.secrets.delete(keyName(p));
}

export async function availableProviders(ctx: vscode.ExtensionContext): Promise<ProviderId[]> {
  const out: ProviderId[] = [];
  for (const p of Object.keys(PROVIDERS) as ProviderId[]) {
    if (await getApiKey(ctx, p)) out.push(p);
  }
  return out;
}
