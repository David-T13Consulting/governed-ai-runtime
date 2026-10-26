# Sovereignty model

**Status: design.** Level L0 is built and in use. An adapter for one L1 provider is built
and tested but not connected to any entry point. L2, L3 and the per-request router are not
built.

## Levels

| Level | Where the request is processed | Example | Exposure to non-EU legal access |
|---|---|---|---|
| L0 | The owner's own machine | Local model on a laptop | None: no provider receives the request |
| L1 | EU-owned provider, EU data centres | Scaleway, Mistral | None claimed; not independently verified |
| L2 | US cloud provider, EU region | A frontier model on a US cloud in Frankfurt | Yes; contractual protection only |
| L3 | US frontier API, global | A frontier model's public API | Yes |

L2 adds data residency but not legal protection, at a higher price than L3. It is mainly
useful where a client's procurement rules require EU residency.

## Rules

1. **The level is a ceiling.** The effective level for a request is the strictest of: the
   owner's setting, the classification of every note retrieved for it, the source it came
   from, and whether the question mentions a name on a client watch-list.
2. **No fallback to a looser level.** If the allowed level is unavailable, the answer says
   so.
3. **Every answer is labelled** with its level, provider, model and cost, and that label is
   logged.
4. **Jobs over the whole knowledge base** run at the strictest level in use.

## Planned routing within the ceiling

1. Retrieve notes without calling any model.
2. Set the ceiling from rules only. Unknown or unlabelled means strictest.
3. Classify the task with a small model at the lowest level.
4. Pick the first model in a routing table, derived from evaluation results, that sits
   inside the ceiling and the monthly budget.
5. Check the answer. If it comes back incomplete, step up once to a stronger model, still
   inside the ceiling.

A rules-only router was rejected as saving little money; a learned router was rejected
because there is not enough data to train one or to explain its choices.

## What exists in code today

- `dbrain/adapter.py`: the L0 adapter. Refuses any non-local address, redirects and proxy
  settings. No fallback branch.
- `dbrain/provider.py`: the L1 adapter. Chosen only when provider, level and model are all
  set and all match a closed list in the code; adding one is a reviewed code change. The key
  is read from the keychain at each call and never stored, logged or echoed. Not connected.
