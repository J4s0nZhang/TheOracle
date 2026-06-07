# Skill: Technical Context Handoff

## Description
Generates an ultra-dense, highly technical engineering checkpoint file (`AGENT_HANDOFF.md`) in the project root. This file enables a fresh AI agent to immediately resume the current engineering task without losing state, repeating failed approaches, or wasting context tokens on exploratory commands.

## When to Use
- When the user explicitly requests a checkpoint, handoff, or state save.
- When shifting context to a new agent instance, hitting a token limit wall, or pausing work for the session.

## Execution Protocol

### 1. State Analysis
Before writing the file, silently perform the following checks using your available terminal tools:
- Run `git status` and `git diff` to identify exactly which files have uncommitted changes.
- Review your internal conversation history to identify what approaches or libraries were tried and *failed*.
- Note the exact terminal command or test that is currently blocked or failing.

### 2. Output Generation
Write a markdown file named `AGENT_HANDOFF.md` directly to the project root. Do not include conversational filler, pleasantries, or meta-commentary in the file. Use the exact template structure below:

```text
# BREADCRUMB: AI AGENT HANDOFF

## 1. Core Objective
- [1-2 sentences stating the ultimate goal of this active branch/task]

## 2. Milestone Progress
- [x] Completed Milestone
- [/] In-Progress Milestone (Describe the exact partial/halfway state)
- [ ] Remaining Milestone

## 3. Ephemeral Technical State
- **Modified Files:** [Paths of uncommitted/active files]
- **Current Branch State:** [e.g., "Compiles locally but breaks on runtime validation", "Tests passing up to service X"]
- **Active Structural Changes:** [Any local database schema edits, API signature updates, or environment variables introduced]

## 4. Next Action Queue
1. [The exact next physical step, e.g., "Run `npm run test` and fix the Type error on line 42 of auth.ts"]
2. [Logical follow-up action]
3. [Logical follow-up action]

## 5. Dead Ends & Rationale (Do Not Repeat)
- **Tried & Failed:** [Specific approach/library tried] -> **Reason:** [Why it failed/token drain]
- **Architectural Pivot:** [Why a specific design pattern or fix was chosen over another during this session]