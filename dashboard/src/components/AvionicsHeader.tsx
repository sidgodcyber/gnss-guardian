import React, { useRef, useState } from 'react';
import { GuardianState } from '../types/gnssGuardian';

interface AvionicsHeaderProps {
  currentState: GuardianState | null;
  hasSession: boolean;
  activeTab: string;
  setActiveTab: (tab: string) => void;
  serverSessions: string[];
  currentSessionName: string;
  onSelectSession: (name: string) => void;
  onPickFolder: (files: FileList) => void;
  liveAddress: string | null;
  onConnectLive: (address: string) => void;
  onDisconnectLive: () => void;
  view: View;
  setView: (v: View) => void;
}

export type View = 'demo' | 'details';

const navItems = [
  { id: 'replay', label: 'Replay Viewer' },
  { id: 'logs', label: 'Flight Logs' },
];

// The states of the pipeline's state machine (gnss_guardian/state.py), read from dashboard.json.
const stateSteps: Array<{ id: string; label: string; matches: (s: GuardianState | null) => boolean }> = [
  { id: 'GNSS_OK', label: 'GNSS_OK', matches: (s) => s === 'GNSS_OK' },
  { id: 'SUSPECT', label: 'SUSPECT', matches: (s) => s === 'SUSPECT' },
  { id: 'JAMMED_OR_SPOOFED', label: 'JAMMED | SPOOFED', matches: (s) => s === 'JAMMED' || s === 'SPOOFED' },
  { id: 'VISION_NAV', label: 'VISION_NAV', matches: (s) => s === 'VISION_NAV' },
  { id: 'RETURN_HOME', label: 'RETURN_HOME', matches: (s) => s === 'RETURN_HOME' },
  { id: 'HOME_REACHED', label: 'HOME_REACHED', matches: (s) => s === 'HOME_REACHED' },
];

const activeStyle: Record<string, string> = {
  GNSS_OK: 'bg-emerald-950 text-emerald-300 border-emerald-500 font-semibold',
  SUSPECT: 'bg-amber-950 text-amber-200 border-amber-500 font-bold',
  JAMMED_OR_SPOOFED: 'bg-red-950 text-red-200 border-red-500 font-bold',
  VISION_NAV: 'bg-sky-950 text-sky-200 border-sky-400 font-bold',
  RETURN_HOME: 'bg-violet-950 text-violet-200 border-violet-400 font-bold',
  HOME_REACHED: 'bg-teal-950 text-teal-200 border-teal-400 font-bold',
};

// The state chain with the current state highlighted (both views; compact in the demo top bar).
export const StateChain: React.FC<{ currentState: GuardianState | null; hasSession: boolean; compact?: boolean }> = ({
  currentState,
  hasSession,
  compact = false,
}) => {
  const pill = compact ? 'px-1.5 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs';
  return (
    <div className={`flex items-center ${compact ? 'gap-1' : 'gap-1.5'} min-w-max`}>
      {hasSession && (currentState === null || currentState === 'WARMING_UP') && (
        <span className={`${pill} rounded font-mono border bg-slate-800 text-slate-200 border-slate-500 mr-1`}>
          {compact ? 'WARM-UP' : 'WARM-UP (no verdicts yet)'}
        </span>
      )}
      {stateSteps.map((step, index) => {
        const isActive = step.matches(currentState);
        return (
          <React.Fragment key={step.id}>
            <div
              className={`${pill} rounded font-mono border ${
                isActive ? activeStyle[step.id] : 'bg-slate-900/60 text-slate-500 border-slate-800/80'
              }`}
            >
              {step.label}
            </div>
            {index < stateSteps.length - 1 && <span className="text-slate-600 font-mono text-xs select-none">→</span>}
          </React.Fragment>
        );
      })}
    </div>
  );
};

const ViewToggle: React.FC<{ view: View; setView: (v: View) => void }> = ({ view, setView }) => (
  <div className="flex items-center bg-slate-900 rounded-md p-0.5 border border-slate-700 text-xs shrink-0" role="tablist">
    {(['demo', 'details'] as const).map((v) => (
      <button
        key={v}
        role="tab"
        aria-selected={view === v}
        onClick={() => setView(v)}
        className={`px-2.5 py-1 rounded font-semibold ${view === v ? 'bg-sky-700 text-white' : 'text-slate-400 hover:text-slate-200'}`}
      >
        {v === 'demo' ? 'Demo' : 'Details'}
      </button>
    ))}
  </div>
);

export const AvionicsHeader: React.FC<AvionicsHeaderProps> = ({
  currentState,
  hasSession,
  activeTab,
  setActiveTab,
  serverSessions,
  currentSessionName,
  onSelectSession,
  onPickFolder,
  liveAddress,
  onConnectLive,
  onDisconnectLive,
  view,
  setView,
}) => {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const folderRef = useRef<HTMLInputElement | null>(null);
  const [addr, setAddr] = useState(liveAddress ?? '');

  if (view === 'demo') {
    // One row: title | LIVE/REPLAY | state chain | Pi address + Live | session | Demo/Details
    return (
      <header className="border-b border-slate-800 bg-slate-950 shrink-0">
        <div className="flex items-center gap-3 px-3 py-1.5">
          <span className="text-base font-bold tracking-tight text-slate-100 whitespace-nowrap">GNSS Guardian</span>
          <div
            className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-sky-950/80 border border-sky-500/50 text-[11px] font-mono font-medium text-sky-300 shrink-0"
            title={liveAddress ? 'Polling live.json from the Pi on the LAN (read only)' : 'Reads exported session files only'}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${liveAddress ? 'bg-red-400 animate-pulse' : 'bg-sky-400'}`} />
            <span>{liveAddress ? 'LIVE' : 'REPLAY'}</span>
          </div>
          <div className="flex-1 min-w-0 overflow-x-auto">
            <StateChain currentState={currentState} hasSession={hasSession} compact />
          </div>
          <form
            className="flex items-center gap-1 shrink-0"
            onSubmit={(e) => {
              e.preventDefault();
              if (addr.trim()) onConnectLive(addr.trim());
            }}
          >
            <input
              value={addr}
              onChange={(e) => setAddr(e.target.value)}
              placeholder="Pi address:8080"
              aria-label="Live source: Pi address"
              className="w-36 bg-slate-900 border border-slate-700 text-slate-200 text-xs rounded-md px-2 py-1 font-mono"
            />
            {liveAddress ? (
              <button type="button" onClick={onDisconnectLive}
                className="px-2 py-1 bg-red-900 hover:bg-red-800 text-red-100 text-xs font-medium rounded-md border border-red-700">
                Stop
              </button>
            ) : (
              <button type="submit"
                className="px-2 py-1 bg-sky-800 hover:bg-sky-700 text-sky-50 text-xs font-medium rounded-md border border-sky-600">
                Live
              </button>
            )}
          </form>
          <select
            value={currentSessionName}
            onChange={(e) => e.target.value && onSelectSession(e.target.value)}
            aria-label="Session"
            className="max-w-40 bg-slate-900 border border-slate-700 text-slate-200 text-xs rounded-md px-2 py-1 font-medium shrink-0"
          >
            <option value="">{serverSessions.length ? 'session…' : 'no sessions'}</option>
            {serverSessions.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <input
            type="file"
            ref={folderRef}
            className="hidden"
            {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            onChange={(e) => {
              if (e.target.files && e.target.files.length) onPickFolder(e.target.files);
              e.target.value = '';
            }}
          />
          <button
            onClick={() => folderRef.current?.click()}
            className="px-2 py-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs rounded-md border border-slate-700 shrink-0"
            title="Pick a session/<name> folder that contains dashboard.json"
          >
            Open…
          </button>
          <ViewToggle view={view} setView={setView} />
        </div>
      </header>
    );
  }

  return (
    <header className="border-b border-slate-800 bg-slate-950/95 backdrop-blur-md sticky top-0 z-50">
      <div className="flex flex-wrap items-center justify-between gap-4 px-4 md:px-6 py-2.5">
        <div className="flex items-center gap-3">
          <span className="text-base font-bold tracking-tight text-slate-100 whitespace-nowrap">GNSS Guardian</span>
          <div
            className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-sky-950/80 border border-sky-500/50 text-[11px] font-mono font-medium text-sky-300"
            title={liveAddress ? 'Polling live.json from the Pi on the LAN (read only)' : 'Reads exported session files only'}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${liveAddress ? 'bg-red-400 animate-pulse' : 'bg-sky-400'}`} />
            <span>{liveAddress ? 'Live' : 'Replay mode'}</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <label htmlFor="session-select" className="text-xs font-semibold text-slate-400 hidden sm:inline">
            Session:
          </label>
          <select
            id="session-select"
            value={currentSessionName}
            onChange={(e) => e.target.value && onSelectSession(e.target.value)}
            className="bg-slate-900 border border-slate-700 text-slate-200 text-xs rounded-md px-3 py-1.5 font-medium"
          >
            <option value="">{serverSessions.length ? 'session/…' : 'no session folders served'}</option>
            {serverSessions.map((name) => (
              <option key={name} value={name}>
                session/{name}
              </option>
            ))}
          </select>

          <input
            type="file"
            ref={folderRef}
            className="hidden"
            {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
            onChange={(e) => {
              if (e.target.files && e.target.files.length) onPickFolder(e.target.files);
              e.target.value = '';
            }}
          />
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              if (addr.trim()) onConnectLive(addr.trim());
            }}
          >
            <input
              value={addr}
              onChange={(e) => setAddr(e.target.value)}
              placeholder="Pi address, e.g. 192.168.1.20:8080"
              aria-label="Live source: Pi address"
              className="w-44 bg-slate-900 border border-slate-700 text-slate-200 text-xs rounded-md px-2 py-1.5 font-mono"
            />
            {liveAddress ? (
              <button type="button" onClick={onDisconnectLive}
                className="px-2.5 py-1.5 bg-red-900 hover:bg-red-800 text-red-100 text-xs font-medium rounded-md border border-red-700">
                Stop live
              </button>
            ) : (
              <button type="submit"
                className="px-2.5 py-1.5 bg-sky-800 hover:bg-sky-700 text-sky-50 text-xs font-medium rounded-md border border-sky-600">
                Live
              </button>
            )}
          </form>
          <button
            onClick={() => folderRef.current?.click()}
            className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-md border border-slate-700"
            title="Pick a session/<name> folder that contains dashboard.json (stays on this machine)"
          >
            Open folder…
          </button>
        </div>

        <ViewToggle view={view} setView={setView} />

        <nav className="hidden lg:flex items-center gap-4 text-xs font-medium text-slate-400">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              className={`hover:text-slate-100 transition-colors pb-0.5 ${
                activeTab === item.id ? 'text-sky-400 border-b-2 border-sky-400 font-semibold' : ''
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <button
          onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          className="lg:hidden px-2 py-1 text-xs text-slate-300 rounded bg-slate-900 border border-slate-800"
          aria-label="Toggle menu"
        >
          {mobileMenuOpen ? 'Close' : 'Menu'}
        </button>
      </div>

      <div className="bg-slate-950 px-4 md:px-6 py-1.5 border-t border-slate-900 overflow-x-auto">
        <div className="flex items-center gap-1.5 min-w-max">
          <span className="text-[10px] uppercase font-bold text-slate-500 mr-2 tracking-wider">State:</span>
          <StateChain currentState={currentState} hasSession={hasSession} />
          <span className="ml-3 text-[10px] text-slate-600 font-mono">
            from the pipeline state machine · guidance is advisory, nothing commands the aircraft
          </span>
        </div>
      </div>

      {mobileMenuOpen && (
        <div className="lg:hidden border-t border-slate-800 bg-slate-950 px-4 py-2 space-y-1">
          {navItems.map((item) => (
            <button
              key={item.id}
              onClick={() => {
                setActiveTab(item.id);
                setMobileMenuOpen(false);
              }}
              className={`w-full text-left px-3 py-1.5 text-xs font-medium rounded ${
                activeTab === item.id ? 'bg-sky-950/60 text-sky-400' : 'text-slate-400'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </header>
  );
};
