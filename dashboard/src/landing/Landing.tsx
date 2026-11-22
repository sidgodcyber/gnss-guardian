import EtchedAccretion from '@/components/ui/etched-accretion';

// Project landing: the black-hole hero (dark, it is a shader) and light sections that match the dashboard's
// light theme. Colours are plain hex on purpose: the dashboard remaps Tailwind's slate palette for its light
// theme, and hex keeps this page identical inside the dashboard and on landing.html.

const REPO = 'https://github.com/sidgodcyber/gnss-guardian-sathack';
const INK = 'text-[#0f172a]';
const MUTED = 'text-[#475569]';
const RED = 'text-[#c81e2a]';

const PILLARS = [
  {
    title: 'Detects jamming',
    body: 'Signal strength (C/N0) of every satellite against a clean baseline, satellites lost, HDOP rising, fix lost. A foil-covered antenna reads as blockage within seconds.',
  },
  {
    title: 'Detects spoofing',
    body: 'Position jumps faster than the vehicle can move, GNSS clock jumps against the host clock, altitude rate, uniform signal strength from a single transmitter, satellites the baseline never saw.',
  },
  {
    title: 'Checks the sky',
    body: 'Reported GPS satellite directions against a YUMA almanac on disk. A replayed or foreign sky, or satellites that should be below the horizon, raise the alarm without a camera.',
  },
  {
    title: 'Brings the vehicle home on its camera',
    body: 'A down-facing Pi camera tracks the ground (visual odometry with keyframes). When GNSS cannot be trusted it navigates on the camera and guides the way back along the breadcrumb path to the home keyframe.',
  },
  {
    title: 'AI second opinion',
    body: 'An anomaly model of clean epochs scores every second. Advisory only: it can corroborate a spoofing alarm, never lower a score, clear an alarm or release the spoof latch.',
  },
  {
    title: 'Trust, LEDs, incident reports',
    body: 'GNSS Trust 0–100 with its reasons, green / yellow / red status LEDs on the Pi, and incident reports (time, location, interference type, equipment, logs) sealed with a SHA-256 hash chain.',
  },
];

const PIPELINE = [
  ['NMEA in', 'Quectel L86 over UART: satellites, C/N0, fix, time, 1 Hz'],
  ['Features', 'against a rolling baseline built only from epochs judged clean'],
  ['Two detectors', 'jamming (weighted sum) and spoofing (noisy-OR), CLEAR → SUSPECT → ALARM with hysteresis'],
  ['State machine', 'GNSS_OK → JAMMED / SPOOFED → VISION_NAV → RETURN_HOME → HOME_REACHED'],
  ['Back to GNSS', 'only after GNSS and the camera agree for 10 s (spoof latch); jamming clears when the detectors do'],
];

const HARDWARE = [
  'Raspberry Pi 4 (64-bit Raspberry Pi OS)',
  'Quectel L86 GNSS module, GPS + GLONASS',
  'Pi Camera (OV5647), pointing straight down',
  'Green / yellow / red status LEDs on GPIO',
];

export default function Landing({ onOpenDashboard, heroHeight = '100svh' }: { onOpenDashboard?: () => void; heroHeight?: string }) {
  return (
    <div className="bg-[#f8fafc]">
      <div className="relative w-full">
        <EtchedAccretion height={heroHeight} aria-label="Black hole with a crimson accretion disk">
          <div className="flex h-full flex-col justify-between p-6 sm:p-10">
            <div className="flex items-center justify-between font-mono text-[11px] uppercase tracking-[0.3em] text-white/60">
              <span>GNSS Guardian</span>
              <span className="hidden sm:inline">edge module · Raspberry Pi 4</span>
            </div>
            <div className="max-w-2xl">
              <h1 className="text-4xl font-light leading-[1.05] tracking-tight text-white sm:text-6xl">
                When the sky <span className="italic text-[#ff3b47]">lies</span>,
                <br />
                it knows.
              </h1>
              <p className="mt-5 max-w-xl text-sm leading-relaxed text-white/70 sm:text-base">
                GNSS Guardian watches a receiver for jamming and spoofing, tells you how far to trust it, and when it
                cannot, brings the vehicle home on its own camera.
              </p>
              <div className="mt-6 flex flex-wrap gap-3">
                {onOpenDashboard ? (
                  <button onClick={onOpenDashboard}
                    className="rounded-full bg-white px-5 py-2 text-sm font-semibold text-black hover:bg-white/85">
                    Open the dashboard
                  </button>
                ) : (
                  <a href="./" className="rounded-full bg-white px-5 py-2 text-sm font-semibold text-black hover:bg-white/85">
                    Open the dashboard
                  </a>
                )}
                <a href={REPO}
                  className="rounded-full border border-white/40 px-5 py-2 text-sm font-semibold text-white hover:bg-white/10">
                  Source on GitHub
                </a>
              </div>
            </div>
          </div>
        </EtchedAccretion>
      </div>

      <section className="mx-auto max-w-5xl px-6 py-16 sm:py-20">
        <p className={`font-mono text-[11px] uppercase tracking-[0.3em] ${RED}`}>The problem</p>
        <h2 className={`mt-3 text-2xl font-light sm:text-3xl ${INK}`}>Navigation trusts a signal anyone can drown out or fake.</h2>
        <p className={`mt-4 max-w-3xl leading-relaxed ${MUTED}`}>
          GNSS signals arrive weaker than the noise floor. A cheap jammer blanks them; a spoofer replaces them with a
          believable lie, and the receiver reports a confident, wrong position. GNSS Guardian sits next to the receiver
          as a small edge module: it reads the raw NMEA stream, cross-checks it against physics, the almanac and its own
          camera, says plainly when the position should not be used, and guides the vehicle home without it.
        </p>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-16">
        <p className={`font-mono text-[11px] uppercase tracking-[0.3em] ${RED}`}>What it does</p>
        <div className="mt-6 grid gap-px overflow-hidden rounded-2xl border border-[#e2e8f0] bg-[#e2e8f0] sm:grid-cols-2 lg:grid-cols-3">
          {PILLARS.map((p) => (
            <div key={p.title} className="bg-white p-6">
              <h3 className={`text-base font-semibold ${INK}`}>{p.title}</h3>
              <p className={`mt-2 text-sm leading-relaxed ${MUTED}`}>{p.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-16">
        <p className={`font-mono text-[11px] uppercase tracking-[0.3em] ${RED}`}>How it works</p>
        <ol className="mt-6 space-y-3">
          {PIPELINE.map(([step, body], i) => (
            <li key={step} className="flex gap-4 rounded-xl border border-[#e2e8f0] bg-white p-4">
              <span className="font-mono text-sm text-[#94a3b8]">{String(i + 1).padStart(2, '0')}</span>
              <div>
                <div className={`font-semibold ${INK}`}>{step}</div>
                <div className={`text-sm ${MUTED}`}>{body}</div>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="mx-auto grid max-w-5xl gap-10 px-6 pb-16 md:grid-cols-2">
        <div>
          <p className={`font-mono text-[11px] uppercase tracking-[0.3em] ${RED}`}>Hardware</p>
          <ul className={`mt-4 space-y-2 ${INK}`}>
            {HARDWARE.map((h) => (
              <li key={h} className="border-b border-[#e2e8f0] pb-2">{h}</li>
            ))}
          </ul>
          <p className={`mt-4 text-sm leading-relaxed ${MUTED}`}>
            Runs offline on the Pi. Nothing is transmitted over the air: demo attacks are injected in software before the
            detector, clearly marked as DEMO INJECTION, and the raw receiver log is kept untouched. Guidance is advisory;
            nothing commands the aircraft.
          </p>
        </div>
        <div>
          <p className={`font-mono text-[11px] uppercase tracking-[0.3em] ${RED}`}>On the simulator</p>
          <dl className="mt-4 grid grid-cols-2 gap-4">
            <div className="rounded-xl border border-[#e2e8f0] bg-white p-4">
              <dt className={`text-xs ${MUTED}`}>attack runs</dt>
              <dd className={`mt-1 font-mono text-3xl ${INK}`}>350</dd>
            </div>
            <div className="rounded-xl border border-[#e2e8f0] bg-white p-4">
              <dt className={`text-xs ${MUTED}`}>false-alarm epochs</dt>
              <dd className={`mt-1 font-mono text-3xl ${INK}`}>0</dd>
            </div>
            <div className="col-span-2 rounded-xl border border-[#e2e8f0] bg-white p-4">
              <dt className={`text-xs ${MUTED}`}>time to ALARM, every caught profile</dt>
              <dd className={`mt-1 font-mono text-3xl ${INK}`}>0–4 s</dd>
            </div>
          </dl>
          <p className={`mt-4 text-sm leading-relaxed ${MUTED}`}>
            7 jamming and spoofing profiles × 10 runs × 5 configurations, plus 30 min clean. Simulator data, not field
            data. The drift with natural signal strength is caught only with the camera on; that is why the camera is
            there.
          </p>
        </div>
      </section>
    </div>
  );
}
