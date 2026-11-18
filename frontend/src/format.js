const grouped = new Intl.NumberFormat('en-US');
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

export function compactNumber(value) {
  return Math.abs(value) < 10000 ? grouped.format(value) : compact.format(value);
}

export function duration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined || Number.isNaN(milliseconds)) return '—';
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60000) return `${Number((milliseconds / 1000).toFixed(2))} s`;
  const minutes = Math.floor(milliseconds / 60000);
  return `${minutes}m ${Math.round((milliseconds % 60000) / 1000)}s`;
}

export function usd(value) {
  const amount = Number(value);
  return `$${amount.toFixed(amount >= 1 ? 2 : 4)}`;
}

export function percent(ratio) {
  return `${(ratio * 100).toFixed(1)}%`;
}

const labels = {
  WAITING_APPROVAL: 'Waiting for approval',
};

const tones = {
  SUCCEEDED: 'good',
  RUNNING: 'info',
  WAITING_APPROVAL: 'warning',
  TIMED_OUT: 'serious',
  FAILED: 'critical',
};

export function statusLabel(status) {
  if (labels[status]) return labels[status];
  const words = String(status).toLowerCase().replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function statusTone(status) {
  return tones[status] ?? 'neutral';
}

export function sortRows(rows, key, direction) {
  if (!key) return rows;
  const sign = direction === 'descending' ? -1 : 1;
  return [...rows].sort((first, second) => {
    const [a, b] = [first[key], second[key]];
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * sign;
    return String(a).localeCompare(String(b)) * sign;
  });
}
