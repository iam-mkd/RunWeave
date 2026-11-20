import React, { useEffect, useRef, useState } from 'react';
import { cancelJob, getJob, getProject, getReport, listJobs, listReports, startJob } from '../api.js';
import {
  DataTable, EmptyState, ErrorState, KeyValueList, LineChart, LoadingState, PageHeader, Pager, Panel, StatTile, StatusBadge,
  usePages,
} from '../components/index.js';
import { compactNumber, duration, percent, usd } from '../format.js';
import { href } from '../router.js';
import { CopyBlock } from './parts.jsx';

const pollMs = 1000;
const maxEvents = 2000;
const seconds = (value) => `${Number(value).toFixed(value < 10 ? 1 : 0)}s`;
const ms = (value) => (value === null || value === undefined ? '—' : `${Number(value).toFixed(1)} ms`);
const when = (value) => (value ? new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');
const profileLabel = (profile) => (profile === null || profile === undefined ? 'Server settings' : profile || 'No faults');

// Pure helpers (tested)

export function liveSeries(events) {
  const ticks = events.filter((event) => event.event === 'tick');
  return {
    throughput: ticks.map((tick) => ({ x: tick.elapsed_ms / 1000, y: tick.throughput_rps ?? 0 })),
    p95: ticks.map((tick) => ({ x: tick.elapsed_ms / 1000, y: tick.p95_latency_ms ?? 0 })),
  };
}

export function parseLevels(text) {
  const parts = String(text).split(/[\s,]+/).filter(Boolean);
  if (!parts.length || parts.some((part) => !/^\d{1,3}$/.test(part))) return null;
  const levels = [...new Set(parts.map(Number))].sort((a, b) => a - b);
  if (levels.length > 12 || levels.some((level) => level < 1 || level > 256)) return null;
  return levels;
}

export function levelsText(levels) {
  return (levels ?? []).join(' ');
}

// Data hooks

function useProject() {
  const [state, setState] = useState({ project: null, missing: false, error: '' });
  useEffect(() => {
    const controller = new AbortController();
    getProject({ signal: controller.signal })
      .then((project) => setState({ project, missing: false, error: '' }))
      .catch((error) => {
        if (controller.signal.aborted) return;
        setState({ project: null, missing: error.detail?.code === 'NO_PROJECT', error: error.message });
      });
    return () => controller.abort();
  }, []);
  return state;
}

function useReport(id) {
  const [state, setState] = useState({ report: null, error: '' });
  useEffect(() => {
    if (!id) { setState({ report: null, error: '' }); return undefined; }
    const controller = new AbortController();
    setState({ report: null, error: '' });
    getReport(id, { signal: controller.signal })
      .then((report) => setState({ report, error: '' }))
      .catch((error) => { if (!controller.signal.aborted) setState({ report: null, error: error.message }); });
    return () => controller.abort();
  }, [id]);
  return state;
}

function useReports(kind, refresh) {
  const [reports, setReports] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    listReports(kind, { signal: controller.signal })
      .then((body) => setReports(body.reports))
      .catch(() => { if (!controller.signal.aborted) setReports([]); });
    return () => controller.abort();
  }, [kind, refresh]);
  return reports;
}

// Follows the kind's running job (resuming one started earlier or from another tab) by
// polling its events once a second; the job itself runs in a separate process.
function useJob(kind, onReport) {
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState('');
  const cursor = useRef(0);
  const reported = useRef(onReport);
  reported.current = onReport;

  useEffect(() => {
    const controller = new AbortController();
    listJobs({ signal: controller.signal }).then(({ jobs }) => {
      const live = jobs.find((item) => item.kind === kind && item.status === 'RUNNING');
      if (live) { cursor.current = 0; setEvents([]); setJob(live); }
    }).catch(() => {});
    return () => controller.abort();
  }, [kind]);

  const running = job?.status === 'RUNNING';
  useEffect(() => {
    if (!job || !running) return undefined;
    const controller = new AbortController();
    let timer;
    async function poll() {
      try {
        const body = await getJob(job.id, cursor.current, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (body.events.length) {
          cursor.current = body.events.at(-1).seq;
          setEvents((current) => [...current, ...body.events].slice(-maxEvents));
        }
        setJob(body.job);
        setError('');
        if (body.job.status !== 'RUNNING') {
          if (body.job.report_id) reported.current(body.job.report_id);
          return;
        }
      } catch (failure) {
        if (controller.signal.aborted) return;
        setError(failure.message);
      }
      timer = setTimeout(poll, pollMs);
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job?.id, running]);

  async function start(request) {
    setError('');
    try {
      const created = await startJob(request);
      cursor.current = 0;
      setEvents([]);
      setJob(created);
    } catch (failure) {
      setError(failure.message);
    }
  }

  async function cancel() {
    if (!job) return;
    try { setJob(await cancelJob(job.id)); } catch (failure) { setError(failure.message); }
  }

  return { job, events, error, running, start, cancel };
}

// Shared pieces

export function NoProject() {
  return <EmptyState title="No project in this workspace" icon="agents">
    <p>Load tests, evals and shadow comparisons run against the agent in a project's <code>runweave.toml</code>. Create one and start the workspace from its folder:</p>
    <CopyBlock id="no-project-commands" text={'runweave init my-agent\ncd my-agent\nrunweave serve'} />
  </EmptyState>;
}

function Verdict({ passed, good = 'Gates passed', bad = 'Gates failed' }) {
  if (passed === null || passed === undefined) return <StatusBadge tone="neutral" label="No verdict" />;
  return <StatusBadge tone={passed ? 'good' : 'critical'} label={passed ? good : bad} />;
}

function headlineText(report) {
  const h = report.headline ?? {};
  if (report.kind === 'eval') return `${h.passed} / ${h.total} cases · p95 ${ms(h.p95_latency_ms)}`;
  if (report.kind === 'bench') return `${h.levels} level(s) · up to ${h.throughput_rps} runs/s · p95 ${ms(h.p95_latency_ms)} · ${compactNumber(h.failures ?? 0)} failed`;
  return `${h.pairs} pairs · ${h.regressions} regressed`;
}

export function ReportList({ screen, reports }) {
  if (reports === null) return <LoadingState>Loading saved reports…</LoadingState>;
  return <DataTable caption="Saved reports" captionHidden rowKey={(row) => row.id} rows={reports}
    empty="No saved reports yet. Start one above, or run the command in a terminal." columns={[
      { key: 'id', label: 'Report', render: (row) => <a className="mono" href={href({ screen, params: { report: row.id } })}>{row.id}</a> },
      { key: 'created_at', label: 'When', render: (row) => when(row.created_at) },
      { key: 'agent', label: 'Agent', render: (row) => `${row.agent?.name ?? '—'} ${row.agent?.version ?? ''}` },
      { key: 'dataset', label: 'Dataset' },
      { key: 'headline', label: 'Result', render: (row) => headlineText(row) },
      { key: 'passed', label: 'Verdict', render: (row) => <Verdict passed={row.passed}
        good={row.kind === 'shadow' ? 'Eligible' : 'Passed'} bad={row.kind === 'shadow' ? 'Not eligible' : 'Failed'} /> },
    ]} />;
}

function Gates({ gates }) {
  if (gates.passed) return <p className="row"><Verdict passed /></p>;
  return <ErrorState><strong>Gates failed.</strong><ul className="bare">{gates.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></ErrorState>;
}

function JobStatus({ job, error, onCancel, children }) {
  if (!job && !error) return null;
  const tone = { RUNNING: 'info', COMPLETED: job?.passed === false ? 'serious' : 'good', FAILED: 'critical', CANCELLED: 'neutral' }[job?.status];
  const label = job?.status === 'COMPLETED' ? (job.passed === false ? 'Completed · gates failed' : 'Completed') : job?.status;
  return <div className="stack">
    {job && <div className="row spaced">
      <div className="row"><StatusBadge tone={tone} label={label?.charAt(0) + label?.slice(1).toLowerCase()} live={job.status === 'RUNNING'} />
        {children}</div>
      {job.status === 'RUNNING' && <button type="button" onClick={onCancel}>Cancel</button>}
    </div>}
    {job?.status === 'FAILED' && job.error && <ErrorState>{job.error}</ErrorState>}
    {error && <ErrorState>{error}</ErrorState>}
  </div>;
}

function ProjectSummary({ project }) {
  return <p className="muted">Project <span className="mono">{project.name}</span> · agent <span className="mono">{project.agent.name}</span> ({project.agent.kind}).
    The same command runs from a terminal; reports are saved to the project's report folder.</p>;
}

function DatasetSelect({ id, project, value, onChange, compact = false }) {
  return <div className={compact ? undefined : 'field'}><label htmlFor={id} className={compact ? 'visually-hidden' : undefined}>Dataset</label>
    <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
      {project.datasets.map((dataset) => <option key={dataset.name} value={dataset.name}>
        {dataset.name}{dataset.cases === null ? ' (invalid)' : ` · ${dataset.cases} cases`}</option>)}
    </select></div>;
}

// Load tests

export function BenchResults({ report }) {
  const env = report.environment;
  const failures = report.cases.flatMap((row) => Object.entries(row.failures).map(([category, count]) => ({
    id: `${row.fault_profile}-${row.concurrency}-${category}`, category, count, profile: row.fault_profile, concurrency: row.concurrency,
  })));
  const soak = report.soak;
  const failurePages = usePages(failures, { resetKey: report.id });
  return <div className="stack">
    <Gates gates={report.gates} />
    <p className="muted">{report.agent.name} {report.agent.version} on {report.dataset.name} · {env.mode}
      {env.logical_cpus ? ` · ${env.logical_cpus} logical CPUs · ${compactNumber(env.memory_mib)} MiB · Python ${env.python} · ${env.platform}` : ''}</p>
    <Panel title="Results by level" id="bench-levels">
      <div className="panel-body flush">
        <DataTable caption="Results by fault profile and concurrency" captionHidden rowKey={(row) => `${row.fault_profile}-${row.concurrency}`} rows={report.cases} columns={[
          { key: 'fault_profile', label: 'Faults', render: (row) => profileLabel(row.fault_profile) },
          { key: 'concurrency', label: 'Concurrency', numeric: true },
          { key: 'runs', label: 'Runs', numeric: true },
          { key: 'throughput_rps', label: 'Runs/s', numeric: true },
          { key: 'latency', label: 'p50 / p95 / p99', numeric: true, render: (row) => `${row.latency_ms.p50 ?? '—'} / ${row.latency_ms.p95 ?? '—'} / ${row.latency_ms.p99 ?? '—'} ms` },
          { key: 'queue', label: 'Queue p95', numeric: true, render: (row) => ms(row.queue_wait_ms.p95) },
          { key: 'status', label: 'Status API p95', numeric: true, render: (row) => ms(row.status_api_ms.p95) },
          { key: 'lag', label: 'Loop lag p99', numeric: true, render: (row) => ms(row.loop_lag_p99_bucket_ms) },
          { key: 'memory', label: 'Peak RSS', numeric: true, render: (row) => (row.memory ? `${row.memory.rss_peak_mib} MiB` : 'n/a') },
          { key: 'failed', label: 'Failed', numeric: true, render: (row) => compactNumber(Object.values(row.failures).reduce((a, b) => a + b, 0)) },
          { key: 'tokens', label: 'Tokens', numeric: true, render: (row) => compactNumber(row.tokens) },
        ]} />
      </div>
    </Panel>
    <Panel title="Failure categories" id="bench-failures">
      <div className="panel-body flush">
        <DataTable caption="Failure categories" captionHidden rowKey={(row) => row.id} rows={failurePages.visible} empty="No failures." columns={[
          { key: 'category', label: 'Category', render: (row) => <span className="mono">{row.category}</span> },
          { key: 'profile', label: 'Faults', render: (row) => profileLabel(row.profile) },
          { key: 'concurrency', label: 'Concurrency', numeric: true },
          { key: 'count', label: 'Count', numeric: true },
        ]} />
        <Pager label="Failure categories" window={failurePages.window} onPage={failurePages.setPage} />
      </div>
    </Panel>
    {soak && <Panel title="Soak" id="bench-soak">
      <p>{compactNumber(soak.runs)} runs in {duration(soak.duration_ms)} at concurrency {soak.concurrency} ({soak.throughput_rps} runs/s), p95 {ms(soak.latency_ms.p95)}
        {soak.memory ? `, RSS ${soak.memory.rss_start_mib} → ${soak.memory.rss_end_mib} MiB (peak ${soak.memory.rss_peak_mib})` : ''}.</p>
      {soak.windows.length > 1 && <LineChart title="Soak throughput" series={[{ name: 'runs/s', points: soak.windows.map((w) => ({ x: w.elapsed_ms / 1000, y: w.throughput_rps })) }]}
        formatX={seconds} xLabel="Elapsed" height={180} />}
    </Panel>}
    <p className="muted">{report.limitations}</p>
  </div>;
}

function LiveLoad({ events }) {
  const series = liveSeries(events);
  const latest = [...events].reverse().find((event) => event.event === 'tick');
  const level = [...events].reverse().find((event) => event.event === 'level_started' || event.event === 'soak_started');
  const levelsDone = events.filter((event) => event.event === 'level').length;
  return <div className="stack">
    <div className="grid four">
      <StatTile label="Current level" value={level ? `${level.phase === 'soak' || level.event === 'soak_started' ? 'Soak · ' : ''}${level.concurrency} · ${profileLabel(level.fault_profile)}` : '—'} />
      <StatTile label="Throughput" value={latest ? `${latest.throughput_rps} runs/s` : '—'} />
      <StatTile label="p95 latency so far" value={ms(latest?.p95_latency_ms)} />
      <StatTile label="Completed" value={latest ? `${compactNumber(latest.completed)}${latest.runs ? ` / ${compactNumber(latest.runs)}` : ''} · ${levelsDone} level(s) done` : '—'} />
    </div>
    {series.throughput.length > 1 ? <div className="grid two">
      <Panel title="Throughput" id="live-throughput"><LineChart title="Completed runs per second" series={[{ name: 'runs/s', points: series.throughput }]} formatX={seconds} xLabel="Elapsed" height={180} /></Panel>
      <Panel title="Latency" id="live-latency"><LineChart title="p95 latency in the current level" series={[{ name: 'p95', points: series.p95 }]} formatX={seconds} formatY={(value) => `${Math.round(value)} ms`} xLabel="Elapsed" height={180} /></Panel>
    </div> : <p className="muted">Charts appear after the first second of load.</p>}
  </div>;
}

export function LoadTestsScreen({ report: reportId, navigate }) {
  const { project, missing, error } = useProject();
  const [refresh, setRefresh] = useState(0);
  const job = useJob('bench', (id) => { setRefresh((value) => value + 1); navigate(href({ screen: 'load-tests', params: { report: id } })); });
  const { report, error: reportError } = useReport(reportId);
  const reports = useReports('bench', refresh);
  const [form, setForm] = useState(null);
  useEffect(() => {
    if (project && !form) {
      setForm({ dataset: project.datasets[0]?.name ?? '', levels: levelsText(project.bench.concurrency), runs: String(project.bench.runs),
        profiles: project.bench.fault_profiles, soak: String(project.bench.soak_seconds) });
    }
  }, [project, form]);
  const levels = form ? parseLevels(form.levels) : null;
  const runs = Number(form?.runs);
  const soak = Number(form?.soak);
  const valid = form && levels && Number.isInteger(runs) && runs >= 1 && runs <= 10000 && soak >= 0 && soak <= 3600 && form.profiles.length;

  function toggle(profile) {
    setForm((current) => ({ ...current, profiles: current.profiles.includes(profile)
      ? current.profiles.filter((item) => item !== profile) : [...current.profiles, profile] }));
  }

  return <>
    <PageHeader eyebrow="Load tests" title="Load tests" description="Run your agent at increasing concurrency, with and without seeded faults. With the mock model this measures RunWeave and your agent code on this machine, not provider capacity." />
    {missing && <NoProject />}
    {error && !missing && <ErrorState>{error}</ErrorState>}
    {project && form && <div className="grid sidebar-layout">
      <Panel title="Scenario" id="scenario">
        <form className="stack" onSubmit={(event) => {
          event.preventDefault();
          if (valid) job.start({ kind: 'bench', dataset: form.dataset, concurrency: levels, runs, fault_profiles: form.profiles, soak_seconds: soak });
        }}>
          <DatasetSelect id="lt-dataset" project={project} value={form.dataset} onChange={(dataset) => setForm({ ...form, dataset })} />
          <div className="field"><label htmlFor="lt-levels">Concurrency levels</label>
            <input id="lt-levels" value={form.levels} aria-invalid={levels ? undefined : 'true'} aria-describedby="lt-levels-hint"
              onChange={(event) => setForm({ ...form, levels: event.target.value })} />
            <span className="field-hint" id="lt-levels-hint">Up to 12 levels between 1 and 256, e.g. 1 8 32.</span></div>
          <div className="field"><label htmlFor="lt-runs">Runs per level</label>
            <input id="lt-runs" type="number" min="1" max="10000" value={form.runs} onChange={(event) => setForm({ ...form, runs: event.target.value })} /></div>
          <fieldset className="field options">
            <legend>Fault profiles</legend>
            {['', ...project.fault_profiles].map((profile) => <label key={profile || 'none'}>
              <input type="checkbox" checked={form.profiles.includes(profile)} onChange={() => toggle(profile)} />{profileLabel(profile)}
            </label>)}
            <span className="field-hint">Faults act on model calls through the LLM proxy.</span>
          </fieldset>
          <div className="field"><label htmlFor="lt-soak">Soak afterwards (seconds)</label>
            <input id="lt-soak" type="number" min="0" max="3600" value={form.soak} onChange={(event) => setForm({ ...form, soak: event.target.value })} /></div>
          <button className="primary" disabled={!valid || job.running}>{job.running ? 'Running…' : 'Start load test'}</button>
          <span className="field-hint">Same as <code>runweave bench</code>. The test runs in its own process, so this page does not slow it down.</span>
        </form>
      </Panel>
      <div className="stack">
        <ProjectSummary project={project} />
        <JobStatus job={job.job} error={job.error} onCancel={job.cancel} />
        {job.running && <LiveLoad events={job.events} />}
        {!job.running && report && <div className="grid two">
          {benchSummary(report).map((tile) => <StatTile key={tile.label} label={tile.label} value={tile.value} />)}
        </div>}
        {!job.running && !report && !reportError && <p className="muted">Start a load test, or open a saved one below.</p>}
      </div>
    </div>}
    {project && <div className="stack spaced-top">
      {reportError && <ErrorState>{reportError}</ErrorState>}
      {report && !job.running && <BenchResults report={report} />}
      <Panel title="Saved load tests" id="bench-reports"><div className="panel-body flush"><ReportList screen="load-tests" reports={reports} /></div></Panel>
    </div>}
  </>;
}

// Evals

function AssertionList({ assertions }) {
  return <ul className="bare stack tight">{assertions.map((item) => <li key={item.name} className="row">
    <StatusBadge tone={item.passed ? 'good' : 'critical'} label={item.passed ? 'Pass' : 'Fail'} />
    <span className="mono">{item.name}</span>{item.message && <span>{item.message}</span>}
  </li>)}</ul>;
}

/** Stat tiles for a saved load test: best throughput, p95 at the highest level, failures and levels. */
export function benchSummary(report) {
  const top = Math.max(...report.cases.map((row) => row.concurrency));
  const p95 = Math.max(...report.cases.filter((row) => row.concurrency === top).map((row) => row.latency_ms.p95 ?? 0));
  const failed = report.cases.reduce((sum, row) => sum + Object.values(row.failures).reduce((a, b) => a + b, 0), 0);
  return [
    { label: 'Best throughput', value: `${Math.max(...report.cases.map((row) => row.throughput_rps))} runs/s` },
    { label: `p95 at concurrency ${top}`, value: ms(p95) },
    { label: 'Failed runs', value: compactNumber(failed) },
    { label: 'Levels', value: String(report.cases.length) },
  ];
}

export function visibleCases(cases, { failedOnly, search }) {
  const needle = search.trim().toLowerCase();
  return cases.filter((row) => (!failedOnly || !row.passed) && (!needle || row.id.toLowerCase().includes(needle)));
}

export function EvalResults({ report, selected, onSelect }) {
  const s = report.summary;
  const anyFailed = report.cases.some((row) => !row.passed);
  const [failedOnly, setFailedOnly] = useState(anyFailed);
  const [search, setSearch] = useState('');
  const shown = visibleCases(report.cases, { failedOnly, search });
  const pages = usePages(shown, { resetKey: `${report.id}|${failedOnly}|${search}` });
  const current = report.cases.find((row) => row.id === selected) ?? report.cases.find((row) => !row.passed) ?? report.cases[0];
  const regressions = report.baseline?.regressions ?? [];
  return <div className="stack">
    <div className="grid four">
      <StatTile label="Cases passed" value={`${s.passed} / ${s.total}`} />
      <StatTile label="Pass rate" value={percent(s.pass_rate)} />
      <StatTile label="p95 latency" value={ms(s.p95_latency_ms)} />
      <StatTile label="Estimated cost" value={`${usd(s.cost_usd)} · ${compactNumber(s.tokens)} tokens`} />
    </div>
    <Gates gates={report.gates} />
    {regressions.length > 0 && <ErrorState><strong>Regressed against the baseline:</strong> {regressions.join(', ')}</ErrorState>}
    <div className="grid two">
      <Panel title="Cases" id="eval-cases">
        <div className="table-tools row">
          <label className="row"><input type="checkbox" checked={failedOnly} onChange={(event) => setFailedOnly(event.target.checked)} />Failed only</label>
          <label className="visually-hidden" htmlFor="eval-case-search">Find a case</label>
          <input id="eval-case-search" type="search" className="mono" placeholder="Find a case" value={search}
            onChange={(event) => setSearch(event.target.value)} />
          <span className="muted">{compactNumber(shown.length)} of {compactNumber(report.cases.length)} cases</span>
        </div>
        <div className="panel-body flush">
          <DataTable caption="Evaluation cases" captionHidden rowKey={(row) => row.id} rows={pages.visible} selectedKey={current?.id}
            empty={failedOnly ? 'No failed cases match.' : 'No cases match.'}
            onRowClick={(row) => onSelect(row.id)} columns={[
              { key: 'id', label: 'Case', render: (row) => <button type="button" className="text-button mono" onClick={() => onSelect(row.id)}>{row.id}</button> },
              { key: 'passed', label: 'Result', render: (row) => <StatusBadge tone={row.passed ? 'good' : 'critical'} label={row.passed ? 'Pass' : 'Fail'} /> },
              { key: 'status', label: 'Run', render: (row) => <StatusBadge status={row.status} /> },
              { key: 'assertions', label: 'Assertions', numeric: true, render: (row) => `${row.assertions.filter((a) => a.passed).length} / ${row.assertions.length}` },
              { key: 'latency_ms', label: 'Latency', numeric: true, render: (row) => ms(row.latency_ms) },
            ]} />
          <Pager label="Cases" window={pages.window} onPage={pages.setPage} />
        </div>
      </Panel>
      {current && <Panel title={current.id} id="eval-case" actions={<StatusBadge tone={current.passed ? 'good' : 'critical'} label={current.passed ? 'Pass' : 'Fail'} />}>
        <div className="stack">
          <AssertionList assertions={current.assertions} />
          <KeyValueList items={[
            { label: 'Run status', value: <StatusBadge status={current.status} /> },
            { label: 'Latency', value: ms(current.latency_ms) },
            { label: 'Tokens', value: compactNumber(current.tokens) },
          ]} />
          <p className="muted">Outputs are not stored in reports. The test ran in its own RunWeave instance, which stopped when it finished.</p>
        </div>
      </Panel>}
    </div>
  </div>;
}

function LiveCases({ events }) {
  const cases = events.filter((event) => event.event === 'case' || event.event === 'pair');
  const total = events.find((event) => event.event === 'started')?.total;
  return <div className="stack">
    <p className="muted">{cases.length}{total ? ` of ${total}` : ''} done</p>
    <ul className="bare stack tight">{cases.slice(-12).map((item) => <li key={item.seq} className="row">
      <StatusBadge tone={item.event === 'pair' ? (item.regressed ? 'critical' : 'good') : item.passed ? 'good' : 'critical'}
        label={item.event === 'pair' ? (item.regressed ? 'Regressed' : item.fixed ? 'Fixed' : 'Same') : item.passed ? 'Pass' : 'Fail'} />
      <span className="mono">{item.id}</span>
    </li>)}</ul>
  </div>;
}

export function EvalsScreen({ report: reportId, navigate }) {
  const { project, missing, error } = useProject();
  const [refresh, setRefresh] = useState(0);
  const [dataset, setDataset] = useState('');
  const [selected, setSelected] = useState(null);
  const job = useJob('eval', (id) => { setRefresh((value) => value + 1); navigate(href({ screen: 'evals', params: { report: id } })); });
  const { report, error: reportError } = useReport(reportId);
  const reports = useReports('eval', refresh);
  const chosen = dataset || project?.datasets[0]?.name || '';

  return <>
    <PageHeader eyebrow="Evals" title="Evals"
      description={<>Run a dataset through your agent. Assertions and gates decide pass or fail, the same way <code>runweave eval</code> does in CI.</>}
      actions={project && <form className="row" onSubmit={(event) => { event.preventDefault(); job.start({ kind: 'eval', dataset: chosen }); }}>
        <DatasetSelect id="eval-dataset" project={project} value={chosen} onChange={setDataset} compact />
        <button className="primary" disabled={job.running || !chosen}>{job.running ? 'Running…' : 'Run eval'}</button>
      </form>} />
    {missing && <NoProject />}
    {error && !missing && <ErrorState>{error}</ErrorState>}
    {project && <div className="stack">
      <ProjectSummary project={project} />
      <JobStatus job={job.job} error={job.error} onCancel={job.cancel} />
      {job.running && <LiveCases events={job.events} />}
      {reportError && <ErrorState>{reportError}</ErrorState>}
      {report && !job.running && <EvalResults report={report} selected={selected} onSelect={setSelected} />}
      <Panel title="Saved evals" id="eval-reports"><div className="panel-body flush"><ReportList screen="evals" reports={reports} /></div></Panel>
    </div>}
  </>;
}

// Shadow

const gateLabels = {
  candidate_pass_rate_met: 'Candidate pass rate',
  no_paired_regressions: 'No paired regressions',
  outcome_parity: 'Outcome parity',
  candidate_cost_within_limit: 'Cost per case',
  candidate_latency_within_limit: 'p95 latency',
};

function gateThreshold(name, settings = {}) {
  if (name === 'candidate_pass_rate_met') return `≥ ${percent(settings.min_pass_rate ?? 1)}`;
  if (name === 'no_paired_regressions') return '0 regressed';
  if (name === 'outcome_parity') return settings.require_output_parity ? 'Same status and output' : 'Same status';
  if (name === 'candidate_cost_within_limit') return `≤ ${Math.round((settings.max_cost_ratio ?? 1.05) * 100)}% of current`;
  return `≤ ${settings.max_latency_ratio ?? 2}× + ${settings.latency_slack_ms ?? 50} ms`;
}

/** What the candidate measured for a promotion gate, beside its threshold. */
export function gateMeasured(name, report) {
  const { primary, candidate, pairs, settings = {} } = report;
  if (name === 'candidate_pass_rate_met') return percent(candidate.pass_rate);
  if (name === 'no_paired_regressions') return `${compactNumber(report.regressions.length)} regressed`;
  if (name === 'outcome_parity') {
    const output = settings.require_output_parity;
    const same = pairs.filter((pair) => pair.differences.status_equal && (!output || pair.differences.output_equal)).length;
    return `${compactNumber(same)} of ${compactNumber(pairs.length)}`;
  }
  if (name === 'candidate_cost_within_limit') {
    const [now, next] = [Number(primary.cost_usd), Number(candidate.cost_usd)];
    return now > 0 ? `${Math.round((next / now) * 100)}%` : usd(candidate.cost_usd);
  }
  return ms(candidate.p95_latency_ms);
}

const signedMs = (value) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(1)} ms`;

export function ShadowResults({ report }) {
  const { primary, candidate } = report;
  const changed = report.pairs.filter((pair) => pair.differences.regressed || pair.differences.fixed);
  const [changedOnly, setChangedOnly] = useState(changed.length > 0);
  const shownPairs = changedOnly ? changed : report.pairs;
  const pairPages = usePages(shownPairs, { resetKey: `${report.id}|${changedOnly}` });
  const failed = Object.entries(report.gates).filter(([, ok]) => !ok).map(([name]) => gateLabels[name] ?? name);
  return <div className="stack">
    <div className="grid two">
      <StatTile label="Current" value={<span className="mono">{primary.agent.name} · {primary.agent.version}</span>} />
      <StatTile label="Candidate" value={<span className="mono">{candidate.agent.name} · {candidate.agent.version}</span>} />
    </div>
    {report.promotion_eligible
      ? <p className="row"><StatusBadge tone="good" label="Eligible for promotion" /><span className="muted">Nothing is switched automatically.</span></p>
      : <ErrorState><strong>Not eligible.</strong> Failed: {failed.join(', ')}.</ErrorState>}
    <div className="grid two">
      <Panel title="Promotion gates" id="shadow-gates">
        <div className="panel-body flush">
          <DataTable caption="Promotion gates" captionHidden rowKey={(row) => row.name} rows={Object.entries(report.gates).map(([name, ok]) => ({ name, ok }))} columns={[
            { key: 'name', label: 'Gate', render: (row) => gateLabels[row.name] ?? row.name },
            { key: 'threshold', label: 'Threshold', render: (row) => gateThreshold(row.name, report.settings) },
            { key: 'measured', label: 'Candidate', render: (row) => gateMeasured(row.name, report) },
            { key: 'ok', label: 'Result', render: (row) => <StatusBadge tone={row.ok ? 'good' : 'critical'} label={row.ok ? 'Pass' : 'Fail'} /> },
          ]} />
        </div>
      </Panel>
      <Panel title="Side by side" id="shadow-compare">
        <div className="panel-body flush">
          <DataTable caption="Current and candidate" captionHidden rowKey={(row) => row.label} rows={[
            { label: 'Pass rate', now: percent(primary.pass_rate), next: percent(candidate.pass_rate) },
            { label: 'p95 latency', now: ms(primary.p95_latency_ms), next: ms(candidate.p95_latency_ms) },
            { label: 'Tokens', now: compactNumber(primary.tokens), next: compactNumber(candidate.tokens) },
            { label: 'Estimated cost', now: usd(primary.cost_usd), next: usd(candidate.cost_usd) },
          ]} columns={[
            { key: 'label', label: 'Measure' },
            { key: 'now', label: 'Current', numeric: true },
            { key: 'next', label: 'Candidate', numeric: true },
          ]} />
        </div>
      </Panel>
    </div>
    <Panel title="Paired cases" id="shadow-pairs">
      <div className="table-tools row">
        <label className="row"><input type="checkbox" checked={changedOnly} onChange={(event) => setChangedOnly(event.target.checked)} />Changed only</label>
        <span className="muted">{changedOnly ? `${compactNumber(changed.length)} changed of ${compactNumber(report.pairs.length)} pairs`
          : `${compactNumber(report.pairs.length)} pairs`}</span>
      </div>
      <div className="panel-body flush">
        <DataTable caption="Paired case differences" captionHidden rowKey={(row) => row.id} rows={pairPages.visible}
          empty={changedOnly ? 'No case changed its verdict.' : 'No pairs.'} columns={[
          { key: 'id', label: 'Case', render: (row) => <span className="mono">{row.id}</span> },
          { key: 'change', label: 'Change', render: (row) => <StatusBadge tone={row.differences.regressed ? 'critical' : row.differences.fixed ? 'good' : 'neutral'}
            label={row.differences.regressed ? 'Regressed' : row.differences.fixed ? 'Fixed' : 'Same verdict'} /> },
          { key: 'status', label: 'Status', render: (row) => (row.differences.status_equal ? 'Same' : `${row.primary.status} → ${row.candidate.status}`) },
          { key: 'output', label: 'Output', render: (row) => (row.differences.output_equal ? 'Equal' : 'Different') },
          { key: 'latency', label: 'Δ latency', numeric: true, render: (row) => signedMs(row.differences.latency_ms) },
          { key: 'cost', label: 'Δ cost', numeric: true, render: (row) => row.differences.cost_usd },
          { key: 'failing', label: 'Newly failing', render: (row) => row.differences.newly_failing.join(', ') || '—' },
        ]} />
        <Pager label="Paired cases" window={pairPages.window} onPage={pairPages.setPage} />
      </div>
    </Panel>
    <p className="muted">{report.limitations}</p>
  </div>;
}

export function ShadowScreen({ report: reportId, navigate }) {
  const { project, missing, error } = useProject();
  const [refresh, setRefresh] = useState(0);
  const [dataset, setDataset] = useState('');
  const [candidate, setCandidate] = useState(null);
  const job = useJob('shadow', (id) => { setRefresh((value) => value + 1); navigate(href({ screen: 'shadow', params: { report: id } })); });
  const { report, error: reportError } = useReport(reportId);
  const reports = useReports('shadow', refresh);
  const chosen = dataset || project?.datasets[0]?.name || '';
  const target = candidate ?? project?.shadow.candidate ?? '';
  const configured = project?.shadow.candidate;

  return <>
    <PageHeader eyebrow="Shadow" title="Shadow comparison" description="Run a candidate version beside the current agent on the same cases, each with its own RunWeave and budgets, and check the promotion gates. Nothing is switched automatically." />
    {missing && <NoProject />}
    {error && !missing && <ErrorState>{error}</ErrorState>}
    {project && <div className="stack">
      <form className="stack tight" onSubmit={(event) => {
        event.preventDefault();
        job.start({ kind: 'shadow', dataset: chosen, ...(target && target !== configured ? { candidate: target } : {}) });
      }}>
        <div className="row form-row">
          <DatasetSelect id="shadow-dataset" project={project} value={chosen} onChange={setDataset} />
          <div className="field grow"><label htmlFor="shadow-candidate">Candidate</label>
            <input id="shadow-candidate" className="mono" placeholder="for example agent_v2:run" value={target}
              onChange={(event) => setCandidate(event.target.value)} aria-describedby="shadow-candidate-hint" /></div>
          <button className="primary" disabled={job.running || !chosen || (!target && !configured)}>{job.running ? 'Running…' : 'Compare'}</button>
        </div>
        <span className="field-hint" id="shadow-candidate-hint">A Python <code>module:function</code> or an HTTP agent URL{configured ? '; defaults to [shadow] candidate' : ''}.</span>
      </form>
      <ProjectSummary project={project} />
      <JobStatus job={job.job} error={job.error} onCancel={job.cancel} />
      {job.running && <LiveCases events={job.events} />}
      {reportError && <ErrorState>{reportError}</ErrorState>}
      {report && !job.running && <ShadowResults report={report} />}
      <Panel title="Saved comparisons" id="shadow-reports"><div className="panel-body flush"><ReportList screen="shadow" reports={reports} /></div></Panel>
    </div>}
  </>;
}
