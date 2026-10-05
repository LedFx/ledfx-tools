# Why full preview hides the apparent 2.4× gain

## Finding

The original 2.376× result is a whole-application improvement in the no-subscriber case, substantially explained by skipping previews that nobody
consumes. It is not a measurement of a 2.4× DDP sender improvement. Unlimited
full preview adds a shared conversion/JSON/scheduling workload and repeatedly
transmits unchanged images. Independent Python 3.15 CPU/GIL sampling confirms
that this load consumes the main loop and interpreter resources.

Current websocket mailbox coalescing happens after expensive payload conversion
and encoding. The follow-up roadmap below separates the observed bottleneck from
proposed subscription filtering and earlier source-frame coalescing.

## Unprofiled results

Baseline: LedFx main `a5ec8278ae80bfd6c54049648f7f16359c34bf23`. Candidate:
clean native/effect/preview integration
`091ac8b9c6c902406c92092913475a40ab4f89a5`. Same CPython 3.12.14, NumPy 2.5.3,
controller, standard asyncio loop and independent native loopback DDP receiver.
Each treatment has three randomized adjacent pairs, two seconds warmup and eight
seconds measured. Render pacing is bypassed; Rainbow's effect-update cadence
stays about 60 Hz. Individual-row medians below differ from medians of paired
ratios.

| 50,000 RGB pixels | Baseline render FPS | Candidate render FPS | Median paired ratio | Pair range |
| --- | ---: | ---: | ---: | ---: |
| No subscriber; preview pacing bypassed | 496 | 1,148 | 2.376× | 2.215–2.425× |
| Full preview; preview pacing bypassed | 681 | 719 | 1.023× | 1.009–1.103× |
| Full preview; normal 60 Hz preview limit | 1,040 | 1,101 | 1.060× | 0.980–1.127× |

The small full-preview before/after differences overlap plausible host variation
and do not justify a confident sender speedup claim. With normal preview pacing,
candidate output remains around 1,100 render FPS and approximately 60 websocket
FPS. With unlimited preview it renders around 719 FPS and sends around 721
websocket FPS. Both deliver about 59 changed RGB images per second. This is
repeated-frame throughput, not 1,100 newly computed animations or physical LED
latency.

Normal-preview process CPU medians are 100.11% baseline and 92.37% candidate
(100% is one core). This short host-local sample does not establish a precise
CPU saving or a universal effect result. Independent DDP completions are also
affected by packet loss/reordering under stress; zero malformed packets does not
mean zero incomplete frames.

## The hidden no-preview baseline cost

The benchmark's `none` case selects the default 81-pixel preview size; its
`full` case selects all 50,000 pixels. The baseline converts/resizes and encodes
previews even when no websocket subscribes. It processes both DeviceUpdateEvent
and VirtualUpdateEvent, around two callbacks per render. In `none`, resizing
uses Pillow and touches the complete source image. In `full`, resizing is
skipped.

Measured callback mean wall times are approximately 0.654 ms baseline-none,
0.136 ms baseline-full, and 0.001 ms candidate-none. The candidate returns
immediately when there are no visualization listeners. This also explains why
the baseline appears faster after a full-preview client is added: it stops doing
the much more expensive downsampling path.

CPU sampling of the central measurement window attributes approximately 46% of
captured baseline-none CPU samples to preview preparation, with Pillow resize
the dominant leaf. That work disappears from the candidate-none profile. No
sender-only interpretation of the 2.4× figure is supported.

## What full preview costs

At 50,000 RGB pixels, each image has 150 kB raw RGB or 200 kB base64 before JSON
overhead. Candidate unlimited preview transmits roughly 144 MB/s while the RGB
image changes about 59 times/s. The transport named `compressed` uses base64; it
does not perform image or zlib compression.

Both source update callbacks convert/encode. Their matching benchmark
device/virtual IDs allow the later mailbox to coalesce them, but only after both
payloads were prepared. General device and virtual outputs can differ, so
deleting either source event would be incorrect. Source callbacks also reach the
event loop through `call_soon_threadsafe`; the final websocket mailbox does not
bound/coalesce the earlier source-callback queue.

The candidate array-assembly mean grows from about 0.148 ms without preview to
0.618 ms with unlimited full preview. That assembly implementation is unchanged
between these modes. Wall durations include thread descheduling and
lock/interpreter contention, rather than demonstrating a fourfold arithmetic
regression.

## Independent sampling

Used an isolated CPython 3.15.0rc2 environment with a compatible native sender
wheel and the same before/after source checkouts. Sampling is separate
diagnostic evidence from the CPython 3.12 unprofiled comparisons; it does not
establish identical percentages on other Python versions. Python 3.15 sampling
records all threads at 1 kHz in CPU or GIL mode. Six CPU profiles cover both
treatments for no subscriber, unlimited full preview and 60 Hz full preview. Two
additional candidate GIL profiles also independently sample the controller's CPU
activity.

Profiles are trimmed to the central approximately 7.9 seconds using the
controller timestamps around native receiver snapshots, with 50 ms removed from
each boundary. This excludes startup, warmup and shutdown; binary timestamps and
controller timestamps share the Linux monotonic clock. Windowed percentages
below use valid captured thread samples; all ten retained windows were
independently checked against explicit bounds and source-row timestamps. The
recordings and raw manifests are not committed here. See the [Python 3.15
profiling guide](../docs/python315-profiling.md) for capture, replay and window
selection.

| Candidate workload | Main-loop share of captured CPU samples | Preview preparation | Websocket JSON | Main-loop share of captured GIL samples |
| --- | ---: | ---: | ---: | ---: |
| Unlimited full preview | 35% | 12% CPU / 16% GIL | 10% CPU / 32% GIL | 65% |
| Full preview at 60 Hz | 10% | <1% CPU / 2% GIL | 1% CPU / 4% GIL | 21% |

Shares refer to valid captured samples in the selected window, not measured
fractions of exact CPU time. Nonblocking recording reports approximately 16–20%
sampling errors in CPU runs and 10–12% in GIL runs, so treat percentages as
approximate hotspot attribution with possible sampling bias. They do not resolve
Rust/kernel internals: a stack stopped at `_PacketSender.send` includes native
work and binding boundaries.

The separate controller capture has 2,027 central-window CPU samples under
unlimited preview versus 189 at 60 Hz. JSON decoding and base64 validation are
its largest active leaves. These samples are not proof that the client is
saturated; the receiver/controller share host resources, and their CPU is
excluded from server CPU metrics. Main-loop JSON/interpreter pressure and
redundant preview work are directly observed; precise division among scheduler,
socket, client and memory limits needs a narrower follow-up.

The initial cProfile runs were unsuitable for thread-attributed percentages:
this CPython 3.12 build records other-thread calls even when enabled from the
main thread, and the resulting shared timing contexts have inconsistent
cumulative values. A minimal second-thread reproduction and actual profiles
verified this. The [tools README](../README.md#profile-effects) documents the
limitation and recommends sampling for thread attribution.

## Follow-up roadmap

After the ledfx-senders integration lands in LedFx, evaluate subscription
filtering and early source-frame coalescing as a separate application change.
These are proposed experiments; no preview implementation was changed for this
report.

1. Match interested subscriptions using source metadata before
   resizing/encoding. A listener for one virtual should not cause every
   unrelated device/virtual to prepare a payload. Preserve conservative behavior
   for filters that depend on computed payload fields.
2. Keep one pending source frame per `(is_device, vis_id)` and select the newest
   at the configured preview cadence before conversion. Bound pending work
   upstream of the expensive conversion and JSON path, while preserving reliable
   control messages.
3. Preserve stable ownership of NumPy snapshots and device/virtual differences.
   Device arrays can be reused/mutated; delayed processing must not read an
   incoherent frame. Keep shape, brightness, numeric conversion and source
   identity semantics.
4. Consider cached encoded output for repeated unchanged images only with
   explicit invalidation for source, layout, brightness and transmission
   settings. Measure whether identity/version tracking is cheaper than repeated
   comparisons; do not introduce a full-frame equality pass blindly.
5. Re-run real 60 Hz and diagnostic unlimited-preview cases with changed-frame
   counts and structural output validation. Use a sender-specific campaign in
   ledfx-senders for transport-only claims. Benchmark/source hashes and
   installed native wheel hashes must remain explicit.

## Provenance and reproduction

The unprofiled candidate's CPython 3.12 native extension SHA256 was
`e90afb380c1afd618febd9e8874a1c24acbb2be6b1294d087be63dcec2f448f3`; the
diagnostic CPython 3.15 extension SHA256 was
`95b16b75b362257573ecdbd7b138b084c38ee07fd872b14213a312223fb773dc`. Both used
the independent receiver binary SHA256
`2caa2a2425fb6f100c480c78a6045f9a86a6b2234a6e43a31093aef1279e4093`. These
unpublished implementation bytes distinguish this checkpoint evidence from any
same-version released package. Application source revisions above identify
historical checkpoints, not a promise that their native dependency APIs are
available from the package index.

Use the [profiling guide](../docs/python315-profiling.md) to reproduce the
workload on supplied compatible checkouts/environments. It includes unprofiled
paired controls, CPU/GIL capture, optional separate controller capture and
replay. Retain source/dependency/native hashes, exact commands, raw rows and
window bounds with each new campaign. This report preserves reviewed findings
rather than raw measurements, profiles or flamegraphs. Short single-host
loopback evidence does not establish physical LED latency, general network
capacity or every effect's performance.
