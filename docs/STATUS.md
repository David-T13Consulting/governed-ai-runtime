# What is proven, and what is not

"Proven" here means a test fails if the claim stops being true. It does not mean the
boundary cannot be broken. The things most likely to be wrong are the ones no test covers:
retrieval quality, the model's faithfulness, and attacks nobody thought of.

| Claim | State |
|---|---|
| Answers carry citations with file and line ranges, and one of three verdicts | Proven for the pipeline; answer quality depends on the model |
| Scope is enforced in code | Proven by tests that attempt the read: `..`, symlinks out of and inside the knowledge base, a symlinked allowlist root, a swapped folder after indexing, mis-cased paths |
| The query path is read-only | Proven: every file is hashed before and after a query, including the traced command-line path |
| No silent cloud fallback | Proven for non-local addresses, names resolving off the machine, redirects and proxy settings; measured runs were also made with all outbound traffic blocked except the local model |
| Traces hold no question text | Proven: the question is stored as a hash; model errors that echo the question are kept out |
| Repository free of credentials | `secret_scan.sh` passes, and it tests itself against a planted key so it cannot pass by doing nothing |
| Client-labelled notes never reach a model | Proven for recognisable labels. Accepted residual risk: a label disguised beyond recognition is refused as unreadable rather than flagged as client |

## Known weaknesses

- **Grounding is not faithfulness.** An answer must share at least 15% of its content words
  with what it cites. That catches an answer that cites real evidence while discussing
  something else, but not a plausible false claim built from the evidence's own words. One
  measured run produced exactly that.
- **No entailment check yet**, so no answer is reported as `supported`.
- **Decline detection** is a phrase list in English and French; it has missed declines and
  fired on hedged correct answers.
- **Abstention depends on the model.** Retrieval returns near-miss evidence for questions
  the notes cannot answer.
- **Latency** misses the 8-second p95 target with the current local model.

## Limits of "read-only"

The guards stop misconfigured, aliased, symlinked and hard-linked destinations, and a folder
swapped between check and open. They do not stop another process running as the same user
that moves a file while it is being written; such a process could already write the notes
directly.
