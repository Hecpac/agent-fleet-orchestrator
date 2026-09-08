# Instruction discovery and transfer

The project root `AGENTS.md` is the maintainer contract for this repository.
There were no existing tracked project `AGENTS.md` files at this change's
baseline. It is not installed into another repository or the user's global
Codex home. Historical CMUX workflows keep their own launch path.

| Surface | Transfer and authority |
| --- | --- |
| Local maintainer | Root project AGENTS plus the current user's authorized task. |
| `mission-run.py` → Herdr driver | Task contract contains role, stage criteria, permission boundary, output protocol and a target instruction packet. |
| Candidate | A clean, independent clone at the durable baseline retains target files; no synthetic AGENTS is injected over them. |
| Five Herdr turns | `project_instructions` contains the same CAS snapshot ID and actual baseline instruction content, SHA-256 and directory scope. The whole task is also in CAS and bound to its run. |
| Herdr CLI launch | Explicit candidate cwd, configured model/effort and role permission flags; uses the selected Codex home. This change neither replaces that home nor proves automatic global instruction discovery. |
| Historical interactive Codex launcher | `fleet_codex_home.py` provisions/verifies its existing ephemeral home with a small role contract. It does not copy the maintainer's global AGENTS or skills. Existing auth binding behavior is unchanged. |
| Historical Kimi readers | `fleet-up.sh` copies `orchestration/prompts/kimi_reviewer_agents.md` into its own reader clone before sealing; `run-kimi-reviewer.sh` supplies `KIMI_AGENTS_MD` for the standalone reader. These existing, explicitly selected routes are preserved and are not used by Herdr. |

`fleet_herdr_instructions.py` discovers tracked `AGENTS.md` and
`AGENTS.override.md` blobs from the exact Git baseline. Within each directory,
override replaces AGENTS. Root scope applies throughout the candidate; nested
scope refines it only within that directory. All selected scopes are supplied,
so a role need not guess whether automatic CLI nested discovery occurred.
The controller packet does not claim to reproduce every Codex configuration
option (custom fallback names, ancestor/global files or automatic truncation).
Those are outside this explicit, bounded transfer protocol.

Only regular UTF-8 instruction files are supported: at most 32 selected scopes,
32 KiB per file and 64 KiB total. Symlinks and excess size fail before role boot;
content is never silently truncated. The clean-baseline requirement excludes
untracked target instructions. The first Plan task anchors the snapshot in CAS;
later turns and recovery reuse it, including when Worker changes instruction
files as an authorized deliverable. Such edits do not grant new Mission
authority. Historical tasks lacking a packet retain their original contract.

Provider-free fixtures call the actual `mission-run.py` entry point with a
simulated backend, create/clone synthetic Git repositories, inspect all five
submitted task payloads and verify their CAS bytes. They also test nested and
override scopes, rejected linked/oversized files, baseline preservation and
recovery after a worker edit. Existing backend tests check CLI cwd/arguments;
ephemeral-home tests execute provisioning and verify its on-disk contract.
These prove file discovery and transfer, not real-model obedience or automatic
global-skill inheritance. A provider campaign remains separately authorized.
