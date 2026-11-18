// Turns an agent's input JSON Schema (as Pydantic emits it) into form fields and back.
// Anything the form cannot represent becomes a JSON field rather than being guessed.

function resolve(schema, root) {
  let node = schema ?? {};
  for (let depth = 0; node.$ref && depth < 8; depth += 1) {
    const match = /^#\/\$defs\/([^/]+)$/.exec(node.$ref);
    node = match ? root.$defs?.[match[1]] ?? {} : {};
  }
  return node;
}

function unwrapOptional(schema, root) {
  const options = schema.anyOf ?? schema.oneOf;
  if (!Array.isArray(options)) return { node: resolve(schema, root), nullable: false };
  const real = options.filter((option) => option.type !== 'null');
  return real.length === 1
    ? { node: { ...resolve(real[0], root), default: schema.default, title: schema.title ?? real[0].title, description: schema.description ?? real[0].description }, nullable: true }
    : { node: schema, nullable: options.length !== real.length };
}

export function humanize(name) {
  const words = name.replace(/[_-]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// Pydantic titles default to the Title Cased field name; only a custom title says more.
function labelFor(name, title) {
  const generated = humanize(name).replace(/\b\w/g, (letter) => letter.toUpperCase());
  return title && title !== generated ? title : humanize(name);
}

function kindOf(node) {
  if (Array.isArray(node.enum) && node.enum.every((value) => typeof value === 'string')) return 'select';
  if (node.type === 'string') return (node.maxLength ?? Infinity) > 200 ? 'textarea' : 'text';
  if (node.type === 'integer' || node.type === 'number' || node.type === 'boolean') return node.type;
  if (node.type === 'array' && node.items?.type === 'string' && !node.items.enum) return 'list';
  return 'json';
}

export function formFields(schema) {
  if (!schema || schema.type !== 'object' || typeof schema.properties !== 'object') {
    return { fields: [], supported: false };
  }
  const required = new Set(schema.required ?? []);
  const fields = Object.entries(schema.properties).map(([name, property]) => {
    const { node, nullable } = unwrapOptional(property, schema);
    const kind = kindOf(node);
    const title = property.title ?? node.title;
    return {
      name,
      kind,
      label: labelFor(name, title),
      description: property.description ?? node.description ?? '',
      required: required.has(name),
      nullable,
      default: property.default ?? node.default,
      constraints: {
        minLength: node.minLength, maxLength: node.maxLength, pattern: node.pattern,
        minimum: node.minimum, maximum: node.maximum, enum: node.enum, maxItems: node.maxItems,
      },
    };
  });
  return { fields, supported: true };
}

export function initialValues(fields) {
  return Object.fromEntries(fields.map((field) => {
    const value = field.default;
    if (field.kind === 'boolean') return [field.name, value === true];
    if (value === undefined || value === null) return [field.name, ''];
    if (field.kind === 'list') return [field.name, Array.isArray(value) ? value.join('\n') : ''];
    if (field.kind === 'json') return [field.name, JSON.stringify(value, null, 2)];
    return [field.name, String(value)];
  }));
}

function convert(field, raw) {
  if (field.kind === 'boolean') return { value: raw === true };
  const text = typeof raw === 'string' ? raw : '';
  if (field.kind === 'list') {
    const items = text.split('\n').map((item) => item.trim()).filter(Boolean);
    return items.length ? { value: items } : { empty: true };
  }
  if (!text.trim()) return { empty: true };
  if (field.kind === 'integer' || field.kind === 'number') {
    const number = Number(text.trim());
    if (!Number.isFinite(number) || (field.kind === 'integer' && !Number.isInteger(number))) {
      return { error: field.kind === 'integer' ? 'Enter a whole number.' : 'Enter a number.' };
    }
    return { value: number };
  }
  if (field.kind === 'json') {
    try {
      return { value: JSON.parse(text) };
    } catch {
      return { error: 'Enter valid JSON.' };
    }
  }
  return { value: text };
}

export function buildInput(fields, values) {
  const input = {};
  const errors = {};
  for (const field of fields) {
    const result = convert(field, values[field.name]);
    if (result.error) errors[field.name] = result.error;
    else if (result.empty) {
      if (field.required) errors[field.name] = 'Required.';
    } else input[field.name] = result.value;
  }
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, input };
}
