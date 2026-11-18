import React, { useEffect, useState } from 'react';
import { Icon } from './icons.jsx';
import { ThemeSwitch } from './layout.jsx';

function Brand({ href, className }) {
  return <a href={href} className={className}><span className="mark" aria-hidden="true">R</span>RunWeave</a>;
}

export function AppShell({ sections, active, connection, theme, onThemeChange, context, footer, brandHref = '/', children }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  return <div className="app-shell">
    <a className="skip-link" href="#main">Skip to content</a>
    <aside id="primary-navigation" className="sidebar" data-open={open ? 'true' : 'false'}>
      <Brand href={brandHref} className="brand sidebar-brand" />
      <nav aria-label="Primary">
        <ul>{sections.map((section) => <li key={section.id}>
          <a href={section.href} aria-current={section.id === active ? 'page' : undefined} onClick={() => setOpen(false)}>
            <Icon name={section.id} />{section.label}
          </a>
        </li>)}</ul>
      </nav>
      <div className="sidebar-footer">
        <ThemeSwitch value={theme} onChange={onThemeChange} />
        {footer}
      </div>
    </aside>
    {open && <div className="scrim" aria-hidden="true" onClick={() => setOpen(false)} />}
    <div className="app-main">
      <header className="topbar">
        <button type="button" className="menu-button icon-button" aria-expanded={open} aria-controls="primary-navigation" onClick={() => setOpen((value) => !value)}>
          <Icon name={open ? 'close' : 'menu'} /><span className="visually-hidden">Menu</span>
        </button>
        <Brand href={brandHref} className="brand topbar-brand" />
        <div className="topbar-context">{context}</div>
        <div className="connection" role="status">
          <span aria-hidden="true" className={`dot ${connection.state}`} />{connection.label}
        </div>
      </header>
      <main id="main" tabIndex={-1}>{children}</main>
    </div>
  </div>;
}
