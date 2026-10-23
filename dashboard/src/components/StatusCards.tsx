import React from 'react';
import { Level, Row } from '../types/gnssGuardian';
import { fmt, stateLabel } from '../services/session';

const levelText: Record<Level, string> = {
  CLEAR: 'text-emerald-300',
  SUSPECT: 'text-amber-300',
  ALARM: 'text-red-300',
};

const stateText: Record<string, string> = {
  GNSS_OK: 'text-emerald-300',
  SUSPECT: 'text-amber-300',
  JAMMED: 'text-red-300',
  SPOOFED: 'text-red-300',
  VISION_NAV: 'text-sky-300',
  'WARM-UP': 'text-slate-300',
};

const Card: React.FC<{ label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }> = ({
  label,
  value,
  sub,
  tone = 'text-slate-100',
}) => (
  <div className="p-3 rounded-lg bg-slate-900/80 border border-slate-800 min-w-0">
    <div className="text-[10px] uppercase tracking-wider font-semibold text-slate-500">{label}</div>
    <div className={`text-base font-mono font-bold mt-1 truncate ${tone}`}>{value}</div>
    {sub !== undefined && <div className="text-[10px] font-mono text-slate-400 mt-0.5 truncate">{sub}</div>}
  </div>
);

export const StatusCards: React.FC<{ row: Row }> = ({ row }) => {
  const { gnss, jamming, spoofing, vision } = row;
  const st = stateLabel(row.state);
  const nav =
    row.state === 'VISION_NAV' && vision?.e != null && vision?.n != null
      ? Math.hypot(vision.e, vision.n)
      : gnss.e != null && gnss.n != null
      ? Math.hypot(gnss.e, gnss.n)
      : null;
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3">
      <Card label="State" value={st} tone={stateText[st]} sub={row.gnss_trusted ? 'GNSS trusted' : 'GNSS not trusted'} />
      <Card
        label="GNSS fix"
        value={gnss.fix_valid ? 'valid' : 'no fix'}
        tone={gnss.fix_valid ? 'text-slate-100' : 'text-red-300'}
        sub={`${gnss.sats_used ?? '–'} used / ${gnss.sats_tracked ?? '–'} tracked`}
      />
      <Card label="HDOP" value={fmt(gnss.hdop, 1)} />
      <Card label="Mean C/N0" value={fmt(gnss.cn0_mean, 1)} sub="dB-Hz" />
      <Card label="Jamming" value={jamming.level} tone={levelText[jamming.level]} sub={`score ${fmt(jamming.score, 2)}`} />
      <Card label="Spoofing" value={spoofing.level} tone={levelText[spoofing.level]} sub={`score ${fmt(spoofing.score, 2)}`} />
      <Card
        label="Vision"
        value={vision ? vision.mode.replace(/_/g, ' ') : 'no camera'}
        tone={vision?.mode === 'dead_reckoning' ? 'text-sky-300' : 'text-slate-100'}
        sub={vision ? `${vision.keyframes} keyframes` : undefined}
      />
      <Card label="From home" value={fmt(nav, 1, 'm')} sub={row.state === 'VISION_NAV' ? 'vision position' : 'GNSS position'} />
    </div>
  );
};
