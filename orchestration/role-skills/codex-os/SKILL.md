---
name: codex-os
description: Route substantial engineering and product work through the smallest useful sequence of discovery, decisions, implementation, verification, review, and handoff. Use automatically for new projects, websites, web pages, applications, product design, redesigns, multi-step changes, builds, fixes, or operational work; skip simple answers and one-step edits.
---

# Codex OS

Deliver the requested outcome with evidence while loading only the workflow guidance the task actually needs.

## Shape the work

1. Read the applicable `AGENTS.md` instructions and inspect relevant state before proposing changes.
2. Establish the objective, verified current state, constraints, success criteria, and allowed side effects.
3. Choose the smallest workflow below. Do not run every phase by default.

| Need | Route |
| --- | --- |
| Repository state, ownership, or risk is unclear | Use `$fase-0-recon` before implementation. |
| A missing product, architecture, or scope decision would materially change the result | Use `$entrevista-pre-slice` for material user decisions that investigation and existing instructions cannot resolve. Pause dependent work only; continue authorized independent work. |
| The main goal is design exploration, UX research or audit, faithful visual recreation, redesign direction, or prototype QA | Use `$product-design:index` to select the focused design workflow before implementation. |
| A website, landing page, redesign, or visual-polish task needs warmth, personality, editorial detail, brand specificity, or rescue from generic AI aesthetics | Apply `$human-web-art-direction` as the art-direction and anti-generic gate before visual concepting or implementation. When Product Design also applies, preserve its source-selection, approval, and fidelity workflow. |
| Implementation spans multiple meaningful slices | Maintain `$impl-notes` during execution and apply `$slice-gate` before advancing. |
| Behavior can be exercised after a change | Use `$smoke-verify` and preserve the evidence it requires. |
| The user requests a commit | Use `$commit` only after verification and review. |
| The user requests deployment | Use `$deploy`; obtain the confirmation it requires immediately before production. |

Handle ordinary inspection, a narrow reversible edit, or a focused test directly when loading another skill would add no decision value.

## Execute by verified slices

- Prefer one agent. Use parallel agents only when workstreams are independent, have clear ownership, and can be verified separately.
- Keep interactive discovery and decision-making in the current conversation. Start a fresh execution context for an independent work order when accumulated context would distract from it; transfer the objective, decisions, constraints, relevant paths, and definition of done.
- Make the smallest coherent reversible change, then run the most relevant available check before expanding scope.
- Keep unresolved facts unresolved. Do not convert a partial screenshot, process exit, green CI status, deployment state, or agent report into semantic success without the evidence the task requires.
- Use `$impl-notes` for material deviations. Record them before dependent work when they change a decision, constraint, or verification plan. Otherwise, a concise delivery note is sufficient. Documentation does not itself create an approval checkpoint.

## Close the evidence loop

For substantial changes, seek three forms of proof when applicable:

1. Automated evidence: targeted tests, type checks, lint, build, or an equivalent machine check.
2. Functional evidence: browser, simulator, logs, API behavior, rendered artifact, or a minimal smoke test.
3. Review evidence: inspect the complete relevant diff or result from a fresh defect-focused perspective.

Do not manufacture a gate that the project does not support. If a check cannot be run, state why, preserve the unverified status, and identify the smallest next check.

## Preserve authorization

This workflow never grants permission beyond the user's request. Follow the active `AGENTS.md` approval boundaries, including existing authorization and any explicit final-stage approval requirement. Pause only at the action whose required approval remains outstanding.

Finish with the outcome, supporting evidence, remaining risks or unknowns, and the recommended next step. Do not claim completion while required verification or an authorized deliverable remains unfinished.
