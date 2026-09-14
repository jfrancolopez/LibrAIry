# NAS validation — production, by hand and by script

**Status: production validation required.** Nothing in this document has been
run. It is the procedure, written so that it can be.

M2-03 shipped its resource modes with one claim unproven, and the roadmap has
carried it since:

> Quiet leaves the NAS responsive under a full inbox.

A build machine cannot prove that. The thing at risk is a film stuttering over
SMB while the array is busy, and there is no film and no array on a laptop.
Every other number in [performance.md](performance.md) was measured here and is
honest about being measured here; this one has to be measured on the box.

## What to run

`scripts/nas_validation.py`, on the NAS, as the user LibrAIry runs as.

    # Safe on production: reads /proc, times a few reads, changes nothing.
    python3 scripts/nas_validation.py --observe --seconds 300 \
        --probe /mnt/user/media --port 8080 --json-out idle.json

Run that twice — once with the inbox empty and the worker idle, once with a
genuinely full inbox — and the difference between the two is what LibrAIry
costs the machine.

    # Drives the worker through each mode against its own synthetic inbox.
    python3 scripts/nas_validation.py --drive /mnt/user/appdata/librairy-validation \
        --files 400 --probe /mnt/user/media --json-out modes.json

`--drive` refuses any directory that is inside, or contains, the installation's
inbox, library, quarantine or appdata. It files its own files and deletes them.

## What the columns mean

| | |
|---|---|
| **CPU%** | share of the whole box, excluding idle and iowait. A NAS waiting on its disks is not a NAS doing work |
| **load** | peak 1-minute load average — what an over-subscribed box shows first |
| **PSI cpu / io** | `some avg10` from `/proc/pressure`: the share of the last ten seconds in which at least one task was *stalled* waiting. The closest thing Linux gives to "somebody would have noticed", and the most important column here |
| **memMB** | lowest `MemAvailable` seen. Available, not free: on a NAS almost all of free memory is cache |
| **degC** | warmest sensor the board exposes, because a NAS that throttles is a NAS whose owner blames the software |
| **read MB/s** | a timed sequential read off the array — a **proxy** for a stream that must not stutter |
| **UI ms** | one request to LibrAIry's own dashboard, from the box |

**PSI io is the number to watch.** A box at 90% CPU with no IO pressure is
working hard and serving files perfectly well. A box at 30% CPU with IO pressure
is the one whose film stutters.

## What the script cannot do

It prints this every run, and it is not a formality:

- Play a film from the array over SMB or NFS, on another device, for the whole
  of a `worker: full` window. Did it stutter, once?
- Copy a large folder to the array from another machine during the same window.
  Did the transfer rate drop noticeably?
- Browse photographs in LibrAIry's UI while the worker is busy. Did a page take
  longer than it does on an idle box?
- Leave it running with a genuinely full inbox for an evening. Was the machine
  unpleasant to use at any point?

A sequential read finishing in 40 ms says the array had headroom. It does not
say nobody noticed anything, and the script must not be quoted as though it did.

## What the answer changes

**Constants, not architecture.** The batch caps and the pauses in
`librairy/resources.py` are the dials, and they were set from
`scripts/measure_worker_load.py` on a laptop — which found that the cap is *not*
what makes Quiet quiet (0.72 CPU-seconds per wall second against Balanced's
0.76); the pause after the cycle is (0.34 sustained against 0.70).

So the likely outcome is a changed `busy_sleep`, not a changed design. Redesign
`ResourcePolicy` only if the measurements say something this does not predict —
and record what they said either way, here, with the date and the hardware.

## Recording a run

Paste the table, name the machine and the array, and say what a person saw:

    2026-__-__  <model>, <cores>, <RAM>, array <n> disks <type>
    window              CPU%   load  PSI cpu  PSI io   memMB   degC  read MB/s  UI ms
    ...
    By hand: film played from /mnt/user/media over SMB throughout `worker: full`.
             <stuttered / did not stutter>
