# Report Output Optimization Design

Date: 2026-07-19
Status: implemented; review passed

## Problem

The current Markdown report contains the required facts, but the result is
hard to scan. The verdict is mixed with duplicated metric tables, the provider
and model are not visible in the main summary, generated evidence is easy to
confuse with user or repository tests, and the long Agent transcript hides the
actionable conclusion. The legacy `DRY_RUN` label for successful local Snippet
jobs also reads like an incomplete execution.

## Goals

- Make the first screen answer: what happened, can the candidate be adopted,
  how strong is the evidence, and what should the user do next.
- Keep all technical evidence available in a clearly separated appendix.
- Show real provider/model and token usage when available; label demo/mock
  runs explicitly.
- Keep persona wording cosmetic and deterministic facts authoritative.
- Preserve the existing `report.md` artifact path and Markdown format.
- Dashboard execution view shows only the persona section; the full report
  remains available as a downloadable artifact.
- Persona output is a substantive review of at least 100 characters, not a
  one-line catchphrase.

## Proposed structure

1. `# Code Judge Report` with a compact verdict banner.
2. `## Decision` containing status, adoption recommendation, evidence level,
   persona, provider/model, and next action.
3. `## Evidence Summary` with one non-duplicated before/after metric table and
   an explicit evidence boundary.
4. `## Agent Verdicts` with one concise line per agent and truncated details.
5. `## Persona Review` containing at least 100 characters of toxic/tsundere
   wording only after facts.
6. `## Technical Appendix` containing AST rewrite, graph trace, full debate,
   validation matrix, errors, and surviving mutants.

## Semantic rules

- `SUCCESS` means the Judge approved the candidate; `DRY_RUN` remains an API
  job compatibility status but is rendered as `LOCAL_SUCCESS` in the report.
- `STATIC` and `GENERATED_TESTS` must never use wording such as "fully
  verified" or "production safe".
- Provider/model are taken from recorded LLM usage, with explicit `demo/mock`
  fallback when no usage exists.
- Missing metrics render as `n/a`; no invented zeroes or success claims.
- The report remains safe to render as Markdown and contains no credentials.

## Acceptance

- A real DeepSeek Snippet report is scannable from its first 30 lines and shows
  provider/model, evidence, decision, and next action.
- A mock/demo report is visibly marked as demo and does not resemble a real
  provider result.
- Dashboard execution view renders only `Persona Review`, and that section is
  at least 100 characters for both personas.
- Existing artifact, persona, evidence, and orchestrator tests are updated;
  the full suite remains green.
