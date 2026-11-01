# Smerfume Backend

## Project

Smerfume is a Django-based e-commerce backend focused primarily on selling
fragrances and providing a strong, trustworthy buying experience.

The product discovery system supports the shopping experience but Smerfume
should not become an encyclopedia-style fragrance database.

## Stack

- Python
- Django
- Django REST Framework
- PostgreSQL
- Django Unfold

## Current Architecture

- Product catalogue is already implemented.
- Product hierarchy:
  Product → Edition → Variant
- Inventory should remain simple.

Inventory must support:

- Retail bottles
- Testers
- Partial bottles
- Decanting
- Damaged stock
- Promotional/gift stock
- Returns

Do not assume additional architecture unless documented elsewhere.

## Documentation Hierarchy

The following documents contain detailed project requirements and should be
consulted selectively rather than loaded unnecessarily:

docs/PRODUCT_BIBLE.md
    Original baseline business requirements.

docs/CHANGE_DECISIONS.md
    Approved business changes made after the original Product Bible.

docs/BUSINESS_REQUIREMENTS.md
    Business requirements and business rules.

docs/PRODUCT_CATALOG.md
    Product catalogue structure and product hierarchy.

docs/INVENTORY_MANAGEMENT.md
    Inventory rules and inventory requirements.

docs/FEATURES.md
    Functional feature requirements.

docs/MODULES.md
    Logical application/domain modules.

docs/TECH_STACK.md
    Technology decisions and preferences.

docs/ARCHITECTURE.md
    Intended system architecture.

Also maintain:

.claude/CURRENT_STATE.md
    Lightweight current development state used to continue work after
    /clear without reconstructing previous conversations.

.claude/DECISIONS.md
    Important technical/architectural decisions made during development.

## Source of Truth

For business requirements:

1. Check docs/CHANGE_DECISIONS.md for approved changes.
2. Then refer to docs/PRODUCT_BIBLE.md.
3. Do not invent business requirements.
4. Do not silently change business rules.
5. Do not treat suggestions or discussions as approved requirements.

An APPROVED decision in CHANGE_DECISIONS.md takes precedence over the
corresponding older requirement in PRODUCT_BIBLE.md.

The original Product Bible must not be silently rewritten during development.

## Context and Token Efficiency

Optimize for minimal context usage.

Do NOT read the entire repository or all documentation for every task.

Before starting a task:

1. Read .claude/CURRENT_STATE.md.
2. Identify the specific module affected.
3. Read only the documentation relevant to that task.
4. Inspect only the relevant source code and tests.
5. Avoid rereading files whose contents are already known and have not changed.
6. Do not reconstruct previous conversations unnecessarily.

Do not load the complete Product Bible for normal implementation tasks.

Use targeted documentation and code inspection instead.

After /clear, use CURRENT_STATE.md as the primary continuation point.

## Project State

Maintain .claude/CURRENT_STATE.md as a concise snapshot of the current
development state.

It should contain only:

- Current development phase
- Current task
- Recently completed work
- Next steps
- Known issues
- Important unfinished work
- Important implementation notes
- Pending decisions

Do NOT turn CURRENT_STATE.md into a historical log.

Remove outdated information when updating it.

Update CURRENT_STATE.md only after meaningful work such as:

- Feature completion
- Significant bug fix
- Architecture change
- Important technical decision
- Meaningful development milestone

Do not update it for every small file edit.

## Technical Decisions

Record important technical or architectural decisions in:

.claude/DECISIONS.md

Each decision should include:

- Decision
- Reason
- Impact
- Status

Do not record trivial implementation details.

## Important Rules

- Do not redesign existing models unless explicitly requested.
- Do not modify unrelated apps.
- Do not introduce unnecessary dependencies.
- Prefer Django ORM.
- Follow existing project conventions.
- Before modifying a model, inspect its current relationships and migrations.
- Do not generate migrations unless model changes require them.
- Run relevant tests after changes.
- Do not commit directly without explicitly asking for confirmation.
- Keep implementations simple.
- Avoid over-engineering.
- Prefer small, incremental changes.
- Explain architectural changes before implementing them.
- Maintain PEP-8 standards.

## Third-Party Integrations

All third-party integrations must be replaceable/toggleable where practical.

Examples include:

- Payment gateways
- SMS providers
- Email providers
- Checkout providers
- Shipping providers
- Analytics providers
- Other external services

The implementation should allow switching providers through configuration
rather than requiring business logic to be rewritten.

Do not introduce unnecessary abstraction layers solely for theoretical
future providers.

## Error Handling and Logging

API exceptions and unexpected application errors must be captured through
the project's logging/error-handling mechanism.

Do not silently swallow exceptions.

Do not expose internal exception details, stack traces, database information,
credentials, or other sensitive information through public APIs.

## API Standards

When creating or modifying an API:

- Use appropriate serializers and validation.
- Validate client-provided data.
- Do not expose sensitive information.
- Do not expose internal database primary keys in public URLs unless
  explicitly required.
- Follow existing API conventions.
- Create API documentation/docstrings.
- API documentation is mandatory and cannot be skipped.
- Add appropriate tests.

## Development Workflow

### Branching Rules

Never implement features, bug fixes, defects, refactors, migrations, or other code changes directly on `dev`, `uat`, or `main`.

If the current branch is `dev`, `uat`, or `main` and the task requires code changes, create a new branch before editing files.

Do not reuse an old feature branch for unrelated new work.

For every meaningful code change:

1. Start from the approved `dev` baseline.
2. Verify the working tree is clean.
3. Verify local `dev` matches the expected `origin/dev`.
4. If `origin/dev` has advanced unexpectedly, stop and report before branching.
5. Create a new branch before modifying code.
6. Use a clear branch prefix:
   - `feat/` for features
   - `fix/` for bugs/defects
   - `refactor/` for refactors
   - `chore/` for maintenance/tooling
   - `docs/` for documentation-only changes
7. Keep unrelated work out of the branch.
8. Do not merge into `dev` without explicit confirmation.
9. Do not push or modify `uat` or `main` unless explicitly instructed.

Examples:

- `feat/redis-throttling`
- `fix/order-cancellation-stock-release`
- `refactor/inventory-reservation-locking`
- `docs/booked-incoming-architecture`

### Before implementation

1. Understand the existing implementation.
2. Identify affected files/modules.
3. Check relevant business requirements.
4. Check approved business decisions.
5. Check relevant technical decisions.
6. For significant architectural changes, explain the proposed approach before implementation.

### During implementation

- Make the smallest appropriate change.
- Avoid unrelated refactoring.
- Follow existing patterns.
- Add or update tests.

### After implementation

- Run relevant tests.
- Check for regressions.
- Update `CURRENT_STATE.md` if the task meaningfully changes project state.
- Record significant technical decisions in `DECISIONS.md`.

## Security

- Never expose credentials, passwords, API keys, tokens or secrets.
- Do not commit secrets.
- Do not expose internal database identifiers unnecessarily.
- Validate and sanitize external input.
- Do not trust client-provided values for business-critical fields.
- Use appropriate authentication and authorization controls.

## Communication

For small changes:
- Briefly explain the issue.
- Implement the change.
- Run relevant tests.
- Summarize the result.

For significant changes:
- Explain the proposed approach first.
- Identify affected modules/files.
- Identify potential risks.
- Ask for confirmation before making major architectural changes.

Do not repeatedly explain information that is already documented.

## General Principle

Prefer:

Small Context
+ Relevant Documentation
+ Relevant Code
+ Current State
+ Targeted Changes

over:

Entire Repository
+ Entire Documentation
+ Entire Conversation History

The goal is to maintain a clean, scalable and understandable codebase while
minimizing unnecessary context and token consumption.

## Session Handoff and Project State

Maintain two local project-state files:

- `.claude/SESSION_HANDOFF.md`
  - short-lived handoff between Claude Code sessions
  - updated before `/clear` or `/compact`
  - records only the context needed to continue the current work

- `.claude/CURRENT_STATE.md`
  - durable summary of the actual project state
  - tracks completed/pending modules, branches, remote state,
    migrations, test baseline, deployment prerequisites and known debt

Both files are local working-state files and must not be staged or committed
unless explicitly instructed.

### Before `/clear` or `/compact`

When the user indicates that the current session is ending, being cleared,
or being compacted:

1. Update `.claude/SESSION_HANDOFF.md`.
2. Update `.claude/CURRENT_STATE.md` if the project state changed during
   the session.
3. Do not perform a broad repository reread just to update these files.
   Use the work and results already established in the current session.

`SESSION_HANDOFF.md` should contain only:

- current branch and HEAD
- base branch / relevant remote status
- current task or phase
- what was completed in this session
- commits created
- tests/checks already run and their results
- important decisions made
- uncommitted changes that must be preserved
- blockers or prerequisites
- exact next recommended step

Keep it concise. Do not turn it into project documentation.

### When CURRENT_STATE.md must be updated

Update `.claude/CURRENT_STATE.md` whenever a meaningful project-state
transition occurs, including:

- a feature, bug fix, defect, refactor or other meaningful change is merged
- a branch is pushed to a remote
- `dev`, `uat`, `staging`, `main` or another deployment branch changes
- a feature is deployed to an environment
- a major phase/module becomes complete
- a feature flag is enabled or disabled as part of rollout
- migrations are applied to a shared environment
- the canonical test baseline changes materially
- a blocker or deployment prerequisite is resolved or introduced
- an important pending item becomes completed or obsolete

Do not wait for the end of the session if one of these events happens.

### Remote-state recording

When work is pushed or deployed, CURRENT_STATE.md must explicitly record:

- branch
- local HEAD
- corresponding remote HEAD when known
- whether local and remote match
- merge status
- environment affected
- migration status if relevant
- feature-flag status if relevant
- latest relevant test/check result

Never claim something is pushed, merged or deployed unless it was actually
verified.

### Before ending a substantial task

Check that SESSION_HANDOFF.md and CURRENT_STATE.md do not contradict each
other.

If CURRENT_STATE.md contains stale information discovered during the task,
correct it locally before handoff.


## Figma Read-Only Design Reference

The connected Smerfume Figma file is a READ-ONLY product/design reference.

Claude may:
- inspect Figma files, pages, frames and components
- read layout, text, fields, states and interaction context
- use Figma to understand customer-facing and admin-facing workflows
- compare Figma requirements with the existing backend
- reference Figma when designing APIs, models, services and admin workflows

Claude must NEVER:
- create Figma frames
- modify designs
- move, resize or delete layers
- edit text
- change components, styles, variables or prototypes
- write to the Figma canvas
- generate or send designs back into Figma

If a Figma tool/action would modify the Figma file, DO NOT use it.

Before backend implementation for a UI-driven feature:
1. Inspect only the relevant Figma frames.
2. Identify the user actions, fields, states, validations and data required.
3. Compare them with the existing backend.
4. Report mismatches before implementation.

Figma is the authoritative UI/product reference, but it does not override:
- approved DEC decisions
- backend data-integrity rules
- security/permission rules
- GST/accounting decisions
- inventory/ledger invariants

If Figma conflicts with an approved backend decision, stop and report the
conflict instead of changing either side automatically.

Do not broadly scan the whole Figma file unless explicitly requested.
Use only the relevant frames for the current feature.

## Smerfume Figma

Primary Figma file:
https://www.figma.com/design/Ryqnrl7GMJrSFajJeLs3BT/Smerfume?node-id=259-476&t=8pDGaaVrRsQ1ulPr-1

Use this file through the connected Figma MCP as a read-only reference.
