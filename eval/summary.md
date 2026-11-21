# Evaluation (simulator only, 10 runs per attack profile, 1 clean run of 30 min)

Every number here is from the SIMULATOR (tools/fake_l86.py), not real L86 data. Camera = simulated velocity (true walk + 0.2 m/s noise). AI model: provisional, not real L86 (simulator + phone proxies).

| Profile | Config | Detected (ALARM) | Latency to ALARM, median (min–max) | to SUSPECT | First indicator | False-alarm epochs | Wrong detector | Source |
|---|---|---|---|---|---|---|---|---|
| jam-sudden | full | 10/10 | 0 s | 0 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-sudden | no-camera | 10/10 | 0 s | 0 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-sudden | no-AI | 10/10 | 0 s | 0 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-sudden | no-sky | 10/10 | 0 s | 0 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-sudden | rules-only | 10/10 | 0 s | 0 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-ramp | full | 10/10 | 3 s | 2 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-ramp | no-camera | 10/10 | 3 s | 2 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-ramp | no-AI | 10/10 | 3 s | 2 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-ramp | no-sky | 10/10 | 3 s | 2 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| jam-ramp | rules-only | 10/10 | 3 s | 2 s | drop_frac | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-jump | full | 10/10 | 0 s | 0 s | speed | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-jump | no-camera | 10/10 | 0 s | 0 s | speed | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-jump | no-AI | 10/10 | 0 s | 0 s | speed | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-jump | no-sky | 10/10 | 0 s | 0 s | speed | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-jump | rules-only | 10/10 | 0 s | 0 s | speed | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift | full | 10/10 | 0 s | 0 s | uniform_cn0 | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift | no-camera | 10/10 | 0 s | 0 s | uniform_cn0 | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift | no-AI | 10/10 | 0 s | 0 s | uniform_cn0 | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift | no-sky | 10/10 | 1 s (0–1) | 0 s | uniform_cn0 | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift | rules-only | 0/10 | - | 0 s | uniform_cn0 | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-time | full | 10/10 | 0 s | 0 s | time | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-time | no-camera | 10/10 | 0 s | 0 s | time | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-time | no-AI | 10/10 | 0 s | 0 s | time | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-time | no-sky | 10/10 | 0 s | 0 s | time | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-time | rules-only | 10/10 | 0 s | 0 s | time | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift-natural | full | 10/10 | 2.5 s (2–4) | 2 s (1–3) | vision | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift-natural | no-camera | 0/10 | - | - | - | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift-natural | no-AI | 10/10 | 2.5 s (2–4) | 2 s (1–3) | vision | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift-natural | no-sky | 10/10 | 2.5 s (2–4) | 2 s (1–3) | vision | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-drift-natural | rules-only | 0/10 | - | - | - | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-sky | full | 10/10 | 0 s | 0 s | sky_geom | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-sky | no-camera | 10/10 | 0 s | 0 s | sky_geom | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-sky | no-AI | 10/10 | 0 s | 0 s | sky_geom | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-sky | no-sky | 10/10 | 2.5 s (2–4) | 2 s (1–3) | vision | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| spoof-sky | rules-only | 0/10 | - | - | - | 0 | 0 | simulator, seeds 100, 101, 102, 103, 104, 105, 106, 107, 108, 109 |
| clean | full | – | - | - | - | 0 in 30 min | – | simulator, seeds 100 |
| clean | no-camera | – | - | - | - | 0 in 30 min | – | simulator, seeds 100 |
| clean | no-AI | – | - | - | - | 0 in 30 min | – | simulator, seeds 100 |
| clean | no-sky | – | - | - | - | 0 in 30 min | – | simulator, seeds 100 |
| clean | rules-only | – | - | - | - | 0 in 30 min | – | simulator, seeds 100 |
