import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, test } from 'vitest';
import {
  AppShell, DataTable, EmptyState, ErrorState, KeyValueList, LineChart, LoadingState,
  PageHeader, Panel, Sparkline, StatTile, StatusBadge, ThemeSwitch, Timeline,
} from '../src/components/index.js';

const render = (element) => renderToStaticMarkup(element);
const hostile = '<img src=x onerror="alert(1)">';

describe('StatusBadge', () => {
  test('pairs an icon shape with a text label and a stable state hook', () => {
    const html = render(<StatusBadge status="SUCCEEDED" />);
    assert.match(html, /class="status succeeded"/);
    assert.match(html, /data-tone="good"/);
    assert.match(html, /<svg[^>]*aria-hidden="true"/);
    assert.match(html, />Succeeded</);
  });

  test('accepts an explicit tone and escapes labels', () => {
    const html = render(<StatusBadge tone="critical" label={hostile} />);
    assert.match(html, /data-tone="critical"/);
    assert.doesNotMatch(html, /<img/);
  });

  test('can announce changes as a live status', () => {
    assert.match(render(<StatusBadge status="RUNNING" live />), /role="status"/);
  });
});

describe('DataTable', () => {
  const columns = [
    { key: 'name', label: 'Agent', sortable: true },
    { key: 'ms', label: 'p95', numeric: true, sortable: true, render: (row) => `${row.ms} ms` },
  ];

  test('renders a captioned semantic table with sort state', () => {
    const html = render(<DataTable caption="Agents" columns={columns} rows={[{ id: 1, name: hostile, ms: 12 }]}
      rowKey={(row) => row.id} sort={{ key: 'ms', direction: 'descending' }} onSort={() => {}} />);
    assert.match(html, /<caption[^>]*>Agents<\/caption>/);
    assert.match(html, /<th scope="col"[^>]*aria-sort="descending"/);
    assert.match(html, /<button[^>]*>p95/);
    assert.match(html, /class="num"[^>]*>12 ms</);
    assert.doesNotMatch(html, /<img/);
  });

  test('shows an empty message spanning every column', () => {
    const html = render(<DataTable caption="Runs" columns={columns} rows={[]} rowKey={(row) => row.id} empty="No runs yet." />);
    assert.match(html, /colSpan="2"|colspan="2"/);
    assert.match(html, /No runs yet\./);
  });
});

describe('state components', () => {
  test('errors are alerts, loading is a status, empty states have a heading', () => {
    assert.match(render(<ErrorState>{hostile}</ErrorState>), /role="alert"/);
    assert.doesNotMatch(render(<ErrorState>{hostile}</ErrorState>), /<img/);
    assert.match(render(<LoadingState />), /role="status"[^>]*>.*Loading/);
    assert.match(render(<EmptyState title="Nothing here">Start a run.</EmptyState>), /<h3[^>]*>Nothing here<\/h3>/);
  });
});

describe('layout components', () => {
  const sections = [
    { id: 'runs', label: 'Runs', href: '#/runs', milestone: 19 },
    { id: 'evals', label: 'Evals', href: '#/evals', milestone: 22 },
  ];

  test('the shell has a skip link, labelled navigation, current page and one main', () => {
    const html = render(<AppShell sections={sections} active="evals" connection={{ state: 'connected', label: 'Offline demo' }}
      theme="system" onThemeChange={() => {}}><p>content</p></AppShell>);
    assert.match(html, /<a[^>]*href="#main"[^>]*>Skip to content<\/a>/);
    assert.match(html, /<nav[^>]*aria-label="Primary"/);
    assert.match(html, /<a[^>]*aria-current="page"[^>]*href="#\/evals"|<a[^>]*href="#\/evals"[^>]*aria-current="page"/);
    assert.equal(html.match(/<main/g).length, 1);
    assert.match(html, /<main[^>]*id="main"/);
    assert.match(html, /Offline demo/);
    assert.match(html, /<button[^>]*aria-expanded="false"[^>]*aria-controls="primary-navigation"/);
  });

  test('page header, panel and key-value list use headings and description lists', () => {
    assert.match(render(<PageHeader eyebrow="Runs" title="Run an investigation." description="d" />), /<h1[^>]*>Run an investigation\.<\/h1>/);
    const panel = render(<Panel title="Run details" id="detail">x</Panel>);
    assert.match(panel, /<section[^>]*aria-labelledby="detail-title"/);
    assert.match(panel, /<h2 id="detail-title"[^>]*>Run details<\/h2>/);
    assert.match(render(<KeyValueList items={[{ label: 'Requests', value: 3 }]} />), /<dl[^>]*>.*<dt>Requests<\/dt><dd>3<\/dd>/);
  });

  test('theme switch is a labelled radio group with the current choice checked', () => {
    const html = render(<ThemeSwitch value="dark" onChange={() => {}} />);
    assert.match(html, /<fieldset/);
    assert.match(html, /<legend[^>]*>Theme<\/legend>/);
    assert.equal(html.match(/type="radio"/g).length, 3);
    assert.match(html, /value="dark"[^>]*checked=""|checked=""[^>]*value="dark"/);
  });
});

describe('data components', () => {
  test('stat tiles show label and value; timeline is an ordered list of states', () => {
    assert.match(render(<StatTile label="Throughput" value="90.0 runs/s" />), /Throughput.*90\.0 runs\/s/);
    const timeline = render(<Timeline steps={[
      { id: 'a', title: 'Model decision', status: 'SUCCEEDED', duration: '12 ms' },
      { id: 'b', title: hostile, status: 'FAILED', detail: hostile },
    ]} />);
    assert.match(timeline, /<ol/);
    assert.equal(timeline.match(/<li/g).length, 2);
    assert.match(timeline, />Failed</);
    assert.doesNotMatch(timeline, /<img/);
  });

  test('line charts have an accessible name, legend for multiple series and a table view', () => {
    const series = [
      { name: 'p50', points: [{ x: 1, y: 10 }, { x: 2, y: 12 }] },
      { name: 'p95', points: [{ x: 1, y: 30 }, { x: 2, y: 45 }] },
    ];
    const html = render(<LineChart title="Latency" series={series} formatY={(value) => `${value} ms`} />);
    assert.match(html, /<svg[^>]*role="img"[^>]*aria-label="Latency/);
    assert.equal(html.match(/<path[^>]*class="series-line/g).length, 2);
    assert.match(html, /class="legend"/);
    assert.match(html, /<table/);
    assert.match(html, /<th scope="col"[^>]*>p95<\/th>/);
    assert.match(html, /45 ms/);
  });

  test('a single-series chart needs no legend; sparklines are named images', () => {
    const html = render(<LineChart title="Queue depth" series={[{ name: 'depth', points: [{ x: 1, y: 2 }, { x: 2, y: 3 }] }]} />);
    assert.doesNotMatch(html, /class="legend"/);
    assert.match(render(<Sparkline label="Errors per minute" values={[1, 3, 2]} />), /role="img"[^>]*aria-label="Errors per minute"/);
  });
});

describe('scroll regions', () => {
  test('a table that fits adds no tab stop or region role', () => {
    const html = renderToStaticMarkup(<DataTable caption="Fits" columns={[{ key: 'name', label: 'Name' }]}
      rows={[{ id: 1, name: 'a' }]} rowKey={(row) => row.id} />);
    assert.match(html, /<div class="table-wrap">/);
    assert.doesNotMatch(html, /tabindex|role="region"/i);
  });
});
