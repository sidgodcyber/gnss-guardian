import React, { useMemo, useState } from 'react';
import { DashboardFile } from '../types/gnssGuardian';
import { stateLabel } from '../services/session';

interface Props {
  data: DashboardFile;
  currentTimeSec: number;
  onSeek: (t: number) => void;
}

type Kind = 'STATE' | 'DETECTOR' | 'EVENT';
interface Entry {
  t: number;
  t_gnss?: string;
  kind: Kind;
  title: string;
  detail: string;
}

const kindStyle: Record<Kind, string> = {
  STATE: 'bg-sky-950/60 text-sky-200 border-sky-600/60',
  DETECTOR: 'bg-amber-950/60 text-amber-200 border-amber-600/60',
  EVENT: 'bg-slate-800 text-slate-300 border-slate-600',
};

export const IncidentLogView: React.FC<Props> = ({ data, currentTimeSec, onSeek }) => {
  const [filter, setFilter] = useState<'ALL' | Kind>('ALL');

  const entries = useMemo(() => {
    const out: Entry[] = [];
    for (const tr of data.transitions) {
      out.push({
        t: tr.t,
        t_gnss: tr.t_gnss,
        kind: 'STATE',
        title: `${tr.from ? stateLabel(tr.from) : 'start'} → ${stateLabel(tr.state)}`,
        detail: [tr.reasons.join('; '), tr.indicators.length ? `indicators: ${tr.indicators.join(', ')}` : '']
          .filter(Boolean)
          .join(' · '),
      });
    }
    for (const l of data.detector_levels) {
      out.push({
        t: l.t,
        t_gnss: l.t_gnss,
        kind: 'DETECTOR',
        title: `${l.detector} → ${l.level} (${l.score.toFixed(2)})`,
        detail: l.reasons.join('; '),
      });
    }
    for (const e of data.events) {
      if (e.t !== null) out.push({ t: e.t, kind: 'EVENT', title: e.kind.replace(/_/g, ' '), detail: e.detail });
    }
    const order: Record<Kind, number> = { EVENT: 0, DETECTOR: 1, STATE: 2 };
    return out.sort((a, b) => a.t - b.t || order[a.kind] - order[b.kind]);
  }, [data]);

  const shown = entries.filter((e) => filter === 'ALL' || e.kind === filter);
  const currentIdx = shown.reduce((acc, e, i) => (e.t <= currentTimeSec ? i : acc), -1);

  return (
    <div className="flex flex-col bg-slate-900/90 border border-slate-800 rounded-xl p-5 gap-5 shadow-xl">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800">
        <div>
          <h2 className="text-base font-semibold text-slate-100">Flight Logs</h2>
          <div className="text-xs text-slate-400 mt-0.5">
            {data.transitions.filter((t) => t.state).length} state transitions · {data.detector_levels.length} detector level
            changes · {data.keyframes.length} keyframes · times in seconds from the first epoch (host clock)
          </div>
        </div>
        <div className="flex items-center bg-slate-950 p-1 rounded-lg border border-slate-800 text-xs font-mono">
          {(['ALL', 'STATE', 'DETECTOR', 'EVENT'] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1 rounded ${filter === f ? 'bg-sky-600 text-white font-bold' : 'text-slate-400 hover:text-slate-200'}`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs font-mono">
          <thead>
            <tr className="border-b border-slate-800 text-slate-400 text-[11px] uppercase tracking-wider">
              <th className="pb-2 pr-3">t (s)</th>
              <th className="pb-2 pr-3">GNSS time</th>
              <th className="pb-2 pr-3">Type</th>
              <th className="pb-2 pr-3">Entry</th>
              <th className="pb-2 pr-3">Detail</th>
              <th className="pb-2 text-right" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60">
            {shown.map((e, i) => (
              <tr key={i} className={i === currentIdx ? 'bg-sky-950/30' : ''}>
                <td className="py-2 pr-3 text-sky-400 font-bold">{e.t.toFixed(1)}</td>
                <td className="py-2 pr-3 text-slate-400 whitespace-nowrap">{e.t_gnss ?? ''}</td>
                <td className="py-2 pr-3">
                  <span className={`px-1.5 py-0.5 rounded text-[10px] border ${kindStyle[e.kind]}`}>{e.kind}</span>
                </td>
                <td className="py-2 pr-3 text-slate-100 whitespace-nowrap">{e.title}</td>
                <td className="py-2 pr-3 text-slate-300 font-sans">{e.detail}</td>
                <td className="py-2 text-right">
                  <button
                    onClick={() => onSeek(e.t)}
                    className="px-2 py-1 text-[11px] bg-slate-800 hover:bg-slate-700 text-sky-300 rounded border border-slate-700"
                  >
                    jump
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
