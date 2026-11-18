import React from 'react';
import { statusLabel, statusTone } from '../format.js';
import { Icon } from './icons.jsx';

export function StatusBadge({ status, tone, label, live = false }) {
  const resolvedTone = tone ?? statusTone(status);
  const className = status ? `status ${String(status).toLowerCase()}` : 'status';
  return <span className={className} data-tone={resolvedTone} role={live ? 'status' : undefined}>
    <Icon name={resolvedTone} size={12} className="status-icon" />
    {label ?? statusLabel(status)}
  </span>;
}
