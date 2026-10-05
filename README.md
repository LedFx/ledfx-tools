# ledfx-tools

A collection of tools related to LedFx. Existing installers and the Discord GIF
bot remain in `installers/` and `discord/`.

The `ledfx_performance` package measures a separately supplied LedFx application
checkout. It owns reusable app benchmarks, effect fixtures, profile reports and
paired before/after comparisons. It never writes to that checkout. Protocol
encoder benchmarks, native build/release tooling and packet microbenchmarks
belong in [ledfx-senders](https://github.com/LedFx/ledfx-senders).

## Setup

Use Python 3.11 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyrefly check
uv run pytest -q
```

Install each LedFx checkout's dependencies in its own environment before running
it. The worker interpreter must also have NumPy, Pillow, aiohttp, pydantic and
psutil (these are present in LedFx's development environment). The external
tools package is loaded by its own source launcher; it does not need to be
installed inside either app checkout. Both --repo and --python are required.
Supply the actual virtual environment's Python path, such as
`/work/LedFx/.venv/bin/python`.

A normal package install also provides `ledfx-pixel-bench`, `ledfx-paired-bench`
and `ledfx-profile-report`. `uv run python -m ledfx_performance.pixel --help`
describes all supported measurement controls. The controller's dependencies are
separate from the selected worker environment; recorded worker versions identify
the app's actual NumPy, audio, noise and sender packages.

## Whole application measurement

Run a selected effect with independent DDP output validation and no/full browser
preview. Use absolute checkout and interpreter paths:

```sh
uv run python -m ledfx_performance.pixel \
  --repo /work/LedFx --python /work/LedFx/.venv/bin/python \
  --effects rainbow --pixels 50000 --streams none,full \
  --warmup 2 --seconds 8 --repeats 3 \
  --output /tmp/ledfx-measurements/rainbow.jsonl
```

The standard asyncio loop is used for both old and new applications. Normal
effect cadence and configured render/preview rate limits remain active.
`--unpaced` removes only the target virtual's render pacing, leaving
animation/audio update cadence intact; `--unpaced-preview` separately bypasses
the preview rate limit. These are diagnostic throughput controls and are
recorded in each row.

`none` subscribes no browser preview. `default` requests up to 81 preview
pixels. `full` explicitly bypasses the product's 65,536-pixel preview cap inside
the worker, including at 500,000 pixels. Full preview is a stress workload, not
normal product configuration. Preview shape, base64 encoding and complete RGB
length are validated. These structural checks do not prove the effect or encoder
produced the expected colors. An independent receiver process checks complete
ordered DDP RGB byte coverage rather than counting PUSH flags as frames.

For an effect catalog, real synthetic audio analysis and local
media/screen/source fixtures:

```sh
uv run python -m ledfx_performance.pixel \
  --repo /work/LedFx --python /work/LedFx/.venv/bin/python \
  --effects all --synthetic-audio --fixtures --pixels 50000 \
  --streams full --repeats 1 --seconds 8 --keep-going \
  --output /tmp/ledfx-measurements/catalog.jsonl
```

Matrix layouts automatically choose a divisor near the square root. `--rows`
overrides layout and must divide the pixel count. `--effect-config` applies a
JSON object of overrides. Local fixtures supply GIF/Image Spin media, synthetic
screen capture, frontend frames and a source virtual for Blender/Radial. Random
Flash's fixture increases hit probability so a short screening run is active.
Synthetic PCM replaces audio hardware but passes through real LedFx
aubio/melbank DSP. Fixtures omit capture hardware, browser capture and device-
specific overhead.

## Compare two checkouts

The paired driver runs adjacent baseline/candidate cases in randomized treatment
order, inside randomized repeat blocks. Both checkouts and interpreters are
required; there are no frozen commits, import fallbacks or hard-coded wheel
APIs:

```sh
uv run python -m ledfx_performance.matrix \
  --baseline-repo /work/LedFx-before \
  --baseline-python /work/LedFx-before/.venv/bin/python \
  --candidate-repo /work/LedFx-after \
  --candidate-python /work/LedFx-after/.venv/bin/python \
  --effects rainbow --streams none,full --pixels 50000 \
  --warmup 2 --seconds 8 --repeats 3 \
  --output /tmp/ledfx-measurements/paired-50k
```

Repeat with `--pixels 500000` for scaling. Run a separate diagnostic directory
with `--unpaced --unpaced-preview` for uncapped render/preview capacity.
`--plan-only` saves a manifest of exact commands without launching LedFx. It
must use a new output directory. `--keep-going` records failed cases and
continues; the overall command exits unsuccessfully if any trial fails. Failures
never enter successful paired ratios.

For a direct baseline gate, supply `--baseline /tmp/before.jsonl` to the pixel
runner. It compares median rendering, complete DDP delivery and preview
throughput only for matching
effect/configuration/geometry/pacing/profile/receiver/Python scenarios. Missing
matches fail instead of silently comparing unlike workloads. This gate does not
equate repeated static image delivery with animation speed.

The optional `--receiver /absolute/path/to/ddp-receiver` uses an externally
built native collector from `ledfx-senders`. It must implement the existing
`ddp-structural PIXELS batched BIND` JSON-lines snapshot/stop interface and
disclose `unique_identity: false`. The default Python receiver needs no native
build; use the same receiver choice for both treatments. Receiver
saturation/drops limit what any local benchmark can conclude.

## Profile effects

See the [Python 3.15 profiling guide](docs/python315-profiling.md) for environment
setup, CPU/GIL capture, controller sampling and replay. The reviewed
[preview bottleneck finding](findings/2026-10-05-preview-bottleneck.md) separates
observed results from the follow-up subscription/coalescing roadmap.

`--profile main`, `--profile render` and `--profile effect` capture cProfile
only during the measured interval. Inspect retained `.prof` files with `pstats`
or SnakeViz. Keep profiled throughput separate from the primary unprofiled
campaign.
The render option wraps frame assembly and device sending, rather than the whole
render cycle. These selectors do not guarantee thread isolation: CPython builds
can include other threads in a deterministic profile and mix their timing
contexts. Treat these profiles as hotspot hints; use all-thread sampling for
thread attribution and avoid deriving CPU percentages from overlapping timings.

Python 3.15 or newer in the worker environment supports `--sampling cpu`,
`--sampling wall` and `--sampling gil`. Recordings include
startup/warmup/shutdown; replay them using the same interpreter version
(`--timeout` bounds each replay):

```sh
uv run python -m ledfx_performance.pixel \
  --repo /work/LedFx --python /work/LedFx/.venv/bin/python3.15 \
  --effects all --synthetic-audio --fixtures --pixels 50000 \
  --streams full --sampling cpu --repeats 1 --keep-going \
  --output /tmp/ledfx-measurements/profiled-catalog.jsonl
uv run python -m ledfx_performance.profiles \
  /tmp/ledfx-measurements/profiled-catalog.jsonl \
  --python /work/LedFx/.venv/bin/python3.15
```

Profile summaries rank leaf and inclusive samples within active
render/effect/audio/preview stacks, excluding imports/shutdown from active
shares and counting recursive frames once per stack. Sampling results and short
catalog runs identify candidate hotspots; they are not precise speed
measurements.

## Results and limits

Outputs live in ignored `artifacts/` or outside this repository. Existing output
files/directories are refused. Measurements, manifests, captured logs, profiles
and generated benchmark artifacts are not tracked in source. Reviewed written
findings live in `findings/`. Each pixel result has adjacent
metadata with app/source/dependency and installed sender hashes, a summary and
`.artifacts/` containing logs, readiness/provenance data, timing samples and
requested profiles. The paired driver retains exact commands, process status,
stdout/stderr and partial outcomes before aborting on failure. It cleans up only
its owned process tree. Receiver FPS uses receiver-side snapshot intervals;
server CPU uses its own process-CPU observation interval. Paired comparisons
keep the controller interpreter/dependencies fixed while selecting each worker
environment. Effective validated effect configs are recorded, and generated
media paths compare by fixture content identity.

Server CPU includes instrumentation and uses 100% for one core; independent
receiver, controller/client and sampling process CPU are excluded. RSS is the
end-of-window footprint, including harness buffers, and is not peak-memory or
leak evidence. Useful metrics include render FPS, independently received
complete DDP FPS, changed preview FPS and server CPU. DDP byte coverage cannot
establish unique frame identity across sequence wrap/reorder, and a completion
can cross a measurement boundary. Static repeated frames are transport
throughput rather than new animation frames. Changed preview counts include the
first observed image.

Match environments, workload configs, instrumentation and receiver mode; inspect
range/variation and failed trials. Short single-host loopback results cannot
establish physical LED latency, LAN/Wi-Fi capacity, every preset's performance
or hardware behavior. Historical Python byte-buffer/scheduler prototypes and
recorded spike archives were intentionally not migrated.

The performance package and its tests derive from LedFx tools and retain the
GPL-3.0 license in `PERFORMANCE-LICENSE`. The existing tools retain their
original MIT license in `LICENSE`.
