# Profile LedFx with Python 3.15

Use sampling to locate work in render, effect and event-loop threads. Keep
unprofiled paired measurements as the throughput evidence. The
[preview bottleneck finding](../findings/2026-10-05-preview-bottleneck.md) shows why
all preview messages and changed RGB images answer different questions.

## Select environments

Run commands from this tools checkout after `uv sync --locked`. The tools
`.venv/bin/python` is the fixed controller: it launches workers, validates
WebSocket frames and records metrics. `--python` selects the application worker
interpreter; `--repo` selects its LedFx source before import. The tools package
need not be installed in the worker. Preserve virtual-environment executable
paths rather than replacing them with resolved system-Python paths.

Supply a working CPython 3.15 environment with compatible LedFx runtime
dependencies and native wheels. The historical checkouts in the finding declare
Python `<3.15`; `uv sync` in those app checkouts cannot create this experimental
profiling environment. Do not edit their metadata or install incompatible ABI
wheels to force it. To provision a separate environment from a supplied, pinned
runtime dependency list and compatible wheel directory:

```sh
uv python install 3.15
uv venv --python 3.15 artifacts/app315
uv pip install --python artifacts/app315/bin/python \
  --only-binary=:all: --find-links /path/to/compatible-wheels \
  -r /path/to/validated-runtime-requirements.txt
artifacts/app315/bin/python -m profiling.sampling run --help
```

The requirements file must list runtime dependencies rather than the LedFx
package whose metadata excludes 3.15; workers load the explicit source checkout.
Include NumPy, Pillow, aiohttp, pydantic and psutil as well as the app's audio,
noise and other runtime dependencies. Missing CPython 3.15 wheels are an
unprepared environment, not a reason to relax dependency requirements. A tested
pre-existing 3.15 environment can be supplied directly. Use the same interpreter
build to capture and replay; the investigation used 3.15.0rc2, and future CLI
changes should be checked with `--help`.

Define paths once (use a new output directory for every campaign):

```sh
perf_repo="$PWD"
perf_controller="$perf_repo/.venv/bin/python"
perf_baseline=/work/LedFx-before
perf_candidate=/work/LedFx-after
perf_baseline_python=/work/LedFx-before/.venv/bin/python
perf_candidate_python=/work/LedFx-after/.venv/bin/python
perf_worker315="$perf_repo/artifacts/app315/bin/python"
perf_receiver=/work/ledfx-receiver
perf_output="$perf_repo/artifacts/preview-investigation"
mkdir -p "$perf_output"
```

The receiver is an externally supplied executable implementing the
[documented DDP collector interface](../README.md#compare-two-checkouts).
Alternatively omit `--receiver` everywhere to use the Python collector. Keep
receiver choice/binary identical across matched cases; do not build native tools
inside LedFx to run these commands.

## Establish unprofiled controls

This runs adjacent randomized before/after pairs with render pacing bypassed and
normal 60 Hz preview pacing. Worker dependencies should match except for the
intended treatment; the controller stays fixed. Record fresh source/dependency
and installed native hashes from the generated metadata.

```sh
"$perf_controller" -m ledfx_performance.matrix \
  --baseline-repo "$perf_baseline" --baseline-python "$perf_baseline_python" \
  --candidate-repo "$perf_candidate" --candidate-python "$perf_candidate_python" \
  --effects rainbow --pixels 50000 --streams none,full \
  --unpaced --seconds 8 --warmup 2 --repeats 3 \
  --receiver "$perf_receiver" --output "$perf_output/paired-preview60"
```

Repeat with `--unpaced-preview` and a new output directory for unlimited-preview
stress. Omit both unpaced flags for configured render/preview pacing. `full`
raises the preview cap to the whole frame, including beyond the product's
65,536-pixel limit; label larger full-preview tests as diagnostics. Rainbow's
animation update cadence remains about 60 Hz even when render/transport FPS rises.

## Capture worker CPU and GIL samples

These commands profile the candidate worker's threads while keeping the
controller unprofiled. `--sampling` launches the supplied worker through
`profiling.sampling run --all-threads --mode MODE --binary` at its default 1 kHz.
CPU samples select threads observed on CPU; GIL samples select the holder of the
interpreter lock. Neither is a precise native/kernel CPU-time measurement.

```sh
for perf_mode in cpu gil; do
  "$perf_controller" -m ledfx_performance.pixel \
    --repo "$perf_candidate" --python "$perf_worker315" \
    --effects rainbow --pixels 50000 --streams full \
    --unpaced --unpaced-preview --seconds 8 --warmup 2 --repeats 1 \
    --sampling "$perf_mode" --receiver "$perf_receiver" \
    --output "$perf_output/full-stress-$perf_mode.jsonl"
done
```

Capture separate matched cases with `--unpaced-preview` omitted for 60 Hz
preview, or `--streams none` for no subscriber. Supply the baseline checkout and
its compatible 3.15 worker to compare implementations. Profiling changes timing:
use these rows to locate work, not to replace the unprofiled paired ratios.

To sample client/controller CPU separately, use a prepared 3.15 interpreter with
the tools controller dependencies. The launcher loads the tools package from
source. The inner `--sampling gil` still profiles the app worker independently:

```sh
perf_controller315=/work/tools-controller315/bin/python
"$perf_controller315" -m profiling.sampling run \
  --all-threads --mode cpu --binary -o "$perf_output/controller-cpu.bin" \
  "$perf_repo/src/ledfx_performance/entry.py" ledfx_performance.pixel \
  --repo "$perf_candidate" --python "$perf_worker315" \
  --effects rainbow --pixels 50000 --streams full \
  --unpaced --unpaced-preview --seconds 8 --warmup 2 --repeats 1 \
  --sampling gil --receiver "$perf_receiver" \
  --output "$perf_output/full-stress-controller-gil.jsonl"
```

Receiver/client/controller CPU is excluded from the server CPU metric but still
shares host resources. High client sample counts alone do not prove saturation.
Do not run tests, builds or other campaigns concurrently with timed cases.

## Replay and select measurement windows

Each row retains `sampling.bin` under its `.artifacts/EFFECT/REPEAT-STREAM/`
directory. Replay with the capture interpreter:

```sh
perf_binary="$perf_output/full-stress-cpu.jsonl.artifacts/rainbow/0-full/sampling.bin"
"$perf_worker315" -m profiling.sampling replay --collapsed \
  -o "$perf_output/full-stress-cpu.collapsed" "$perf_binary"
"$perf_worker315" -m profiling.sampling replay --flamegraph \
  -o "$perf_output/full-stress-cpu.html" "$perf_binary"
"$perf_controller" -m ledfx_performance.profiles \
  "$perf_output/full-stress-cpu.jsonl" --python "$perf_worker315" --timeout 120
```

These replays include startup, warmup and shutdown. The package report filters
for active render/effect/audio/preview stacks and counts recursive frames once;
it does not restrict timestamps to the measured interval. Its active-sample
percentages have a different denominator from an all-thread windowed summary.
Standard collapsed/flamegraph replay has no measurement-window selector in the
3.15.0rc2 CLI used here. A full-recording flamegraph must not be labeled as a
measured-window CPU share.

For windowed analysis, retain timestamped binary records and explicitly filter
before aggregating stacks. On the Linux setup used for the finding, sample
microsecond timestamps and controller `perf_counter` timestamps share the
monotonic clock. The central interval was selected as:

```text
start_us = round((first_snapshot.controller_response_seconds + 0.050) * 1_000_000)
end_us   = round((last_snapshot.controller_request_seconds - 0.050) * 1_000_000)
retain start_us <= sample_timestamp_us < end_us
```

Snapshot controller timestamps are available with the external collector. Verify
clock correspondence on the host rather than assuming cross-process/platform
alignment. Store start/end, source row, interpreter/recording provenance and
margin with the derived result; the finding's retained windows were about 7.9 s.
Preserve thread identity and report category shares of valid captured samples,
not nominal sampling opportunities or exact CPU-time shares. Keep sampling
errors separate: nonblocking recording may miss busy/interpreter transitions,
and whole-recording error rates need not equal the trimmed-window error rate.

Check repeat variation, errors and top stacks before inferring a bottleneck.
Python frames at native bindings do not resolve Rust or kernel internals.
Deterministic cProfile selectors cover partial functions and may mix cross-thread
timing contexts on some builds; prefer sampling for thread attribution. Raw
rows, recordings, manifests and generated visuals stay in ignored `artifacts/`
or external storage. Reviewed written findings can be committed in `findings/`.
