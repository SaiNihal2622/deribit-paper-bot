# Trader — per-cycle decision prompt

Below is the live market + account state for this cycle.

```json
{{CONTEXT}}
```

Decide what to do with the candidate plans.

**OUTPUT FORMAT — strict JSON object, nothing else. No prose, no markdown, no code fences, no thinking aloud.**

Schema:
```json
{"action": "approve|veto|downsize|hold", "target_qty": <int>, "rationale": "<=30 words"}
```

Rules:
- `action` must be exactly one of `approve`, `veto`, `downsize`, `hold` (lowercase).
- `target_qty` must be a positive integer ≤ each plan's `qty`.
- `rationale` is one short sentence (<= 30 words).
- If there are no candidate plans, return `{"action":"hold","target_qty":1,"rationale":"no candidate plans"}`.
- Do not propose new keys or change strategy files.

Begin your response with `{` and end with `}`.
