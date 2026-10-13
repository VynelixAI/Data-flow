---
name: dataflow-quality
description: >
  Apply DQX inside this agent when copying a Unity Catalog source to a destination.
  Use when the user asks to copy data, validate source to target, quarantine bad rows,
  or generate quality rules for a source that changes every run. Uses the metadata
  form from dqx-define-checks and apply_checks_by_metadata from dqx-apply-checks.
---

# DataFlow quality

Load `dqx-define-checks` and `dqx-apply-checks` before you invent a check. This product always uses DQX metadata, never a mix of classes and metadata in one run.

1. Build checks from the source that was just read: column names, types, and a small sample. A different source gets a different rule set. Do not reuse a previous dataset's column list.
2. Each check is `{name, criticality, check: {function, arguments}}`. `criticality` is `error` or `warn`. Give every rule a stable snake_case `name`.
3. Prefer built-ins: `is_not_null` and `is_unique`. Use `sql_expression` only when no built-in fits. The SQL expression is the pass condition: DQX fails the row when it evaluates to false. Omit `negate` so it stays at that default. Do not write the JSON token `false` into notebook Python.
4. Apply with `apply_checks_by_metadata`. Rows with `_errors` go to quarantine. Rows with only `_warnings` stay in the valid output.
5. Report each failing rule with its count and sample keys. A failure does not turn the rule off.
6. Call `load_skill` for `dqx-profile-and-generate` only when the user asks to profile or suggest rules, and for `dqx-storage` only when they ask to save or load a checks file.
