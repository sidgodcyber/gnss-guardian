import React, { useEffect, useRef, useState } from 'react';
import { DashboardFile, Row } from '../types/gnssGuardian';
import { fmt, frameAt } from '../services/session';

interface Props {
  data: DashboardFile;
  row: Row;
  videoUrl?: string;
  liveStreamUrl?: string;      // MJPEG overlay from the Pi (live --overlay PORT)
  currentTimeSec: number;
  isPlaying: boolean;
  playbackSpeed: number;
}

const DRIFT_S = 0.3;

// Optional local clip: dashboard/public/downcam.webm (or .mp4), git-ignored and never published. When
// present it plays on loop in this panel instead of the session video / live stream; ?downcam=off turns it
// off. It is not synchronised with the session: the VIO numbers next to it come from the session's own
// camera track. WebM (VP8) plays everywhere; phone MP4s (H.264) may not.
const CLIP_URLS = ['/downcam.webm', '/downcam.mp4'];
let clipProbe: Promise<string | null> | null = null;
export function probeClip(): Promise<string | null> {
  if (new URLSearchParams(window.location.search).get('downcam') === 'off') return Promise.resolve(null);
  clipProbe ??= (async () => {
    for (const url of CLIP_URLS) {
      try {
        const r = await fetch(url, { method: 'HEAD' });
        if (r.ok && (r.headers.get('content-type') ?? '').startsWith('video/')) return url;
      } catch {
        /* not served */
      }
    }
    return null;
  })();
  return clipProbe;
}

// The clip as rough film: played slowly, sampled frame by frame at a low rate onto a low-resolution canvas
// (cover-cropped to fill the panel), with grain, a little desaturation and contrast, and the odd scratch.
const FILM = { speed: 0.5, fps: 8, pixel: 3, grain: 70 };

const ClipFilm: React.FC<{ src: string }> = ({ src }) => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d', { willReadFrequently: true });
    if (!video || !canvas || !ctx) return;
    video.playbackRate = FILM.speed;
    video.play().catch(() => undefined);

    const draw = () => {
      const W = Math.max(1, Math.round(canvas.clientWidth / FILM.pixel));
      const H = Math.max(1, Math.round(canvas.clientHeight / FILM.pixel));
      if (canvas.width !== W || canvas.height !== H) {
        canvas.width = W;
        canvas.height = H;
      }
      const vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh) return;
      if (video.playbackRate !== FILM.speed) video.playbackRate = FILM.speed;
      const s = Math.max(W / vw, H / vh);                    // cover: fill the panel, crop the overflow
      const sw = W / s, sh = H / s;
      ctx.filter = 'grayscale(0.35) contrast(1.3) brightness(0.92) sepia(0.12)';
      ctx.drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, W, H);
      ctx.filter = 'none';
      const img = ctx.getImageData(0, 0, W, H);
      const d = img.data;
      const flicker = (Math.random() - 0.5) * 18;
      for (let i = 0; i < d.length; i += 4) {
        const n = (Math.random() - 0.5) * FILM.grain + flicker;
        d[i] += n;
        d[i + 1] += n;
        d[i + 2] += n;
      }
      ctx.putImageData(img, 0, 0);
      if (Math.random() < 0.35) {                            // a scratch on some frames
        ctx.fillStyle = `rgba(255,255,255,${0.15 + Math.random() * 0.25})`;
        ctx.fillRect(Math.floor(Math.random() * W), 0, 1, H);
      }
    };
    const id = window.setInterval(draw, 1000 / FILM.fps);
    return () => window.clearInterval(id);
  }, [src]);

  return (
    <>
      <video ref={videoRef} src={src} muted autoPlay loop playsInline aria-hidden
        className="absolute w-px h-px opacity-0 pointer-events-none" />
      <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" style={{ imageRendering: 'pixelated' }} />
    </>
  );
};

export const DownCamView: React.FC<Props> = ({ data, row, videoUrl, liveStreamUrl, currentTimeSec, isPlaying, playbackSpeed }) => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [videoError, setVideoError] = useState<string | null>(null);
  const [clip, setClip] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    probeClip().then((url) => live && setClip(url));
    return () => {
      live = false;
    };
  }, []);
  const fps = data.meta.video_fps ?? 30;
  const frame = frameAt(data.timeline, currentTimeSec);
  const target = frame === null ? null : frame / fps;

  // The replay clock is the master; the video follows it (rate while playing, exact time when paused).
  useEffect(() => {
    const vid = videoRef.current;
    if (!vid || target === null) return;
    vid.playbackRate = playbackSpeed;
    if (isPlaying) {
      if (Math.abs(vid.currentTime - target) > DRIFT_S) vid.currentTime = target;
      if (vid.paused) vid.play().catch(() => undefined);
    } else {
      if (!vid.paused) vid.pause();
      if (Math.abs(vid.currentTime - target) > 0.5 / fps) vid.currentTime = target;
    }
  }, [target, isPlaying, playbackSpeed, fps]);

  useEffect(() => setVideoError(null), [videoUrl]);

  const v = row.vision;
  const speed = v?.v_e != null && v?.v_n != null ? Math.hypot(v.v_e, v.v_n) : null;
  const navByVision = row.fused?.source === 'vision' || row.fused?.source === 'vision_stale';

  return (
    <div className="flex flex-col bg-slate-900/90 border border-slate-800 rounded-xl overflow-hidden h-full shadow-xl">
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 bg-slate-950/90 border-b border-slate-800">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-200">Down-Cam VIO</span>
        <span
          className={`text-[10px] font-mono px-2 py-0.5 rounded border ${
            navByVision ? 'bg-sky-500/25 text-sky-300 border-sky-400' : 'bg-slate-800 text-slate-400 border-slate-700'
          }`}
        >
          {navByVision
            ? row.fused?.source === 'vision_stale'
              ? 'camera track lost: position held'
              : 'position from vision (dead reckoning)'
            : v
            ? `vision ${v.mode.replace(/_/g, ' ')}`
            : 'no vision data'}
        </span>
      </div>

      <div className="relative flex-1 min-h-0 bg-black">
        {clip ? (
          <ClipFilm src={clip} />
        ) : liveStreamUrl ? (
          <img
            src={liveStreamUrl}
            alt="live camera overlay from the Pi"
            onError={() => setVideoError('no overlay stream: is live running with --overlay on this port?')}
            className="absolute inset-0 w-full h-full object-contain"
          />
        ) : videoUrl ? (
          <video
            ref={videoRef}
            src={videoUrl}
            muted
            playsInline
            preload="auto"
            onError={() => setVideoError('the browser could not play this video')}
            onLoadedMetadata={(e) => {
              if (target !== null) e.currentTarget.currentTime = target;
            }}
            className="absolute inset-0 w-full h-full object-contain"
          />
        ) : (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-500 p-6 text-center">
            No video in this session (replay was run without --video, or the folder has no dashboard_video.webm).
          </div>
        )}
        {clip && (
          <div className="absolute bottom-3 left-3 px-2 py-1 bg-slate-950/85 border border-slate-800 rounded text-[10px] font-mono text-slate-400">
            camera clip (looped, not synced to this session)
          </div>
        )}
        {!clip && videoUrl && frame === null && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-300 bg-black/70">
            before the first camera frame
          </div>
        )}
        {videoError && (
          <div className="absolute bottom-3 left-3 right-3 p-2 text-xs rounded bg-red-950/90 border border-red-500/60 text-red-200">
            {videoError}
          </div>
        )}

        {v && !liveStreamUrl && (
          <div className="absolute top-3 left-3 p-2.5 bg-slate-950/85 border border-slate-800 rounded-lg text-[11px] font-mono text-slate-300 space-y-1">
            <div className="flex justify-between gap-4"><span className="text-slate-500">quality</span><span>{fmt(v.quality, 2)}</span></div>
            <div className="flex justify-between gap-4"><span className="text-slate-500">inliers</span><span>{v.n_inliers ?? '–'}</span></div>
            <div className="flex justify-between gap-4"><span className="text-slate-500">speed</span><span>{fmt(speed, 2, 'm/s')}</span></div>
            <div className="flex justify-between gap-4">
              <span className="text-slate-500">heading</span>
              <span>{v.aligned ? fmt(v.heading, 0, '°') : 'not aligned'}</span>
            </div>
            <div className="flex justify-between gap-4"><span className="text-slate-500">keyframes</span><span>{v.keyframes}</span></div>
            {v.flag && <div className="text-amber-300">{v.flag}</div>}
          </div>
        )}
        {!clip && videoUrl && frame !== null && (
          <div className="absolute bottom-3 right-3 px-2 py-1 bg-slate-950/85 border border-slate-800 rounded text-[10px] font-mono text-slate-400">
            frame {Math.round(frame)} · {fps} fps
          </div>
        )}
      </div>
    </div>
  );
};
