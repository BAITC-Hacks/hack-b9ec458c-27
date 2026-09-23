---
name: logistics-hackathon-mvp
description: Analyze a logistics hackathon case, select a defensible demo-first MVP, and implement the approved plan after an explicit GO. Use for logistics hackathon case discovery, architecture, task splitting, and the subsequent build; do not use for routine feature work or unrelated logistics questions.
---

# Logistics Hackathon MVP

Turn an unfamiliar logistics case into one working, evidence-backed end-to-end MVP for a two-developer team.

Match the user's language. Keep confirmed facts, assumptions, and unknowns visibly distinct. Treat the case statement, judging criteria, repository, and supplied data as the source of truth.

## Select the mode

- If no approved plan exists, or the user asks to analyze, choose, design, or plan the case, read [references/case-analysis.md](references/case-analysis.md). Do not modify files in this mode.
- Enter implementation mode only when the user explicitly says `GO`, explicitly approves the proposed plan, or directly asks to implement an already identified approved plan. Then read [references/implementation.md](references/implementation.md).
- If `GO` is ambiguous because several plans exist, ask which plan was approved before editing.
- When the case centers on routing/optimization or document extraction, also read only the relevant section of [references/logistics-patterns.md](references/logistics-patterns.md).

## Non-negotiable rules

- Do not invent datasets, APIs, integrations, business metrics, savings, SLAs, constraints, test results, or product capabilities.
- Do not silently invent business scoring weights or optimization objectives. When the case omits them, either ask if they change the MVP choice or use a transparent, configurable demo heuristic labeled as synthetic; never claim that it is optimal or production policy.
- Do not use an LLM to guess deterministic results. Prefer Python, SQL, optimization libraries, or trusted APIs for routes, distances, capacities, costs, sorting, ETA calculations, and KPIs.
- Use an LLM or agent only where language understanding, unstructured inputs, exception handling, explanation, source synthesis, or tool orchestration provides concrete value.
- If the case does not benefit from an agent, say so and propose the stronger non-agent architecture.
- Prefer one clear user, one valuable workflow, one real data path, at most three essential tools, and one convincing 60-second demo.
- Keep critical or externally consequential actions behind human approval. Approval of a plan does not authorize unrelated actions, publishing, deployment, purchases, or credential use.
- Keep the primary demo useful when an optional API, model, credential, or network dependency is unavailable, and state which capabilities are degraded.
- Never expose secrets or place credentials in source files, logs, screenshots, fixtures, or documentation.

## Expected result

Before `GO`, finish with a decision-ready MVP plan and stop. After `GO`, produce a verified vertical slice, report the exact checks run and their outcomes, list changed files, and identify remaining limitations without claiming unperformed results.
