// A TypeScript agent on runweave-agent with the `openai` client, Chat Completions and a
// small tool loop. The client talks to RunWeave's LLM proxy (ctx.proxy); in mock mode the
// proxy answers from fixtures.json, so the agent runs offline with no API key.
import { AgentError, defineAgent, serveAgents } from 'runweave-agent';
import OpenAI from 'openai';

const FORECASTS = { paris: 'sunny, 21 °C', london: 'light rain, 14 °C', tokyo: 'cloudy, 18 °C' };
const MAX_TURNS = 4;
const TOOLS = [
  {
    type: 'function',
    function: {
      name: 'get_weather',
      description: 'Current weather for a city',
      parameters: {
        type: 'object',
        properties: { city: { type: 'string' } },
        required: ['city'],
        additionalProperties: false,
      },
    },
  },
];

// Model output is untrusted: only declared tools run, with validated arguments.
function callTool(name, rawArguments) {
  let args;
  try {
    args = JSON.parse(rawArguments);
  } catch {
    return 'error: arguments are not valid JSON';
  }
  if (name !== 'get_weather' || typeof args !== 'object' || args === null) {
    return `error: unknown tool ${String(name).slice(0, 64)}`;
  }
  const city = args.city;
  if (typeof city !== 'string' || city.length === 0 || city.length > 100) {
    return 'error: city must be a non-empty string';
  }
  return `${city}: ${FORECASTS[city.toLowerCase()] ?? 'unknown'}`;
}

const weather = defineAgent({
  name: 'typescript-weather',
  version: '0.1.0',
  description: 'The openai npm client and a Chat Completions tool loop, through the proxy.',
  maxSteps: 10,
  inputSchema: {
    type: 'object',
    title: 'Question',
    properties: { question: { type: 'string', title: 'Question', minLength: 1, maxLength: 2000 } },
    required: ['question'],
    additionalProperties: false,
  },
  outputSchema: {
    type: 'object',
    properties: { answer: { type: 'string' }, turns: { type: 'integer' } },
    required: ['answer', 'turns'],
  },
  async run(input, ctx) {
    if (!ctx.proxy) throw new AgentError('AGENT_UNAVAILABLE');
    const client = new OpenAI({ apiKey: ctx.proxy.token, baseURL: ctx.proxy.openaiBaseUrl, maxRetries: 2 });
    const messages = [
      { role: 'system', content: 'Answer weather questions with the get_weather tool.' },
      { role: 'user', content: input.question },
    ];
    for (let turn = 1; turn <= MAX_TURNS; turn += 1) {
      const completion = await client.chat.completions.create(
        { model: 'gpt-4.1-mini', messages, tools: TOOLS, max_tokens: 512 },
        { signal: ctx.signal },
      );
      const message = completion.choices[0].message;
      const calls = (message.tool_calls ?? []).filter((call) => call.type === 'function');
      if (calls.length === 0) return { answer: message.content ?? '', turns: turn };
      messages.push({ role: 'assistant', content: message.content, tool_calls: calls });
      for (const call of calls) {
        const { name, arguments: raw } = call.function;
        const result = await ctx.step(String(name).slice(0, 64), () => callTool(name, raw), { detail: raw });
        messages.push({ role: 'tool', tool_call_id: call.id, content: result });
      }
    }
    throw new AgentError('AGENT_TOO_MANY_TURNS');
  },
});

// Port 0 picks a free port; runweave eval reads the address from this line.
const server = await serveAgents([weather], { port: Number(process.env.PORT ?? 0) });
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close().then(() => process.exit(0)));
