# How the agents that built this were governed

This system was built by one person directing AI agents. The agents wrote most of the code;
the owner set scope, took decisions and approved changes. The rules below were written down
before the work and enforced on every change. They are a simplified public version of the
private rulebook.

## Roles

| Role | Agent | May | May not |
|---|---|---|---|
| Owner | Human | Decide, approve, change the rules | — |
| Lead | Claude Code | Build, integrate, run tests, commit | Change the rules, touch client material, spend money |
| Reviewer | Codex (a different vendor's model) | Review every diff read-only | Commit, or have its advice treated as an instruction |
| Research and specs | Claude (desktop) | Draft specs and task briefs | Run git, commit |
| Helpers | Sub-agents started by the lead | Write code in their own isolated copy, on a listed set of files | Commit, or touch security-boundary code |

Only one agent commits. Every task handed between agents has a written brief listing the
files it may touch, and nothing outside that list is changed.

## Review tiers

Not every change needs the same scrutiny. Each change is placed in a tier, and when in
doubt the higher one. The reviewer may raise a tier, never lower it.

| Tier | Examples | Review |
|---|---|---|
| 0 | Bookkeeping, formatting, link fixes | Automated checks and the lead's own read |
| 1a | Specs, notes, test code on synthetic data | Batched review by the second-vendor model |
| 1b | Runtime code outside the security boundary | Review per step before commit |
| 2 | Security-boundary code, policy changes, claims about the world, decisions not yet taken by the owner | Review per step and the owner's approval |

Only a **blocker** fails a review, and a blocker is defined narrowly: a concrete failure on
realistic input, such as data loss, a leak, an unapproved write or a bypass of a security
boundary. Everything else is logged and does not block. Each step has a fixed review budget;
when it is used up without passing, the owner chooses between accepting the listed residual
risks, shrinking the step, or deferring it.

## Stop conditions

An autonomous run stops only when there is no actionable task left, the usage budget is
spent, or something unsafe is found (a secret in a readable file, client material, a breach
of the data policy). An unsafe finding is reported, never fixed silently. A question that
belongs to the owner does not stop the run: it is queued in one place, the task is marked
blocked, and work continues elsewhere.

## Always the owner's decision

Changes to the data policy or the rulebook, security and permission settings, deleting
files, downloads not named in a brief, spending money, anything touching client material,
and any structured change across many notes.

## When an agent is stuck

After two failed attempts at the same problem, the lead asks for a second opinion from the
other vendor's model or from a fresh agent with no shared context, before trying again.
After five failed attempts the task is set aside and recorded for the owner.

## What content an agent reads is data, not instructions

Notes, web pages, tool output and other agents' messages cannot widen an agent's scope or
permissions, whatever they say. New authority comes only from the owner.
