# Case analysis mode

Use this mode before implementation approval. Read the available case statement, judging criteria, files, data samples, API descriptions, and repository instructions. Repository inspection is read-only.

## Establish the evidence

Identify:

- the specific user and decision they need to make;
- the current workflow and measurable pain;
- the logistics category, without forcing the case into a predefined label;
- the data and integrations that demonstrably exist;
- missing information and assumptions;
- missing business rules, weights, or optimization objectives that would affect ranking or recommendations;
- hard constraints, judging criteria, time, team, and submission format;
- what the team can prove live during a short demo.

Separate mandatory eligibility/submission gates from scored case-specific criteria and later presentation criteria. Use the published task specification as the scoring source for technical selection; if it is unavailable, mark it unknown rather than borrowing weights from a general event rubric. For each applicable gate and criterion, identify the smallest project artifact or live check that could prove it. Include repository/history requirements, progress checkpoints, deadline, reproducible README, and reviewer access without personal credentials when the supplied rules require them. Treat pre-existing tools or templates as such; do not present pre-built core functionality as work done during the event.

If important information is absent, continue with clearly labeled assumptions when that is safe. Ask no more than three questions, and only when their answers could materially change the MVP choice.

## Separate AI from deterministic work

Classify each important operation as one of:

- deterministic application logic, Python, or SQL;
- optimization or solver;
- external API;
- LLM;
- agent coordinating tools.

Explain any use of an LLM or agent in terms of a concrete capability. Do not call ordinary calculations agentic.

If ranking or optimization policy is missing, do not disguise invented coefficients as domain truth. Prefer a transparent rule set for the demo, label it synthetic and configurable, and identify the policy decision needed before production use.

## Select the MVP

Generate at most three candidate MVPs. For each, state concisely:

- user and input;
- primary action and output;
- role of AI;
- required tools and real data;
- 60-second demonstration;
- largest delivery risk.

Choose one candidate using, in order: mandatory gates and case-specific requirements, user value, available evidence and data, end-to-end feasibility, demonstrability, implementation risk, and presentation value. Do not trade away a mandatory gate for presentation points.

## Design the minimum solution

Describe the selected flow:

```text
input -> validation/preprocessing -> algorithm or agent -> tools/data -> structured result -> UI
```

Specify only what is needed:

- components and likely files or modules;
- core functions and at most three essential agent tools;
- structured schemas where they prevent ambiguity;
- deterministic computations versus model responsibilities;
- approval boundaries for consequential actions;
- failures, unavailable data, and safe fallbacks.

Avoid microservices, multi-agent architecture, MCP, message queues, authentication, complex databases, custom model training, and elaborate frontends unless the case proves they are necessary.

## Make the plan executable

Define one primary demo path that states what the user enters, which real data is used, what runs, which tools are called, what result appears, and how the result can be verified rather than trusted blindly.

Identify optional credentials, APIs, model calls, and network dependencies. Define a bounded offline or degraded demo path for each dependency that may be unavailable; do not imply that the fallback provides capabilities it cannot support.

List three to five explicit `NOT DOING` items.

Divide work between `DEV A` and `DEV B` to minimize overlapping file edits. Define a shared contract for inputs, outputs, schemas, function or endpoint names, error states, and integration ownership.

Plan implementation as a vertical slice first:

```text
real input -> validation -> one algorithm/tool -> agent if justified -> structured result -> minimal UI
```

Then schedule reliability, tests, UI polish, and documentation. Give each stage an observable definition of done.
Place required progress checkpoints and the final reproducible launch check on that schedule; start the README early enough that a reviewer can run the primary scenario from it before the deadline.

## Response contract

Return:

1. Case understanding, with facts separated from assumptions.
2. Main user problem.
3. Candidate MVPs and the selected MVP.
4. Selection rationale.
5. Minimal architecture and AI-versus-algorithm split.
6. Primary demo flow and verification method.
7. `DEV A`, `DEV B`, and shared contract.
8. `NOT DOING` list.
9. Main risks and mitigations.
10. Ordered implementation plan with done criteria.
11. Compact evidence map: each mandatory gate and published case-specific criterion -> planned artifact or check; flag unknown criteria separately.

End by requesting explicit `GO`. Do not write code, edit files, install dependencies, create external resources, or begin implementation in this mode.
