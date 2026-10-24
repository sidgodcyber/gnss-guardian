import React, { useMemo } from 'react';
import { DashboardFile, Detector, GuardianState, IndicatorDef, Level, Row } from '../types/gnssGuardian';
import { fmt, fmtTime, stateLabel } from '../services/session';

interface Props {
  data: DashboardFile;
  row: Row;
  currentTimeSec: number;
  isPlaying: boolean;
  playbackSpeed: number;
  onSeek: (t: number) => void;
  onTogglePlay: () => void;
  onSetSpeed: (s: number) => void;
  onRestart: () => void;
}

const markerColor = (s: GuardianState | null) =>
  s === 'GNSS_OK'
    ? 'bg-emerald-500 border-emerald-300'
    : s === 'SUSPECT'
    ? 'bg-amber-500 border-amber-300'
    : s === 'JAMMED' || s === 'SPOOFED'
    ? 'bg-red-500 border-red-300'
    : s === 'VISION_NAV'
    ? 'bg-sky-500 border-sky-300'
    : s === 'RETURN_HOME'
    ? 'bg-violet-500 border-violet-300'
    : s === 'HOME_REACHED'
    ? 'bg-teal-500 border-teal-300'
    : 'bg-slate-500 border-slate-300';

const levelStyle: Record<Level, string> = {
  CLEAR: 'bg-emerald-950/70 border-emerald-500/50 text-emerald-300',
  SUSPECT: 'bg-amber-950/80 border-amber-500 text-amber-200',
  ALARM: 'bg-red-950/90 border-red-500 text-red-200',
};

// Raw value the detector ramps for each indicator (same inputs as gnss_guardian/detect/*.py).
function featureValue(det: Detector, ind: IndicatorDef, row: Row): string {
  const f = row.features;
  const num = (k: string) => (typeof f[k] === 'number' ? (f[k] as number) : null);
  if (ind.key === 'fix_lost') return f.fix_lost ? 'yes' : 'no';
  if (det === 'spoofing' && ind.key === 'speed') {
    const v = Math.max(num('implied_speed_mps') ?? 0, num('sog_mps') ?? 0);
    return fmt(v, 1, 'm/s');
  }
  if (ind.key === 'vision') {
    return `${fmt(num('vision_diff_mps'), 2)} / ${fmt(num('vision_drift_mps'), 2)} m/s`;
  }
  const v = num(ind.feature);
  return v === null ? '–' : `${v.toFixed(ind.unit === 'fraction' ? 2 : 1)}${ind.unit && ind.unit !== 'fraction' ? ` ${ind.unit}` : ''}`;
}

const ScoreStrip: React.FC<{ data: DashboardFile; t: number; onSeek: (t: number) => void }> = ({ data, t, onSeek }) => {
  const W = 1000;
  const H = 70;
  const dur = Math.max(1, data.meta.duration_s);
  const h = data.config.hysteresis;
  const path = (det: Detector) =>
    data.timeline
      .map((r, i) => `${i ? 'L' : 'M'}${((r.t / dur) * W).toFixed(1)},${(H - r[det].score * H).toFixed(1)}`)
      .join(' ');
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="w-full h-16 bg-slate-950 rounded border border-slate-800 cursor-pointer"
      onClick={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        onSeek(((e.clientX - r.left) / r.width) * dur);
      }}
    >
      {[h.suspect_on, h.alarm_on].map((y, i) => (
        <line key={i} x1={0} x2={W} y1={H - y * H} y2={H - y * H} stroke={i ? '#7f1d1d' : '#78350f'} strokeDasharray="6 6" />
      ))}
      <path d={path('jamming')} fill="none" stroke="#f97316" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      <path d={path('spoofing')} fill="none" stroke="#a78bfa" strokeWidth={2} vectorEffect="non-scaling-stroke" />
      <line x1={(t / dur) * W} x2={(t / dur) * W} y1={0} y2={H} stroke="#e2e8f0" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
    </svg>
  );
};

const DetectorCard: React.FC<{ det: Detector; data: DashboardFile; row: Row }> = ({ det, data, row }) => {
  const d = row[det];
  const cfg = data.config[det];
  const fired = cfg.indicators.filter((i) => (d.parts[i.key] ?? 0) > 0).length;
  return (
    <div className="p-3 rounded-lg bg-slate-950/70 border border-slate-800 flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wider font-semibold text-slate-300">
          {det === 'jamming' ? 'Jamming / blockage' : 'Spoofing'} indicators
        </span>
        <span className="text-[10px] font-mono text-slate-400">
          {cfg.combine} · {fired}/{cfg.indicators.length} fired
        </span>
      </div>
      <div className={`px-2 py-1 rounded border font-mono text-xs flex justify-between ${levelStyle[d.level]}`}>
        <span className="font-bold">{d.level}</span>
        <span>score {d.score.toFixed(2)}</span>
      </div>
      <div className="space-y-1">
        {cfg.indicators.map((ind) => {
          const p = d.parts[ind.key];
          const on = (p ?? 0) > 0;
          return (
            <div
              key={ind.key}
              className={`px-2 py-1 rounded border text-[11px] font-mono ${
                on ? 'bg-red-950/40 border-red-500/40 text-red-100' : 'bg-slate-900/60 border-slate-800/80 text-slate-400'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate" title={ind.label}>
                  {ind.label}
                </span>
                <span className="shrink-0">{p === undefined || p === null ? 'n/a' : p.toFixed(2)}</span>
              </div>
              <div className="h-1 mt-1 bg-slate-800 rounded overflow-hidden">
                <div className={`h-full ${on ? 'bg-red-400' : 'bg-slate-600'}`} style={{ width: `${Math.min(1, p ?? 0) * 100}%` }} />
              </div>
              <div className="flex justify-between text-[10px] text-slate-500 mt-0.5">
                <span>value {featureValue(det, ind, row)}</span>
                <span>
                  {ind.span ? `ramp ${ind.span[0]}→${ind.span[1]}` : 'on/off'} · w {ind.weight}
                </span>
              </div>
            </div>
          );
        })}
      </div>
      {d.reasons.length > 0 && (
        <div className="text-[10px] text-slate-300 font-mono border-t border-slate-800 pt-1.5">{d.reasons.join('; ')}</div>
      )}
    </div>
  );
};

export const DetectionIndicatorsPanel: React.FC<Props> = ({
  data,
  row,
  currentTimeSec,
  isPlaying,
  playbackSpeed,
  onSeek,
  onTogglePlay,
  onSetSpeed,
  onRestart,
}) => {
  const dur = Math.max(0.001, data.meta.duration_s);
  const markers = useMemo(() => data.transitions.filter((tr) => tr.state !== null && tr.state !== 'WARMING_UP'), [data]);
  const h = data.config.hysteresis;

  return (
    <div className="bg-slate-900/90 rounded-xl border border-slate-800 p-4 shadow-xl flex flex-col gap-4">
      <div className="bg-slate-950/80 rounded-lg p-3 border border-slate-800/90 flex flex-col gap-2.5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <button
              onClick={onTogglePlay}
              className={`px-3 py-1.5 rounded-md font-semibold text-xs text-white ${
                isPlaying ? 'bg-amber-600 hover:bg-amber-500' : 'bg-emerald-600 hover:bg-emerald-500'
              }`}
            >
              {isPlaying ? 'PAUSE' : 'PLAY'}
            </button>
            <button
              onClick={onRestart}
              className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-md text-xs border border-slate-700"
            >
              RESTART
            </button>
            <div className="flex items-center bg-slate-900 rounded p-0.5 border border-slate-800 ml-1">
              {[0.5, 1, 2].map((s) => (
                <button
                  key={s}
                  onClick={() => onSetSpeed(s)}
                  className={`px-2 py-0.5 text-[11px] font-mono rounded ${
                    playbackSpeed === s ? 'bg-sky-600 text-white font-bold' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  {s}×
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-3 font-mono text-xs">
            <span className="text-sky-400 font-semibold">{fmtTime(currentTimeSec)}</span>
            <span className="text-slate-500">/ {fmtTime(dur)}</span>
            <span className="text-slate-400">{row.t_gnss}</span>
            <span className="px-2 py-0.5 rounded text-[10px] uppercase border border-slate-600 text-slate-200">
              {stateLabel(row.state)}
            </span>
          </div>
        </div>

        <div className="relative w-full pt-1">
          <input
            type="range"
            min={0}
            max={dur}
            step={0.1}
            value={currentTimeSec}
            onChange={(e) => onSeek(parseFloat(e.target.value))}
            className="w-full h-2 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-sky-500"
            aria-label="Replay position"
          />
          <div className="relative h-4 w-full mt-1">
            {markers.map((tr, i) => (
              <button
                key={i}
                onClick={() => onSeek(tr.t)}
                style={{ left: `${(tr.t / dur) * 100}%` }}
                className="absolute -top-1.5 -translate-x-1/2 group"
                title={`${tr.state} at ${tr.t.toFixed(1)} s (${tr.t_gnss})${tr.reasons.length ? ': ' + tr.reasons.join('; ') : ''}`}
              >
                <div className={`w-2 h-3.5 rounded-xs border ${markerColor(tr.state)} ${currentTimeSec >= tr.t ? '' : 'opacity-40'}`} />
              </button>
            ))}
          </div>
        </div>

        <ScoreStrip data={data} t={currentTimeSec} onSeek={onSeek} />
        <div className="flex flex-wrap gap-4 text-[10px] font-mono text-slate-500">
          <span className="text-orange-400">— jamming score</span>
          <span className="text-violet-400">— spoofing score</span>
          <span>dashed: SUSPECT on {h.suspect_on} · ALARM on {h.alarm_on}</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-12 gap-3">
        <div className="lg:col-span-3 p-3 rounded-lg bg-slate-950/70 border border-slate-800 text-[11px] font-mono text-slate-300 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider text-slate-400 font-semibold font-sans">Hysteresis (config)</div>
          {row.warming_up && <div className="text-amber-300">warming up: no verdicts until the baseline is ready</div>}
          <div>SUSPECT at score ≥ {h.suspect_on}; back to CLEAR after {h.suspect_hold_s} s below {h.suspect_off}</div>
          <div>
            ALARM on {h.alarm_k} epochs ≥ {h.alarm_on} or 1 epoch ≥ {h.alarm_immediate}
          </div>
          <div>
            leave ALARM after {h.alarm_hold_s} s below {h.alarm_off}
          </div>
          <div>
            minimum ALARM: {Object.entries(h.min_alarm_s).map(([k, v]) => `${k} ${v} s`).join(', ')}
          </div>
          <div className="text-slate-500 pt-1 border-t border-slate-800">from config/default.yaml at export time</div>
        </div>
        <div className="lg:col-span-4">
          <DetectorCard det="jamming" data={data} row={row} />
        </div>
        <div className="lg:col-span-5">
          <DetectorCard det="spoofing" data={data} row={row} />
        </div>
      </div>
    </div>
  );
};
