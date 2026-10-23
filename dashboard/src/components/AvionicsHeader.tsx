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
}

const navItems = [
  { id: 'replay', label: 'Replay Viewer' },
  { id: 'logs', label: 'Flight Logs' },
];

// RETURN_HOME / HOME_REACHED belong to the Phase 3 state machine, which the pipeline does not have yet.
const stateSteps: Array<{ id: string; label: string; matches: (s: GuardianState | null) => boolean; implemented: boolean }> = [
  { id: 'GNSS_OK', label: 'GNSS_OK', matches: (s) => s === 'GNSS_OK', implemented: true },
  { id: 'SUSPECT', label: 'SUSPECT', matches: (s) => s === 'SUSPECT', implemented: true },
  { id: 'JAMMED_OR_SPOOFED', label: 'JAMMED | SPOOFED', matches: (s) => s === 'JAMMED' || s === 'SPOOFED', implemented: true },
  { id: 'VISION_NAV', label: 'VISION_NAV', matches: (s) => s === 'VISION_NAV', implemented: true },
  { id: 'RETURN_HOME', label: 'RETURN_HOME', matches: (s) => s === 'RETURN_HOME', implemented: false },
  { id: 'HOME_REACHED', label: 'HOME_REACHED', matches: (s) => s === 'HOME_REACHED', implemented: false },
];

const activeStyle: Record<string, string> = {
  GNSS_OK: 'bg-emerald-950 text-emerald-300 border-emerald-500 font-semibold',
  SUSPECT: 'bg-amber-950 text-amber-200 border-amber-500 font-bold',
  JAMMED_OR_SPOOFED: 'bg-red-950 text-red-200 border-red-500 font-bold',
  VISION_NAV: 'bg-sky-950 text-sky-200 border-sky-400 font-bold',
};

export const AvionicsHeader: React.FC<AvionicsHeaderProps> = ({
  currentState,
  hasSession,
  activeTab,
  setActiveTab,
  serverSessions,
  currentSessionName,
  onSelectSession,
  onPickFolder,
}) => {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const folderRef = useRef<HTMLInputElement | null>(null);

  return (
    <header className="border-b border-slate-800 bg-slate-950/95 backdrop-blur-md sticky top-0 z-50">
      <div className="flex flex-wrap items-center justify-between gap-4 px-4 md:px-6 py-2.5">
        <div className="flex items-center gap-3">
          <span className="text-base font-bold tracking-tight text-slate-100 whitespace-nowrap">GNSS Guardian</span>
          <div
            className="flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-sky-950/80 border border-sky-500/50 text-[11px] font-mono font-medium text-sky-300"
            title="Reads exported session files only: no network, no telemetry link"
          >
            <span className="w-1.5 h-1.5 rounded-full bg-sky-400" />
            <span>Replay mode</span>
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
          <button
            onClick={() => folderRef.current?.click()}
            className="px-2.5 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-md border border-slate-700"
            title="Pick a session/<name> folder that contains dashboard.json (stays on this machine)"
          >
            Open folder…
          </button>
        </div>

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
          {hasSession && currentState === null && (
            <span className="px-2.5 py-1 rounded text-xs font-mono border bg-slate-800 text-slate-200 border-slate-500 mr-2">
              WARM-UP (no verdicts yet)
            </span>
          )}
          {stateSteps.map((step, index) => {
            const isActive = step.matches(currentState);
            return (
              <React.Fragment key={step.id}>
                <div
                  title={step.implemented ? undefined : 'Not produced by the pipeline yet (Phase 3 state machine)'}
                  className={`px-2.5 py-1 rounded text-xs font-mono border ${
                    isActive
                      ? activeStyle[step.id]
                      : step.implemented
                      ? 'bg-slate-900/60 text-slate-500 border-slate-800/80'
                      : 'bg-transparent text-slate-700 border-dashed border-slate-800 line-through'
                  }`}
                >
                  {step.label}
                </div>
                {index < stateSteps.length - 1 && <span className="text-slate-600 font-mono text-xs select-none">→</span>}
              </React.Fragment>
            );
          })}
          <span className="ml-3 text-[10px] text-slate-600 font-mono">
            derived from detector levels + vision mode · RETURN_HOME / HOME_REACHED not implemented
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
