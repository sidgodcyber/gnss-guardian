import React, { useMemo } from 'react';
import { DashboardFile, Row } from '../types/gnssGuardian';
import { fmt } from '../services/session';

// The camera navigation, step by step (our 8-step diagram), from what each epoch exported. Read-only.
//   green = working, amber = weak, grey = idle (or camera offline).

type Status = 'ok' | 'weak' | 'idle';
interface Step { n: number; title: string; method: string; status: Status; value: string }

const MIN_POINTS = 30;            // enough corners / tracks for a steady estimate (display threshold)
const GOOD_QUALITY = 0.5;

const tone: Record<Status, string> = {
  ok: 'border-emerald-500/70 bg-emerald-950/40 text-emerald-200',
  weak: 'border-amber-500/70 bg-amber-950/40 text-amber-200',
  idle: 'border-slate-800 bg-slate-900/50 text-slate-500',
};
const dot: Record<Status, string> = { ok: 'bg-emerald-400', weak: 'bg-amber-400', idle: 'bg-slate-600' };

function lvl(x: number | null | undefined, good: number): Status {
  if (x == null || x <= 0) return 'idle';
  return x >= good ? 'ok' : 'weak';
}

export function cameraOffline(data: DashboardFile, isLive: boolean): string | null {
  if (data.timeline.some((r) => r.vision)) return null;
  return isLive ? 'the Pi sends no camera track (live without a camera)' : 'this session has no camera track';
}

function steps(data: DashboardFile, row: Row, matchAge: number | null): Step[] {
  const v = row.vision;
  const cam = data.config.camera;
  const speed = v?.v_e != null && v?.v_n != null ? Math.hypot(v.v_e, v.v_n) : null;
  const out = v?.n_tracked != null && v.n_inliers != null ? Math.max(0, v.n_tracked - v.n_inliers) : null;
  const q = v?.quality ?? null;
  const src = row.fused?.source;
  const g = row.guidance;
  const mode =
    src === 'vision_stale' ? 'held (track lost)'
      : v?.mode === 'gnss_anchored' ? 'GNSS-anchored'
      : v?.mode === 'dead_reckoning' ? 'dead reckoning'
      : v?.mode === 'waiting_for_home' ? 'waiting for home' : '–';
  return [
    { n: 1, title: 'Find points', method: 'Shi-Tomasi', status: lvl(v?.n_corners, MIN_POINTS),
      value: v?.n_corners != null ? `${v.n_corners} corners` : '–' },
    { n: 2, title: 'Track movement', method: 'Lucas-Kanade', status: lvl(v?.n_tracked, MIN_POINTS),
      value: v?.n_tracked != null ? `${v.n_tracked} tracked${v.orb ? ' (ORB)' : ''}` : '–' },
    { n: 3, title: 'Estimate motion', method: 'RANSAC', status: lvl(q, GOOD_QUALITY),
      value: v ? `${v.n_inliers ?? 0} in · ${out ?? '–'} out · q ${fmt(q, 2)}` : '–' },
    { n: 4, title: 'Pixels to metres', method: 'height / focal', status: v ? (q != null && q >= GOOD_QUALITY ? 'ok' : 'weak') : 'idle',
      value: cam ? `${cam.height_m} m · f ${cam.focal_px} px · ${fmt(speed, 2, 'm/s')}` : fmt(speed, 2, 'm/s') },
    { n: 5, title: 'Align heading', method: 'learned from GNSS', status: !v ? 'idle' : v.aligned ? 'ok' : 'weak',
      value: !v ? '–' : v.aligned ? `aligned · ${fmt(v.heading, 0, '°')}` : 'not aligned yet' },
    { n: 6, title: 'Position', method: 'dead reckoning', status: !v || v.mode === 'waiting_for_home' ? 'idle' : src === 'vision_stale' ? 'weak' : 'ok',
      value: v?.mode === 'dead_reckoning' || src === 'vision_stale'
        ? `${mode} · ${fmt(v?.since_anchor_m, 1, 'm')} since fix` : mode },
    { n: 7, title: 'Keyframes', method: 'ORB', status: !v ? 'idle' : v.keyframes > 0 ? 'ok' : 'weak',
      value: !v ? '–' : `${v.keyframes} stored${matchAge != null ? ` · match ${matchAge.toFixed(0)} s ago` : ''}${v.home_confirmed ? ' · HOME ✓' : ''}` },
    { n: 8, title: 'Return home', method: 'reverse path', status: g || row.state === 'HOME_REACHED' ? 'ok' : 'idle',
      value: row.state === 'HOME_REACHED' ? 'home reached'
        : g ? `home ${fmt(g.home_distance_m, 1, 'm')} @ ${fmt(g.home_bearing_deg, 0)}° · next ${g.waypoint.id === 'home' ? 'home' : `kf ${g.waypoint.id}`}`
        : 'not guiding' },
  ];
}

function sentence(row: Row, offline: string | null): string {
  if (offline) return `Camera offline: ${offline}. The vision steps are idle; GNSS detection still runs.`;
  const v = row.vision;
  const src = row.fused?.source;
  if (row.state === 'HOME_REACHED' && v?.home_confirmed)
    return `Home confirmed by keyframe match (${v.home_confirmed.offset_m} m, ${v.home_confirmed.inliers} inliers).`;
  if (row.state === 'RETURN_HOME' && row.guidance)
    return `Returning home on the camera: ${fmt(row.guidance.home_distance_m, 1, 'm')} to go along the stored path.`;
  if (src === 'vision_stale') return 'Camera track lost: position held at the last camera estimate.';
  if (src === 'vision')
    return `GNSS distrusted: navigating on camera (dead reckoning ${fmt(v?.since_anchor_m, 1, 'm')}).`;
  if (row.warming_up) return 'Warming up: GNSS baseline building, camera waiting for the home point.';
  if (row.gnss_trusted && v?.mode === 'gnss_anchored') return 'GNSS trusted: camera following GPS and storing keyframes.';
  if (!row.gnss_trusted) return 'GNSS not trusted: holding the last trusted fix.';
  return 'GNSS trusted.';
}

export const VisionPipeline: React.FC<{ data: DashboardFile; row: Row; isLive: boolean; compact?: boolean }> = ({
  data,
  row,
  isLive,
  compact = false,
}) => {
  const offline = cameraOffline(data, isLive);
  // when did the current keyframe match first appear (rows carry the latest match; its age = now - first row)
  const firstSeen = useMemo(() => {
    const m = new Map<number, number>();
    for (const r of data.timeline) {
      const t = r.vision?.last_match?.t;
      if (t != null && !m.has(t)) m.set(t, r.t);
    }
    return m;
  }, [data]);
  const lm = row.vision?.last_match;
  const matchAge = lm && firstSeen.has(lm.t) ? Math.max(0, row.t - firstSeen.get(lm.t)!) : null;
  const list = steps(data, row, matchAge).map((s) => (offline ? { ...s, status: 'idle' as Status, value: '–' } : s));

  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900/80 ${compact ? 'px-2 py-1.5' : 'p-3'} flex flex-col gap-1.5`}>
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wider font-semibold text-slate-400 shrink-0">Vision pipeline</span>
        <span className={`text-xs truncate ${offline ? 'text-slate-500' : 'text-slate-200'}`} title={sentence(row, offline)}>
          {sentence(row, offline)}
        </span>
      </div>
      <div className="flex items-stretch gap-1 min-w-0 overflow-x-auto">
        {list.map((s, i) => (
          <React.Fragment key={s.n}>
            <div className={`flex-1 min-w-[108px] rounded-lg border ${tone[s.status]} ${compact ? 'px-2 py-1' : 'px-2.5 py-2'}`}
              title={`${s.n}. ${s.title} (${s.method}): ${s.value}`}>
              <div className="flex items-center gap-1.5">
                <span className={`w-2 h-2 rounded-full shrink-0 ${dot[s.status]}`} />
                <span className="text-[11px] font-semibold truncate">{s.n}. {s.title}</span>
              </div>
              {!compact && <div className="text-[10px] text-slate-500 truncate">{s.method}</div>}
              <div className="text-[11px] font-mono truncate mt-0.5">{s.value}</div>
            </div>
            {i < list.length - 1 && <span className="self-center text-slate-600 text-xs select-none">→</span>}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
};
