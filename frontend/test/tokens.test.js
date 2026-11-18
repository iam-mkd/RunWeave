import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, test } from 'vitest';

const css = readFileSync(new URL('../src/design/tokens.css', import.meta.url), 'utf8');

function block(selector) {
  const start = css.indexOf(`${selector} {`);
  assert.notEqual(start, -1, `missing ${selector} block`);
  const body = css.slice(start + selector.length + 2, css.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]));
}

const base = block(':root');
const dark = base;
const light = { ...base, ...block(':root[data-theme="light"]') };
const systemLight = { ...base, ...block(':root:where(:not([data-theme="dark"]))') };

function luminance(hex) {
  assert.match(hex, /^#[0-9a-f]{6}$/i, `expected a 6-digit hex color, got ${hex}`);
  const [r, g, b] = [1, 3, 5].map((index) => {
    const channel = parseInt(hex.slice(index, index + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(first, second) {
  const [high, low] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (high + 0.05) / (low + 0.05);
}

function expectContrast(theme, foreground, backgrounds, minimum) {
  for (const background of backgrounds) {
    const ratio = contrast(theme[foreground], theme[background]);
    assert.ok(ratio >= minimum, `${foreground} on ${background} is ${ratio.toFixed(2)}:1, needs ${minimum}:1`);
  }
}

const surfaces = ['page', 'surface', 'surface-raised', 'surface-sunken'];
const referenceSeries = {
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#0ca30c', '#9085e9', '#e66767'],
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
};

test('the system light theme matches the explicit light theme', () => {
  assert.deepEqual(systemLight, light);
});

for (const [name, theme] of Object.entries({ dark, light })) {
  describe(`${name} theme meets WCAG 2.1 AA`, () => {
    test('text tokens reach 4.5:1 on every surface', () => {
      for (const text of ['text', 'text-secondary', 'text-muted']) expectContrast(theme, text, surfaces, 4.5);
    });

    test('accent text and text on accent controls reach 4.5:1', () => {
      expectContrast(theme, 'accent-text', ['page', 'surface', 'surface-raised'], 4.5);
      for (const background of ['accent', 'accent-hover']) expectContrast(theme, 'text-on-accent', [background], 4.5);
    });

    test('focus rings and form-control borders reach 3:1', () => {
      expectContrast(theme, 'focus', surfaces, 3);
      expectContrast(theme, 'border', ['surface', 'surface-sunken'], 3);
    });

    test('status indicators that carry meaning reach 3:1', () => {
      const required = name === 'dark'
        ? ['status-good', 'status-warning', 'status-serious', 'status-critical', 'status-info']
        // Light warning/serious are below 3:1 by design; icon + label carry the meaning.
        : ['status-good', 'status-critical', 'status-info'];
      for (const status of required) expectContrast(theme, status, ['surface'], 3);
    });

    test('chart series keep the validated categorical order', () => {
      assert.deepEqual(Array.from({ length: 8 }, (_, index) => theme[`series-${index + 1}`]), referenceSeries[name]);
    });
  });
}

test('status colors are fixed across themes', () => {
  for (const status of ['good', 'warning', 'serious', 'critical']) {
    assert.equal(light[`status-${status}`], dark[`status-${status}`]);
  }
});
