import React, { useState } from 'react';
import { api, approvalBody, terminal } from '../api.js';
import { ErrorState, KeyValueList, LoadingState, PageHeader, Panel, StatTile, StatusBadge } from '../components/index.js';
import { compactNumber, duration, usd } from '../format.js';
import { href } from '../router.js';
import { JsonBlock, RunOutput, RunTimeline, runModelSummary } from './parts.jsx';
import { useApproval, useLiveRun } from './useLiveRun.js';

function dateLabel(value) {
  return value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }) : '—';
}

function elapsed(snapshot) {
  if (!snapshot.started_at) return null;
  const end = snapshot.finished_at ? Date.parse(snapshot.finished_at) : Date.now();
  return Math.max(0, end - Date.parse(snapshot.started_at));
}

const liveLabels = { live: 'Live', polling: 'Refreshing every second' };

export function RunDetailScreen({ id }) {
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const view = useLiveRun(id, revision);
  const { snapshot } = view;
  const approval = useApproval(id, snapshot?.status, revision);

  async function act(suffix, data) {
    setBusy(true);
    setActionError('');
    try {
      await api(`/v1/runs/${id}/${suffix}`, { data });
    } catch (error) {
      setActionError(error.message);
    } finally {
      setRevision((value) => value + 1);
      setBusy(false);
    }
  }

  if (!id) {
    return <><PageHeader eyebrow={<a href={href({ screen: 'runs' })}>Runs</a>} title="Run not found" />
      <ErrorState>That link does not contain a valid run ID.</ErrorState></>;
  }
  const running = snapshot && !terminal.has(snapshot.status);
  const usage = snapshot?.usage;
  const models = runModelSummary(snapshot?.steps ?? []);

  return <>
    <PageHeader
      eyebrow={<><a href={href({ screen: 'runs' })}>Runs</a>{snapshot && <> / <a href={href({ screen: 'runs', params: { agent: snapshot.agent } })}>{snapshot.agent}</a></>}</>}
      title={<span className="mono title-id">{id}</span>}
      actions={snapshot && <>
        <span className="run-state"><StatusBadge status={snapshot.status} live /></span>
        {view.mode && liveLabels[view.mode] && <span className="muted live-mode">{liveLabels[view.mode]}</span>}
        {snapshot.input && <a className="button" href={href({ screen: 'new-run', params: { agent: snapshot.agent, from: id } })}>Re-run</a>}
        {running && <button type="button" disabled={busy} onClick={() => act('cancel', {})}>Cancel run</button>}
      </>} />
    {!snapshot && !view.error && <LoadingState>Loading run…</LoadingState>}
    {view.error && <ErrorState>{view.error}</ErrorState>}
    {actionError && <ErrorState>{actionError}</ErrorState>}
    {snapshot && <>
      <div className="grid four">
        <StatTile label="Duration" value={duration(elapsed(snapshot))} />
        <StatTile label="Steps" value={String(snapshot.steps.length)} />
        <StatTile label={usage?.uncertain_requests ? 'Tokens (estimate)' : 'Tokens (in / out)'}
          value={usage ? `${compactNumber(usage.input_tokens)} / ${compactNumber(usage.output_tokens)}` : '—'} />
        <StatTile label="Estimated cost" value={usage ? usd(usage.estimated_cost_usd) : '—'} />
      </div>
      <div className="grid detail-layout spaced-top">
        <Panel title="Timeline" id="timeline"><RunTimeline steps={snapshot.steps} /></Panel>
        <div className="stack">
          {snapshot.error && <ErrorState>{snapshot.error.message} <code>{snapshot.error.code}</code></ErrorState>}
          {snapshot.status === 'WAITING_APPROVAL' && approval.agent && <AgentApprovalNotice runId={id} />}
          {snapshot.status === 'WAITING_APPROVAL' && !approval.agent && <Panel title="Approval requested" id="approval">
            {approval.error && <ErrorState>{approval.error}</ErrorState>}
            {approval.approval && <div className="stack">
              <pre>{approval.approval.call.arguments}</pre>
              <p className="muted">Expires {dateLabel(approval.approval.expires_at)}</p>
              {approval.approval.status === 'pending' && <div className="row">
                <button type="button" className="primary" disabled={busy}
                  onClick={() => act('approval', approvalBody(approval.approval, 'approve'))}>Approve ticket</button>
                <button type="button" disabled={busy}
                  onClick={() => act('approval', approvalBody(approval.approval, 'reject'))}>Reject</button>
              </div>}
            </div>}
          </Panel>}
          {snapshot.result && <Panel title="Output" id="output"><RunOutput agent={snapshot.agent} result={snapshot.result} /></Panel>}
          {snapshot.input && <Panel title="Input" id="input"><JsonBlock value={snapshot.input} /></Panel>}
          <Panel title="Details" id="details">
            <KeyValueList items={[
              { label: 'Agent', value: `${snapshot.agent}${snapshot.agent_version ? ` ${snapshot.agent_version}` : ''}` },
              { label: 'Submitted', value: dateLabel(snapshot.submitted_at) },
              { label: 'Started', value: dateLabel(snapshot.started_at) },
              { label: 'Finished', value: dateLabel(snapshot.finished_at) },
              { label: 'Model requests', value: usage ? usage.requests : '—' },
              { label: 'Proxied model calls', value: String(models.calls) },
              { label: 'Injected faults', value: String(models.faults) },
              ...(snapshot.approval_wait_seconds ? [{ label: 'Approval wait', value: duration(snapshot.approval_wait_seconds * 1000) }] : []),
            ]} />
          </Panel>
        </div>
      </div>
    </>}
  </>;
}

/** A run waiting on `ctx.request_approval`: decide it in the Approvals inbox. */
export function AgentApprovalNotice({ runId }) {
  return <Panel title="Waiting for approval" id="agent-approval">
    <div className="stack">
      <p>The agent asked a person to approve an action and is waiting. The run's deadline is paused meanwhile.</p>
      <p><a href={href({ screen: 'approvals', params: { run: runId } })}>Open Approvals</a></p>
    </div>
  </Panel>;
}
