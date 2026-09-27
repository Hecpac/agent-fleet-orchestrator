---
name: codex-os
description: Plan or synthesize an assigned Herdr Lead stage when coordinating scope, decisions and evidence adds value. Use with the supplied stage contract and selected skills.
---

# Codex OS

Complete the assigned Plan or Synthesis stage using its supplied criteria. Implementation, specialized design and verification needs belong in the bounded plan or synthesis; they do not authorize Lead to execute other roles' work or load unselected skills.

## Shape the work

1. Use the supplied project instructions and inspect relevant state before proposing changes.
2. Establish the objective, verified current state, constraints, success criteria, and allowed side effects.
3. Use only selected skill content when its condition applies. Keep future-stage requirements in the plan, without making them prerequisites for the current result.

| Need | Route |
| --- | --- |
| A missing product, architecture, or scope decision would materially change the result | Use selected `entrevista-pre-slice` guidance for material user decisions that investigation and existing instructions cannot resolve. Pause dependent work only; continue authorized independent work. |
| The assigned stage is Synthesis | Use selected `slice-gate` guidance to reconcile completed stages against the supplied acceptance criteria. |

Handle ordinary inspection directly within the supplied read-only lane.

## Plan and synthesize within the role

- In Plan, identify the smallest coherent change, relevant boundaries and observable checks for later stages. A failing baseline may support a viable plan.
- In Synthesis, reconcile supplied results, discrepancies and residual risks against the frozen candidate. Do not wait for the controller's archive or terminal verdict.
- Keep unresolved facts unresolved. Do not convert a partial screenshot, process exit, green CI status, deployment state, or agent report into semantic success without the evidence the task requires.
- Include material deviations and their impact on scope, decisions or evidence in the stage result; notes do not create an approval checkpoint.

## Close the evidence loop

Assess the evidence required by the assigned stage:

1. Automated evidence: targeted tests, type checks, lint, build, or an equivalent machine check.
2. Functional evidence: browser, simulator, logs, API behavior, rendered artifact, or a minimal smoke test.
3. Review evidence: inspect the complete relevant diff or result from a fresh defect-focused perspective.

Within the same stage, reuse evidence that covers the required behavior when the relevant code, inputs and environment have not changed. Repeat checks only for relevant changes, failures or new uncertainty, within the supplied lane. Preserve independently assigned Reviewer and Verifier work; Lead does not replace it or make future stages a prerequisite for Plan PASS.

Do not manufacture a gate that the project does not support. If a check cannot be run, state why, preserve the unverified status, and identify the smallest next check.

## Preserve authorization

The supplied task governs the sole-writer rule, permissions, delegation, controller closure authority and result protocol. This skill grants no additional authority. Reuse existing authorization and pause only work whose required decision or approval remains outstanding.

Finish when the assigned stage's criteria are met. Return its outcome, supporting evidence, remaining risks or unknowns and recommendation in the supplied result protocol, including raw JSON when required. Do not claim Mission closure.
