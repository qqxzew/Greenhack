import * as vscode from 'vscode';
import { PROVIDERS, ProviderId, callModel, estimateTokens } from './providers';
import { pickModel, estimateTier, costCents, premiumCostCents } from './router';
import { loadUsage, addSpend, resetUsage, budgetCents } from './usage';
import { getApiKey, setApiKey, clearApiKey, availableProviders } from './secrets';
import { fetchTokenStats, apiBase, TokenStats, postHeartbeat } from './backend';

let statusBar: vscode.StatusBarItem;
let pollTimer: ReturnType<typeof setInterval> | undefined;
let lastStats: TokenStats | undefined;
let lastUsedModel: string | null = null;
let lastUsedTier: string | null = null;
let lastActiveTask: string = '';

const POLL_MS = 2000;

export async function activate(context: vscode.ExtensionContext) {
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 1000);
  statusBar.command = 'argus.showStatus';
  context.subscriptions.push(statusBar);
  statusBar.text = '$(sync~spin) Argus…';
  statusBar.show();

  context.subscriptions.push(
    vscode.commands.registerCommand('argus.setApiKey', () => cmdSetApiKey(context)),
    vscode.commands.registerCommand('argus.clearApiKey', () => cmdClearApiKey(context)),
    vscode.commands.registerCommand('argus.run', () => cmdRun(context)),
    vscode.commands.registerCommand('argus.resetUsage', () => cmdReset(context)),
    vscode.commands.registerCommand('argus.showStatus', () => cmdShowStatus(context)),
    vscode.commands.registerCommand('argus.connect', () => startupFlow(context, true)),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('argus')) void refreshStatusBar(context);
    }),
    { dispose: () => stopPolling() },
  );

  // 1) On first launch: pop the "enter API key" box. 2) Then start the realtime poll.
  await startupFlow(context, false);
  startPolling(context);
}

export function deactivate() {
  stopPolling();
}

// ── Startup: ask for an API key, then connect to the Argus backend ──────────
async function startupFlow(ctx: vscode.ExtensionContext, force: boolean) {
  const available = await availableProviders(ctx);
  if (available.length === 0 || force) {
    // A bare QuickPick on a background window is easy to miss. Lead with a
    // persistent notification + button so the key prompt is impossible to
    // overlook, THEN open the provider/key boxes.
    if (!force) {
      const choice = await vscode.window.showInformationMessage(
        'Argus is ready. Connect an LLM provider API key to route real prompts and track real spend.',
        'Connect API key',
        'Later',
      );
      if (choice !== 'Connect API key') {
        vscode.window.showInformationMessage(
          'Argus: monitor-only mode (no real LLM calls). ' +
            'Run “Argus: Connect (enter API key)” from the Command Palette anytime.',
        );
        await refreshStatusBar(ctx);
        return;
      }
    }
    const entered = await promptApiKey(ctx);
    if (!entered && available.length === 0) {
      vscode.window.showWarningMessage(
        'Argus: no key entered — monitor-only mode. ' +
          'Run “Argus: Connect (enter API key)” to try again.',
      );
    }
  }
  await refreshStatusBar(ctx);
}

/** The startup "enter API key" box. Returns true if a key was saved. */
async function promptApiKey(ctx: vscode.ExtensionContext): Promise<boolean> {
  const pick = await vscode.window.showQuickPick(
    (Object.keys(PROVIDERS) as ProviderId[]).map((id) => ({ label: PROVIDERS[id].label, id })),
    { placeHolder: 'Argus: pick an LLM provider to connect', ignoreFocusOut: true },
  );
  if (!pick) return false;

  const key = await vscode.window.showInputBox({
    prompt: `Enter your ${PROVIDERS[pick.id].label} API key — Argus routes traffic through its savings algorithms`,
    placeHolder: PROVIDERS[pick.id].keyHint,
    password: true,
    ignoreFocusOut: true,
  });
  if (!key) return false;

  await setApiKey(ctx, pick.id, key.trim());
  vscode.window.showInformationMessage(
    `Argus: ${PROVIDERS[pick.id].label} key saved. Connecting to the algorithms at ${apiBase()}…`,
  );
  return true;
}

// ── Realtime polling of the backend token counters ──────────────────────────
function startPolling(ctx: vscode.ExtensionContext) {
  stopPolling();
  pollTimer = setInterval(() => void refreshStatusBar(ctx), POLL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function fmtTok(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(Math.round(n));
}

async function refreshStatusBar(ctx: vscode.ExtensionContext) {
  const stats = await fetchTokenStats();
  lastStats = stats;
  const available = await availableProviders(ctx);
  const u = loadUsage(ctx);

  // Push live telemetry to backend so the dashboard's "Extension" agent shows real data.
  const tokEst = u.spentCents > 0 ? Math.round((u.spentCents / 100) * (1_000_000 / 3)) : 0;
  void postHeartbeat({
    status: u.calls > 0 ? 'active' : 'idle',
    calls: u.calls,
    spend_cents: u.spentCents,
    saved_cents: u.savedCents,
    tokens_estimated: tokEst,
    last_model: lastUsedModel,
    last_tier: lastUsedTier,
    providers_count: available.length,
    backend_online: stats.online,
    active_task: lastActiveTask,
    budget_cents: budgetCents(),
  });

  if (!stats.online) {
    statusBar.text = `$(plug) Argus · offline`;
    const md = new vscode.MarkdownString();
    md.appendMarkdown(`**Argus — backend unreachable**\n\n`);
    md.appendMarkdown(`Can't see the algorithms at \`${apiBase()}\`.\n\n`);
    md.appendMarkdown('Start the backend:\n');
    md.appendCodeblock('cd Argus\npython -m uvicorn main:app --port 8000\npython seed_demo.py', 'bash');
    statusBar.tooltip = md;
    return;
  }

  const pct = Math.round(stats.savedPct * 100);
  // The headline: tokens WITH Argus vs WITHOUT, in realtime.
  statusBar.text = `$(zap) Argus  ${fmtTok(stats.withArgus)} w/ · ${fmtTok(stats.withoutArgus)} w/o · -${pct}%`;

  const md = new vscode.MarkdownString();
  md.appendMarkdown(`**Argus — realtime token savings**\n\n`);
  md.appendMarkdown(`- With Argus: **${stats.withArgus.toLocaleString()}** tokens\n`);
  md.appendMarkdown(`- Without Argus: **${stats.withoutArgus.toLocaleString()}** tokens\n`);
  md.appendMarkdown(`- Saved: **${stats.saved.toLocaleString()}** tokens (**-${pct}%**)\n`);
  md.appendMarkdown(`- Cache hit-rate: **${Math.round(stats.cacheHitRate * 100)}%**\n\n`);
  md.appendMarkdown(`- Connected to: \`${apiBase()}\` · 🟢 live\n`);
  md.appendMarkdown(
    `- Providers: **${available.length}/3**` +
      (available.length ? ' — ' + available.map((p) => PROVIDERS[p].label).join(', ') : ' (no key entered)') +
      `\n`,
  );
  md.appendMarkdown(`- Local prompts run: **${u.calls}**\n\n`);
  md.appendMarkdown(`_Click for details and actions._`);
  statusBar.tooltip = md;
}

async function cmdSetApiKey(ctx: vscode.ExtensionContext) {
  await promptApiKey(ctx);
  await refreshStatusBar(ctx);
}

async function cmdClearApiKey(ctx: vscode.ExtensionContext) {
  const available = await availableProviders(ctx);
  if (!available.length) {
    vscode.window.showInformationMessage('Argus: no saved keys.');
    return;
  }
  const pick = await vscode.window.showQuickPick(
    available.map((id) => ({ label: PROVIDERS[id].label, id })),
    { placeHolder: 'Remove provider key' },
  );
  if (!pick) return;

  await clearApiKey(ctx, pick.id);
  await refreshStatusBar(ctx);
  vscode.window.showInformationMessage(`Argus: ${PROVIDERS[pick.id].label} key removed.`);
}

async function cmdRun(ctx: vscode.ExtensionContext) {
  const prompt = await vscode.window.showInputBox({
    prompt: 'Prompt for Argus — the router auto-picks the cheapest suitable model',
    placeHolder: 'e.g. rewrite this function more concisely',
    ignoreFocusOut: true,
  });
  if (!prompt) return;

  const available = await availableProviders(ctx);
  const tier = estimateTier(prompt);
  // No keys yet? Still demo the routing across all providers with a simulated cost.
  const pool = available.length ? available : (Object.keys(PROVIDERS) as ProviderId[]);
  const model = pickModel(pool, tier);
  if (!model) {
    vscode.window.showErrorMessage('Argus: could not pick a model.');
    return;
  }

  const hasKey = available.includes(model.provider);
  let inTok: number;
  let outTok: number;
  let simulated = false;
  let replyNote = '';
  lastUsedModel = model.apiId;
  lastUsedTier = tier;
  lastActiveTask = `Routing to ${model.label} [${tier} tier]…`;

  if (hasKey) {
    try {
      const key = (await getApiKey(ctx, model.provider))!;
      const res = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Argus → ${model.label}…` },
        () => callModel(model.provider, model.apiId, key, prompt),
      );
      inTok = res.inputTokens;
      outTok = res.outputTokens;
      replyNote = res.text ? ` · reply: ${res.text.slice(0, 60).replace(/\s+/g, ' ')}…` : '';
    } catch (err: any) {
      vscode.window.showErrorMessage(`Argus: ${model.label} call failed: ${err?.message ?? err}`);
      return;
    }
  } else {
    simulated = true;
    inTok = estimateTokens(prompt);
    outTok = Math.round(estimateTokens(prompt) * 1.5) + 80;
  }

  const spent = costCents(model, inTok, outTok);
  const saved = Math.max(0, premiumCostCents(inTok, outTok) - spent);
  lastActiveTask = `${model.label} [${tier}] — ${inTok}↑ ${outTok}↓ tok · saved ${saved.toFixed(2)}¢`;
  await addSpend(ctx, spent, saved);
  await refreshStatusBar(ctx);

  vscode.window.showInformationMessage(
    `Argus → ${model.label} [${tier}] · in ${inTok}/out ${outTok} tok · ${spent.toFixed(2)}¢ ` +
      `(saved ${saved.toFixed(2)}¢ vs premium)${simulated ? ' · simulated (no key)' : ''}${replyNote}`,
  );
}

async function cmdReset(ctx: vscode.ExtensionContext) {
  const ok = await vscode.window.showWarningMessage(
    'Reset the Argus usage counter?',
    { modal: true },
    'Reset',
  );
  if (ok !== 'Reset') return;
  await resetUsage(ctx);
  await refreshStatusBar(ctx);
}

async function cmdShowStatus(ctx: vscode.ExtensionContext) {
  const s = lastStats;
  const available = await availableProviders(ctx);
  const head = s?.online
    ? `Argus live · ${fmtTok(s.withArgus)} w/ Argus / ${fmtTok(s.withoutArgus)} w/o · -${Math.round(s.savedPct * 100)}% · providers ${available.length}/3`
    : `Argus · backend offline (${apiBase()}) · providers ${available.length}/3`;
  const choice = await vscode.window.showInformationMessage(
    head,
    'Run prompt',
    'Connect key',
    'Reset',
  );
  if (choice === 'Run prompt') void cmdRun(ctx);
  else if (choice === 'Connect key') void cmdSetApiKey(ctx);
  else if (choice === 'Reset') void cmdReset(ctx);
}
