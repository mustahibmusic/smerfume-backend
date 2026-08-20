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

Before implementation:

1. Understand the existing implementation.
2. Identify affected files/modules.
3. Check relevant business requirements.
4. Check approved business decisions.
5. Check relevant technical decisions.
6. For significant architectural changes, explain the proposed approach
   before implementation.

During implementation:

- Make the smallest appropriate change.
- Avoid unrelated refactoring.
- Follow existing patterns.
- Add or update tests.

After implementation:

- Run relevant tests.
- Check for regressions.
- Update CURRENT_STATE.md if the task meaningfully changes project state.
- Record significant technical decisions in DECISIONS.md.

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
