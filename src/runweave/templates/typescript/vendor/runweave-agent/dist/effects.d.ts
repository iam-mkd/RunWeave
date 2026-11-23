export declare const EFFECT_NAME_PATTERN: RegExp;
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
export declare class EffectResponseLost extends Error {
    readonly code = "EFFECT_RESPONSE_LOST";
    readonly effect: string;
    constructor(effect: string);
}
/** `null` when absent, `undefined` when present but invalid (the request is rejected). */
export declare function parseEffectFaults(value: unknown): EffectFaults | null | undefined;
/** Whether the n-th effect call (from 0) of the attempt loses its response. */
export declare function effectLost(faults: EffectFaults | null, position: number, name: string): boolean;
/** One logical write: its idempotency key, or (without one) the effect itself. */
export declare function operationDigest(name: string, key: string | null | undefined): string;
export declare function resourceDigest(value: unknown): string;
