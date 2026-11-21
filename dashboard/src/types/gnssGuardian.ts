// Mirrors gnss_guardian/dashboard_export.py (format gnss-guardian-dashboard/2). The state, fused
// position, guidance and latch come from the pipeline's state machine (gnss_guardian/state.py).

export type GuardianState =
  | 'WARMING_UP'
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

export type Cause = 'jamming' | 'spoofing';

// gnss: GNSS trusted · vision: camera dead reckoning from the last trusted fix ·
// vision_stale: camera track stopped (low texture), held · last_fix: no camera, last trusted fix held
export interface Fused {
  source: 'gnss' | 'vision' | 'vision_stale' | 'last_fix';
  e: number;
  n: number;
  home_distance_m: number;
  home_bearing_deg: number;
}

export interface Waypoint {
  id: number | 'home';
  e: number;
  n: number;
}

// Advisory only: the module never commands the aircraft.
export interface Guidance {
  advisory: true;
  home_bearing_deg: number;
  home_distance_m: number;
  waypoint: Waypoint & { bearing_deg: number; distance_m: number };
  waypoints_left: number;
  path_remaining_m: number;
}

// Spoof latch: GNSS_OK only after GNSS and the camera agree for needed_s in a row.
export interface Latch {
  agree_s: number;
  needed_s: number;
  agreeing: boolean;
  detail: string;
}

export interface Route {
  t: number | null;
  waypoints: Waypoint[];
}

export interface Row {
  t: number;
  t_gnss: string;
  seq: number;
  state: GuardianState | null;
  cause: Cause | null;
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
  fused: Fused | null;
  guidance: Guidance | null;
  latch: Latch | null;
  frame: number | null;
  injection?: Injection | null;     // demo spoof injection active at this row (live --demo-inject)
}

// Demo spoof injection (software, before the detector): never a real attack.
export interface Injection {
  id: number;
  type: 'jump' | 'drift';
  bearing_deg: number;
  m: number | null;
  mps: number | null;
  elapsed_s: number;
  offset_m: number;
}

// live.json from `gnss-guardian live --overlay PORT` (format gnss-guardian-live/1).
export interface LiveView {
  format: string;
  written_utc: string;
  node_id: string;
  session: string | null;
  seq: number;
  t: number;
  t_gnss: string;
  state: GuardianState | null;
  labels: string[];
  home: { set: boolean; detail: string } | null;
  home_keyframe: { status: string; detail: string; features?: number } | null;
  vision: { fps: number | null; mode: string | null; aligned: boolean | null } | null;
  injection: { enabled: boolean; active: Injection | null };
  track: Array<[number, number, number, string]>;
  keyframes: Array<[number, number, boolean]>;
  row: Row;
  config: DashboardConfig;
  age_s?: number;
}

export interface Transition {
  t: number;
  t_gnss: string;
  state: GuardianState | null;
  from: GuardianState | null;
  cause: Cause | null;
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
  routes: Route[];
  events: SessionEventRow[];
}

export interface LoadedSession {
  id: string;
  name: string;
  data: DashboardFile;
  videoUrl?: string;
  liveBase?: string;      // live source: http://<pi>:PORT (overlay stream at /, live.json)
}
