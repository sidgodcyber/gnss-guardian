import React from 'react';
import { Row } from '../types/gnssGuardian';
import { fmt, stateLabel } from '../services/session';

const Cell: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className="p-2.5 bg-slate-900/80 rounded-lg border border-slate-800 min-w-0">
    <div className="text-[10px] uppercase font-semibold text-slate-500">{label}</div>
    <div className="text-sm font-mono font-bold mt-1 text-slate-100 truncate">{value}</div>
  </div>
);

export const SensorFusionView: React.FC<{ row: Row }> = ({ row }) => {
  const { gnss, vision: v, features: f } = row;
  const num = (k: string) => (typeof f[k] === 'number' ? (f[k] as number) : null);
  return (
    <div className="flex flex-col bg-slate-900/90 border border-slate-800 rounded-xl p-5 gap-5 shadow-xl">
      <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800">
        <div>
          <h2 className="text-base font-semibold text-slate-100">Sensor Fusion / Integrity Monitor</h2>
          <div className="text-xs text-slate-400 mt-0.5">GNSS receiver and down-camera values from this epoch of the replay</div>
        </div>
        <div className="flex gap-2 text-xs font-mono">
          <span className="px-3 py-1 rounded-md border bg-slate-950 border-slate-800 text-slate-300">
            state <b className="text-sky-400">{stateLabel(row.state)}</b>
          </span>
          <span className="px-3 py-1 rounded-md border bg-slate-950 border-slate-800 text-slate-300">
            GNSS {row.warming_up ? 'warming up' : row.gnss_trusted ? 'trusted' : 'not trusted'}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        <div className="p-4 rounded-xl bg-slate-950/70 border border-slate-800">
          <div className="text-xs font-bold uppercase tracking-wider text-emerald-400 mb-3">GNSS receiver (L86)</div>
          <div className="grid grid-cols-2 gap-3">
            <Cell label="Fix" value={gnss.fix_valid ? 'valid' : 'no fix'} />
            <Cell label="Satellites used / tracked" value={`${gnss.sats_used ?? '–'} / ${gnss.sats_tracked ?? '–'}`} />
            <Cell label="HDOP" value={fmt(gnss.hdop, 2)} />
            <Cell label="Mean C/N0" value={fmt(gnss.cn0_mean, 1, 'dB-Hz')} />
            <Cell label="Mean C/N0 drop" value={fmt(num('mean_drop_db'), 1, 'dB')} />
            <Cell label="GNSS time jump" value={fmt(num('time_jump_s'), 2, 's')} />
          </div>
        </div>

        <div className="p-4 rounded-xl bg-slate-950/70 border border-slate-800">
          <div className="text-xs font-bold uppercase tracking-wider text-sky-400 mb-3">Vision (down camera)</div>
          {v ? (
            <div className="grid grid-cols-2 gap-3">
              <Cell label="Mode" value={v.mode.replace(/_/g, ' ')} />
              <Cell label="Quality / inliers" value={`${fmt(v.quality, 2)} / ${v.n_inliers ?? '–'}`} />
              <Cell label="Velocity (east, north)" value={`${fmt(v.v_e, 2)}, ${fmt(v.v_n, 2)} m/s`} />
              <Cell label="Keyframes" value={v.keyframes} />
              <Cell label="GNSS vs vision (3 s)" value={fmt(num('vision_diff_mps'), 2, 'm/s')} />
              <Cell label="GNSS vs vision drift (10 s)" value={fmt(num('vision_drift_mps'), 2, 'm/s')} />
            </div>
          ) : (
            <div className="text-xs text-slate-500">No camera in this session: replay ran without --video.</div>
          )}
        </div>
      </div>
    </div>
  );
};
