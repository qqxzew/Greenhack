import { ProviderId } from './providers';

// The "algorithm" that does the saving: pick the cheapest model that still fits the task tier.
export type Tier = 'cheap' | 'mid' | 'premium';

export interface ModelSpec {
  provider: ProviderId;
  apiId: string;
  label: string;
  tier: Tier;
  inPrice: number; // USD per 1M input tokens (approximate list prices — adjust freely)
  outPrice: number; // USD per 1M output tokens
}

// Approximate public list prices. Illustrative and easy to edit.
export const MODELS: ModelSpec[] = [
  { provider: 'gemini', apiId: 'gemini-1.5-flash', label: 'Gemini 1.5 Flash', tier: 'cheap', inPrice: 0.075, outPrice: 0.30 },
  { provider: 'openai', apiId: 'gpt-4o-mini', label: 'GPT-4o mini', tier: 'cheap', inPrice: 0.15, outPrice: 0.60 },
  { provider: 'anthropic', apiId: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5', tier: 'cheap', inPrice: 1.00, outPrice: 5.00 },
  { provider: 'gemini', apiId: 'gemini-1.5-pro', label: 'Gemini 1.5 Pro', tier: 'mid', inPrice: 1.25, outPrice: 5.00 },
  { provider: 'openai', apiId: 'gpt-4o', label: 'GPT-4o', tier: 'mid', inPrice: 2.50, outPrice: 10.00 },
  { provider: 'anthropic', apiId: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6', tier: 'mid', inPrice: 3.00, outPrice: 15.00 },
  { provider: 'anthropic', apiId: 'claude-opus-4-8', label: 'Claude Opus 4.8', tier: 'premium', inPrice: 15.00, outPrice: 75.00 },
];

const TIER_RANK: Record<Tier, number> = { cheap: 0, mid: 1, premium: 2 };

/** Cheap heuristic to size the task — keeps easy prompts off expensive models. */
export function estimateTier(prompt: string): Tier {
  const len = prompt.length;
  const heavy = /(refactor|architect|prove|theorem|optimi[sz]e|security|vulnerab|debug|design a|complex|multi-step|reason)/i.test(prompt);
  if (heavy || len > 1200) return 'premium';
  if (len > 400 || /(why|explain|analy|compare|plan)/i.test(prompt)) return 'mid';
  return 'cheap';
}

function avgPrice(m: ModelSpec): number {
  return (m.inPrice + m.outPrice) / 2;
}

/**
 * Pick the cheapest model among available providers that satisfies the tier.
 * If no model matches the exact tier, fall back to the whole pool — but always
 * prefer the lowest average price, which is the whole point of "economy".
 */
export function pickModel(available: ProviderId[], tier: Tier): ModelSpec | undefined {
  const pool = MODELS.filter((m) => available.includes(m.provider));
  if (pool.length === 0) return undefined;

  const want = TIER_RANK[tier];
  const exact = pool.filter((m) => TIER_RANK[m.tier] === want);
  const candidates = exact.length ? exact : pool;
  return candidates.slice().sort((a, b) => avgPrice(a) - avgPrice(b))[0];
}

export function costCents(m: ModelSpec, inTok: number, outTok: number): number {
  const usd = (inTok * m.inPrice + outTok * m.outPrice) / 1_000_000;
  return usd * 100;
}

const PREMIUM_MODEL = MODELS.slice().sort((a, b) => avgPrice(b) - avgPrice(a))[0];

/** What the most expensive model would have cost — the basis for the "saved" figure. */
export function premiumCostCents(inTok: number, outTok: number): number {
  return costCents(PREMIUM_MODEL, inTok, outTok);
}
