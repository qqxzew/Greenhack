import * as vscode from 'vscode';

// Persistent spend/savings counters, kept in globalState so they survive reloads.
const SPENT = 'argus.spentCents';
const SAVED = 'argus.savedCents';
const CALLS = 'argus.calls';

export interface Usage {
  spentCents: number;
  savedCents: number;
  calls: number;
}

export function loadUsage(ctx: vscode.ExtensionContext): Usage {
  return {
    spentCents: ctx.globalState.get<number>(SPENT, 0),
    savedCents: ctx.globalState.get<number>(SAVED, 0),
    calls: ctx.globalState.get<number>(CALLS, 0),
  };
}

export async function addSpend(
  ctx: vscode.ExtensionContext,
  spentCents: number,
  savedCents: number,
): Promise<Usage> {
  const u = loadUsage(ctx);
  const next: Usage = {
    spentCents: u.spentCents + spentCents,
    savedCents: u.savedCents + savedCents,
    calls: u.calls + 1,
  };
  await ctx.globalState.update(SPENT, next.spentCents);
  await ctx.globalState.update(SAVED, next.savedCents);
  await ctx.globalState.update(CALLS, next.calls);
  return next;
}

export async function resetUsage(ctx: vscode.ExtensionContext): Promise<void> {
  await ctx.globalState.update(SPENT, 0);
  await ctx.globalState.update(SAVED, 0);
  await ctx.globalState.update(CALLS, 0);
}

export function budgetCents(): number {
  return vscode.workspace.getConfiguration('argus').get<number>('budgetCents', 10000);
}
