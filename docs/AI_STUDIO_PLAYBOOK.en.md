# AI Studio Playbook (reusable across projects)

> **Origin:** practice from the Cinderage project, July–August 2026. This system grew out of a **failure** — it was not designed on a whiteboard.
> **Applies to:** any project shaped like "one founder + multiple AI worker windows + deliverables the founder must eyeball personally".
> **What to change when porting:** see §9. Everything else transfers as-is.

---

## 0. Read this first, or the rules below will look like bureaucracy

**The incident (real numbers, not hypothetical):** one flagship AI session ran continuously for **23 days**, owning 2 flagship maps + 3 side quests + 4 external models — 9 parallel lines of work.

| Metric | Value |
| --- | --- |
| Continuous runtime | 23 days, single thread |
| Context compactions | **244** |
| Tool calls | ~27,000 |
| Total tool output | 3.2 GB |
| Texture inspection | 7 full-res images per call = **24 MB**; 542 calls / 1.18 GB total |
| Attempts per map | ~40 |
| Review documents produced | 126 (68 of them attempt logs) |
| **Fully delivered** | **0 maps** |

**The blocker on each of the five images, verbatim:** ancestor field not exact-type; log line count ≠ contract; call path landed in a frozen resolver; first 4K texture failed the strict PNG gate; frozen SHA drifted from the actual binary; transfer failed before request creation; missing one-time permission schema.

**Seven blockers, zero related to the quality of the work itself.** All of them lived in the evidence chain, identity verification, permission schema, and transport layer. Most of the time the engine was never even opened.

### Three self-reinforcing mechanisms (this is what actually needs preventing)

1. **All-or-nothing:** the release contract required ten items to close simultaneously (47 assets + 27 images + 9 offline renders + SHA-256 + lineage ID + perceptual-hash distance…), "one missing item or one orphan screenshot rejects the batch." **The probability of closing ten items at once trends to zero.**
2. **Zero tolerance of self:** `any P0/P1/P2 is rejected outright` — with the **maker also acting as judge**.
3. **Moving target:** every attempt strengthened the contract; the bar kept rising and old candidates could never catch up.

### Two amplifiers

- **Single session, never rotated** → 244 compactions → each compaction drops a layer of detail → re-read files, re-screenshot, re-explain its own rules → context fills again. **Most compute went into "remembering what it was doing."** Worse, compaction slowly drifted it into the self-consistent world of "maintaining the evidence chain."
- **The manager doing the work itself** → management produces documents (instantly visible); production produces artifacts (need the engine to see). When both compete in one context, **management inevitably eats production.** This is structural, not an attitude problem.

**Every rule below dismantles one of these five mechanisms.**

---

## 1. Roles: one session, one job

| Role | Does | Never does |
| --- | --- | --- |
| **Front desk (butler)** | Dispatch, collect, summarize, maintain the work-order table. **The only role the founder talks to** | **Produces nothing itself.** Never touches gates, never does line work |
| **Builder** | One task per session: build, verify, screenshot, commit, submit | Never touches gates, never manages others' tasks, never writes protocol docs |
| **Reviewer** | Fresh eyes, look-only, flags issues against the criteria | Never edits deliverables, never reworks for the builder, **never invents new acceptance criteria** |
| **Integrator** | Merges branches, runs gates, verifies on the real engine. Sole holder of the heavy-resource lease | No creative decisions, no aesthetic judgment |

### Three rules for the front desk (break one and the incident replays)

1. **Move work, don't make work.** Itching to do it? Dispatch it.
2. **Downstream returns summaries only.** Every session comes back with "file paths + ≤20-line note". **No raw images, no full logs, no checker output.**
3. **The front desk rotates too.** Weekly, or **immediately after one compaction**. Handover happens through files, not through the predecessor's summary.

**Test: if replacing it doesn't hurt, the architecture is right.** If replacement loses something, knowledge has leaked into context — that's an alarm.

### Two pairing rules

- **The reviewer must be a different model than the builder.** Same model reviewing its own work is no review at all.
- **The reviewer has advisory power, not veto power.** The cost of a wrong verdict is small; don't over-invest in it.

---

## 2. The mailbox protocol: one-sentence dispatch

**Problem:** every round you paste a long prompt into every window; seven windows means seven copies, each drifting apart.

**Solution:** to every window the founder **always pastes the same one sentence**:

```text
Read <project>/docs/work_orders/inbox/<windowID>/NEXT.md, do what it says,
then write a ≤20-line report back to REPORT.md in the same directory and end the session.
```

| File | Sole author | Readers |
| --- | --- | --- |
| `inbox/<windowID>/NEXT.md` | **Front desk** | Worker (read-only) |
| `inbox/<windowID>/REPORT.md` | **Worker** | Front desk |

- `NEXT.md` **references prompts from this playbook by name, never copies the body** — the playbook is the single source, preventing N drifting copies.
- Each round the front desk edits only the "this round's task" section of `NEXT.md`; the "standing rules" section stays untouched.
- **The window ID is a file path; not one character may be wrong.**

### One model, multiple windows → separate window IDs, never a shared mailbox

`glm_53` / `glm_53_b` / `glm_53_c`: each gets its own inbox, worktree, and branch. A shared `NEXT.md` makes N windows do the same job and stomp each other. Every `NEXT.md` starts with "this is window N; other windows are doing other things; you do only what this file says."

### ⚠️ Iron rule: archive `REPORT.md` before editing `NEXT.md`

`REPORT.md` is overwritten every round — **round history is destroyed by design.** We hit this for real: one worker, while writing its review report, overwrote its own previous build report — two completely different documents, nearly lost together in a branch switch.

```bash
A=docs/work_orders/archive/$(date +%F); mkdir -p "$A"
for d in docs/work_orders/inbox/*/; do e=$(basename "$d")
  [ -f "$d/REPORT.md" ] && cp "$d/REPORT.md" "$A/${e}_REPORT.md"; done
```

**Editing `NEXT.md` without archiving = deliberately destroying delivery records.**

---

## 3. The dispatch board: the founder's action list

**Problem:** the mailbox tells the *workers* what to do, but nothing tells the *founder* which windows to light up, in what order. Verbal instructions scroll away in chat and die with each front-desk rotation.

**Solution:** one file on the desktop, `<project>_dispatch_board.md`, **containing only founder actions**:

```markdown
## 🟢 Ready to dispatch now (no dependencies)
### 1. <windowID> — <one-line task>
    <the full copy-paste kickoff sentence>
    What it will do: <one line>
## ⏳ In flight (do not double-dispatch)
## 🔴 Blocked until X reports back
## ❓ Waiting on your decision (the front desk may not decide this)
## Front desk's own debts (doesn't occupy your windows)
```

**Rule:** project status does not live here (that's the work-order table); done items get deleted, new ones go on top.
It doubles as the front desk's self-check — the "waiting on you" and "front-desk debts" columns make commitments impossible to silently disappear.

---

## 4. Definition of delivered: exactly four items

1. The agreed number of result images/evidence at **fixed paths with fixed filenames**
2. The result **loads, runs, doesn't crash**
3. **One commit** (even if the worker itself is unhappy with this round)
4. **A ≤20-line note:** what was done / what's still short / how the next round should change

**There is no fifth item.** No SHA manifests, no evidence manifests, no lineage IDs, no perceptual hashes, no 27-image packs.

### Three anti-corruption iron rules

| Rule | Content | Dismantles |
| --- | --- | --- |
| **1** | **A build session has no authority to declare itself failed.** Finish and submit; pass/fail is the founder's call | Zero tolerance of self |
| **2** | **Never change gates in the same run that produces a deliverable.** Found a problem? Write it in the note, stop, wait for the founder | Moving target |
| **3** | **Gates may only shrink.** Any "tightening of acceptance criteria" needs founder approval; the default direction is cutting | Moving target |

**Timeboxes must be hard:** when time is up, hand in the current state. **No "one more optimization pass before I submit."**

---

## 5. Staged acceptance, not one-shot perfection

Borrowed from industry practice: **blockout → art pass → polish**, with a human looking at every stage.

| Stage | Judge | Timebox |
| --- | --- | --- |
| S1 skeleton (greybox allowed) | Build session, self-judged | ≤4 hours |
| S2 one quality layer up | **Founder looks at a fixed few images** | ≤1 day |
| S3 polish | **Founder looks + walks through** | ≤1 day |

**Every stage ends with a handoff to the founder. No banking it until "all done."**
S1 is submittable — what you want is to see early whether the direction is right, not a perfect wrong direction three weeks later.

**Failing review is the norm, not an incident.**

### The one thing that cannot be delegated

**"Is this good enough?" is the founder's judgment alone.** The front desk can queue, pre-screen, and make issues concrete — but the final look **cannot pass through retelling**; retelling distorts. The reviewer saves time; it is not a proxy.

### The review surface needs a single entry point

One place that scores, comments, and lets programs read the comments back (we use a local web review portal):

- **Stage outputs go straight into the review queue.** No "only complete sets may enter review" gate — historically 41 stage images got tagged "non-essential" and buried in an archive, so the founder's home screen forever read "0 pending."
- Comments must be **machine-readable**, so the front desk auto-converts them into work orders instead of hand-copying.
- **Current-round items must be distinguishable at a glance** (version number/highlight), and **the maker must be visible at a glance** — the founder decides during probation who gets more windows and who gets retired; this is the data source.

---

## 6. Branch and worktree discipline

### One worker, one worktree

```bash
git -C <repo> worktree add <repo_parent>/_wt/<windowID> -b <windowID>/<task> <trunk>
```

**Sharing one checkout always ends badly. Two real incidents:**

1. **A branch switch wiped another worker's committed files from disk.** Worker A switched to its branch on the shared checkout; a report file worker B had committed on a different branch vanished on the spot. **A never touched B's file** — branches/HEAD are checkout-level, not file-level.
2. **A worktree was emptied of 6,185 files** (deletion-only state); that worker's engine imports never finished and no images could be delivered. That worktree was 408 MB while others were 15 GB — **abnormally small worktree = missing files = can't work**; a useful health metric.

### Single trunk, merge one at a time

```text
① Fix the trunk clean first → it becomes the "stable base"
② Merge each delivery branch as it finishes, delete on merge   ← no banking N branches
③ New task = new branch from the trunk as it is right then
```

**Why:** we once banked 5 branches and merged together; one **pre-existing** red gate rolled the whole batch back, hanging all five branches' work, and caused two file-vanishing incidents on top. **The further behind the trunk, the bigger the incident surface.**

### ⚠️ After the trunk moves, worktrees must realign explicitly

```bash
# ❌ grows from current HEAD = the old base sneaks into the new round
git checkout -b <windowID>/<task>_r2
# ✅ explicitly pin the start to the trunk
git checkout -B <windowID>/<task>_r2 <trunk>
```

### Commit discipline

- **Commits are always path-scoped:** `git commit -- <your own paths>`
- **Never pass globs that match nothing to `git add`** — one bad pathspec silently fails the whole add, staging 0 files
- Disk usage is dominated not by branches (14 branches' refs total **574 bytes**) but by **the engine import cache in every worktree** (~11 GB each in practice). **Don't clear active workers' caches to save space** — cold-cache re-imports are brutally slow and directly caused two failed deliveries

---

## 7. Gate discipline

### Fail-closed, but first separate "pre-existing" from "newly broken"

When a gate goes red, **the first move is a baseline**: run the same gate, unchanged, on a **clean trunk**.

- **Baseline also red** → pre-existing. **Stop.** Trace the source, report to the founder, **do not fix it in the same run** (iron rule 2)
- **Baseline green** → bisect by payload; **merge the clean ones as usual**, bounce the dirty ones back to their authors

Proven value: after one full-batch rollback we took a baseline and found **byte-identical log SHAs** proving "this batch added zero" — that rollback had been pointless. **Without a baseline, the question "did it get worse?" has no answer.**

### ⚠️ Think hard before loosening a gate: baseline thresholds are a trap

Changing "must be zero" to "no more than baseline N" costs you:

- **A blind zone of width N:** one more leak here, one fewer there, same total → **gate passes green**
- **Ratchet effect:** next time N+16, someone helpfully bumps the threshold, and months later the gate is decorative

**If you must loosen, use "exact equality per category" instead of "≤ total"** — any category changing goes red, **including changes for the better** (forcing conscious re-baselining with a paper trail).

### Don't mistake a design tradeoff for a bug

Real case: a "141 resources not released at exit" warning traced back to a cache **deliberately made session-resident** — put there to fix a VRAM crash. **"Release at process exit" and "don't cache during runtime" are two different things:** you can keep the session cache and only release in a controlled way at exit, leaving the crash fix untouched. **Before fixing, figure out which of the two you're touching.**

---

## 8. Pits we've hit (the portable ones)

| Pit | Symptom | Countermeasure |
| --- | --- | --- |
| Session starts editing gates | checker / schema / manifest / authority words in deliverables | Stop it immediately, void the run, redispatch |
| Session won't deliver | "one more pass and I'll submit"; opens successor / attempt N+1 | Timebox hits → submit current state |
| Front desk doing line work | Front desk building artifacts, running the engine | Replace the front desk |
| Placeholder images posing as delivery | Multiple images **byte-identical**, same md5 across tasks | On intake check each image's md5 + size + **mtime is today** |
| Old images posing as new delivery | Previous round's images lying in the folder | Before intake verify mtime and bytes; **old image still present = this run didn't deliver** |
| Heavy-resource contention | Two projects opening engines simultaneously | Everything goes through the queuing broker, **with generous leases** (short leases directly caused two failed image deliveries) |
| All-black screenshots | headless without rendering / boot fade layer not cleared | Windowed rendering + clear the fade |
| Full-screen overlay eats all clicks | The whole UI stops responding | Enable mouse-pass-through on overlays only when truly needed |
| Autosave interrupts typing | Founder's typing broken, cursor jumps | **Don't rebuild that DOM** after save; defer flushing during IME composition |
| Validator rejects unknown fields | Want to seed data first, UI later → validation fails | Schema extension and data write **in the same run** |
| Long-session failure ≠ lost work | Task shows "failed" | **Check the disk:** files are usually there. Commits are the real progress anchor |

---

## 9. What to change when porting to a new project

**Copy as-is:** §1 roles & three rules · §2 mailbox · §3 dispatch board · §4 delivery definition & three iron rules · §5 staging + single review entry · §6 branch discipline · §7 gate discipline · §8 pit table

**Each project fills in exactly four things:**

1. **Criteria** — which items define "good enough" in your project. (Cinderage uses five: fun / atmosphere / layout / texture / quest hooks. Other genres swap items, but they must be **few and concrete, founder-scored, and pointable to a location** — "weak atmosphere" doesn't count; "the east path breaks at the steps" does.)
2. **Fixed deliverables** — how many images, what filenames, which paths. (Fix them so the founder can scan at a glance.)
3. **Gate list** — which commands prove "it runs" in your project (0-error import / smoke load / regression script / leak-free shutdown).
4. **Roster & pairing** — who is good at what, who reviews whom (only guarantee: Reviewer ≠ Builder).

**Don't copy:** engine-specific commands, asset pipelines, POI/level structure, vendor choices inside cost discipline.

---

## 10. One-page cheat sheet

```text
The founder talks to exactly one role (the front desk)
The front desk moves work, makes nothing, rotates after one compaction, hands over via files
One session, one job; close it when done
One-sentence dispatch; task in NEXT.md, report in REPORT.md (archive before editing)
Delivery = result images + it runs + one commit + 20 lines; no fifth item
The maker may not judge itself; no gate changes mid-delivery; gates only shrink
Deliver in stages, starting at S1; failing review is normal
Reviewer: different model, advisory only
Single trunk, merge one at a time, realign worktrees after the trunk moves
Gate red → baseline first; separate pre-existing from newly broken
"Good enough" is the founder's own eyes, never retold
```

---

## Revision log

- **2026-08-17 v1.0:** extracted the reusable parts from Cinderage's `FRONTDESK_HANDBOOK.md` / `OPERATING_MODEL.md` / `AAA_MAP_PRODUCTION_CHARTER.md`, stripped Cinderage-specific content, and added pits hit in the two days of live ops on 2026-08-16/17 (worktree branch-switch wiping files, placeholder/old images posing as delivery, gate-baseline trap, REPORT overwrites, multi-window mailboxes, the dispatch board). The founder required it for reuse by sibling projects.
