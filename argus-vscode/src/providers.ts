// Provider abstraction: one HTTP call per provider, normalized to {text, inputTokens, outputTokens}.
// Real network calls use Node 20's global fetch (declared below to avoid pulling DOM lib types).
declare function fetch(input: any, init?: any): Promise<any>;

export type ProviderId = 'anthropic' | 'openai' | 'gemini';

export interface ProviderMeta {
  id: ProviderId;
  label: string;
  keyHint: string;
}

export const PROVIDERS: Record<ProviderId, ProviderMeta> = {
  anthropic: { id: 'anthropic', label: 'Anthropic (Claude)', keyHint: 'sk-ant-...' },
  openai: { id: 'openai', label: 'OpenAI (GPT)', keyHint: 'sk-...' },
  gemini: { id: 'gemini', label: 'Google (Gemini)', keyHint: 'AIza...' },
};

export interface CallResult {
  text: string;
  inputTokens: number;
  outputTokens: number;
}

/** Rough heuristic: ~4 characters per token. Used as a fallback when an API omits usage. */
export function estimateTokens(text: string): number {
  return Math.max(1, Math.round(text.length / 4));
}

export async function callModel(
  provider: ProviderId,
  apiId: string,
  apiKey: string,
  prompt: string,
  maxTokens = 512,
): Promise<CallResult> {
  if (provider === 'anthropic') return callAnthropic(apiId, apiKey, prompt, maxTokens);
  if (provider === 'openai') return callOpenAI(apiId, apiKey, prompt, maxTokens);
  return callGemini(apiId, apiKey, prompt, maxTokens);
}

async function callAnthropic(apiId: string, apiKey: string, prompt: string, maxTokens: number): Promise<CallResult> {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: apiId,
      max_tokens: maxTokens,
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  if (!res.ok) throw new Error(`Anthropic ${res.status}: ${await res.text()}`);
  const data = await res.json();
  const text = (data.content || []).map((b: any) => b.text || '').join('');
  return {
    text,
    inputTokens: data.usage?.input_tokens ?? estimateTokens(prompt),
    outputTokens: data.usage?.output_tokens ?? estimateTokens(text),
  };
}

async function callOpenAI(apiId: string, apiKey: string, prompt: string, maxTokens: number): Promise<CallResult> {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: apiId,
      max_tokens: maxTokens,
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  if (!res.ok) throw new Error(`OpenAI ${res.status}: ${await res.text()}`);
  const data = await res.json();
  const text = data.choices?.[0]?.message?.content ?? '';
  return {
    text,
    inputTokens: data.usage?.prompt_tokens ?? estimateTokens(prompt),
    outputTokens: data.usage?.completion_tokens ?? estimateTokens(text),
  };
}

async function callGemini(apiId: string, apiKey: string, prompt: string, maxTokens: number): Promise<CallResult> {
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${apiId}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig: { maxOutputTokens: maxTokens },
    }),
  });
  if (!res.ok) throw new Error(`Gemini ${res.status}: ${await res.text()}`);
  const data = await res.json();
  const text = (data.candidates?.[0]?.content?.parts || []).map((p: any) => p.text || '').join('');
  return {
    text,
    inputTokens: data.usageMetadata?.promptTokenCount ?? estimateTokens(prompt),
    outputTokens: data.usageMetadata?.candidatesTokenCount ?? estimateTokens(text),
  };
}
