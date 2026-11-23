import { ACTION_PATTERN, DETAIL_LIMIT, NAME_PATTERN, STEP_EVENT_CAP, TITLE_LIMIT, VERSION_PATTERN, clip, isObject, } from './contract.js';
import { EFFECT_NAME_PATTERN, EffectResponseLost, effectLost, operationDigest, resourceDigest, } from './effects.js';
/** A classified failure whose code RunWeave may show in run snapshots. */
export class AgentError extends Error {
    code;
    constructor(code = 'RUN_FAILED') {
        super(code);
        this.name = 'AgentError';
        this.code = code;
    }
}
export class StepLimitError extends AgentError {
    constructor() { super('AGENT_STEP_LIMIT'); }
}
/** No decision before the approval expired. Uncaught, the run ends TIMED_OUT. */
export class ApprovalExpired extends AgentError {
    constructor() { super('APPROVAL_EXPIRED'); }
}
/** Approvals need a durable (PostgreSQL) RunWeave; this one sent no approvals URL. */
export class ApprovalUnavailable extends AgentError {
    constructor() { super('DURABLE_APPROVAL_REQUIRED'); }
}
/** Throw from `parseInput` to reject input with 422. Never include submitted values. */
export class InputError extends Error {
    errors;
    constructor(errors) {
        super('Invalid input');
        this.name = 'InputError';
        this.errors = errors.slice(0, 20);
    }
}
/** The reason on `ctx.signal` when RunWeave cancels the run or its deadline passes. */
export class RunAborted extends Error {
    reason;
    constructor(reason) {
        super(reason === 'deadline' ? 'The run deadline passed' : 'The run was cancelled');
        this.name = 'RunAborted';
        this.reason = reason;
    }
}
export function defineAgent(options) {
    const { name, version, description = '', maxSteps = 50 } = options;
    if (!NAME_PATTERN.test(name))
        throw new TypeError('Agent name: lowercase letters, digits and hyphens (max 63)');
    if (!VERSION_PATTERN.test(version))
        throw new TypeError('Agent version: 1-40 characters of letters, digits, . + _ -');
    if (description.length > 500)
        throw new TypeError('Agent description: at most 500 characters');
    if (!Number.isInteger(maxSteps) || maxSteps < 1 || maxSteps > 200)
        throw new TypeError('maxSteps must be between 1 and 200');
    if (!isObject(options.inputSchema) || options.inputSchema['type'] !== 'object') {
        throw new TypeError('inputSchema must be a JSON Schema object with "type": "object"');
    }
    if (!isObject(options.outputSchema))
        throw new TypeError('outputSchema must be a JSON Schema object');
    if (typeof options.run !== 'function')
        throw new TypeError('Provide an async run(input, ctx) function');
    return Object.freeze({
        name, version, description, maxSteps,
        inputSchema: options.inputSchema, outputSchema: options.outputSchema,
        parseInput: options.parseInput, parseOutput: options.parseOutput, run: options.run,
    });
}
export class Redactor {
    #secrets;
    constructor(values) {
        this.#secrets = [...new Set(values.filter(Boolean).flatMap((value) => [
                value, JSON.stringify(value).slice(1, -1),
            ]))].sort((a, b) => b.length - a.length);
    }
    redact(text) {
        let result = text;
        for (const secret of this.#secrets)
            result = result.split(secret).join('[REDACTED]');
        return result;
    }
}
const APPROVAL_WAIT_SECONDS = 20;
function pause(ms, signal) {
    return new Promise((resolve, reject) => {
        if (signal.aborted)
            return reject(signal.reason);
        const timer = setTimeout(resolve, ms);
        signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
    });
}
// While RunWeave is unreachable (restarting, say) keep trying: the run cannot progress
// without it, and RunWeave ends the run's request (aborting `signal`) if it gives up.
async function approvalCall(url, token, body, signal) {
    for (let failures = 0;; failures += 1) {
        try {
            const response = await fetch(url, {
                method: 'POST', body, signal,
                headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
            });
            const answer = await response.json().catch(() => ({}));
            return { ...answer, status: response.status };
        }
        catch {
            if (signal.aborted)
                throw signal.reason;
            await pause(Math.min(2000, 200 * 2 ** Math.min(failures, 4)), signal);
        }
    }
}
async function requestApproval(parts, action, args, expiresIn) {
    if (typeof action !== 'string' || !ACTION_PATTERN.test(action)) {
        throw new TypeError('Action must match ^[a-z][a-z0-9_.:-]{0,99}$, for example issue_refund');
    }
    if (expiresIn !== undefined && !(typeof expiresIn === 'number' && expiresIn > 0)) {
        throw new TypeError('expiresIn must be a positive number of seconds');
    }
    const body = JSON.stringify({ action, arguments: args ?? null, expires_in: expiresIn ?? null, wait_seconds: APPROVAL_WAIT_SECONDS });
    if (body.length > 70_000)
        throw new TypeError('Approval arguments are limited to 64 KiB');
    if (!parts.approvalsUrl || !parts.proxy)
        throw new ApprovalUnavailable();
    parts.clock.pause();
    try {
        for (;;) {
            const answer = await approvalCall(parts.approvalsUrl, parts.proxy.token, body, parts.signal);
            if (answer.status !== 200 || !answer.approval) {
                const code = answer.error?.code;
                throw code === 'DURABLE_APPROVAL_REQUIRED' ? new ApprovalUnavailable()
                    : new AgentError(typeof code === 'string' ? code : 'APPROVAL_CALLBACK_FAILED');
            }
            const { approval } = answer;
            if (approval.status === 'pending')
                continue;
            if (approval.status === 'approved' || approval.status === 'rejected') {
                return Object.freeze({
                    id: approval.id, status: approval.status, approved: approval.status === 'approved',
                    decidedBy: approval.decided_by ?? null, decidedAt: approval.decided_at ?? null,
                });
            }
            throw approval.status === 'expired' ? new ApprovalExpired() : new AgentError('APPROVAL_CANCELLED');
        }
    }
    finally {
        parts.clock.resume();
    }
}
function modelProxy(binding) {
    if (!binding)
        return null;
    const proxy = {
        token: binding.token,
        openaiBaseUrl: binding.openai_base_url,
        anthropicBaseUrl: binding.anthropic_base_url,
        env: () => ({
            OPENAI_BASE_URL: binding.openai_base_url, OPENAI_API_KEY: binding.token,
            ANTHROPIC_BASE_URL: binding.anthropic_base_url, ANTHROPIC_API_KEY: binding.token,
        }),
    };
    Object.defineProperty(proxy, 'env', { enumerable: false });
    return Object.freeze(proxy);
}
export function createContext(parts) {
    const steps = [];
    let started = 0;
    let effects = 0;
    const remainingMs = () => parts.clock.remainingMs();
    const context = {
        runId: parts.runId,
        agent: parts.agent,
        attempt: parts.attempt,
        signal: parts.signal,
        proxy: modelProxy(parts.proxy),
        remainingMs,
        remainingSeconds: () => remainingMs() / 1000,
        async step(title, fn, options = {}) {
            if (started >= parts.maxSteps)
                throw new StepLimitError();
            started += 1;
            const record = {
                title: clip(parts.redactor.redact(String(title)), TITLE_LIMIT) || 'Step',
                detail: options.detail === undefined ? null : clip(parts.redactor.redact(String(options.detail)), DETAIL_LIMIT) || null,
                status: 'failed',
                duration_ms: 0,
            };
            if (steps.length < STEP_EVENT_CAP)
                steps.push(record);
            const begin = performance.now();
            try {
                const result = await fn();
                record.status = 'succeeded';
                return result;
            }
            finally {
                record.duration_ms = Math.max(0, performance.now() - begin);
            }
        },
        log(message) {
            parts.logger(JSON.stringify({
                level: 'info', logger: 'runweave.agent', run_id: parts.runId, agent: parts.agent,
                message: parts.redactor.redact(String(message)).slice(0, 1000),
            }));
        },
        usage: () => null,
        requestApproval: (action, args, options = {}) => requestApproval(parts, action, args, options.expiresIn),
        async effect(name, fn, options = {}) {
            if (typeof name !== 'string' || !EFFECT_NAME_PATTERN.test(name)) {
                throw new TypeError(`Effect name ${JSON.stringify(name)} must be 1-64 lowercase letters, digits, '_', '.' or '-'`);
            }
            if (started >= parts.maxSteps)
                throw new StepLimitError();
            started += 1;
            const position = effects;
            effects += 1;
            const record = {
                title: name, detail: null, status: 'failed', duration_ms: 0, kind: 'effect', effect: name,
                operation: operationDigest(name, options.key ?? null), resource: null, fault: null,
            };
            if (steps.length < STEP_EVENT_CAP)
                steps.push(record);
            const begin = performance.now();
            try {
                let result;
                try {
                    result = await fn();
                }
                catch (error) {
                    record.detail = `call failed: ${error instanceof Error ? error.name : 'Error'}`;
                    throw error;
                }
                record.resource = resourceDigest(options.resource ? options.resource(result) : result);
                if (effectLost(parts.effectFaults ?? null, position, name)) {
                    record.detail = 'write committed · response lost';
                    record.fault = 'response_lost';
                    throw new EffectResponseLost(name);
                }
                record.detail = 'write committed';
                record.status = 'succeeded';
                return result;
            }
            finally {
                record.duration_ms = Math.max(0, performance.now() - begin);
            }
        },
    };
    return { context, steps };
}
