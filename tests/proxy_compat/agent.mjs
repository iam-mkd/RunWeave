// Test-only HTTP agent: the official OpenAI and Anthropic Node SDKs, unmodified except for
// their base URL and key, which come from ctx.proxy (sent by RunWeave in the run request).
import Anthropic from '@anthropic-ai/sdk';
import OpenAI from 'openai';
import { defineAgent, serveAgents } from 'runweave-agent';

const inputSchema = { type: 'object', properties: { scenario: { type: 'string' } }, required: ['scenario'] };
const outputSchema = { type: 'object' };
const weather = { type: 'object', properties: { city: { type: 'string' } } };

async function sdkCalls(ctx) {
  if (!ctx.proxy) throw new Error('RunWeave sent no proxy binding');
  const openai = new OpenAI({ apiKey: ctx.proxy.token, baseURL: ctx.proxy.openaiBaseUrl, maxRetries: 0 });
  const claude = new Anthropic({ apiKey: ctx.proxy.token, baseURL: ctx.proxy.anthropicBaseUrl, maxRetries: 0 });

  const chat = await openai.chat.completions.create({ model: 'gpt-anything', messages: [{ role: 'user', content: 'hello' }] });
  let chatStream = '';
  const stream = await openai.chat.completions.create({
    model: 'gpt-anything', messages: [{ role: 'user', content: 'hello' }], stream: true, stream_options: { include_usage: true },
  });
  for await (const chunk of stream) chatStream += chunk.choices[0]?.delta?.content ?? '';

  const tools = [{ type: 'function', function: { name: 'lookup_weather', parameters: weather } }];
  const called = await openai.chat.completions.create({ model: 'm', messages: [{ role: 'user', content: 'weather in Paris?' }], tools });
  const call = called.choices[0].message.tool_calls[0];

  const response = await openai.responses.create({ model: 'm', input: 'hello' });
  let responseStream = '';
  const events = await openai.responses.create({ model: 'm', input: 'hello', stream: true });
  for await (const event of events) if (event.type === 'response.output_text.delta') responseStream += event.delta;

  const message = await claude.messages.create({ model: 'c', max_tokens: 200, messages: [{ role: 'user', content: 'hello' }] });
  const messageStream = claude.messages.stream({ model: 'c', max_tokens: 200, messages: [{ role: 'user', content: 'hello' }] });
  let claudeStream = '';
  messageStream.on('text', (text) => { claudeStream += text; });
  const final = await messageStream.finalMessage();
  const claudeTool = await claude.messages.create({
    model: 'c', max_tokens: 200, tools: [{ name: 'lookup_weather', input_schema: weather }],
    messages: [{ role: 'user', content: 'weather in Paris?' }],
  });
  const toolUse = claudeTool.content.find((block) => block.type === 'tool_use');

  return {
    chat: chat.choices[0].message.content,
    chatStream,
    tool: { name: call.function.name, arguments: JSON.parse(call.function.arguments) },
    response: response.output_text,
    responseStream,
    claude: message.content[0].text,
    claudeStream,
    claudeStreamUsage: final.usage.output_tokens,
    claudeTool: { name: toolUse.name, input: toolUse.input },
  };
}

const agent = defineAgent({
  name: 'node-sdks', version: '1.0.0', maxSteps: 5, inputSchema, outputSchema,
  async run(input, ctx) { return ctx.step('Call the SDKs', () => sdkCalls(ctx)); },
});

const server = await serveAgents([agent], { port: 0 });
console.log(`RUNWEAVE_AGENT_READY ${server.url}`);
