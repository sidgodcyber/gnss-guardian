import React, { useEffect, useState } from 'react';
import { DashboardFile, Detector, Row, TrustScore } from '../types/gnssGuardian';
import { fmt, fmtTime, lastTransitionAt, stateLabel } from '../services/session';
import { ReplayControls, ScoreStrip, featureValue } from './DetectionIndicatorsPanel';
import { TacticalMap } from './TacticalMap';
import { DownCamView, probeClip } from './DownCamView';

// Single-screen view for judging: everything that matters on one 1920x1080 (or 1366x768) screen.
// Left: GNSS Trust, Nav confidence (only while the camera carries the position), state, AI.
// Centre: tactical map. Right: down-cam (or a compact "camera offline" tile, the map takes the space).
// Bottom: score timeline (+ replay controls) and the indicators that fire now.

interface Props {
  data: DashboardFile;
  row: Row;
  history: Row[];
  currentTimeSec: number;
  isPlaying: boolean;
  playbackSpeed: number;
  isLive: boolean;
  stale: boolean;
  videoUrl?: string;
  liveStreamUrl?: string;
  onSeek: (t: number) => void;
  onTogglePlay: () => void;
  onSetSpeed: (s: number) => void;
  onRestart: () => void;
}

const trustColour = (s: number) => (s >= 80 ? '#059669' : s >= 50 ? '#d97706' : '#dc2626');
const trustText = (s: number) => (s >= 80 ? 'text-emerald-300' : s >= 50 ? 'text-amber-300' : 'text-red-300');

const stateTone: Record<string, string> = {
  GNSS_OK: 'text-emerald-300',
  SUSPECT: 'text-amber-300',
  JAMMED: 'text-red-300',
  SPOOFED: 'text-red-300',
  VISION_NAV: 'text-sky-300',
  RETURN_HOME: 'text-violet-300',
  HOME_REACHED: 'text-teal-300',
  'WARM-UP': 'text-slate-300',
};

const CAUSE: Record<string, string> = { jamming: 'Jamming / blockage', spoofing: 'Spoofing' };

function topReasons(t: TrustScore, n = 3): string[] {
  const out: string[] = [];
  if (t.cap) out.push(`capped at ${t.cap.value}: ${t.cap.reason}`);
  for (const p of [...t.penalties].sort((a, b) => (b.points ?? 0) - (a.points ?? 0))) {
    if (out.length >= n) break;
    out.push(`−${Math.round(p.points ?? 0)} ${p.reason}`);
  }
  return out.length ? out : ['no penalties'];
}

// Short screens (1366x768 and similar): smaller rings, tighter cards, shorter timeline.
function useShort(limit = 850): boolean {
  const [short, setShort] = useState(() => window.innerHeight < limit);
  useEffect(() => {
    const on = () => setShort(window.innerHeight < limit);
    window.addEventListener('resize', on);
    return () => window.removeEventListener('resize', on);
  }, [limit]);
  return short;
}

const Card: React.FC<{ title: string; children: React.ReactNode; className?: string }> = ({ title, children, className = '' }) => (
  <div className={`px-3 py-2 rounded-xl bg-slate-900/80 border border-slate-800 min-w-0 ${className}`}>
    <div className="text-[11px] uppercase tracking-wider font-semibold text-slate-400">{title}</div>
    {children}
  </div>
);

const Ring: React.FC<{ score: number; size: number }> = ({ score, size }) => {
  const r = size / 2 - 7;
  const c = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0" aria-hidden>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#e2e8f0" strokeWidth={9} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={trustColour(score)}
        strokeWidth={9}
        strokeLinecap="round"
        strokeDasharray={`${(Math.max(0, Math.min(100, score)) / 100) * c} ${c}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text x="50%" y="52%" textAnchor="middle" dominantBaseline="middle" fill={trustColour(score)}
        fontSize={size * 0.34} fontWeight={700} fontFamily="ui-monospace, monospace">
        {score}
      </text>
    </svg>
  );
};

const TrustCard: React.FC<{ title: string; t: TrustScore; size: number; note?: string }> = ({ title, t, size, note }) => (
  <Card title={title}>
    <div className="flex items-center gap-3 mt-1.5">
      <Ring score={t.score} size={size} />
      <ul className="min-w-0 space-y-1 text-xs text-slate-300">
        {topReasons(t).map((r, i) => (
          <li key={i} className="leading-snug line-clamp-2" title={r}>{r}</li>
        ))}
        {note && <li className="text-slate-500">{note}</li>}
      </ul>
    </div>
  </Card>
);

const StateCard: React.FC<{ data: DashboardFile; row: Row; t: number; stale: boolean; short: boolean }> = ({ data, row, t, stale, short }) => {
  const st = stateLabel(row.state);
  const cause = row.cause ? CAUSE[row.cause] : null;
  const det = row.cause as Detector | null;
  const reason =
    (det && row[det].reasons[0]) ||
    lastTransitionAt(data.transitions, t)?.reasons[0] ||
    (row.gnss_trusted ? 'GNSS trusted: all detectors clear' : row.warming_up ? 'warming up: building the clean baseline' : '');
  return (
    <Card title="State">
      <div className={`${short ? 'text-2xl' : 'text-3xl'} font-bold font-mono mt-0.5 leading-tight ${stale ? 'text-slate-400' : stateTone[st] ?? 'text-slate-100'}`}>
        {stale ? 'GNSS UNKNOWN' : st}
      </div>
      <div className="text-sm text-slate-200 mt-0.5">
        {stale ? `no update from the Pi · last: ${st}` : cause ?? (row.gnss_trusted ? 'GNSS trusted' : 'GNSS not trusted')}
      </div>
      {reason && <div className="text-xs text-slate-400 mt-1 line-clamp-2" title={reason}>{reason}</div>}
      {row.latch && (
        <div className="text-xs text-amber-300 mt-1">
          spoof latch: GNSS and camera agree {row.latch.agree_s.toFixed(0)}/{row.latch.needed_s} s
        </div>
      )}
    </Card>
  );
};

const AICard: React.FC<{ row: Row }> = ({ row }) => {
  const a = row.ai;
  return (
    <Card title="AI second opinion">
      {!a ? (
        <div className="text-xs text-slate-500 mt-1">no AI model in this session</div>
      ) : (
        <>
          <div className="flex items-baseline gap-2 mt-1">
            <span className={`text-2xl font-bold font-mono ${a.level === 'SUSPECT' ? 'text-amber-300' : 'text-emerald-300'}`}>
              {a.score.toFixed(2)}
            </span>
            <span className={`text-xs font-semibold ${a.level === 'SUSPECT' ? 'text-amber-300' : 'text-slate-400'}`}>
              {a.level} · advisory
            </span>
          </div>
          <div className={`text-[11px] truncate ${a.model.startsWith('provisional') ? 'text-amber-300/90' : 'text-slate-400'}`} title={a.model}>
            model: {a.model}
          </div>
          <div className="text-xs text-slate-300 mt-0.5 line-clamp-2" title={a.reasons[0]}>
            {a.reasons[0]?.replace(/^AI: /, '') ?? 'pattern like the clean training epochs'}
          </div>
        </>
      )}
    </Card>
  );
};

const CameraOffline: React.FC<{ isLive: boolean }> = ({ isLive }) => (
  <div className="px-3 py-2 rounded-xl bg-slate-900/80 border border-slate-800 flex items-center gap-2 text-xs text-slate-400">
    <span className="w-2 h-2 rounded-full bg-slate-500 shrink-0" />
    <span>
      <b className="text-slate-300">Camera offline</b> · {isLive ? 'the Pi sends no camera track' : 'no video in this session'}
    </span>
  </div>
);

// Only the indicators that fire now (score part > 0), as chips; then the detectors' reason text.
const FiringIndicators: React.FC<{ data: DashboardFile; row: Row }> = ({ data, row }) => {
  const chips: Array<{ det: Detector; key: string; label: string; p: number; value: string }> = [];
  for (const det of ['jamming', 'spoofing'] as const) {
    for (const ind of data.config[det].indicators) {
      const p = row[det].parts[ind.key] ?? 0;
      if (p > 0) chips.push({ det, key: ind.key, label: ind.label, p, value: featureValue(det, ind, row) });
    }
    const extra = row[det].parts['ai_corroboration'];
    if (det === 'spoofing' && extra) chips.push({ det, key: 'ai', label: 'AI corroboration', p: extra, value: '' });
  }
  chips.sort((a, b) => b.p - a.p);
  const reasons = [...row.spoofing.reasons, ...row.jamming.reasons];
  return (
    <div className="flex items-center gap-2 min-w-0 text-xs">
      <span className="text-[11px] uppercase tracking-wider font-semibold text-slate-500 shrink-0">Firing</span>
      {chips.length === 0 ? (
        <span className="text-slate-500">no indicator firing{row.warming_up ? ' (warming up)' : ''}</span>
      ) : (
        <div className="flex flex-wrap gap-1.5 min-w-0">
          {chips.map((c) => (
            <span
              key={`${c.det}-${c.key}`}
              title={`${c.det}: ${c.label}${c.value ? ` · value ${c.value}` : ''}`}
              className={`px-2 py-0.5 rounded-full border font-mono ${
                c.det === 'jamming' ? 'border-orange-500/60 bg-orange-950/50 text-orange-200' : 'border-violet-400/60 bg-violet-950/50 text-violet-200'
              }`}
            >
              {c.label} {c.p.toFixed(2)}
            </span>
          ))}
        </div>
      )}
      {reasons.length > 0 && (
        <span className="text-slate-400 truncate min-w-0" title={reasons.join('; ')}>
          · {reasons.slice(0, 2).join('; ')}
        </span>
      )}
    </div>
  );
};

export const DemoView: React.FC<Props> = (p) => {
  const { data, row, history, currentTimeSec, isLive, stale, videoUrl, liveStreamUrl } = p;
  const [clip, setClip] = useState(false);
  useEffect(() => {
    probeClip().then((url) => setClip(!!url));
  }, []);
  const hasCamera = clip || !!videoUrl || (isLive && data.timeline.some((r) => r.vision));
  const h = data.config.hysteresis;
  const short = useShort();
  return (
    <div className={`flex-1 min-h-0 flex flex-col gap-2 p-2 ${stale ? 'opacity-60 grayscale' : ''}`}>
      <div
        className={`flex-1 min-h-0 grid gap-2 ${
          hasCamera ? 'grid-cols-[minmax(250px,25fr)_45fr_30fr]' : 'grid-cols-[minmax(250px,25fr)_75fr]'
        }`}
      >
        <div className="min-h-0 overflow-y-auto flex flex-col gap-2">
          {row.trust ? (
            <TrustCard title="GNSS Trust" t={row.trust.gnss} size={short ? 70 : 96} />
          ) : (
            <Card title="GNSS Trust"><div className="text-xs text-slate-500 mt-1">not in this session</div></Card>
          )}
          {row.trust?.nav && (
            <TrustCard title="Nav confidence (camera)" t={row.trust.nav} size={short ? 52 : 72}
              note={!short && row.trust.nav.since_fix_m != null ? `${fmt(row.trust.nav.since_fix_m, 1, 'm')} since the last absolute fix` : undefined} />
          )}
          <StateCard data={data} row={row} t={currentTimeSec} stale={stale} short={short} />
          <AICard row={row} />
          {!hasCamera && <CameraOffline isLive={isLive} />}
        </div>
        <div className="min-h-0">
          <TacticalMap data={data} row={row} history={history} currentTimeSec={currentTimeSec} focus />
        </div>
        {hasCamera && (
          <div className="min-h-0">
            <DownCamView
              data={data}
              row={row}
              videoUrl={videoUrl}
              liveStreamUrl={liveStreamUrl}
              currentTimeSec={currentTimeSec}
              isPlaying={p.isPlaying}
              playbackSpeed={p.playbackSpeed}
            />
          </div>
        )}
      </div>

      <div className={`shrink-0 bg-slate-900/80 rounded-xl border border-slate-800 px-3 ${short ? 'py-1.5 gap-1' : 'py-2 gap-1.5'} flex flex-col`}>
        {isLive ? (
          <div className="flex items-center gap-3 text-xs font-mono text-slate-400">
            <span className="text-red-300 font-semibold">● LIVE</span>
            <span>{fmtTime(currentTimeSec)} · {row.t_gnss}</span>
            <span>updates every 1 s</span>
          </div>
        ) : (
          <ReplayControls
            data={data}
            row={row}
            currentTimeSec={currentTimeSec}
            isPlaying={p.isPlaying}
            playbackSpeed={p.playbackSpeed}
            onSeek={p.onSeek}
            onTogglePlay={p.onTogglePlay}
            onSetSpeed={p.onSetSpeed}
            onRestart={p.onRestart}
          />
        )}
        <ScoreStrip data={data} t={currentTimeSec} onSeek={p.onSeek} className={short ? 'h-9' : 'h-12'} />
        <div className="flex items-center gap-4 text-[11px] font-mono text-slate-500">
          <span className="text-orange-400">— jamming</span>
          <span className="text-violet-400">— spoofing</span>
          <span>dashed: SUSPECT {h.suspect_on} · ALARM {h.alarm_on}</span>
        </div>
        <FiringIndicators data={data} row={row} />
      </div>
    </div>
  );
};
