import React from 'react';
import { Icon } from './icons.jsx';

export function EmptyState({ title, children, icon = 'runs', action }) {
  return <div className="empty-state">
    <div className="empty-mark"><Icon name={icon} size={20} /></div>
    <h3>{title}</h3>
    {children && <div className="empty-body">{children}</div>}
    {action && <div className="empty-action">{action}</div>}
  </div>;
}

export function ErrorState({ children, action }) {
  return <div role="alert" className="error">
    <Icon name="critical" size={14} className="error-icon" />
    <div className="error-body">{children}</div>
    {action}
  </div>;
}

export function LoadingState({ children = 'Loading…' }) {
  return <p role="status" className="loading-state">{children}</p>;
}
