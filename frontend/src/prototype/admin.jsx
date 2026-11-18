import React, { useState } from 'react';
import { DataTable, EmptyState, KeyValueList, PageHeader, Panel, StatusBadge, ThemeSwitch } from '../components/index.js';
import { approvals as initialApprovals, faultProfiles, providers } from './fixtures.js';

const approvalTone = { pending: 'warning', approved: 'good', rejected: 'critical', expired: 'neutral' };
const approvalLabel = { pending: 'Pending', approved: 'Approved', rejected: 'Rejected', expired: 'Expired' };

export function ApprovalsScreen() {
  const [items, setItems] = useState(initialApprovals);
  const [selected, setSelected] = useState(initialApprovals[0].id);
  const current = items.find((item) => item.id === selected);
  const decide = (status) => setItems((list) => list.map((item) => item.id === selected ? { ...item, status } : item));
  const pending = items.filter((item) => item.status === 'pending').length;

  return <>
    <PageHeader eyebrow="Approvals" title="Approvals" description="Agents pause here before a gated action. A decision applies only to these exact arguments, before the expiry." />
    <div className="grid two">
      <Panel title={`Inbox · ${pending} pending`} id="inbox">
        <div className="panel-body flush">
          <DataTable caption="Approval requests" captionHidden rowKey={(row) => row.id} rows={items} selectedKey={selected}
            onRowClick={(row) => setSelected(row.id)} columns={[
              { key: 'action', label: 'Action', render: (row) => <button type="button" className="text-button mono" onClick={() => setSelected(row.id)}>{row.action}</button> },
              { key: 'agent', label: 'Agent' },
              { key: 'expires', label: 'Expires' },
              { key: 'status', label: 'State', render: (row) => <StatusBadge tone={approvalTone[row.status]} label={approvalLabel[row.status]} /> },
            ]} />
        </div>
      </Panel>
      {current ? <Panel title={current.action} id="approval-detail" actions={<StatusBadge tone={approvalTone[current.status]} label={approvalLabel[current.status]} live />}>
        <div className="stack">
          <KeyValueList items={[
            { label: 'Agent', value: current.agent },
            { label: 'Run', value: <a className="mono" href={`#/runs/${current.run}`}>{current.run.slice(0, 8)}</a> },
            { label: 'Requested', value: current.requested },
            { label: 'Expires', value: current.expires },
            { label: 'Argument digest', value: <code>{current.digest}</code> },
            { label: 'Policy', value: <code>{current.policy}</code> },
          ]} />
          <div><h3 className="diff-title">Exact arguments</h3><pre>{current.arguments}</pre></div>
          {current.status === 'pending' ? <div className="row">
            <button type="button" className="primary" onClick={() => decide('approved')}>Approve</button>
            <button type="button" onClick={() => decide('rejected')}>Reject</button>
            <span className="field-hint">Approving runs the action once. Your agent must make it idempotent.</span>
          </div> : <p className="muted">No decision possible: this request is {approvalLabel[current.status].toLowerCase()}.</p>}
        </div>
      </Panel> : <EmptyState title="No approval selected" icon="approvals" />}
    </div>
  </>;
}

export function SettingsScreen({ theme, onThemeChange }) {
  const [mode, setMode] = useState('mock');
  return <>
    <PageHeader eyebrow="Settings" title="Settings" description="Models, keys, budgets and fault profiles for this workspace. Keys are stored in your operating system’s credential store and never sent to the browser." />
    <div className="grid two">
      <Panel title="Model mode" id="mode">
        <fieldset className="choice-grid single">
          <legend className="visually-hidden">Model mode</legend>
          <label className="choice"><input type="radio" name="mode" checked={mode === 'mock'} onChange={() => setMode('mock')} />
            <span className="choice-title">Mock model (default)</span><span className="choice-text">Free, deterministic, offline. Use for CI and load tests.</span></label>
          <label className="choice"><input type="radio" name="mode" checked={mode === 'live'} onChange={() => setMode('live')} />
            <span className="choice-title">Live provider</span><span className="choice-text">Real model calls with your own API key and a spend cap.</span></label>
        </fieldset>
      </Panel>
      <Panel title="Budgets" id="budgets">
        <form className="stack" onSubmit={(event) => event.preventDefault()}>
          <div className="field"><label htmlFor="cap-run">Spend cap per run (USD)</label><input id="cap-run" inputMode="decimal" defaultValue="0.05" /></div>
          <div className="field"><label htmlFor="cap-day">Spend cap per day (USD)</label><input id="cap-day" inputMode="decimal" defaultValue="5.00" /></div>
          <div className="row end"><button className="primary">Save budgets</button></div>
        </form>
      </Panel>
    </div>
    <Panel title="Providers" id="providers" className="spaced-top">
      <div className="panel-body flush">
        <DataTable caption="Model providers" captionHidden rowKey={(row) => row.id} rows={providers} columns={[
          { key: 'name', label: 'Provider' },
          { key: 'state', label: 'Key', render: (row) => <StatusBadge tone={row.tone} label={row.state} /> },
          { key: 'model', label: 'Default model', render: (row) => <span className="mono">{row.model}</span> },
          { key: 'action', label: 'Action', render: (row) => <button type="button">{row.tone === 'good' ? 'Replace key' : 'Add key'}</button> },
        ]} />
      </div>
    </Panel>
    <div className="grid two spaced-top">
      <Panel title="Fault profiles" id="faults">
        <div className="panel-body flush">
          <DataTable caption="Fault profiles" captionHidden rowKey={(row) => row.id} rows={faultProfiles} columns={[
            { key: 'name', label: 'Profile', render: (row) => <span className="mono">{row.name}</span> },
            { key: 'faults', label: 'Injected faults' },
            { key: 'seed', label: 'Seed', numeric: true },
          ]} />
        </div>
      </Panel>
      <Panel title="Appearance" id="appearance">
        <div className="stack"><p className="muted">Follow the operating system or choose a theme.</p>
          <ThemeSwitch name="theme-settings" value={theme} onChange={onThemeChange} /></div>
      </Panel>
    </div>
  </>;
}
