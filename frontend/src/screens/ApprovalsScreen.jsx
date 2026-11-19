import React, { useEffect, useState } from 'react';
import { decideApproval, getApproval, listApprovals } from '../api.js';
import {
  DataTable, EmptyState, ErrorState, KeyValueList, LoadingState, PageHeader, Panel, StatusBadge,
} from '../components/index.js';
import { href } from '../router.js';

const refreshMs = 3000;
const tones = { pending: 'warning', approved: 'good', rejected: 'critical', expired: 'neutral', cancelled: 'neutral' };
const labels = { pending: 'Pending', approved: 'Approved', rejected: 'Rejected', expired: 'Expired', cancelled: 'Cancelled' };
const when = (value) => (value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }) : '—');

export function expiresText(iso, now = Date.now()) {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  if (seconds <= 0) return 'Expired';
  if (seconds < 60) return `in ${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const rest = minutes % 60;
  return `in ${Math.floor(minutes / 60)} h${rest ? ` ${rest} min` : ''}`;
}

function StateBadge({ status, live }) {
  return <StatusBadge tone={tones[status] ?? 'neutral'} label={labels[status] ?? status} live={live} />;
}

export function NotDurable() {
  return <EmptyState title="Approvals need a database" icon="approvals">
    <p>Agents can wait for a person only when RunWeave stores runs in PostgreSQL. Start the workspace with <code>RUNWEAVE_DATABASE_URL</code> set (and <code>alembic upgrade head</code>), then agents' <code>ctx.request_approval</code> calls appear here.</p>
  </EmptyState>;
}

export function ApprovalInbox({ approvals, selected, onSelect, now = Date.now() }) {
  const pending = approvals.filter((item) => item.status === 'pending').length;
  return <Panel title={`Inbox · ${pending} pending`} id="inbox">
    <div className="panel-body flush">
      <DataTable caption="Approval requests" captionHidden rowKey={(row) => row.id} rows={approvals} selectedKey={selected}
        onRowClick={(row) => onSelect(row.id)} empty="No pending requests. Agents' requests appear here while they wait."
        columns={[
          { key: 'action', label: 'Action', render: (row) => <button type="button" className="text-button mono" onClick={() => onSelect(row.id)}>{row.action}</button> },
          { key: 'agent', label: 'Agent' },
          { key: 'run_id', label: 'Run', render: (row) => <span className="mono">{row.run_id.slice(0, 8)}</span> },
          { key: 'expires_at', label: 'Expires', render: (row) => (row.status === 'pending' ? expiresText(row.expires_at, now) : '—') },
          { key: 'status', label: 'State', render: (row) => <StateBadge status={row.status} /> },
        ]} />
    </div>
  </Panel>;
}

export function ApprovalDetail({ approval, now = Date.now(), confirming = null, busy = false, error = '', onDecide, onConfirm, onCancel }) {
  const pending = approval.status === 'pending' && Date.parse(approval.expires_at) > now;
  return <Panel title={approval.action} id="approval-detail" actions={<StateBadge status={approval.status} live />}>
    <div className="stack">
      <KeyValueList items={[
        { label: 'Agent', value: approval.agent },
        { label: 'Run', value: <a className="mono" href={href({ screen: 'run', id: approval.run_id })}>{approval.run_id.slice(0, 8)}</a> },
        { label: 'Requested', value: when(approval.created_at) },
        { label: 'Expires', value: `${when(approval.expires_at)}${approval.status === 'pending' ? ` (${expiresText(approval.expires_at, now)})` : ''}` },
        ...(approval.decided_by ? [{ label: 'Decided by', value: `${approval.decided_by} · ${when(approval.decided_at)}` }] : []),
        { label: 'Argument digest', value: <code className="digest">{approval.argument_digest}</code> },
        { label: 'Policy', value: <code>{approval.policy_version}</code> },
      ]} />
      <div>
        <h3 className="section-label">Exact arguments</h3>
        <pre id="approval-arguments" className="json-block">{JSON.stringify(approval.arguments, null, 2)}</pre>
      </div>
      {error && <ErrorState>{error}</ErrorState>}
      {pending && !confirming && <div className="row">
        <button type="button" className="primary" disabled={busy} onClick={() => onDecide('approve')}>Approve</button>
        <button type="button" disabled={busy} onClick={() => onDecide('reject')}>Reject</button>
        <span className="field-hint">The decision applies to these exact arguments only. Approving lets the agent act once; make the action idempotent in your agent.</span>
      </div>}
      {pending && confirming && <div className="callout" role="group" aria-label="Confirm decision">
        <span>{confirming === 'approve' ? 'Approve this action with the arguments above?' : 'Reject this action? The agent is told it was rejected.'}</span>
        <div className="row">
          <button type="button" className={confirming === 'approve' ? 'primary' : 'danger'} disabled={busy} onClick={onConfirm}>
            {confirming === 'approve' ? 'Confirm approve' : 'Confirm reject'}</button>
          <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
        </div>
      </div>}
      {!pending && <p className="muted">No decision possible: this request is {(labels[approval.status] ?? approval.status).toLowerCase()}{approval.status === 'pending' ? ' (its expiry has passed)' : ''}.</p>}
    </div>
  </Panel>;
}

function useInbox(showDecided) {
  const [state, setState] = useState({ approvals: null, durable: true, error: '' });
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function load() {
      try {
        const body = await listApprovals(showDecided ? 'all' : 'pending', { signal: controller.signal });
        if (controller.signal.aborted) return;
        const approvals = showDecided ? [...body.approvals].reverse() : body.approvals;
        setState({ approvals, durable: body.durable !== false, error: '' });
      } catch (failure) {
        if (controller.signal.aborted) return;
        setState((current) => ({ ...current, error: failure.message }));
      }
      timer = setTimeout(load, refreshMs);
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [showDecided]);
  return state;
}

export function ApprovalsScreen({ run }) {
  const [showDecided, setShowDecided] = useState(false);
  const inbox = useInbox(showDecided);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [confirming, setConfirming] = useState(null);
  const [action, setAction] = useState({ busy: false, error: '' });
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, []);

  // Pick the run's request (from a run page link), else the first one, once the inbox loads.
  useEffect(() => {
    if (selected || !inbox.approvals?.length) return;
    const match = run ? inbox.approvals.find((item) => item.run_id === run) : undefined;
    setSelected((match ?? inbox.approvals[0]).id);
  }, [inbox.approvals, run, selected]);

  // The selected request is read on its own, so it stays visible after it is decided.
  useEffect(() => {
    if (!selected) { setDetail(null); return undefined; }
    const controller = new AbortController();
    const listed = inbox.approvals?.find((item) => item.id === selected);
    if (listed) setDetail(listed);
    else getApproval(selected, { signal: controller.signal }).then(setDetail).catch(() => {});
    return () => controller.abort();
  }, [selected, inbox.approvals]);

  function select(id) {
    setSelected(id);
    setConfirming(null);
    setAction({ busy: false, error: '' });
  }

  async function confirm() {
    if (!detail || !confirming) return;
    setAction({ busy: true, error: '' });
    try {
      setDetail(await decideApproval(detail, confirming));
      setConfirming(null);
      setAction({ busy: false, error: '' });
    } catch (failure) {
      const code = failure.detail?.code;
      const why = code === 'APPROVAL_NOT_PENDING' ? 'This request is no longer pending (it expired or the run ended).'
        : code === 'APPROVAL_DECISION_CONFLICT' ? 'Someone already made the opposite decision.'
          : code === 'APPROVAL_BINDING_MISMATCH' ? 'The request changed; reload it before deciding.' : failure.message;
      setConfirming(null);
      setAction({ busy: false, error: why });
      getApproval(detail.id).then(setDetail).catch(() => {});
    }
  }

  return <>
    <PageHeader eyebrow="Approvals" title="Approvals"
      description="Agents pause here before an action that matters. A decision applies only to the exact arguments shown, before the expiry."
      actions={<label className="row"><input type="checkbox" checked={showDecided} onChange={(event) => setShowDecided(event.target.checked)} />Show decided</label>} />
    {inbox.error && <ErrorState>{inbox.error}</ErrorState>}
    {!inbox.durable ? <NotDurable /> : inbox.approvals === null ? <LoadingState>Loading approvals…</LoadingState> : <div className="grid two">
      <ApprovalInbox approvals={inbox.approvals} selected={selected} onSelect={select} now={now} />
      {detail ? <ApprovalDetail approval={detail} now={now} confirming={confirming} busy={action.busy} error={action.error}
        onDecide={setConfirming} onConfirm={confirm} onCancel={() => setConfirming(null)} />
        : <EmptyState title="Nothing to decide" icon="approvals">Select a request to see its exact arguments.</EmptyState>}
    </div>}
  </>;
}
