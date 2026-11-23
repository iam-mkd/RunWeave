// Effects: side-effecting calls an agent routes through RunWeave with `ctx.effect`.
//
// RunWeave sends the run's effect faults in the run request (`effect_faults`); this SDK
// draws them exactly as RunWeave's Python side does, so the same seed loses the same
// responses in every language. Steps keep SHA-256 digests, never the key or the resource.
import { createHash } from 'node:crypto';
import { isObject } from './contract.js';

export const EFFECT_NAME_PATTERN = /^[a-z][a-z0-9_.-]{0,63}$/;

export interface EffectFault {
  kind: 'response_lost';
  probability: number;
  /** Only these effects; all when absent. */
  effects: string[] | null;
}

export interface EffectFaults {
  seed: number;
  faults: EffectFault[];
}

/** A committed write whose response was lost (an injected fault). Retry with the same key. */
export class EffectResponseLost extends Error {
  readonly code = 'EFFECT_RESPONSE_LOST';
  readonly effect: string;

  constructor(effect: string) {
    super(`The response of effect '${effect}' was lost after the write`);
    this.name = 'EffectResponseLost';
    this.effect = effect;
  }
}

function parseFault(value: unknown): EffectFault | undefined {
  if (!isObject(value) || value['kind'] !== 'response_lost') return undefined;
  const probability = value['probability'];
  if (typeof probability !== 'number' || !(probability >= 0 && probability <= 1)) return undefined;
  const effects = value['effects'];
  if (effects === undefined || effects === null) return { kind: 'response_lost', probability, effects: null };
  if (!Array.isArray(effects) || effects.length < 1 || effects.length > 32) return undefined;
  if (!effects.every((name) => typeof name === 'string' && EFFECT_NAME_PATTERN.test(name))) return undefined;
  return { kind: 'response_lost', probability, effects: [...effects] };
}

/** `null` when absent, `undefined` when present but invalid (the request is rejected). */
export function parseEffectFaults(value: unknown): EffectFaults | null | undefined {
  if (value === undefined || value === null) return null;
  if (!isObject(value)) return undefined;
  const seed = value['seed'];
  const faults = value['faults'];
  if (typeof seed !== 'number' || !Number.isSafeInteger(seed) || seed < 0) return undefined;
  if (!Array.isArray(faults) || faults.length < 1 || faults.length > 16) return undefined;
  const parsed = faults.map(parseFault);
  if (parsed.some((fault) => fault === undefined)) return undefined;
  return { seed, faults: parsed as EffectFault[] };
}

/** sha256("runweave-effect-faults:{seed}:{n}"), first 8 bytes as a fraction of 2**64. */
function draw(seed: number, position: number): number {
  const digest = createHash('sha256').update(`runweave-effect-faults:${seed}:${position}`).digest();
  return Number(digest.readBigUInt64BE(0)) / 2 ** 64;
}

/** Whether the n-th effect call (from 0) of the attempt loses its response. */
export function effectLost(faults: EffectFaults | null, position: number, name: string): boolean {
  if (faults === null) return false;
  const value = draw(faults.seed, position);
  let threshold = 0;
  for (const fault of faults.faults) {
    if (fault.effects !== null && !fault.effects.includes(name)) continue;
    threshold += fault.probability;
    if (value < threshold) return true;
  }
  return false;
}

function digest(kind: string, value: string): string {
  return createHash('sha256').update(`runweave-${kind}:${value}`).digest('hex').slice(0, 32);
}

/** One logical write: its idempotency key, or (without one) the effect itself. */
export function operationDigest(name: string, key: string | null | undefined): string {
  return digest('operation', key === null || key === undefined ? `${name}:once` : `${name}:key:${key}`);
}

// Python's json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False).
function canonical(value: unknown): string {
  if (value === null || value === undefined) return 'null';
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (isObject(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  return JSON.stringify(String(value));
}

export function resourceDigest(value: unknown): string {
  return digest('resource', canonical(value));
}
