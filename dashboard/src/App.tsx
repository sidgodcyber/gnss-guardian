import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { LoadedSession } from './types/gnssGuardian';
import { listServerSessions, loadPickedFolder, loadServerSession, rowIndexAt } from './services/session';
import { AvionicsHeader } from './components/AvionicsHeader';
import { StatusCards } from './components/StatusCards';
import { TacticalMap } from './components/TacticalMap';
import { DownCamView } from './components/DownCamView';
import { DetectionIndicatorsPanel } from './components/DetectionIndicatorsPanel';
import { SensorFusionView } from './components/SensorFusionView';
import { IncidentLogView } from './components/IncidentLogView';

type Tab = 'replay' | 'logs';

const params = new URLSearchParams(window.location.search);

export default function App() {
  const [serverDir, setServerDir] = useState<string | null>(null);
  const [serverSessions, setServerSessions] = useState<string[]>([]);
  const [session, setSession] = useState<LoadedSession | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<Tab>(params.get('tab') === 'logs' ? 'logs' : 'replay');
  const [currentTimeSec, setCurrentTimeSec] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);

  const open = useCallback(async (load: () => Promise<LoadedSession>, startAt = 0) => {
    try {
      const s = await load();
      setSession((prev) => {
        if (prev?.videoUrl?.startsWith('blob:')) URL.revokeObjectURL(prev.videoUrl);
        return s;
      });
      setCurrentTimeSec(Math.max(0, Math.min(startAt, s.data.meta.duration_s)));
      setIsPlaying(false);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    listServerSessions().then((r) => {
      if (!r) return;
      setServerDir(r.dir);
      setServerSessions(r.sessions);
      const wanted = params.get('session');
      const pick = wanted && r.sessions.includes(wanted) ? wanted : r.sessions[0];
      if (pick) open(() => loadServerSession(pick), Number(params.get('t') ?? 0) || 0);
    });
  }, [open]);

  const duration = session?.data.meta.duration_s ?? 0;

  useEffect(() => {
    if (!isPlaying || !session) return;
    let last = performance.now();
    let raf = 0;
    const step = (now: number) => {
      const dt = ((now - last) / 1000) * playbackSpeed;
      last = now;
      setCurrentTimeSec((prev) => {
        const next = prev + dt;
        if (next >= duration) {
          setIsPlaying(false);
          return duration;
        }
        return next;
      });
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [isPlaying, playbackSpeed, duration, session]);

  const rows = session?.data.timeline ?? [];
  const idx = useMemo(() => rowIndexAt(rows, currentTimeSec), [rows, currentTimeSec]);
  const row = rows[idx];
  const history = useMemo(() => rows.slice(0, idx + 1), [rows, idx]);

  const seek = (t: number) => setCurrentTimeSec(Math.max(0, Math.min(t, duration)));

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      <AvionicsHeader
        currentState={row?.state ?? null}
        hasSession={!!row}
        activeTab={activeTab}
        setActiveTab={(t) => setActiveTab(t as Tab)}
        serverSessions={serverSessions}
        currentSessionName={session?.id.startsWith('server:') ? session.name : ''}
        onSelectSession={(name) => open(() => loadServerSession(name))}
        onPickFolder={(files) => open(() => loadPickedFolder(files))}
      />

      <main className="flex-1 p-4 md:p-6 max-w-7xl mx-auto w-full flex flex-col gap-5">
        {error && (
          <div className="px-4 py-3 rounded-lg border border-red-500/60 bg-red-950/60 text-sm text-red-200">{error}</div>
        )}

        {!session || !row ? (
          <EmptyState serverDir={serverDir} />
        ) : (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 bg-slate-900/60 border border-slate-800 rounded-xl text-xs">
              <div className="min-w-0">
                <div className="font-bold text-sm text-slate-100">{session.name}</div>
                <div className="text-slate-400 mt-0.5 font-mono truncate" title={session.data.meta.source ?? ''}>
                  source: {session.data.meta.source ?? 'unknown'} · {session.data.meta.epochs} epochs ·{' '}
                  {session.data.meta.video ? 'video' : 'no video'} · home {session.data.meta.home_set ? 'set' : 'not set'}
                </div>
              </div>
              <div className="text-slate-400 font-mono">
                start {session.data.meta.start_t_gnss ?? '–'} · {duration.toFixed(0)} s
              </div>
            </div>

            {activeTab === 'replay' && (
              <div className="flex flex-col gap-5">
                <StatusCards row={row} />

                <DetectionIndicatorsPanel
                  data={session.data}
                  row={row}
                  currentTimeSec={currentTimeSec}
                  isPlaying={isPlaying}
                  playbackSpeed={playbackSpeed}
                  onSeek={seek}
                  onTogglePlay={() => {
                    if (!isPlaying && currentTimeSec >= duration) setCurrentTimeSec(0);
                    setIsPlaying(!isPlaying);
                  }}
                  onSetSpeed={setPlaybackSpeed}
                  onRestart={() => setCurrentTimeSec(0)}
                />

                <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
                  <div className="lg:col-span-7 h-[460px]">
                    <TacticalMap data={session.data} row={row} history={history} currentTimeSec={currentTimeSec} />
                  </div>
                  <div className="lg:col-span-5 h-[460px]">
                    <DownCamView
                      data={session.data}
                      row={row}
                      videoUrl={session.videoUrl}
                      currentTimeSec={currentTimeSec}
                      isPlaying={isPlaying}
                      playbackSpeed={playbackSpeed}
                    />
                  </div>
                </div>

                <SensorFusionView row={row} />
              </div>
            )}

            {activeTab === 'logs' && (
              <IncidentLogView data={session.data} currentTimeSec={currentTimeSec} onSeek={seek} />
            )}
          </>
        )}
      </main>

      <footer className="border-t border-slate-900 bg-slate-950/80 px-4 py-3 text-center text-xs font-mono text-slate-500">
        GNSS Guardian replay viewer · offline · positions in metres from the home point
      </footer>
    </div>
  );
}

const EmptyState: React.FC<{ serverDir: string | null }> = ({ serverDir }) => (
  <div className="p-6 rounded-xl border border-slate-800 bg-slate-900/60 text-sm text-slate-300 space-y-3">
    <div className="text-base font-semibold text-slate-100">No session loaded</div>
    <p>
      Export a session with the pipeline, then pick it in the session list or with <b>Open folder</b>:
    </p>
    <pre className="bg-slate-950 border border-slate-800 rounded p-3 text-xs font-mono overflow-x-auto">
      gnss-guardian replay RECORDING_FOLDER_OR_FILE.nmea --out session/NAME
    </pre>
    <p className="text-slate-400 text-xs">
      {serverDir
        ? `Listing session folders from ${serverDir} (none with a dashboard.json yet).`
        : 'Served without the dev server: use Open folder to pick a session/<name> folder.'}
    </p>
  </div>
);
