# Storage Optimization

Optional maintenance that converts files to smaller formats. It is off the
critical path in every sense: it runs last, one job at a time, on a fraction of
the machine, and **it never replaces an original**.

Nothing here starts on its own until you queue something.

## The pipeline

```
Opportunity  ->  Queue  ->  Waiting  ->  Running  ->  Verifying  ->  Ready for review
  (advice)     (you ask)   (a reason)   (encoding)   (checking)      (your decision)
```

There is no step after **Ready for review**. LibrAIry cannot put a converted
file into your library, and the only action offered on a finished result is
**Discard result**. Your original was never touched, so there is nothing to
undo and nothing to keep — it is already what your library holds.

## Advisor and executor disagree, on purpose

The **Advisor** is read-only. It looks at what you have and reports where space
is being spent. It never converts anything.

The **executor** answers a different question: can this file be converted
automatically without losing part of it? A 14 GB film with three audio tracks
and two subtitle streams is a real opportunity and something v1 will not touch.
You will see it reported and refused, with the reason.

Making the Advisor stupider so that everything it reports is executable would
lose you the information. The two judgements stay apart.

### What v1 will run

| Operation | Quality | Notes |
|---|---|---|
| WAV → FLAC | **Lossless** | Proven bit-exact: the decoded PCM hashes match |
| Container remux | **Remux** | Stream copy only. If a codec changes, verification fails |
| H.264 → HEVC | **Lossy** | One video, one compatible audio track, nothing else |

Audio is always copied, never re-encoded. If a track cannot be carried over
unchanged, the job is refused rather than given a second lossy encode nobody
asked for.

## When a job may start

Two different settings, and confusing them is the usual mistake.

**Run policy** and **Window** decide *when* a job may **begin**. `Manual only`
means nothing starts by itself; a maintenance window means jobs may begin
inside it. A window that ends before it starts spans midnight, which is
usually what you want.

**A job already running is never killed by the clock.** Something that began at
05:55 carries on past 06:00. The window was a statement about starting, and
stopping an encode at a boundary buys a half-written file and an hour of wasted
CPU.

**Run now** lifts the clock, and only the clock. It cannot reach past a
protected root, a changed source, the concurrency limit, the disk reserve, the
load ceiling or the resource policy. If it is still blocked you are told which
gate — "Waiting for disk space", not "Run now failed".

## Resource use: Low, measured

`Resource use` is fixed at **Low** and `Concurrent jobs` at **1**. Both are
shown on the Settings page and neither can be raised. A higher setting whose
cost nobody has measured is a promise nobody checked.

Low is not `-threads 2`. FFmpeg's `-threads` is FFmpeg's; **libx265 builds its
own worker pool**, sized from the CPUs it can see, and ignores it. Measured in
the production image — 10 CPUs visible, no cgroup CPU quota — as average CPU
seconds consumed per wall-clock second:

| x265 setting | Parallelism | Wall time |
|---|---|---|
| `pools=1:frame-threads=1` | 1.05x | 19.3 s |
| `pools=2:frame-threads=2` | **2.09x** | 9.4 s |
| `pools=4:frame-threads=4` | 4.02x | 5.0 s |
| `pools=8:frame-threads=8` | 6.22x | 4.0 s |

The figure tracks the pool size and not the machine, which is what makes it an
**absolute** bound rather than a share: two cores' worth on a ten-core NAS, and
the same two cores on a sixty-core one. Low is `pools=2:frame-threads=2`.

Thread count was measured over the same runs and is not the metric: an
unbounded encode and a bounded one differed by a couple of threads and by a
factor of four in CPU consumed. `scripts/measure_encoder_load.py` reproduces
the table on your own hardware.

Where the runtime provides `nice` and `ionice`, jobs run at low scheduling
priority as well. Neither is load-bearing, and neither is installed if absent.

## The worker keeps working

An encode does **not** occupy LibrAIry's worker. A job is launched and the
worker cycle returns immediately; later cycles poll the child, read its
progress and notice when it finishes. While a transcode runs, the inbox is
still scanned and filed, audits still take their slices, and backups still run.

A busy inbox prevents a *new* optimization from starting. It never suspends one
already going: repeatedly stopping and restarting an encoder buys half-written
output and orphaned processes in exchange for very little, given the job is
already limited to a small share of the machine.

## Where the output lives

Inside `appdata/optimization/jobs/<job-id>/`, and nowhere else. An incomplete
encode is never visible to Browse, to Search, or to whoever is looking at the
folder over SMB. Cancelled and failed output is removed; verified output is
kept until you discard it, so nothing accumulates silently and nothing
disappears without you saying so.

## Exit code 0 is not success

An encoder that returns 0 has proved it did not crash. **Verifying** is a
separate state that asks the other question: is the output there, is it
readable, is the codec the one that was asked for, are the streams present, and
do the running time, picture size and frame rate still match? Any failure ends
the job as **Failed** with a short explanation, and the source is untouched.

The estimate and the actual result are stored separately and always shown
together. A conversion that saved 3% against an estimate of 35% is a successful
run of the encoder and a failed optimization, and the page says so:
*"Conversion completed. Actual saving 3%. Recommendation: keep the original."*

## Cancelling, and restarts

**Cancel** stops the encode: SIGTERM, a bounded wait, then SIGKILL, then the
staging directory is cleared. It only ever reaches a process LibrAIry started —
identified by the PID *and* the kernel's start time for that PID, recorded at
launch. Nothing greps a process table, so a NAS also running Plex or Jellyfin
is in no danger from this.

If the worker or container stops mid-encode, the job does **not** resume. It
becomes **Failed — "Worker stopped during conversion."**, the incomplete output
is cleared, and retrying is something you ask for. Nothing knows how much of a
half-written file is valid, and re-spending an hour of CPU because a container
was updated is not a decision to take on your behalf.

## What is deliberately not here

No AV1, no hardware encoders (NVENC, QSV, VAAPI), no HDR conversion, no
resizing, no frame-rate conversion, no audio normalization, no image
compression. No suspend-and-resume orchestration. And no adoption: **Use
result**, **Replace original** and **Apply** do not exist, and a test fails if
any of those words appears on a button.
