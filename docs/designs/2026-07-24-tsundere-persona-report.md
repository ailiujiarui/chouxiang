# Tsundere Persona Report Refinement

Date: 2026-07-24
Status: implemented; review passed

## Goal

Improve the `TSUNDERE` persona report by having the configured LLM write the
persona commentary itself. The output should sound like a sharp, reluctant
human code reviewer rather than a deterministic template or an AI-generated
summary, while remaining fair to the author and strictly grounded in evidence.

## Skill assessment

The available skills were reviewed. None targets persona writing or
de-AI-ification. `better-typography` is relevant to typography and layout,
not reviewer voice, so no skill is applied to this change.

## Current implementation findings

- `persona.py` uses fixed opening/body/final strings for `TSUNDERE`.
- The report does not consistently use the actual failed stage, changed
  regions, retry count, mutation result, or adversarial result to shape its
  voice.
- The LLM `insult_review` is generated for the technical run, but the persona
  renderer does not normalize it into a distinct, human-sounding voice.
- Existing tests verify wording differences and evidence preservation, but do
  not check repetition, forbidden phrasing, or failure-specific language.

## Design

### 1. Prompt-driven persona generation

When `ReportPersona.TSUNDERE` is selected, send the structured Judge facts and
the code-specific `insult_review` to a dedicated persona prompt. The LLM must
return JSON with only:

- `opening_verdict`;
- `commentary`;
- `closing_verdict`.

The technical report, evidence level, metrics, and Judge decision remain
authoritative inputs and are rendered by the application. If the LLM call
fails, the existing local fallback is used and the report remains valid.

### 2. Human-sounding style constraints

For `TSUNDERE`:

- prefer short spoken Chinese sentences, contractions, and controlled pauses;
- use at most one interjection per paragraph;
- mention the concrete code fact before the attitude;
- criticize branches, APIs, tests, complexity, or evidence, never the author;
- avoid “作为 AI”“综上所述”“基于当前证据”“建议您”“本次审查”等 model-like
  framing;
- avoid emoji, profanity, identity attacks, and empty praise;
- do not claim certainty beyond the evidence level.

The prompt must explicitly tell the model that it is writing a character's
voice, not explaining how an AI works. It should use concrete verbs and
specific code nouns, vary sentence openings, and avoid generic report
transitions. It should sound mildly reluctant to help, not theatrical or
abusive.

`STRICT` keeps its current neutral semantics and evidence wording.

### 3. Evidence-preserving data contract

Pass normalized facts already available in `RefactorRunResult`: status,
evidence level, changed regions, retry count, pytest/adversarial/mutation
outcomes, LOC/CC deltas, and the raw LLM review. The persona LLM may only
rewrite these facts into voice; it must not invent test results, change Judge
status, or upgrade evidence level.

The public `PersonaReport` schema remains compatible. Existing fields keep
their technical meaning; only the generated text becomes more contextual.

### 4. Report shape

The compact persona section remains in the existing location and keeps the
same headings so Dashboard extraction is unaffected:

1. one-line opening verdict;
2. one short code-specific jab;
3. evidence and metric facts;
4. one actionable closing line;
5. optional debate bullets unchanged.

### 4. Prompt and fallback tests

Add tests that capture the persona request sent to the LLM and validate the
JSON response. Mock mode must return a deterministic, clearly tsundere sample
so offline tests cover the same renderer path. Invalid JSON, missing fields,
forbidden content, and provider failure must use the safe local fallback.

## Tests

Add focused tests that assert:

- successful and failed runs produce different tsundere wording;
- the prompt includes relevant facts for static, generated-test, and repository
  evidence levels;
- retry/adversarial/mutation failures produce a matching warning;
- no forbidden AI framing, identity attack, profanity, or emoji appears;
- fallback rendering is stable and does not expose model framing;
- strict and tsundere reports preserve identical metrics and evidence fields;
- Dashboard extraction and existing Markdown headings remain compatible.

## Acceptance

- `TSUNDERE` output sounds conversational and code-specific without relying on
  an LLM rewrite.
- Technical verdict, evidence level, metrics, and safety boundaries are
  unchanged by persona selection.
- Existing tests remain green and new persona tests pass.
- A self-review checks the design against `persona.py`, `orchestrator.py`,
  `snippet.py`, and Dashboard extraction before completion.

## Verification

- Persona generation uses a dedicated prompt with moderate tsundere guidance.
- DeepSeek and deterministic Mock clients both implement the persona JSON
  contract.
- Invalid, unsafe, or failed persona responses fall back to local copy.
- `pytest -q`: 220 passed; one existing Starlette/httpx deprecation warning.
- `python -m compileall -q src tests`: passed.
- `git diff --check`: passed.

## Non-goals

- No change to Judge logic, AST safety, sandbox behavior, or LLM code
  generation.
- No new dependency or external model call.
- No expansion of the persona into author-directed abuse.
