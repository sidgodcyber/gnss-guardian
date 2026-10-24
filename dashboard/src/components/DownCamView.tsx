import React, { useEffect, useRef, useState } from 'react';
import { DashboardFile, Row } from '../types/gnssGuardian';
import { fmt, frameAt } from '../services/session';

interface Props {
  data: DashboardFile;
  row: Row;
  videoUrl?: string;
  currentTimeSec: number;
  isPlaying: boolean;
  playbackSpeed: number;
}

const DRIFT_S = 0.3;

export const DownCamView: React.FC<Props> = ({ data, row, videoUrl, currentTimeSec, isPlaying, playbackSpeed }) => {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [videoError, setVideoError] = useState<string | null>(null);
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
        {videoUrl ? (
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
        {videoUrl && frame === null && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-300 bg-black/70">
            before the first camera frame
          </div>
        )}
        {videoError && (
          <div className="absolute bottom-3 left-3 right-3 p-2 text-xs rounded bg-red-950/90 border border-red-500/60 text-red-200">
            {videoError}
          </div>
        )}

        {v && (
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
        {videoUrl && frame !== null && (
          <div className="absolute bottom-3 right-3 px-2 py-1 bg-slate-950/85 border border-slate-800 rounded text-[10px] font-mono text-slate-400">
            frame {Math.round(frame)} · {fps} fps
          </div>
        )}
      </div>
    </div>
  );
};
