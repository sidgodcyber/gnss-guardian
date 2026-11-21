import React, { useEffect, useMemo, useRef, useState } from 'react';
import { DashboardFile, Row } from '../types/gnssGuardian';
import { fmt, lastRouteAt } from '../services/session';

interface TacticalMapProps {
  data: DashboardFile;
  row: Row;
  history: Row[];
  currentTimeSec: number;
}

const GRID_STEPS = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];

export const TacticalMap: React.FC<TacticalMapProps> = ({ data, row, history, currentTimeSec }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [zoomFactor, setZoomFactor] = useState(1);
  const [size, setSize] = useState({ w: 0, h: 0 });

  // Extent of the whole session (both tracks, keyframes, home) so the view does not jump while playing.
  const extent = useMemo(() => {
    const xs = [0];
    const ys = [0];
    for (const r of data.timeline) {
      if (r.gnss.e != null && r.gnss.n != null) {
        xs.push(r.gnss.e);
        ys.push(r.gnss.n);
      }
      if (r.vision?.e != null && r.vision?.n != null) {
        xs.push(r.vision.e);
        ys.push(r.vision.n);
      }
    }
    for (const k of data.keyframes) {
      xs.push(k.e);
      ys.push(k.n);
    }
    for (const r of data.routes ?? []) {
      for (const w of r.waypoints) {
        xs.push(w.e);
        ys.push(w.n);
      }
    }
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    return { cx: (minX + maxX) / 2, cy: (minY + maxY) / 2, w: Math.max(10, maxX - minX), h: Math.max(10, maxY - minY) };
  }, [data]);

  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ro = new ResizeObserver(() => setSize({ w: c.clientWidth, h: c.clientHeight }));
    ro.observe(c);
    return () => ro.disconnect();
  }, []);

  const scale = size.w && size.h ? Math.min(size.w / (extent.w * 1.25), size.h / (extent.h * 1.25)) * zoomFactor : 1;

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx || !size.w || !size.h) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.w * dpr;
    canvas.height = size.h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = size.w, H = size.h;
    const toPx = (e: number, n: number) => ({ x: W / 2 + (e - extent.cx) * scale, y: H / 2 - (n - extent.cy) * scale });
    const toM = (x: number, y: number) => ({ e: extent.cx + (x - W / 2) / scale, n: extent.cy - (y - H / 2) / scale });

    ctx.fillStyle = '#090d16';
    ctx.fillRect(0, 0, W, H);

    const step = GRID_STEPS.find((s) => s * scale >= 45) ?? 1000;
    const tl = toM(0, 0), br = toM(W, H);
    ctx.font = '9px monospace';
    ctx.lineWidth = 1;
    for (let e = Math.ceil(tl.e / step) * step; e <= br.e; e += step) {
      const { x } = toPx(e, 0);
      ctx.strokeStyle = Math.abs(e) < 1e-9 ? '#334155' : '#152033';
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, H);
      ctx.stroke();
      ctx.fillStyle = '#475569';
      ctx.fillText(`${+e.toFixed(1)} m`, x + 2, H - 4);
    }
    for (let n = Math.ceil(br.n / step) * step; n <= tl.n; n += step) {
      const { y } = toPx(0, n);
      ctx.strokeStyle = Math.abs(n) < 1e-9 ? '#334155' : '#152033';
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
      ctx.fillStyle = '#475569';
      ctx.fillText(`${+n.toFixed(1)} m`, 2, y - 2);
    }

    const line = (pts: Array<{ e: number; n: number }>, color: string, width: number) => {
      if (pts.length < 2) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const q = toPx(p.e, p.n);
        if (i) ctx.lineTo(q.x, q.y);
        else ctx.moveTo(q.x, q.y);
      });
      ctx.stroke();
    };
    line(
      history.filter((r) => r.gnss.e != null && r.gnss.n != null).map((r) => ({ e: r.gnss.e!, n: r.gnss.n! })),
      '#10b981',
      2.5
    );
    line(
      history.filter((r) => r.vision?.e != null && r.vision?.n != null).map((r) => ({ e: r.vision!.e!, n: r.vision!.n! })),
      '#0ea5e9',
      2.5
    );

    const route = row.guidance ? lastRouteAt(data.routes ?? [], currentTimeSec) : undefined;
    if (route) {
      ctx.setLineDash([6, 4]);
      line(route.waypoints, '#a78bfa', 1.5);
      ctx.setLineDash([]);
    }

    for (const k of data.keyframes) {
      if (k.t === null || k.t > currentTimeSec) continue;
      const q = toPx(k.e, k.n);
      ctx.fillStyle = k.home ? '#e0f2fe' : '#38bdf8';
      ctx.fillRect(q.x - 2.5, q.y - 2.5, 5, 5);
    }

    const home = toPx(0, 0);
    ctx.strokeStyle = '#e2e8f0';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(home.x, home.y, 9, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = '#e2e8f0';
    ctx.font = 'bold 10px monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('H', home.x, home.y);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';

    const marker = (e: number | null, n: number | null, color: string) => {
      if (e == null || n == null) return;
      const q = toPx(e, n);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(q.x, q.y, 5.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    };
    marker(row.gnss.e, row.gnss.n, '#10b981');
    marker(row.vision?.e ?? null, row.vision?.n ?? null, '#0ea5e9');

    // fused position (ring) and, while guiding, an arrow to the next waypoint
    if (row.fused) {
      const q = toPx(row.fused.e, row.fused.n);
      ctx.strokeStyle = '#f5d0fe';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(q.x, q.y, 9, 0, Math.PI * 2);
      ctx.stroke();
      if (row.guidance) {
        const w = toPx(row.guidance.waypoint.e, row.guidance.waypoint.n);
        const a = Math.atan2(w.y - q.y, w.x - q.x);
        ctx.strokeStyle = '#c4b5fd';
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(q.x, q.y);
        ctx.lineTo(w.x, w.y);
        ctx.lineTo(w.x - 8 * Math.cos(a - 0.4), w.y - 8 * Math.sin(a - 0.4));
        ctx.moveTo(w.x, w.y);
        ctx.lineTo(w.x - 8 * Math.cos(a + 0.4), w.y - 8 * Math.sin(a + 0.4));
        ctx.stroke();
      }
    }
  }, [data, row, history, currentTimeSec, size, scale, extent]);

  const v = row.vision;
  const sep =
    row.gnss.e != null && row.gnss.n != null && v?.e != null && v?.n != null
      ? Math.hypot(row.gnss.e - v.e, row.gnss.n - v.n)
      : null;
  const homeAt = data.events.find((e) => e.kind === 'home_set')?.t;

  return (
    <div className="relative w-full h-full bg-slate-950 rounded-xl overflow-hidden border border-slate-800 shadow-2xl flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 bg-slate-900/95 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-200">Tactical Map</span>
          <span className="text-[10px] text-slate-400 bg-slate-800 px-2 py-0.5 rounded font-mono">metres from home (east, north)</span>
        </div>
        <div className="flex items-center bg-slate-800/90 rounded border border-slate-700 text-xs">
          <button onClick={() => setZoomFactor((z) => Math.min(z * 1.5, 20))} className="px-2 py-0.5 text-slate-300" title="Zoom in">
            +
          </button>
          <button onClick={() => setZoomFactor(1)} className="px-2 py-0.5 text-[10px] text-slate-400 border-x border-slate-700">
            fit
          </button>
          <button onClick={() => setZoomFactor((z) => Math.max(z / 1.5, 0.2))} className="px-2 py-0.5 text-slate-300" title="Zoom out">
            −
          </button>
        </div>
      </div>

      <div className="relative flex-1">
        <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" />

        <div className="absolute bottom-3 left-3 flex flex-col gap-1 p-2 bg-slate-950/90 border border-slate-800 rounded-lg text-[11px] font-mono text-slate-300">
          <div className="flex items-center gap-2"><span className="w-3 h-0.5 bg-emerald-500" />GNSS track</div>
          <div className="flex items-center gap-2"><span className="w-3 h-0.5 bg-sky-500" />vision track</div>
          <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full border-2 border-fuchsia-200" />fused position</div>
          <div className="flex items-center gap-2"><span className="w-3 border-t border-dashed border-violet-400" />return route (advisory)</div>
          <div className="flex items-center gap-2"><span className="w-2 h-2 bg-sky-400" />keyframe ({data.keyframes.filter((k) => k.t !== null && k.t <= currentTimeSec).length})</div>
          <div className="flex items-center gap-2"><span className="w-3 h-3 rounded-full border border-slate-200 text-[7px] leading-3 text-center">H</span>home (0, 0)</div>
        </div>

        <div className="absolute top-3 right-3 p-2.5 bg-slate-950/90 border border-slate-800 rounded-lg text-[11px] font-mono text-slate-300 space-y-1">
          <div>
            GNSS <span className="text-emerald-400">{fmt(row.gnss.e, 1)}, {fmt(row.gnss.n, 1)} m</span>
          </div>
          <div>
            vision <span className="text-sky-400">{fmt(v?.e, 1)}, {fmt(v?.n, 1)} m</span>
          </div>
          <div className="pt-1 border-t border-slate-800">GNSS–vision gap: {fmt(sep, 2, 'm')}</div>
          {row.guidance && (
            <div className="pt-1 border-t border-slate-800 text-violet-300">
              home {fmt(row.guidance.home_distance_m, 1, 'm')} at {fmt(row.guidance.home_bearing_deg, 0)}°
              <br />
              next {row.guidance.waypoint.id === 'home' ? 'home' : `kf ${row.guidance.waypoint.id}`}{' '}
              {fmt(row.guidance.waypoint.distance_m, 1, 'm')} at {fmt(row.guidance.waypoint.bearing_deg, 0)}°
            </div>
          )}
        </div>

        {(homeAt == null || currentTimeSec < homeAt) && (
          <div className="absolute top-3 left-3 max-w-xs p-2.5 bg-slate-900/95 border border-slate-700 rounded-lg text-[11px] text-slate-300">
            Home point not set at this time: tracks start once it is
            {homeAt != null ? ` (t = ${homeAt.toFixed(0)} s)` : ' (never set in this session)'}.
          </div>
        )}
      </div>
    </div>
  );
};
