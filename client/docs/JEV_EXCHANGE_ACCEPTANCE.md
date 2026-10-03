# JEV exchange extraction and recovery acceptance

## Scope

Exchange extraction now selects source-bound action anchors before asking the
independent identity, applicability, status and evidence questions. The second
round retains the existing project directory. Original statements, negation,
conditions, speaker attribution and completed/cancelled history remain evidence
requirements; missing candidates and second-highest probabilities are not facts.

Known exchange rejections persist a terminal retry marker without claiming that
the exchange was committed. Recovery continues the other consumers. Active tasks
are still awaited and shielded; transient failures retain the existing attempt
ceiling. Single-character Chinese subjects can recall a stored project title with
lower priority than the existing word match.

The semantic transport preserves native choice, probabilities and confidence.
Metadata must have legal choices and complete option keys, finite values, valid
ranges and provenance, and a valid distribution. Argmax and confidence formula
differences are diagnostic information rather than extra grounds for rejecting a
native accepted choice. All answers are checked before receipt settlement. Legacy
responses remain supported without inventing confidence.

## Frozen implementation and prior validation

- Exchange extractor SHA256:
  `4a9fc4e83ea659a83dce5339694e89ff536c1994c8c52b6c3c7efc27ae7c9a05`.
- Client semantic helper SHA256:
  `8c2b5b70bba18541ae9b2091c985d87cc88b357b9874baca46f9c9722bcf6791`.
- Prior base: `a260c81de668424c5d65c814c7d70f3f43ba2f19`.
- Local core checks: 1,274 passed. Later metadata, legacy transport and receipt
  focused checks: 158 passed; runtime diagnostic checks: 10 passed. Counts overlap
  and must not be summed as unique tests.
- Prior independent source review found no actionable source blocker. Publication
  also requires synchronization with main, local checks and the PR's own CI.

## Native provider comparison

Labels were frozen before calls. Synthetic inputs were sent to native JEV, then
the extracted result went through the real SQLite write, reload and reply-context
projection. Each exact case and implementation counts its first run; development
failures and exposed-subset correction probes were retained.

| First-run group | Before | After |
|---|---:|---:|
| Six fresh scenarios, all checks passing | 1/6 | 6/6 |
| 56 diagnostic scenarios, all checks passing | 5/56 | 51/56 |
| Correct stored facts in the diagnostic group | 3/39 | 36/39 |
| Actual wrong writes in the diagnostic group | 3 | 0 |

The fresh group retained all six expected facts and all five nonempty follow-up
contexts. The diagnostic group retained all 29 measured nonempty follow-up
contexts. These are synthetic scenario results, not population accuracy or full
natural-language reply acceptance.

Three first-run failures came from additional numeric diagnostic rejection. The
corrective probe reused their exact saved native first-round responses and made
only the two missing dependent calls. All three corrective scenarios passed, the
new planned item appeared in the stored follow-up context, the previous completed
repair remained completed, and the user's routine was independently read back
from SQLite. This exposed-subset probe does not replace the first-run 51/56 figure.

## Remaining limits

- Two first-round semantic recall misses remain: selection omitted a completed
  action when another future action was present, and omitted the role's action
  when both speakers used identical text. Both source candidates existed; no
  missing candidate or second-highest probability was promoted into a fact.
- Independent ordinal questions still depend on correct global coverage. Duplicate
  normalization and a capacity answer do not prove complete recall.
- Combining the maximum update slots with a large prior directory can exceed the
  unchanged dependent-request byte bound. That rejection is terminal rather than
  an automatic repetition of an already paid round.
- Native-provider comparison, SQLite persistence and reply-context projection do
  not demonstrate final reply generation, real QQ delivery, device acceptance or
  production deployment. No real customer wallet was modified by these checks.

The separate billing-display server candidate is outside this public client PR.
Its isolated checks cover aggregate duplication and pagination; it has not been
deployed. This client change remains compatible with legacy semantic responses.
