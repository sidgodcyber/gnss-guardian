import EtchedAccretion, { type AccretionParams } from '@/components/ui/etched-accretion';

// Grainy, low-resolution, slow, frame-by-frame: a worn film loop behind the hero.
const LOFI: Partial<AccretionParams> = { speed: 0.35, grain: 2.2, exposure: 1.1, vignette: 0.7 };

const REPO = 'https://github.com/sidgodcyber/gnss-guardian-sathack';

const PILLARS = [
  {
    title: 'Detects jamming',
    body: 'Signal strength (C/N0) against a clean baseline per satellite, satellites lost, HDOP rising, fix lost. A foil-covered antenna reads as blockage within seconds.',
  },
  {
    title: 'Detects spoofing',
    body: 'Position jumps faster than the rig can move, GNSS clock jumps against the host clock, uniform signal strength from one transmitter, satellites the baseline never saw.',
  },
  {
    title: 'Checks the sky',
    body: 'Reported GPS satellite directions against a YUMA almanac on disk. A replayed or foreign sky, or satellites that should be below the horizon, raise the alarm without a camera.',
  },
  {
    title: 'Navigates on its camera',
    body: 'A down-facing Pi camera tracks the ground (visual odometry with keyframes). When GNSS cannot be trusted it guides the way home; GNSS is trusted again only after it agrees with the camera for 10 s.',
  },
  {
    title: 'AI second opinion',
    body: 'An anomaly model of clean epochs scores every second. Advisory only: it can corroborate a spoofing alarm, never lower a score, clear an alarm or release the latch.',
  },
  {
    title: 'Trust, LEDs, reports',
    body: 'GNSS Trust 0–100 with its reasons, three status LEDs on the Pi, and incident reports (time, location, interference type, equipment, logs) sealed with a SHA-256 hash chain.',
  },
];

const HARDWARE = ['Raspberry Pi 4', 'Quectel L86 GNSS (GPS + GLONASS)', 'Pi Camera (OV5647), facing down', 'Green / yellow / red status LEDs'];

export default function Landing() {
  return (
    <main className="min-h-screen bg-[#030307] text-[#e8e6e3]">
      <div className="lofi relative w-full">
        <EtchedAccretion params={LOFI} renderScale={0.3} fps={8} aria-label="Grainy film loop of a black hole">
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
                cannot, brings you home on its own camera.
              </p>
              <div className="mt-6 flex flex-wrap gap-3">
                <a href="./" className="rounded-full bg-white px-5 py-2 text-sm font-semibold text-black hover:bg-white/85">
                  Open the dashboard
                </a>
                <a
                  href={REPO}
                  className="rounded-full border border-white/40 px-5 py-2 text-sm font-semibold text-white hover:bg-white/10"
                >
                  Source on GitHub
                </a>
              </div>
            </div>
          </div>
        </EtchedAccretion>
      </div>

      <section className="mx-auto max-w-5xl px-6 py-16 sm:py-20">
        <p className="font-mono text-[11px] uppercase tracking-[0.3em] text-[#ff3b47]">The problem</p>
        <h2 className="mt-3 text-2xl font-light text-white sm:text-3xl">Navigation trusts a signal anyone can drown out or fake.</h2>
        <p className="mt-4 max-w-3xl leading-relaxed text-white/65">
          GNSS signals arrive weaker than the noise floor. A cheap jammer blanks them; a spoofer replaces them with a
          believable lie, and the receiver reports a confident, wrong position. GNSS Guardian sits next to the receiver
          as a small edge module: it reads the raw NMEA stream, cross-checks it against physics, the almanac and its
          own camera, and says plainly when the position should not be used.
        </p>
      </section>

      <section className="mx-auto max-w-5xl px-6 pb-16">
        <p className="font-mono text-[11px] uppercase tracking-[0.3em] text-[#ff3b47]">What it does</p>
        <div className="mt-6 grid gap-px overflow-hidden rounded-2xl border border-white/10 bg-white/10 sm:grid-cols-2 lg:grid-cols-3">
          {PILLARS.map((p) => (
            <div key={p.title} className="bg-[#07070c] p-6">
              <h3 className="text-base font-semibold text-white">{p.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-white/60">{p.body}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="mx-auto grid max-w-5xl gap-10 px-6 pb-16 md:grid-cols-2">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.3em] text-[#ff3b47]">Hardware</p>
          <ul className="mt-4 space-y-2 text-white/75">
            {HARDWARE.map((h) => (
              <li key={h} className="border-b border-white/10 pb-2">{h}</li>
            ))}
          </ul>
          <p className="mt-4 text-sm leading-relaxed text-white/50">
            Runs offline on the Pi. Nothing is transmitted over the air: demo attacks are injected in software before
            the detector, clearly marked as DEMO INJECTION, and the raw receiver log is kept untouched.
          </p>
        </div>
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.3em] text-[#ff3b47]">On the simulator</p>
          <dl className="mt-4 grid grid-cols-2 gap-4">
            <div className="rounded-xl border border-white/10 p-4">
              <dt className="text-xs text-white/50">attack runs</dt>
              <dd className="mt-1 font-mono text-3xl text-white">350</dd>
            </div>
            <div className="rounded-xl border border-white/10 p-4">
              <dt className="text-xs text-white/50">false-alarm epochs</dt>
              <dd className="mt-1 font-mono text-3xl text-white">0</dd>
            </div>
            <div className="col-span-2 rounded-xl border border-white/10 p-4">
              <dt className="text-xs text-white/50">time to ALARM, every caught profile</dt>
              <dd className="mt-1 font-mono text-3xl text-white">0–4 s</dd>
            </div>
          </dl>
          <p className="mt-4 text-sm leading-relaxed text-white/50">
            7 jamming and spoofing profiles × 10 runs × 5 configurations, plus 30 min clean. Simulator data, not field
            data. The drift with natural signal strength is caught only with the camera on; that is why the camera is
            there.
          </p>
        </div>
      </section>

      <footer className="border-t border-white/10 px-6 py-8 text-center font-mono text-[11px] uppercase tracking-[0.25em] text-white/40">
        GNSS Guardian · guidance is advisory, nothing commands the aircraft
      </footer>
    </main>
  );
}
