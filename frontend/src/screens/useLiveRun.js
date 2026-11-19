import { useEffect, useState } from 'react';
import { api, streamRun, terminal } from '../api.js';

const pollMs = 1000;

function wait(ms, signal) {
  return new Promise((done) => {
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', () => { clearTimeout(timer); done(); }, { once: true });
  });
}

// Follows one run: live server-sent events first, polling when streams are busy or unavailable.
export function useLiveRun(id, revision) {
  const [view, setView] = useState({ id });

  useEffect(() => {
    if (!id) return undefined;
    const controller = new AbortController();
    const { signal } = controller;
    let latest;
    const show = (snapshot) => {
      latest = snapshot;
      if (!signal.aborted) setView((previous) => ({ ...previous, id, snapshot, error: undefined }));
    };

    async function follow() {
      try {
        show(await api(`/v1/runs/${id}`, { signal }));
      } catch (error) {
        if (!signal.aborted) {
          setView({ id, error: error.status === 404 ? 'This run expired or is unavailable.' : error.message, mode: 'idle' });
        }
        return;
      }
      let mode = 'live';
      while (!signal.aborted && !terminal.has(latest?.status)) {
        if (mode === 'live') {
          setView((previous) => ({ ...previous, mode }));
          try {
            const outcome = await streamRun(id, { signal, onSnapshot: show });
            if (outcome === 'reconnect') continue;
          } catch (error) {
            if (signal.aborted) return;
            if (error.status === 404) {
              setView({ id, error: 'This run expired or is unavailable.', mode: 'idle' });
              return;
            }
          }
          mode = 'polling';
          continue;
        }
        setView((previous) => ({ ...previous, mode }));
        await wait(pollMs, signal);
        if (signal.aborted) return;
        try {
          show(await api(`/v1/runs/${id}`, { signal }));
        } catch (error) {
          if (error.status === 404) {
            setView({ id, error: 'This run expired or is unavailable.', mode: 'idle' });
            return;
          }
        }
      }
      if (!signal.aborted) setView((previous) => ({ ...previous, mode: 'idle' }));
    }

    follow();
    return () => controller.abort();
  }, [id, revision]);

  return view.id === id ? view : { id };
}

export function useApproval(id, status, revision) {
  const [state, setState] = useState({});
  useEffect(() => {
    if (status !== 'WAITING_APPROVAL') {
      setState({});
      return undefined;
    }
    const controller = new AbortController();
    api(`/v1/runs/${id}/approval`, { signal: controller.signal })
      .then((approval) => setState({ approval }))
      .catch((error) => {
        if (controller.signal.aborted) return;
        // No ticket approval: the agent itself asked (ctx.request_approval).
        setState(error.status === 404 ? { agent: true } : { error: error.message });
      });
    return () => controller.abort();
  }, [id, status, revision]);
  return state;
}
