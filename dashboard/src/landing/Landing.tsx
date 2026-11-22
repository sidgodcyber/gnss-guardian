import EtchedAccretion from '@/components/ui/etched-accretion';

// Project landing: the black-hole hero above the dashboard (and on landing.html).

const REPO = 'https://github.com/sidgodcyber/gnss-guardian-sathack';
export default function Landing({ onOpenDashboard, heroHeight = '100svh' }: { onOpenDashboard?: () => void; heroHeight?: string }) {
  return (
    <div>
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

    </div>
  );
}
