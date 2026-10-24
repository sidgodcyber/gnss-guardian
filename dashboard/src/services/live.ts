// Live source: poll http://<pi>:PORT/live.json (served by `gnss-guardian live --overlay PORT`) once a
// second and grow a DashboardFile from it, so the replay panels show the running session. The Pi
// sends CORS headers for this; nothing is sent to the Pi except the GET.
import { DashboardFile, LiveView, Row, Transition } from '../types/gnssGuardian';

export const STALE_S = 3;

export function liveBase(address: string): string {
  const a = address.trim().replace(/\/+$/, '');
  if (!a) return '';
  const withScheme = /^https?:\/\//.test(a) ? a : `http://${a}`;
  return /:\d+$/.test(withScheme.replace(/^https?:\/\//, '')) ? withScheme : `${withScheme}:8080`;
}

export function emptyLiveFile(view: LiveView, base: string): DashboardFile {
  return {
    format: 'gnss-guardian-dashboard/2',
    meta: {
      source: `${base}/live.json`,
      mode: 'live',
      created_utc: view.written_utc,
      node_id: view.node_id,
      start_t_gnss: view.t_gnss,
      duration_s: 0,
      epochs: 0,
      home_set: !!view.home?.set,
      state_source: 'pipeline state machine on the Pi (live.json)',
      video: null,
      video_fps: null,
      reference_track: null,
    },
    config: view.config,
    timeline: [],
    transitions: [],
    detector_levels: [],
    keyframes: [],
    routes: [],
    events: [],
  };
}

// Append one live view (if it is a new epoch) to the growing file; returns a new object when it changed.
export function appendLive(file: DashboardFile, view: LiveView): DashboardFile {
  const last = file.timeline[file.timeline.length - 1];
  if (last && view.seq === last.seq) return file;
  const row: Row = { ...view.row, injection: view.injection.active };
  const timeline = [...file.timeline, row].slice(-3600);
  const transitions = [...file.transitions];
  const prevState = last?.state ?? null;
  if (!last || row.state !== prevState) {
    const tr: Transition = {
      t: row.t,
      t_gnss: row.t_gnss,
      state: row.state,
      from: last ? prevState : null,
      cause: row.cause,
      reasons: row.state === 'SPOOFED' || row.state === 'JAMMED' ? row[row.cause ?? 'spoofing']?.reasons ?? [] : [],
      indicators: [],
    };
    transitions.push(tr);
  }
  const detector_levels = [...file.detector_levels];
  for (const d of ['jamming', 'spoofing'] as const) {
    if (last && last[d].level !== row[d].level) {
      detector_levels.push({ t: row.t, t_gnss: row.t_gnss, detector: d, level: row[d].level, score: row[d].score,
                             reasons: row[d].reasons });
    }
  }
  const events = [...file.events];
  const wasInj = last?.injection ?? null;
  const inj = view.injection.active;
  if (inj && (!wasInj || wasInj.id !== inj.id)) {
    events.push({ t: row.t, kind: 'demo_inject',
                  detail: `DEMO INJECTION ${inj.type} ${inj.type === 'jump' ? `${inj.m} m` : `${inj.mps} m/s`} at ${inj.bearing_deg}° (software, not a real attack)` });
  } else if (!inj && wasInj) {
    events.push({ t: row.t, kind: 'demo_inject', detail: 'demo injection stopped' });
  }
  if (view.home?.set && !file.meta.home_set) events.push({ t: row.t, kind: 'home_set', detail: 'home point set' });
  const keyframes = (view.keyframes ?? []).map(([e, n, home], id) => ({ t: null, id, e, n, home }));
  return {
    ...file,
    keyframes,
    meta: { ...file.meta, duration_s: row.t, epochs: file.meta.epochs + 1, home_set: !!view.home?.set },
    timeline,
    transitions,
    detector_levels,
    events,
  };
}

export async function fetchLive(base: string, signal?: AbortSignal): Promise<LiveView> {
  const res = await fetch(`${base}/live.json`, { cache: 'no-store', signal });
  if (!res.ok) throw new Error(`${base}/live.json: ${res.status} ${res.status === 503 ? '(no epoch yet)' : ''}`);
  const v = (await res.json()) as LiveView;
  if (!v || v.format !== 'gnss-guardian-live/1') throw new Error(`${base}/live.json is not gnss-guardian live data`);
  return v;
}
