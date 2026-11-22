import React, { useCallback, useEffect, useRef, useState } from 'react';

// Browser prototype of the Pi's camera pipeline, for the demo: the laptop webcam, corners tracked frame to
// frame (green points, flow lines), the median flow integrated into a path, keyframes saved in this browser
// (IndexedDB; nothing leaves the machine), and a simple place recognition: the live frame matched against the
// saved keyframes by normalised cross-correlation of a small grey thumbnail. Not the Pi's vision code.

const W = 320, H = 240;           // processing size
const CELL = 20;                  // one corner per CELL x CELL cell at most (spread over the image)
const MAX_PTS = 120;
const PATCH = 4;                  // 9 x 9 patch
const SEARCH = 10;                // +-10 px search window around the predicted position
const MAX_MAD = 22;               // mean abs difference above this: track lost
const KEY_EVERY_PX = 90;          // auto keyframe after this much image motion
const MAX_KEYS = 40;
const DESC_W = 32, DESC_H = 24;   // place-recognition thumbnail
const MATCH_MIN = 0.82;           // NCC needed to call a match
const PROC_FPS = 15;

type Pt = { x: number; y: number };
type Track = { p: Pt; prev: Pt; ok: boolean };
type Key = { id: number; t: number; url: string; blob: Blob; desc: Float32Array };

// ---------------------------------------------------------------- image helpers
function toGray(img: ImageData, out: Uint8ClampedArray) {
  const d = img.data;
  for (let i = 0, j = 0; j < out.length; i += 4, j++) out[j] = (d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8;
}

// Shi-Tomasi: minimum eigenvalue of the 5x5 structure tensor, best pixel per cell.
function corners(g: Uint8ClampedArray, keep: Pt[]): Pt[] {
  const n = W * H;
  const xx = new Float32Array(n), yy = new Float32Array(n), xy = new Float32Array(n);
  for (let y = 1; y < H - 1; y++)
    for (let x = 1; x < W - 1; x++) {
      const i = y * W + x;
      const ix = g[i + 1] - g[i - 1], iy = g[i + W] - g[i - W];
      xx[i] = ix * ix; yy[i] = iy * iy; xy[i] = ix * iy;
    }
  const taken = new Set(keep.map((p) => `${Math.floor(p.x / CELL)},${Math.floor(p.y / CELL)}`));
  const out: Array<Pt & { s: number }> = [];
  const m = PATCH + SEARCH + 2;
  for (let cy = 0; cy < H / CELL; cy++)
    for (let cx = 0; cx < W / CELL; cx++) {
      if (taken.has(`${cx},${cy}`)) continue;
      let best = 0, bx = 0, by = 0;
      for (let y = Math.max(m, cy * CELL + 2); y < Math.min(H - m, cy * CELL + CELL - 2); y += 2)
        for (let x = Math.max(m, cx * CELL + 2); x < Math.min(W - m, cx * CELL + CELL - 2); x += 2) {
          let a = 0, b = 0, c = 0;
          for (let dy = -2; dy <= 2; dy++)
            for (let dx = -2; dx <= 2; dx++) {
              const i = (y + dy) * W + x + dx;
              a += xx[i]; c += yy[i]; b += xy[i];
            }
          const l = (a + c) / 2 - Math.sqrt(((a - c) / 2) ** 2 + b * b);
          if (l > best) { best = l; bx = x; by = y; }
        }
      if (best > 6000) out.push({ x: bx, y: by, s: best });
    }
  return out.sort((p, q) => q.s - p.s).slice(0, Math.max(0, MAX_PTS - keep.length)).map(({ x, y }) => ({ x, y }));
}

// Block matching: the patch around p in prev, best SAD within +-SEARCH of p + (ox, oy) in cur (the shift
// predicted from the last frame's median flow, so smooth fast motion stays inside the window).
function track(prev: Uint8ClampedArray, cur: Uint8ClampedArray, p: Pt, ox = 0, oy = 0): Pt | null {
  const px = Math.round(p.x), py = Math.round(p.y);
  const m = PATCH + SEARCH;
  if (px < PATCH || py < PATCH || px >= W - PATCH || py >= H - PATCH) return null;
  if (px + ox < m || py + oy < m || px + ox >= W - m || py + oy >= H - m) return null;
  let best = Infinity, bx = 0, by = 0;
  for (let sy = oy - SEARCH; sy <= oy + SEARCH; sy++)
    for (let sx = ox - SEARCH; sx <= ox + SEARCH; sx++) {
      let sad = 0;
      for (let dy = -PATCH; dy <= PATCH && sad < best; dy++) {
        const r0 = (py + dy) * W + px, r1 = (py + dy + sy) * W + px + sx;
        for (let dx = -PATCH; dx <= PATCH; dx++) sad += Math.abs(prev[r0 + dx] - cur[r1 + dx]);
      }
      if (sad < best) { best = sad; bx = sx; by = sy; }
    }
  if (best / ((2 * PATCH + 1) ** 2) > MAX_MAD) return null;
  return { x: px + bx, y: py + by };
}

function describe(g: Uint8ClampedArray): Float32Array {
  const d = new Float32Array(DESC_W * DESC_H);
  const sx = W / DESC_W, sy = H / DESC_H;
  for (let y = 0; y < DESC_H; y++)
    for (let x = 0; x < DESC_W; x++) {
      let s = 0;
      for (let yy = 0; yy < sy; yy++) for (let xx = 0; xx < sx; xx++) s += g[(y * sy + yy) * W + x * sx + xx];
      d[y * DESC_W + x] = s / (sx * sy);
    }
  let mean = 0;
  for (const v of d) mean += v;
  mean /= d.length;
  let ss = 0;
  for (let i = 0; i < d.length; i++) { d[i] -= mean; ss += d[i] * d[i]; }
  const sd = Math.sqrt(ss / d.length) || 1;
  for (let i = 0; i < d.length; i++) d[i] /= sd;
  return d;
}

const ncc = (a: Float32Array, b: Float32Array) => {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s / a.length;
};

const median = (xs: number[]) => {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : 0;
};

// ---------------------------------------------------------------- browser storage (IndexedDB)
const DB = 'gnss-guardian-webcam', STORE = 'keyframes';
function openDb(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    try {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'id' });
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}
async function dbAll(db: IDBDatabase | null): Promise<Array<{ id: number; t: number; blob: Blob; desc: number[] }>> {
  if (!db) return [];
  return new Promise((resolve) => {
    try {
      const q = db.transaction(STORE).objectStore(STORE).getAll();
      q.onsuccess = () => resolve(q.result ?? []);
      q.onerror = () => resolve([]);
    } catch {
      resolve([]);
    }
  });
}
function dbPut(db: IDBDatabase | null, v: { id: number; t: number; blob: Blob; desc: number[] }) {
  try { db?.transaction(STORE, 'readwrite').objectStore(STORE).put(v); } catch { /* memory only */ }
}
function dbDelete(db: IDBDatabase | null, id: number) {
  try { db?.transaction(STORE, 'readwrite').objectStore(STORE).delete(id); } catch { /* memory only */ }
}
function dbClear(db: IDBDatabase | null) {
  try { db?.transaction(STORE, 'readwrite').objectStore(STORE).clear(); } catch { /* memory only */ }
}

// ---------------------------------------------------------------- component
// Plain-language reason for a getUserMedia failure, with what to do about it.
function cameraError(e: unknown): string {
  const name = e instanceof DOMException ? e.name : '';
  if (name === 'NotReadableError' || name === 'TrackStartError' || /in use|could not start/i.test(String(e)))
    return 'The camera is in use by another app or tab. Close WhatsApp / Zoom / Teams / Meet, the Windows Camera app and any other tab using the camera, then press Retry.';
  if (name === 'NotAllowedError' || name === 'SecurityError')
    return 'Camera permission is blocked. Click the camera icon in the address bar, choose Allow, then press Retry.';
  if (name === 'NotFoundError' || name === 'OverconstrainedError')
    return 'No camera found. Plug one in (or enable it in Windows Settings > Privacy > Camera), then press Retry.';
  return `The camera did not start: ${e instanceof Error ? e.message : String(e)}`;
}

export const WebcamVO: React.FC = () => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const viewRef = useRef<HTMLCanvasElement | null>(null);
  const work = useRef<HTMLCanvasElement | null>(null);
  const dbRef = useRef<IDBDatabase | null>(null);
  const keysRef = useRef<Key[]>([]);
  const [keys, setKeys] = useState<Key[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);                     // Retry re-runs the camera effect
  const [stats, setStats] = useState({ tracked: 0, inliers: 0, flow: 0, fps: 0 });
  const [match, setMatch] = useState<{ id: number; score: number } | null>(null);
  const saveNow = useRef(false);

  const setKeyList = (k: Key[]) => {
    keysRef.current = k;
    setKeys(k);
  };

  // stored keyframes from earlier sessions
  useEffect(() => {
    let alive = true;
    openDb().then(async (db) => {
      dbRef.current = db;
      const rows = await dbAll(db);
      if (!alive) return;
      setKeyList(rows.sort((a, b) => a.id - b.id).map((r) => ({ id: r.id, t: r.t, blob: r.blob,
        url: URL.createObjectURL(r.blob), desc: Float32Array.from(r.desc) })));
    });
    return () => {
      alive = false;
      keysRef.current.forEach((k) => URL.revokeObjectURL(k.url));
    };
  }, []);

  // camera + processing loop
  useEffect(() => {
    let stream: MediaStream | null = null;
    let raf = 0;
    let stop = false;
    const wc = document.createElement('canvas');
    wc.width = W;
    wc.height = H;
    work.current = wc;
    const wctx = wc.getContext('2d', { willReadFrequently: true })!;
    let prev: Uint8ClampedArray | null = null;
    let tracks: Track[] = [];
    const path: Pt[] = [{ x: 0, y: 0 }];
    let pred = { x: 0, y: 0 };                                 // last median flow: the motion prior
    let since = 0, lastProc = 0, frames = 0, fpsT = performance.now(), lastMatch = 0;

    const saveKey = (g: Uint8ClampedArray) => {
      wc.toBlob((blob) => {
        if (!blob) return;
        const id = Date.now();
        const desc = describe(g);
        const k: Key = { id, t: id, blob, url: URL.createObjectURL(blob), desc };
        dbPut(dbRef.current, { id, t: id, blob, desc: Array.from(desc) });
        let list = [...keysRef.current, k];
        while (list.length > MAX_KEYS) {
          const old = list.shift()!;
          URL.revokeObjectURL(old.url);
          dbDelete(dbRef.current, old.id);
        }
        setKeyList(list);
      }, 'image/jpeg', 0.8);
    };

    const step = (now: number) => {
      raf = requestAnimationFrame(step);
      const v = videoRef.current, view = viewRef.current;
      if (!v || !view || v.readyState < 2 || now - lastProc < 1000 / PROC_FPS) return;
      lastProc = now;
      // cover-crop the webcam into the processing canvas
      const s = Math.max(W / v.videoWidth, H / v.videoHeight);
      const sw = W / s, sh = H / s;
      wctx.drawImage(v, (v.videoWidth - sw) / 2, (v.videoHeight - sh) / 2, sw, sh, 0, 0, W, H);
      const g = new Uint8ClampedArray(W * H);
      toGray(wctx.getImageData(0, 0, W, H), g);

      // track, reject outliers against the median flow, refill corners
      if (prev) {
        for (const t of tracks) {
          const q = track(prev, g, t.p, Math.round(pred.x), Math.round(pred.y));
          t.prev = t.p;
          t.ok = !!q;
          if (q) t.p = q;
        }
        tracks = tracks.filter((t) => t.ok);
      }
      const dxs = tracks.map((t) => t.p.x - t.prev.x), dys = tracks.map((t) => t.p.y - t.prev.y);
      const mx = median(dxs), my = median(dys);
      const tol = Math.max(2.5, 0.3 * Math.hypot(mx, my));      // agreement with the median flow
      const inl = tracks.filter((t) => Math.hypot(t.p.x - t.prev.x - mx, t.p.y - t.prev.y - my) < tol);
      pred = prev && inl.length >= 8 ? { x: mx, y: my } : { x: 0, y: 0 };
      if (prev && inl.length >= 8) {
        const last = path[path.length - 1];
        path.push({ x: last.x - mx, y: last.y - my });          // the camera moves opposite to the image
        if (path.length > 600) path.shift();
        since += Math.hypot(mx, my);
      }
      tracks = inl.length >= 8 ? inl : tracks;
      if (tracks.length < MAX_PTS * 0.6) {
        const add = corners(g, tracks.map((t) => t.p));
        tracks.push(...add.map((p) => ({ p, prev: p, ok: true })));
      }

      // keyframes: after enough motion, or on request
      if (saveNow.current || (since > KEY_EVERY_PX && inl.length >= 15)) {
        saveNow.current = false;
        since = 0;
        saveKey(g);
      }
      // place recognition: every 0.5 s against the saved keyframes
      if (now - lastMatch > 500 && keysRef.current.length) {
        lastMatch = now;
        const d = describe(g);
        let best: { id: number; score: number } | null = null;
        for (const k of keysRef.current.slice(0, -1)) {                // not the frame just stored
          const sc = ncc(d, k.desc);
          if (!best || sc > best.score) best = { id: k.id, score: sc };
        }
        setMatch(best && best.score >= MATCH_MIN ? best : null);
      }
      prev = g;

      // draw: the webcam, green corners, yellow flow (x1.5, toward the past), path inset
      const vw = view.clientWidth, vh = view.clientHeight;
      if (view.width !== vw || view.height !== vh) { view.width = vw; view.height = vh; }
      const ctx = view.getContext('2d')!;
      const k = Math.max(vw / W, vh / H), ox = (vw - W * k) / 2, oy = (vh - H * k) / 2;
      ctx.drawImage(wc, ox, oy, W * k, H * k);
      ctx.lineWidth = 1.5;
      for (const t of tracks) {
        const x1 = ox + t.p.x * k, y1 = oy + t.p.y * k;
        const x0 = x1 + 1.5 * (t.prev.x - t.p.x) * k, y0 = y1 + 1.5 * (t.prev.y - t.p.y) * k;
        ctx.strokeStyle = 'rgba(250,204,21,0.9)';
        ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
        ctx.fillStyle = '#22c55e';
        ctx.beginPath(); ctx.arc(x1, y1, 2.5, 0, Math.PI * 2); ctx.fill();
      }
      const S = 90, ix = vw - S - 8, iy = 8;
      ctx.fillStyle = 'rgba(2,6,23,0.6)';
      ctx.fillRect(ix, iy, S, S);
      const ext = Math.max(40, ...path.map((p) => Math.max(Math.abs(p.x), Math.abs(p.y)) * 1.15));
      ctx.strokeStyle = '#22c55e';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      path.forEach((p, i) => {
        const X = ix + S / 2 + (p.x / ext) * (S / 2 - 4), Y = iy + S / 2 + (p.y / ext) * (S / 2 - 4);
        if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
      });
      ctx.stroke();

      frames++;
      if (now - fpsT > 1000) {
        setStats({ tracked: tracks.length, inliers: inl.length, flow: Math.hypot(mx, my), fps: frames * 1000 / (now - fpsT) });
        frames = 0;
        fpsT = now;
      }
    };

    (async () => {
      try {
        if (!navigator.mediaDevices?.getUserMedia)
          throw new Error('this page has no camera access: open it as http://127.0.0.1:3000 or localhost (or https)');
        setError(null);
        try {
          stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false });
        } catch (first) {
          if (first instanceof DOMException && first.name === 'NotAllowedError') throw first;
          stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });   // any resolution
        }
        if (stop) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        const v = videoRef.current!;
        v.srcObject = stream;
        await v.play();
        raf = requestAnimationFrame(step);
      } catch (e) {
        if (!stop) setError(cameraError(e));
      }
    })();
    return () => {
      stop = true;
      cancelAnimationFrame(raf);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [attempt]);

  const download = useCallback((k: Key) => {
    const a = document.createElement('a');
    a.href = k.url;
    a.download = `keyframe_${new Date(k.t).toISOString().replace(/[:.]/g, '-')}.jpg`;
    a.click();
  }, []);

  const clear = () => {
    keysRef.current.forEach((k) => URL.revokeObjectURL(k.url));
    dbClear(dbRef.current);
    setKeyList([]);
    setMatch(null);
  };

  const matchIdx = match ? keys.findIndex((k) => k.id === match.id) : -1;
  return (
    <div className="absolute inset-0 bg-black">
      <video ref={videoRef} muted playsInline className="absolute w-px h-px opacity-0 pointer-events-none" />
      <canvas ref={viewRef} className="absolute inset-0 w-full h-full" />
      {error && (
        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-3 p-6 text-center bg-black/90">
          <div className="text-sm font-semibold text-red-300">Camera not available</div>
          <div className="max-w-xs text-xs leading-relaxed text-slate-300">{error}</div>
          <button onClick={() => setAttempt((a) => a + 1)}
            className="px-3 py-1.5 rounded bg-emerald-600 text-white text-xs font-semibold">Retry</button>
        </div>
      )}
      <div className="absolute top-3 left-3 p-2.5 bg-slate-950/85 border border-slate-800 rounded-lg text-[11px] font-mono text-slate-300 space-y-1">
        <div className="flex justify-between gap-4"><span className="text-slate-500">tracked</span><span>{stats.tracked}</span></div>
        <div className="flex justify-between gap-4"><span className="text-slate-500">inliers</span><span>{stats.inliers}</span></div>
        <div className="flex justify-between gap-4"><span className="text-slate-500">flow</span><span>{stats.flow.toFixed(1)} px/f</span></div>
        <div className="flex justify-between gap-4"><span className="text-slate-500">keyframes</span><span>{keys.length}</span></div>
        <div className="flex justify-between gap-4"><span className="text-slate-500">rate</span><span>{stats.fps.toFixed(0)} fps</span></div>
      </div>
      {match && (
        <div className="absolute inset-0 border-4 border-emerald-400 pointer-events-none">
          <div className="absolute top-2 left-1/2 -translate-x-1/2 px-2 py-0.5 rounded bg-emerald-600 text-white text-xs font-bold font-mono">
            MATCH keyframe #{matchIdx + 1} ({match.score.toFixed(2)})
          </div>
        </div>
      )}
      <div className="absolute inset-x-2 bottom-2 flex flex-col gap-1.5">
        <div className="flex gap-1 overflow-x-auto">
          {keys.slice(-12).map((k) => (
            <button key={k.id} onClick={() => download(k)} title="download this keyframe"
              className={`shrink-0 w-14 h-10 rounded overflow-hidden border-2 ${match?.id === k.id ? 'border-emerald-400' : 'border-slate-700'}`}>
              <img src={k.url} alt="" className="w-full h-full object-cover" />
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 text-[11px]">
          <button onClick={() => (saveNow.current = true)}
            className="px-2 py-1 rounded bg-emerald-600 text-white font-semibold">Save frame</button>
          <button onClick={() => keys.forEach((k, i) => setTimeout(() => download(k), i * 250))} disabled={!keys.length}
            className="px-2 py-1 rounded bg-slate-800 text-slate-200 border border-slate-700 disabled:opacity-40">Download all</button>
          <button onClick={clear} disabled={!keys.length}
            className="px-2 py-1 rounded bg-slate-800 text-slate-200 border border-slate-700 disabled:opacity-40">Clear</button>
          <span className="ml-auto px-1.5 py-0.5 rounded bg-slate-950/85 text-slate-400 font-mono">
            browser prototype · frames stay in this browser
          </span>
        </div>
      </div>
    </div>
  );
};
