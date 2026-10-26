---
classification: public
---
# AGENTS.md: sample vault

A synthetic personal knowledge base, used to demonstrate and test the runtime. Every note is
invented. The owner is fictional.

## Hard rules
1. Read-only. Agents change a note only when the owner asks for that change.
2. Keep out: client confidential information, secrets, and sensitive data about anyone but
   the owner.
3. Every note carries `classification: public | internal | client` in its frontmatter.
   A note labelled `client` is a violation: the runtime stops every query until it is removed.
4. Propose, don't populate: claims and decisions enter the vault only after the owner confirms.
5. When unsure, stop and ask.

## Review
Every change an agent proposes is reviewed by a second model from a different vendor before
it is committed. Only the lead agent commits.
