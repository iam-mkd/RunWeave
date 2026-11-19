// The RunWeave HTTP agent contract, runweave.agent/v1 (contracts/agent-http-v1.openapi.json).
export const CONTRACT_VERSION = 'runweave.agent/v1';
export const MANIFEST_PATH = '/runweave/v1/agents';
export const NAME_PATTERN = /^[a-z][a-z0-9-]{0,62}$/;
export const VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}$/;
export const MAX_DEADLINE_MS = 86_400_000;
export const TITLE_LIMIT = 120;
export const DETAIL_LIMIT = 1000;
export const STEP_EVENT_CAP = 200;
export const ACTION_PATTERN = /^[a-z][a-z0-9_.:-]{0,99}$/;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TOKEN_PATTERN = /^rwp_[A-Za-z0-9_-]{20,200}$/;
function parseProxy(value) {
    if (value === undefined || value === null)
        return null;
    if (!isObject(value))
        return undefined;
    const { token, openai_base_url: openai, anthropic_base_url: anthropic } = value;
    if (typeof token !== 'string' || !TOKEN_PATTERN.test(token))
        return undefined;
    if (typeof openai !== 'string' || typeof anthropic !== 'string' || openai.length > 2048 || anthropic.length > 2048)
        return undefined;
    return { token, openai_base_url: openai, anthropic_base_url: anthropic };
}
export function isObject(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function isInteger(value, min, max) {
    return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max;
}
/** Validates the request envelope. Unknown fields are ignored for forward compatibility. */
export function parseRunRequest(value) {
    if (!isObject(value))
        return null;
    const { run_id: runId, attempt, input, deadline_ms: deadlineMs } = value;
    if (typeof runId !== 'string' || !UUID_PATTERN.test(runId))
        return null;
    if (!isInteger(attempt, 1, 1000) || !isInteger(deadlineMs, 1, MAX_DEADLINE_MS) || !isObject(input))
        return null;
    const proxy = parseProxy(value['proxy']);
    if (proxy === undefined)
        return null;
    const approvals = value['approvals_url'];
    if (approvals !== undefined && approvals !== null && (typeof approvals !== 'string' || approvals.length > 2048))
        return null;
    return {
        run_id: runId.toLowerCase(), attempt, input, deadline_ms: deadlineMs, proxy,
        approvals_url: typeof approvals === 'string' ? approvals : null,
    };
}
export function isUuid(value) {
    return UUID_PATTERN.test(value);
}
export function clip(text, limit) {
    const trimmed = text.trim();
    return trimmed.length > limit ? `${trimmed.slice(0, limit - 1)}…` : trimmed;
}
