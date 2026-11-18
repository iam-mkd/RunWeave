import React from 'react';
import { themePreferences } from '../design/theme.js';

const themeLabels = { system: 'System', dark: 'Dark', light: 'Light' };

export function PageHeader({ eyebrow, title, description, actions }) {
  return <div className="page-header">
    <div className="page-header-text">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>
      {description && <p className="page-description">{description}</p>}
    </div>
    {actions && <div className="page-actions">{actions}</div>}
  </div>;
}

export function Panel({ title, id, actions, children, className = '' }) {
  return <section className={`panel ${className}`.trim()} aria-labelledby={`${id}-title`}>
    <div className="panel-heading">
      <h2 id={`${id}-title`}>{title}</h2>
      {actions}
    </div>
    <div className="panel-body">{children}</div>
  </section>;
}

export function KeyValueList({ items, className = 'kv' }) {
  return <dl className={className}>{items.map((item) => <div key={item.label}>
    <dt>{item.label}</dt><dd>{item.value}</dd>
  </div>)}</dl>;
}

export function ThemeSwitch({ value, onChange, name = 'theme' }) {
  return <fieldset className="theme-switch">
    <legend className="visually-hidden">Theme</legend>
    {themePreferences.map((preference) => <label key={preference}>
      <input type="radio" name={name} value={preference} checked={value === preference} onChange={() => onChange(preference)} />
      <span>{themeLabels[preference]}</span>
    </label>)}
  </fieldset>;
}
