---
title: Worker debate — record the protocol, make critics question harder, and keep one model by default
status: draft
author: kirocrew agent session, directed by zejiangg
created: 2026-11-08
last-audited: 2026-11-08
audited-at: 664feb41be
doc-pr: null
implementation-prs: []
tracking-issues: [33405]
supersedes: []
superseded-by: []
---

# RFC: Worker debate — record the protocol, make critics question harder, and keep one model by default

Status: draft. Nothing built; this document changes no code. Every code
reference was read on main at `664feb41be`.

## Summary

A Conductor can run a **worker debate**: several workers argue one open
question in shared files while the Conductor relays and referees. The protocol
is skill text only. It grew through four docs PRs and never had an RFC.

This document does three things:

1. **Records the current design** with `file:line`, and lists the places where
   other docs disagree with it (§1).
2. **Proposes one change**: let critics borrow the questioning style of a
   "grill me" session, so that objections test the proposal's claims instead
   of asking the proposal to say more (§2). The recommended form is about 25
   lines of skill text.
3. **Records why the panel moved from mixed models to one model by default**,
   what that loses, and when a mixed panel is still worth it (§3).

§4 lists open questions for the team, each with a recommendation.

Readers who have not followed the work can read §1.1 and §2.1 first.

## Motivation

- **No single record.** The rules live in
  [`goal-conductor/references/worker-debate.md`](../../src/kiro_crew/builtin_skills/goal-conductor/references/worker-debate.md)
  and the Worker debate section of
  [`goal-conductor/SKILL.md`](../../src/kiro_crew/builtin_skills/goal-conductor/SKILL.md).
  The reasons live in four PR bodies. The one RFC that describes the debate,
  [`rfc-worker-messaging.md`](rfc-worker-messaging.md), predates three of the
  four PRs.
- **Objections are often shallow.** The owner observed that a debate's result
  is weaker than the result of a "grill me" session. §2.2 shows what that looks
  like in a real debate note.
- **The model rule changed without a design record.** The first version asked
  for critics on a different model family. The current text asks for one model.
  Only a PR body says why.

## Goals

1. A reader can learn the whole protocol and its history from one document.
2. Critics surface real problems: claims that do not hold, assumptions nobody
   checked, alternatives nobody weighed.
3. The model choice and its trade-off are written down, so the team can revisit
   it with data.

## Non-goals

- No product code. No new tool, ledger field, acceptance kind or UI.
- No change to how often the owner is asked. Owner-in-the-loop changes are out
  of scope (see §2.5).
- No change to round limits, budgets, relay delivery or the checkpoint rules.
- No typed worker-to-worker messaging. That is
  [`rfc-worker-messaging.md`](rfc-worker-messaging.md)'s subject.

## 1. Current design

### 1.1 History

| PR | Merged | What it added |
|---|---|---|
| [#26316](https://github.com/kirodotdev/KiroCrew/pull/26316) | 2026-10-30 | The protocol: proposers, single-aspect critics, the Conductor as referee, stable objection ids, evidence rule, a round cap of 3, the debate note, the human's free-form ruling. Critics on a different model family where one is offered. |
| [#26932](https://github.com/kirodotdev/KiroCrew/pull/26932) | 2026-11-01 | Owner needs `R1..Rn` in the anchor. Round 0 belongs to the critics. Mechanism table. Relay delivery rules (no `wait`, frozen round files, relay tags, `steer=true` resend). The checkpoint with `soft_rounds` / `hard_rounds` replaces `max_rounds`. The Conductor may offer a debate. |
| [#27969](https://github.com/kirodotdev/KiroCrew/pull/27969) | 2026-11-03 | Critic roles derived from failure modes, with a role card. A debate is offered only for a costly, contested choice. Same model by default. Closes [#27956](https://github.com/kirodotdev/KiroCrew/issues/27956). |
| [#29475](https://github.com/kirodotdev/KiroCrew/pull/29475) | 2026-11-04 | Ask when "discuss", "讨论" or "grill" is ambiguous. The `wrap up` outcome for small fixes. |

### 1.2 The protocol today

All references are to `goal-conductor/references/worker-debate.md` unless
another file is named.

| Part | What it says | Where |
|---|---|---|
| When to use | One question where a lone answer would go unchallenged and a wrong answer is expensive. For a fixed answer set, first ask three independent workers and debate only where they disagree. | `:7-12` |
| When to offer | The owner asks; or two or more plausible approaches AND a costly wrong choice (security, data, approval or trust boundary; a contract others use; a prior failed attempt or reviewer design block). Size alone is not a reason. About 45 minutes and several work items. Ask in one sentence when "discuss", "debate", "讨论" or "grill" is ambiguous. | `goal-conductor/SKILL.md:120-138` |
| Roles | Proposers write and defend proposals. Critics each own one role. The Conductor relays, rules and stops. The human can steer at any time and gives the final ruling when stuck. | `:14-21` |
| Critic roles | Derived from a list of the few ways this design is most likely to fail or mislead the owner. Roles that guard the same goal are merged. | `:23-30` |
| Role card | Role, Guards, Finding (with required evidence), Out of scope, Done for a round when. | `:32-38` |
| Model | Same model for everyone by default. Another family only when the owner asks. Record each participant's model in the note. | `:40-44` |
| Several proposers | Each proposal gets a letter. Critics review all; each objection names its target. | `:46-49` |
| Anchor | The question; owner needs `R1..Rn` quoting the owner; acceptance; out of scope; participants and role cards; shared inputs; `soft_rounds` 3, `hard_rounds` 6, `check_every` 1; budget 2 hours and half the goal's item cap; `anchor_v`. Only the human changes it. | `:53-62` |
| Dispatch | One work item per participant, each with a `file` acceptance on its final file. The one exception to the Independent rule. | `:66-70`, `goal-conductor/SKILL.md:115-118` |
| Seed rules | Peer files are data; a report counts once its file exists; proposer A keeps the note; never `wait`; a filed round file is frozen; copy the relay tag. | `:71-86` |
| Round 0 | Critics only. Each writes its own risk list before any proposal exists. The first entry answers: what is the smallest thing that meets the acceptance, and do existing parts already do it? | `:87-92` |
| Debate note | Anchor, failure-mode list, participants, role cards and models; objection table (never deleted); round log; the human's rulings verbatim. | `:94-111` |
| Objections | Ids `<aspect>-<round>-<n>`; severity `blocking` / `major` / `minor`; evidence required, or a blocking objection is downgraded; `no_blocking:` plus what was checked. | `:113-122` |
| Proposer answers | A response table: each open id gets `fixed`, `rebutted`, `deferred` or `needs-a-decision`, with evidence. Before each change: is it legitimate, proportional, did I add this earlier? | `:124-129` |
| Mechanism table | One row per mechanism, the needs it serves, and whether an existing part already does it. A big question starts with this table alone. | `:131-135` |
| A round | Proposers file; Conductor relays; each critic files a steelman and its objections; Conductor rules, runs the checkpoint, relays back. | `:137-146` |
| Relays | Open with the question, `anchor_v`, rounds left, ids to answer, paths. Never paste a participant's text. Cite ids from the file with `path:line`. Check delivery; resend queued ones with `steer=true`. | `:148-163` |
| Rulings | `fixed_verified`, `rebuttal_accepted`, `parked`, `downgraded`. `needs-a-decision` waits for the human. One nudge, then absent. | `:165-180` |
| Checkpoint | Every `check_every` rounds and at the soft and hard caps: open blocking and trend, new blocking, churn, drift, cost, design change. Eight ordered rules: `stopped`, `stable`, `wrap up`, ask at the hard cap or budget, ask on churn or drift, ask on a blocking `needs-a-decision`, ask when blocking stops dropping, else continue (with a written reason past `soft_rounds`). | `:186-225` |
| Human ruling | The Conductor states the open blocking ids, the proposer's answer and each option's cost. The user answers free-form; the Conductor maps it to ids, asks back once if unclear, and quotes it verbatim to the note keeper. | `:227-241` |
| Finishing | Final files: proposers consolidate with `final_of:`; critics give each objection's end state. Outcome line `OUTCOME: stable`, `ruled` or `stopped`. Only `stable` means it converged. | `:243-257` |
| Ledger | Short summary and the note path; never the discussion. | `:259-264` |
| Resuming | Read the ledger, open the note, continue from the last round-log line. | `:266-271` |
| Limits | Same owner and trust level only; no private memory, credentials or customer text; one relay per participant per round. | `:273-282` |

### 1.3 Where other docs disagree with the skill text

| Place | What it says | What the skill says | This PR |
|---|---|---|---|
| `rfc-worker-messaging.md:237-239` (trigger T-b) | A debate that "goes past `max_rounds`" | `max_rounds` was replaced by `soft_rounds` / `hard_rounds` in #26932 (`:57-58`) | Fixed: now names `hard_rounds` |
| `rfc-worker-messaging.md:351-355` (open question 4) | The 3-round cap is approximate; "a separate study in progress will design" round check-ins | The study shipped as the checkpoint (`:186-225`) | Fixed: marked answered, points here |
| `rfc-worker-messaging.md:247` | "single-aspect critics" | Since #27969 a critic owns one role from a failure-mode list; the role's short name is still the `<aspect>` key (`:23-30`) | Left as is: still true in effect |
| `rfc-worker-messaging.md`, whole | Silent on `wrap up`, role cards and the model rule | `:32-44`, `:211-215` | Left as is: out of its scope; this RFC records them |
| `rfc-worker-messaging.md` index row in [README.md](README.md) | Describes the debate only as the skill-text relay baseline | Same | Left as is: still correct |
| PR body of #26316 | Critics on a different model family; cap 3 | Same model; soft 3 / hard 6 | PR text cannot be edited; §1.1 records the change |
| `goal-conductor/SKILL.md:121` vs [`skills/grill/SKILL.md:3-4`](../../skills/grill/SKILL.md) | The word "grill" may mean a debate, and the Conductor asks | The grill skill also triggers on "grill me", "challenge this", "poke holes" | Not a contradiction, since the Conductor asks; but two features answer to one word. See §4 Q3 |

`docs/system-specs/modules/crew-mode.md:4128` and
the Bot's tracking prompt in `src/kiro_crew/crewmate_prompts.json` send the Bot conductor to
`goal-conductor` for worker debate. They agree with the skill text.

## 2. Make critics question harder

### 2.1 What a grill session does

Three grill sources were read.

- **The built-in `grill` skill** ([`skills/grill/SKILL.md`](../../skills/grill/SKILL.md)).
  Exactly one question per turn (`:9-11`). Each turn is context, one question
  and the asker's own recommended answer (`:26-29`). Facts are looked up
  silently; only decisions are asked (`:33`). If the plan is simple, it says so
  instead of inventing questions (`:38`).
- **The Research Lab grill tree**
  (`src/kiro_crew/apps/builtins/auto_research/campaign/grill.py`). A question
  tree, at most depth 4 and 5 children per node (`:23-24`). Its prompt asks for
  distinct, non-overlapping angles, one focused question per node, a
  recommended answer, and never a question the agent could look up (`:47-67`).
- **An external "grill me" interviewer agent** that the owner uses (not in this
  repo; described here in our own words). It works Socratically. It asks one
  question at a time and gives its own recommended answer. It goes depth-first:
  a new branch is followed to its end before it returns to the main thread.
  When the user makes a claim, it asks for the mechanism, the number or the
  evidence behind it. When something is called obvious, it asks what happens if
  that is wrong. It names the alternatives and asks why each was rejected. Its
  stock questions include: what happens when this part fails; have you checked
  it really works that way; what would you keep if scope were cut in half; what
  is the rollback plan. It summarizes every few questions.

The common core is four habits:

| Habit | What it forces |
|---|---|
| **Question, not claim** | The other side must answer something specific. |
| **Burden on the claim** | Whoever says "X does Y" shows the mechanism or the evidence. |
| **Depth first** | A point is not dropped until it is resolved. |
| **Failure-seeking prompts** | Assumption wrong? Part fails? Alternative rejected why? Half the scope? Rollback? |

### 2.2 What the debate does instead

Today's critic tools are the role card (`:32-38`), the Round 0 risk list
(`:87-92`) and the objection table (`:103-104`, `:113-122`). Compared with the
four habits:

| Habit | Debate today | Gap |
|---|---|---|
| Question, not claim | An objection is a claim: "the plan must say X". | The proposer can close it by adding a sentence. |
| Burden on the claim | The evidence rule binds the **critic** (`:119-120`). The proposer's mechanism table says whether an existing part "already does it" (`:131-133`) but asks for no proof. | A proposer's claim about the code can stand unchecked. |
| Depth first | Each critic files every objection in one batch per round (`:145`). | A weak answer is checked a round later, or by only one critic, or never. |
| Failure-seeking prompts | Round 0 asks one fixed question: the smallest thing, and do existing parts do it (`:89-91`). The role card names the goal a critic guards, not what to ask. | Nothing prompts "what if this is wrong", "what did you reject", "rollback". |

**Evidence from a real debate.** The richest record on disk is a plan debate
run on 2026-11-02: two proposers, three critics (measurement, safety,
operations), rounds 0 to 3 plus finals, about 32 minutes. Its note and round
files are local research files, not in this repo. Counted from that note:

| Measure | Value |
|---|---|
| Objection rows | 50 (7 blocking, 25 major, 18 minor) |
| Closed as `fixed` by the proposer | 40 |
| Closed by removing a mechanism | 2 (the same finding, raised by two critics) |
| Blocking rows about the note's own bookkeeping, not the design | 2 |
| Proposer A's plan, round 1 to final | 1,514 to 3,657 words (2.4 times) |

Three patterns stand out:

1. **Objections add; they rarely cut.** Most rows asked the plan to state one
   more rule. Closing them grew the plan 2.4 times. Only one row asked to fold
   rows together. No row asked what would stay if the plan were cut in half.
2. **A proposer's claim about the code went unchecked by one of three
   critics.** In round 3, proposer A wrote that `kirocrew pod up --seed <dir>`
   "already seeds a pod's home", citing `src/kiro_crew/pod/README.md` lines
   88-128. Lines 105-109 of that same range say a directory seed copies only a
   cleaned `config.json`. The measurement and safety critics caught it as
   blocking. The operations critic marked the dependent row "holds, propose
   close" in the same round. A rule that the claim-maker shows the proof, and
   that a critic checks it before closing, catches this every time rather than
   two times out of three.
3. **An assumption passed the debate and failed in use.** The debate ruled
   `fixed_verified` on "use the packaged fake ACP backend for gateway timing",
   with a rule to check the wiring first. When the plan ran on 2026-11-03, the
   fake backend never reported its MCP servers. So every sample carried a fixed
   6.0-second MCP wait, and the session was thrown away each turn, so warm
   turns were never measured. A real `kiro-cli` run by the owner was needed.
   The critics asked "is it wired?" but not "what happens if the fake backend
   behaves differently from the real one on this path?"

The PR record shows the same shape. #26932 found that proposers "spent two
rounds deleting what they had added". #27969 found two critic roles repeating
each other. #29475 found that rounds 4 to 6 of a six-round debate spent about
14 of its 46 minutes on small fixes.

**What is missing as evidence.** No transcript of a "grill me" session on a
comparable question is saved, so the two cannot be scored side by side. The
one saved Research Lab grill tree is an owner-scoping session, not a design
critique. The case above rests on the debate's own record and on the
mechanism, not on a measured comparison. §2.6 proposes how to measure it.

### 2.3 Why the gap produces weaker results

The chain is short:

1. An objection is a claim, so the cheapest answer is to add text.
2. Adding text closes the id, so ids close without the design getting tested.
3. The evidence rule binds the critic, so a proposer's claims about the code
   get checked only if a critic happens to check them.
4. Critics batch their objections, so no single point is chased to its end.
5. The result is a longer plan with many closed ids, and a few untested
   assumptions that fail later in use.

A grill session reverses steps 1, 3 and 4. It asks a question, puts the burden
on the claim and stays on one point until it is resolved.

### 2.4 Options

**Option A: a question bank in the role card.** Add a `Questions` line to each
critic's role card: the three to five failure-seeking questions from §2.1 that
fit its role. Round 0 answers them for the question as asked.

**Option B: claim checks.** Three small rules on parts that already exist:

1. A critic may file an objection in **question form**. It quotes one claim
   from the proposal (`path:line`), asks one question about it from a short
   bank (the mechanism or evidence behind it; what if it is wrong; what
   happens when this part fails; which alternative was rejected and why; what
   stays at half the scope; how it is rolled back), and gives the critic's
   recommended answer. It gets an ordinary id and severity.
2. The proposer answers a question-form objection **with evidence**: a
   `path:line`, a command and its output, or a source link. Added text alone is
   not an answer. The mechanism table gains a `proof` column for every row
   that says an existing part already does it.
3. The Conductor rules `fixed_verified` on a question-form objection only after
   the critic that raised it says it checked the evidence. If the answer does
   not hold, the critic re-asks under **the same id** rather than opening a new
   one. That is depth-first in its smallest form.

**Option C: depth-first rounds.** Each critic may open at most two blocking or
major threads per round, and must follow each to resolution before opening
more.

**Option D: an assumption challenger.** Add one critic whose failure mode is
"an unverified claim or an unweighed alternative", with the question bank as
its role card.

| | A: question bank | B: claim checks | C: depth-first rounds | D: assumption challenger |
|---|---|---|---|---|
| Fixes "objections add" (§2.2 pattern 1) | partly: the bank prompts "half the scope" | yes: a question needs an answer, not a sentence | partly | partly |
| Fixes unchecked proposer claims (pattern 2) | no: still the critic's burden | **yes**: proof column and checked evidence | no | partly: one critic checks |
| Fixes untested assumptions (pattern 3) | partly | yes: "what if it is wrong" on a quoted claim | partly | yes |
| Extra cost per round | none | a few lines per answer; no new relay | more rounds: breadth drops, so the soft cap is reached with ids unseen | one more participant: one more relay and one more work item per round |
| Fit with the protocol | good: one role-card line | good: reuses ids, severity, dispositions, rulings and the mechanism table | poor: the checkpoint and caps assume a batch per round | fair: #27969 merged overlapping roles; this role overlaps every other |
| Skill text | about 8 lines | about 25 lines | about 15 lines, plus checkpoint changes | about 10 lines |

### 2.5 Recommendation

**Adopt Option B.** It is the smallest change that addresses all three
observed patterns. Option A is smaller, but it leaves the burden of proof
where it is now, so pattern 2 stays. Option C costs rounds the budget does not
have. Option D adds a participant and repeats a role overlap #27969 removed.

Option B reuses the existing objection id, severity, response table, mechanism
table and ruling. It adds one objection form, one table column and one ruling
condition. It does not change round limits, the checkpoint, relays or the
owner's part.

**Owner in the loop.** Out of scope for this proposal. One small thing does
fall out: a `needs-a-decision` that reaches the human could reuse the grill
turn shape (context, one question, a recommendation). The human-ruling text
(`:227-232`) already asks for the options and their cost, so this is wording,
not a new step. See §4 Q5.

### 2.6 How to tell whether it worked

Run the next real debate with Option B and count, from its note:

| Measure | Baseline (2026-11-02 debate) | Better if |
|---|---|---|
| Share of closed ids closed by checked evidence, not by added text | not tracked | most question-form ids |
| Plan growth, round 1 to final | 2.4 times | lower |
| Ids closed by removing a mechanism | 2 of 50 | higher |
| Proposer claims about the code later found wrong in use | at least 1 | 0 |

Write the verdict into this RFC.

## 3. Model choice

### 3.1 What changed and why

| When | Rule | Reason given |
|---|---|---|
| #26316, 2026-10-30 | Critics on a different model family where the host offers one. | Independence: a critic should not share the proposer's blind spots. |
| Owner request, 2026-11-02 morning | One proposal to shorten debates: allow faster models only in the last rounds, still a different family from the proposer. | Debate length: 46 minutes for six rounds. |
| #27956, decided 2026-11-02; #27969, merged 2026-11-03 | Same model for everyone by default. Another family only when the owner asks. Record each participant's model in the note (`:40-44`). | Other models failed mid-turn. The slowest model sets every round's pace. Role cards and the independent Round 0 lists already guard against a panel that agrees with itself. |

Two of the reasons follow from the protocol itself:

- **The slowest sets the pace.** Step d waits for every critic before ruling
  (`:146`). A round takes as long as its slowest participant.
- **A mid-turn failure stalls the round.** A participant that does not file
  gets one nudge and is then marked absent; its open objections stay open
  (`:176-178`). Open ids keep the outcome from reaching `stable`.

**What is missing as evidence.** No saved transcript of a critic failing
mid-turn on another model was found; the claim rests on the owner's report in
#27956. The 2026-11-02 plan debate note has no model column, because it ran
before the record-the-model rule. Future notes carry one, so the choice can be
revisited with data.

### 3.2 What one model loses

- **Shared blind spots.** Critics trained the same way tend to miss the same
  things and accept the same wrong fact.
- **Shared taste.** A model may rate its own style of answer higher.

The evidence rule (`:119-120`) and Option B's claim checks reduce the first
loss for anything that can be checked against code, a command or a source.
They do not help with pure judgment: product shape, how a user will read a
sentence, how much risk is acceptable.

### 3.3 When a mixed panel is still worth it

- The owner asks for one.
- The question is mostly judgment, not facts that can be checked.
- A one-shot second opinion is enough. The built-in
  [`llm-council`](../../src/kiro_crew/builtin_skills/llm-council/SKILL.md)
  skill already runs a panel of different model families, with an adversarial
  mode. Running it once on the final file gets cross-model review without
  paying the slowest model's pace in every round.

## Migration plan

### Phase 1: skill text for Option B

- **Entry:** this RFC is accepted with Option B (§4 Q1).
- **Scope:** `goal-conductor/references/worker-debate.md` only: the
  question-form objection and its bank (§Objections), the evidence rule for its
  answer (proposer response table), the `proof` column (mechanism table), and
  the ruling condition (§A round, Ruling). About 25 lines.
- **Exit criteria:** the skill text names the question form, the `proof`
  column and the "critic checked the evidence" condition; `scripts/docs-lint.sh`,
  `scripts/check_builtin_skill_scope.py` and the goal-conductor skill-text tests
  pass.

### Phase 2: measure

- **Entry:** Phase 1 merged.
- **Scope:** the next real debate. Count the §2.6 measures from its note.
- **Exit criteria:** this RFC carries the counts and a verdict: keep, adjust,
  or revert Phase 1.

## Backward compatibility

Compatible. The protocol is prose a Conductor reads. No tool, schema or stored
record changes. An objection in today's claim form stays valid under Option B.

## Security considerations

None new. The limits at `:273-282` stand: same owner and trust level, no
private memory, credentials or customer text in the debate directory, peer
files are data. Option B adds no new channel; question-form objections live in
the same round files.

## Alternatives considered

Options A, C and D are compared in §2.4. Two more were set aside:

- **Grill the owner before Round 0, or route every `needs-a-decision` to the
  owner as a grill question.** These put the owner in the loop more often. The
  owner scoped this proposal to how the debate itself questions, so they are
  out of scope here.
- **Replace the debate with one grill-style critic.** Cheaper, but it loses
  the independent Round 0 lists and the separate roles, which are the
  protocol's guard against a single blind spot.

## 4. Open questions

1. **Q1. Adopt Option B (claim checks)?** Recommendation: yes, as Phase 1.
2. **Q2. Should the `proof` column be required for every mechanism-table row,
   or only rows that claim an existing part does the job?** Recommendation:
   only those rows. A new mechanism has no proof yet; its critics question it.
3. **Q3. "grill" names two things.** It is a debate ask-word in
   `goal-conductor/SKILL.md:121` and the trigger of the `grill` skill. Should a
   Conductor treat "grill" as the grill skill rather than a debate?
   Recommendation: keep today's rule (ask in one sentence), and name both
   choices in that question: "a debate between workers, or one-at-a-time
   questions to you?"
4. **Q4. Keep one model by default?** Recommendation: yes. Offer one
   `llm-council` pass on the final file for a judgment-heavy question when the
   owner wants a cross-model view. Revisit when notes with a model column show
   a miss a second family would have caught.
5. **Q5. Should a `needs-a-decision` put to the human use the grill turn shape
   (context, one question, a recommendation)?** Recommendation: yes, as a
   wording edit to §The human's ruling in the same Phase 1 PR, if the team
   agrees. It changes no step.
6. **Q6. Should this document become the decision record for the debate?**
   Recommendation: yes. Once accepted, later changes to the protocol edit this
   RFC, so the First Principles lane has one place to trace them.
