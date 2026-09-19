<!-- $$name/$$slug/$$summary below are reserved for init_feature's substitution --
     never write these exact tokens in illustrative text elsewhere in this
     file, or they'll be substituted too. `{...}`-style examples (e.g.
     `{project-name}/{slug}/README.md`) stay safe regardless -- only
     `$`-prefixed tokens are ever recognized. -->
# $name — Knowledge Base

> **Feature:** `$slug`

<overview>
$summary
</overview>

<key_files>
<!-- Entry points and key files Claude should navigate to first. -->
<!-- Use symbol names (functions, classes) — NEVER line numbers; they drift as code changes. -->
<!-- Wrap each path in backticks — the related-kb signal only reads backtick-quoted tokens; -->
<!-- a bare path here is invisible to it. -->
<!-- Example:
  - `src/adapter.py`     — Adapter1, Adapter2
  - `src/cache.py`             — _cache_function1(), _cache_function2()
  - `jobs/migrate_job.py`       — migration script, parallel workers
-->
</key_files>

<technical_stack>
<!-- Languages, frameworks, libraries, infrastructure components used by this feature. -->
<!-- Number entries TS-1, TS-2, ... for easy cross-reference with other sections. -->
</technical_stack>

<business_rules>
<!-- Invariants that must always hold. Use **[rule] title** blocks — same tag -->
<!-- vocabulary as save_memory, so a promoted [rule] entry carries over as-is. -->
<!-- Example: **[rule] customer_id is always null for platform-attributed data** -->
</business_rules>

<architecture>
<!-- Key design decisions, data flow, component boundaries, and why they were chosen. -->
<!-- Example:
  Input → Validate → Transform → Persist → Emit event
  - Transform runs in a single transaction with Persist; never split them.
  - Event emission is fire-and-forget; failures are logged but do not roll back.
-->
</architecture>

<critical_warnings>
<!-- Gotchas, sharp edges, past incidents. Anything that would cause a silent bug if ignored. -->
<!-- Use **[tag] title** blocks — same tag vocabulary as save_memory ([gotcha], -->
<!-- [constraint], etc.) — so a promoted entry carries its tag over unchanged. -->
<!-- Example: **[gotcha] record_id is null — never use it in filtering logic** -->
</critical_warnings>

<open_items>
<!-- Known gaps, deferred work, or decisions still pending. -->
<!-- Use a table: | ID | Issue | Owner | Blocks | -->
</open_items>

<related_tickets>
<!-- Features or tickets that share context with this one. Claude will hint to load these when loading this KB. -->
<!-- Format: one entry per line — slug (TICKET-ID): reason -->
<!-- Example:
  fraud-detection (PROJ-1282): shares transaction state; write order depends on payment-gateway completing first
  integration-test (PROJ-1284): validates this feature end-to-end
-->
</related_tickets>

<checklist>
<!-- Track progress for any phase: pre-launch gate, migration steps, review checklist, deployment steps, etc. -->
<!-- Example:
### Blockers
- [ ] OI-1 Confirm refund policy with payments lead — blocks execution

### Tasks
- [x] Build core module
- [ ] Wire into pipeline

### Testing
- [ ] Run functional scenario against MinIO
-->
</checklist>
