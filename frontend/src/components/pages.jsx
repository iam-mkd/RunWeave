import React, { useEffect, useState } from 'react';

/** Which slice of `total` rows page `page` shows (clamped to pages that exist). */
export function pageWindow(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(0, Math.trunc(page) || 0), pages - 1);
  const start = current * size;
  return { page: current, pages, start, end: Math.min(total, start + size), total };
}

/** Pages over `rows`; returns to the first page whenever `resetKey` changes. */
export function usePages(rows, { size = 100, resetKey } = {}) {
  const [page, setPage] = useState(0);
  useEffect(() => { setPage(0); }, [resetKey]);
  const window = pageWindow(rows.length, page, size);
  return { window, visible: rows.slice(window.start, window.end), setPage };
}

const grouped = new Intl.NumberFormat('en-US');

/** Previous/Next with the rows shown, announced politely. Renders nothing for one page. */
export function Pager({ label, window, onPage }) {
  if (window.pages <= 1) return null;
  return <nav className="pager" aria-label={`${label} pages`}>
    <button type="button" disabled={window.page === 0} onClick={() => onPage(window.page - 1)}>Previous</button>
    <p className="pager-status" aria-live="polite">
      Rows {grouped.format(window.start + 1)}–{grouped.format(window.end)} of {grouped.format(window.total)}
    </p>
    <button type="button" disabled={window.page >= window.pages - 1} onClick={() => onPage(window.page + 1)}>Next</button>
  </nav>;
}

/**
 * Bounds long chart series to about `max` points: each bucket keeps its lowest and highest
 * point (by the first series), so spikes and dips stay visible; the first and last points
 * are kept, and every series uses the same x positions.
 */
export function downsample(series, max = 400) {
  const reference = series[0]?.points ?? [];
  if (reference.length <= max) return series;
  const buckets = Math.max(1, Math.floor((max - 2) / 2));
  const span = (reference.length - 2) / buckets;
  const keep = [0];
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const from = 1 + Math.floor(bucket * span);
    const to = Math.min(reference.length - 1, 1 + Math.floor((bucket + 1) * span));
    let low = from;
    let high = from;
    for (let index = from; index < to; index += 1) {
      if (reference[index].y < reference[low].y) low = index;
      if (reference[index].y > reference[high].y) high = index;
    }
    keep.push(...(low < high ? [low, high] : low === high ? [low] : [high, low]));
  }
  keep.push(reference.length - 1);
  const indices = [...new Set(keep)].sort((a, b) => a - b);
  return series.map((line) => ({ ...line, points: indices.map((index) => line.points[index]).filter(Boolean) }));
}

