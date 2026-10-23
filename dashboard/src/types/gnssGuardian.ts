// Mirrors gnss_guardian/dashboard_export.py (format gnss-guardian-dashboard/1).

export type GuardianState =
  | 'GNSS_OK'
  | 'SUSPECT'
  | 'JAMMED'
  | 'SPOOFED'
  | 'VISION_NAV'
  | 'RETURN_HOME'
  | 'HOME_REACHED';

export type Level = 'CLEAR' | 'SUSPECT' | 'ALARM';
export type Detector = 'jamming' | 'spoofing';

export interface DetectorRow {
  level: Level;
  score: number;
  parts: Record<string, number | null>;
  reasons: string[];
}

export interface VisionRow {
  mode: 'waiting_for_home' | 'gnss_anchored' | 'dead_reckoning' | string;
  e: number | null;
  n: number | null;
  heading: number | null;
  aligned: boolean;
  quality: number | null;
  n_inliers: number | null;
  flag: string | null;
  v_e: number | null;
  v_n: number | null;
  keyframes: number;
  home_dist_m: number | null;
}

export interface Row {
  t: number;
  t_gnss: string;
  seq: number;
  state: GuardianState | null;
  warming_up: boolean;
  gnss_trusted: boolean;
  gnss: {
    fix_valid: boolean;
    sats_used: number | null;
    sats_tracked: number | null;
    hdop: number | null;
    cn0_mean: number | null;
    e: number | null;
    n: number | null;
  };
  jamming: DetectorRow;
  spoofing: DetectorRow;
  features: Record<string, number | boolean | null>;
  vision: VisionRow | null;
  frame: number | null;
}

export interface Transition {
  t: number;
  t_gnss: string;
  state: GuardianState | null;
  from: GuardianState | null;
  reasons: string[];
  indicators: string[];
}

export interface LevelChange {
  t: number;
  t_gnss: string;
  detector: Detector;
  level: Level;
  score: number;
  reasons: string[];
}

export interface Keyframe {
  t: number | null;
  id: number;
  e: number;
  n: number;
  home: boolean;
}

export interface SessionEventRow {
  t: number | null;
  kind: string;
  detail: string;
}

export interface IndicatorDef {
  key: string;
  label: string;
  feature: string;
  span: [number, number] | null;
  unit: string;
  weight: number;
}

export interface DashboardConfig {
  jamming: { combine: string; indicators: IndicatorDef[] };
  spoofing: { combine: string; indicators: IndicatorDef[] };
  hysteresis: {
    suspect_on: number;
    suspect_off: number;
    suspect_hold_s: number;
    alarm_on: number;
    alarm_k: number;
    alarm_immediate: number;
    alarm_off: number;
    alarm_hold_s: number;
    min_alarm_s: Record<string, number>;
  };
}

export interface DashboardMeta {
  source: string | null;
  mode: string | null;
  created_utc: string | null;
  node_id: string | null;
  start_t_gnss: string | null;
  duration_s: number;
  epochs: number;
  home_set: boolean;
  state_source: string;
  video: string | null;
  video_fps: number | null;
  reference_track: null;
}

export interface DashboardFile {
  format: string;
  meta: DashboardMeta;
  config: DashboardConfig;
  timeline: Row[];
  transitions: Transition[];
  detector_levels: LevelChange[];
  keyframes: Keyframe[];
  events: SessionEventRow[];
}

export interface LoadedSession {
  id: string;
  name: string;
  data: DashboardFile;
  videoUrl?: string;
}
