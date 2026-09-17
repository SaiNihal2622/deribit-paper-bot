# Trader — per-cycle decision prompt

Below is the live market + account state for this cycle.

```json
{{CONTEXT}}
```

Decide what to do with the candidate plans. Reply with **strict JSON only**.
If there are no candidate plans, return `{"action":"hold","target_qty":1,"rationale":"..."}`.

Hard rules:
- `target_qty` must be a positive integer ≤ each plan's `qty`.
- Do not propose new keys or change strategy files.
- One short sentence rationale (<= 30 words).
