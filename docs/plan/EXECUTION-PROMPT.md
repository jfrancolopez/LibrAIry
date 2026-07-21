# LibrAIry Phase Execution Prompt

Copy everything between the markers into a fresh AI-agent session, replacing `{{PHASE_FILE}}` with the path of the ONE phase to execute (e.g. `docs/plan/phase-1-core-engine.md`). Run phases in numeric order; never give an agent more than one phase.

---- BEGIN PROMPT ----

You are implementing one phase of **LibrAIry**, an AI-powered file organization and library manager, from a self-contained specification.

## Your input

- The specification: `{{PHASE_FILE}}` in this repository. **Read it completely before writing any code.** It contains the product context, safety invariants, glossary, entry criteria, scope, design constraints, backlog items with acceptance criteria, verification steps, and the exit gate for your phase.
- The repository checkout you are working in.

The specification is **authoritative**. Where it conflicts with older code, comments, or other documentation, the specification wins. Do NOT reopen product decisions listed in its "Product Context" or "Design constraints" sections — they were made deliberately by the project owner.

## Safety rules (absolute, no exceptions)

1. NEVER delete or overwrite user files. LibrAIry's core promise is non-destruction; your implementation AND your development activity must both honor it.
2. All file movement goes through the core engine (`executor.py`) — never write ad-hoc move code elsewhere.
3. Never run the pipeline, tests, or experiments against real user data paths. All filesystem tests use temporary directories.
4. Never weaken, skip, or delete an existing safety/invariant test to make your work pass. If one fails, your code is wrong or the conflict must be reported.
5. No scope creep: implement only what your phase's "In scope" lists. Its "Out of scope" list is binding even when an item seems quick or obvious.

## Procedure

1. **Verify entry criteria.** Run every check in the spec's "Entry criteria" section. If any fails, STOP, report exactly what failed, and do not start the phase.
2. **Work the backlog in dependency order.** For each item (`P<N>-<nn>`):
   a. Re-read its description, acceptance criteria, and test notes.
   b. Write the tests alongside (or before) the implementation — every acceptance criterion needs an automated check unless it is explicitly a manual/hardware step.
   c. Run the FULL test suite (`ruff check src tests && pytest`), not just your new tests.
   d. Tick the item's acceptance-criteria checkboxes in `{{PHASE_FILE}}` and commit with the message format `P<N>-<nn>: <item title>`.
3. **Keep the phase doc current.** Set its status line to `IN PROGRESS` when you start. The phase doc is the only plan document you may edit — never touch other phase docs or the master README.
4. **Finish.** Run every step in "Verification steps" and every item in the "Exit gate checklist". Only when ALL pass: set the status line to `DONE`, and produce a final report: what shipped, test results, deviations from the spec (if any, with reasons), and the Open Questions Log entries you added.

## When you are blocked or the spec is ambiguous

- If information is missing or two spec statements conflict: append the question AND the decision you took to the spec's "Open questions log", choose the **safest reversible default** (the one that moves/changes the least and is easiest to undo), and continue.
- If a genuine blocker prevents progress (entry criteria failure, broken environment, a spec instruction that would violate a safety rule): STOP and report it precisely. Never "work around" a safety rule.
- Exit-gate items that require the project owner's hardware or judgment (e.g. a real-UNRAID drill): implement and verify everything automatable, then list these explicitly in your final report as awaiting the owner — do not tick their boxes yourself and do not set the phase DONE.

## Prohibitions

- No new runtime dependencies beyond those the spec's "Design constraints" allow. If you believe one is genuinely needed, log the justification in the Open Questions Log and use the most boring, widely-trusted option.
- No edits to other `docs/plan/*.md` files, no reformatting of unrelated code, no drive-by refactors, no version bumps unless the spec says so.
- No commits of secrets, real API keys, or user data; test fixtures are synthetic.
- Do not push, tag, or release unless the spec's phase explicitly includes it or the project owner asked.
- Do not mark acceptance criteria done without a passing automated check (or an explicit manual-step note in your report).

## Report format (end of run)

1. **Status**: DONE / BLOCKED (with the blocker) / PARTIAL (with remaining items).
2. **Shipped**: backlog items completed, files added/changed (summary, not a diff).
3. **Test evidence**: suite results, new test counts, any `slow`/perf numbers.
4. **Deviations & open questions**: every Open Questions Log entry you added, with your chosen default.
5. **Awaiting owner**: manual exit-gate items (hardware drills, judgment calls), if any.
6. **Handoff notes**: anything the next phase's agent must know that the specs don't already say (should be rare — if it's important, it likely belongs in your phase doc's "Notes for future phases" section; put it there too).

Begin now: read `{{PHASE_FILE}}` end to end, then run its entry criteria.

---- END PROMPT ----

## Notes for the human operator

- One phase per agent session. Fresh context each time is the design: every phase doc is self-contained.
- Between phases, skim the finished doc's Open Questions Log and final report — that is where the agent recorded every judgment call it made on your behalf.
- If you change a product decision between phases, follow the "Decision-change protocol" in [README.md](README.md) BEFORE launching the next agent.
- Suggested kickoff message alongside the prompt: nothing. The prompt is complete by itself; adding instructions risks contradicting the spec.
