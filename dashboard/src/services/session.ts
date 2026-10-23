import { DashboardFile, GuardianState, LoadedSession, Row, Transition } from '../types/gnssGuardian';

const FORMAT_PREFIX = 'gnss-guardian-dashboard/';

function check(data: DashboardFile, where: string): DashboardFile {
  if (!data || typeof data.format !== 'string' || !data.format.startsWith(FORMAT_PREFIX)) {
    throw new Error(`${where}: not a gnss-guardian dashboard.json (run gnss-guardian replay again to export it)`);
  }
  return data;
}

// Folders under the repo's session/ directory, served by the Vite dev/preview server (vite.config.ts).
export async function listServerSessions(): Promise<{ dir: string; sessions: string[] } | null> {
  try {
    const res = await fetch('/sessions/index.json');
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function loadServerSession(name: string): Promise<LoadedSession> {
  const res = await fetch(`/sessions/${encodeURIComponent(name)}/dashboard.json`);
  if (!res.ok) throw new Error(`session/${name}: dashboard.json not found (${res.status})`);
  const data = check(await res.json(), `session/${name}`);
  return {
    id: `server:${name}`,
    name,
    data,
    videoUrl: data.meta.video ? `/sessions/${encodeURIComponent(name)}/${data.meta.video}` : undefined,
  };
}

// A session folder picked in the browser (<input webkitdirectory>): nothing leaves the machine.
export async function loadPickedFolder(files: FileList): Promise<LoadedSession> {
  const list = Array.from(files);
  const json = list.find((f) => f.name === 'dashboard.json');
  if (!json) throw new Error('no dashboard.json in that folder: pick a session/<name> folder written by gnss-guardian replay');
  const folder = (json.webkitRelativePath || json.name).split('/').slice(-2, -1)[0] || 'picked folder';
  const data = check(JSON.parse(await json.text()), folder);
  const video = data.meta.video ? list.find((f) => f.name === data.meta.video) : undefined;
  return {
    id: `local:${folder}:${Date.now()}`,
    name: `${folder} (local folder)`,
    data,
    videoUrl: video ? URL.createObjectURL(video) : undefined,
  };
}

// Index of the last row at or before t (0 when t is before the first row).
export function rowIndexAt(rows: Row[], t: number): number {
  let lo = 0;
  let hi = rows.length - 1;
  if (hi < 0 || t <= rows[0].t) return 0;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (rows[mid].t <= t) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

// Fractional video frame at t, interpolated between the per-second frame indices of the rows.
export function frameAt(rows: Row[], t: number): number | null {
  const i = rowIndexAt(rows, t);
  const a = rows[i];
  if (!a || a.frame === null) return null;
  const b = rows[i + 1];
  if (!b || b.frame === null || b.t <= a.t || t <= a.t) return a.frame;
  return a.frame + ((Math.min(t, b.t) - a.t) / (b.t - a.t)) * (b.frame - a.frame);
}

export function stateLabel(state: GuardianState | null): string {
  return state ?? 'WARM-UP';
}

export function lastTransitionAt(transitions: Transition[], t: number): Transition | undefined {
  let found: Transition | undefined;
  for (const tr of transitions) {
    if (tr.t <= t) found = tr;
    else break;
  }
  return found;
}

export function fmtTime(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
}

export function fmt(x: number | null | undefined, digits = 1, unit = ''): string {
  if (x === null || x === undefined || !Number.isFinite(x)) return '–';
  return `${x.toFixed(digits)}${unit ? ` ${unit}` : ''}`;
}
