import React from 'react';
import { Sparkline } from './charts.jsx';
import { Icon } from './icons.jsx';
import { StatusBadge } from './StatusBadge.jsx';

export function StatTile({ label, value, delta, trend }) {
  return <div className="stat-tile">
    <div className="stat-label">{label}</div>
    <div className="stat-value">{value}</div>
    {delta && <div className="stat-delta" data-tone={delta.good ? 'good' : 'critical'}>
      <Icon name={delta.direction === 'down' ? 'down' : 'up'} size={12} />{delta.text}
    </div>}
    {trend && <Sparkline label={`${label} trend`} values={trend} />}
  </div>;
}

export function Timeline({ steps }) {
  return <ol className="timeline">{steps.map((step) => <li key={step.id} className="timeline-step">
    <span className="timeline-marker" aria-hidden="true" />
    <div className="timeline-body">
      <div className="timeline-head">
        <span className="timeline-title">{step.title}</span>
        <StatusBadge status={step.status} tone={step.tone} label={step.label} />
        {step.duration && <span className="timeline-duration">{step.duration}</span>}
      </div>
      {step.meta && <div className="timeline-meta">{step.meta}</div>}
      {step.detail && <pre className="timeline-detail">{step.detail}</pre>}
    </div>
  </li>)}</ol>;
}
