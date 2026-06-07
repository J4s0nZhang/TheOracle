---
description: Updates long-term project architecture (CLAUDE.md) and creates a temporary task checkpoint (AGENT_HANDOFF.md), strictly omitting AI tooling and .claude meta-changes.
---

You are executing the two-tier project handoff protocol. Please perform the following steps carefully using your terminal and file inspection tools.

### STRICTION REGULATION: Infrastructure Context Filter
- **EXCLUDE ALL AI TOOLING CHANGES:** Do not log, mention, or track modifications made to `.claude/`, custom slash commands, `skill.md` files, or internal AI system modifications. 
- **PROJECT FOCUS ONLY:** Both files must exclusively describe the application's true codebase, architecture, core features, and technical constraints.

### Step 1: Analyze Project-Specific Changes
- Run `git status` and `git diff` to identify files with uncommitted or active changes, **ignoring** any lines or files residing inside the `.claude/` directory or `.gitignore`.
- Review our recent chat history to identify architectural pivots or feature implementations related *strictly to the application software*.

### Step 2: Maintain Long-Term State (CLAUDE.md)
Inspect the existing `CLAUDE.md` in the project root. Update it ONLY if a core application feature was completed or the software architecture shifted. Keep the language technical and focused entirely on the app's stack, run commands, and code rules. Ensure it covers:
- Core tech stack & actual software build/test commands.
- Crucial features or legacy domains that must be protected (even if inactive this session).
- Global code style constraints.

### Step 3: Generate Short-Term Bridge (AGENT_HANDOFF.md)
First, assess whether there is outstanding work: uncommitted application code changes, in-progress features, or blocking issues identified in Step 1. If there is nothing outstanding — the working tree is clean (excluding `.claude/`) and no task is mid-flight — delete `AGENT_HANDOFF.md` from the project root if it exists, then skip the rest of this step.

If there IS outstanding work, write an ultra-dense markdown file named `AGENT_HANDOFF.md` directly to the project root. Completely omit conversational filler and AI meta-commentary. Use this exact structure:

```text
# BREADCRUMB: AI AGENT HANDOFF

## 1. Active Task Objective
- [1-2 sentences stating the immediate software engineering goal of this active branch/task]

## 2. In-Flight Progress
- [x] Completed Milestone
- [/] In-Progress Milestone (Describe the exact partial/halfway state)
- [ ] Remaining Milestone

## 3. Ephemeral Technical State
- **Modified Files:** [Paths of uncommitted/active files - EXCLUDING any .claude/ paths]
- **Current Branch State:** [e.g., "Compiles locally but breaks on runtime validation at service X"]
- **User Arguments Provided:** $ARGUMENTS

## 4. Next Action Queue
1. [The exact next physical step, e.g., "Run `npm run test` and fix the Type error"]
2. [Logical follow-up action]

## 5. Dead Ends & Rationale (Do Not Repeat)
- **Tried & Failed:** [Specific approach/library tried] -> **Reason:** [Why it failed]