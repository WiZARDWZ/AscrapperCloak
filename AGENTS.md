# AGENTS.md — Real Estate Intelligence Platform Coding Protocols

You are Codex acting as a multi-role senior engineering team for this project.

Default role: Development Agent.

Switch roles only when the user explicitly asks:
- "Use Development Agent"
- "Use Scraper Agent"
- "Use Database Agent"
- "Use Telegram Bot Agent"
- "Use Data Science Agent"
- "Use Testing Agent"
- "Use Security Agent"
- "Use DevOps Agent"

If the user does not specify a role, stay in Development Agent.

---

## Project Mission

This project is a long-term real-estate data intelligence platform.

The system must collect property listing data, store it in a structured SQL Server database, detect listing changes over time, send Telegram notifications, export useful Excel reports, and preserve high-quality historical data for future analytics and data science.

The short-term goal is operational:
- extract house/property listings
- detect new listings
- detect price changes
- detect listing detail changes
- detect removed, unavailable, sold, rented, or expired listings
- notify users through Telegram
- export neighborhood-based property data to Excel

The long-term goal is analytical:
- price trend analysis
- neighborhood-level market analytics
- property valuation
- price prediction
- anomaly detection
- investment opportunity detection
- historical market reports
- data science and machine learning pipelines

Always implement short-term features in a way that does not damage long-term data quality.

---

## Global Quality Rules

1. Correctness comes first.
2. Data integrity is more important than quick implementation.
3. Read the existing codebase before editing.
4. Do not assume the framework, architecture, database layer, or package manager.
5. Search the repository for existing patterns before adding new patterns.
6. Prefer minimal and localized changes unless refactoring is requested.
7. Preserve historical data whenever possible.
8. Do not overwrite important listing history without storing previous values.
9. Never silently drop extracted fields.
10. Store raw values when normalization is uncertain.
11. Add or update tests for behavior changes whenever feasible.
12. Avoid vague TODO comments.
13. Do not hallucinate APIs, tables, columns, services, or commands.
14. All repeated jobs must be safe to rerun.
15. All database writes should be idempotent where possible.
16. Do not hardcode secrets, tokens, connection strings, proxy credentials, or admin IDs.
17. Use explicit types, clear interfaces, and deterministic behavior.
18. Keep scraping, parsing, persistence, notification, export, and analytics responsibilities separate.
19. Do not implement captcha bypass, credential abuse, or anti-bot evasion.
20. Browser automation may be used only for rendering and extraction, respecting target-site rules and project policy.

---

## Required First Steps Before Any Edit

Before making code changes, inspect the relevant parts of the repository:

1. Project entry points
2. README and documentation
3. Existing AGENTS.md or project instructions
4. Database schema, migrations, models, or ORM mappings
5. Scraper, crawler, parser, or browser automation modules
6. Telegram bot modules
7. Configuration and environment loading
8. Scheduled jobs, workers, or background services
9. Existing tests and test utilities
10. Existing logging and error handling patterns

Then summarize:
- current behavior
- requested behavior
- files likely to change
- database impact
- data integrity risks
- test strategy

---

## Standard Response Format

After doing code work, respond using this structure:

### Plan
Short checklist of the implementation plan.

### Findings
What was discovered in the existing codebase.

### Edits
What changed and where.

### Code
Important snippets or patch summary. Do not paste huge files unless requested.

### Database Impact
Tables, columns, indexes, migrations, or query behavior affected.

### Tests/Verification
Commands run or commands the user should run.
Include expected results.

### Risks & Alternatives
Remaining risks, tradeoffs, and safer alternatives if relevant.

---

## Architecture Principles

Design the system as a clean pipeline:

```text
Source Website
  -> Fetcher / Browser Renderer
  -> Parser / Extractor
  -> Normalizer
  -> Validator
  -> SQL Server Persistence
  -> Change Detection
  -> Notification / Export / Analytics

Keep each responsibility separate.

Do not mix:

scraping logic with Telegram formatting
parsing logic with database schema decisions
Telegram access control with crawler execution
Excel export logic with live scraping
analytics logic with operational write-path persistence
notification formatting with change detection logic
Recommended Project Areas

Use the existing project structure first.

If the project has no clear structure, prefer a structure similar to:

src/
  config/
  logging/
  sources/
  scraping/
  parsing/
  normalization/
  persistence/
  change_detection/
  notifications/
  telegram_bot/
  exports/
  analytics/
  jobs/
  tests/

Do not restructure the project unless the user explicitly asks.

Core Domain Concepts

Use consistent domain language in code and database design.

Listing

A property advertisement from a source website.

A listing should have a stable identity based on:

source
source listing ID, when available
canonical URL
stable fingerprint/hash when the source does not expose an ID

Never rely only on title or price for listing identity.

Snapshot

A point-in-time capture of listing data.

Snapshots preserve listing history and enable future analysis.

Event

A detected state transition.

Common event types:

LISTING_NEW
PRICE_CHANGED
DETAILS_CHANGED
STATUS_CHANGED
LISTING_REMOVED
LISTING_SOLD
LISTING_RENTED
LISTING_REAPPEARED
Notification

A Telegram message produced from an event.

Notifications must be logged and deduplicated.

Export

A reproducible output, such as Excel export by neighborhood.

Exports should be based on stored database data, not live scraping results, unless the user explicitly requests live scraping.

SQL Server Data Storage Rules

SQL Server is the primary database.

The database must support both:

operational features
future analytics and data science

General rules:

Store raw extracted values and normalized values when possible.
Preserve historical values through snapshots or history tables.
Use UTC timestamps for system-generated times.
Store source-local published or updated times separately when available.
Use DECIMAL for prices and currency values.
Do not use floating point types for money.
Use numeric types for area, rooms, floor, year, and similar fields when normalization is reliable.
Store units explicitly when relevant.
Use nullable fields when source data may be missing.
Use controlled status values where possible.
Use unique constraints to prevent duplicate listings.
Add indexes for lookup, change detection, exports, and analytics queries.
Use parameterized queries only.
Use transactions for multi-step writes.
Migrations should include forward and rollback paths when supported by the project.
Avoid destructive schema changes without a data preservation plan.
Do not delete listing history unless the user explicitly requests it.

Recommended database concepts:

Sources
Listings
ListingSnapshots
ListingEvents
ListingPriceHistory
ListingStatusHistory
ListingMedia
Locations
Neighborhoods
CrawlRuns
CrawlErrors
NotificationLogs
TelegramUsers
TelegramRoles
TelegramAccessRules
ExportJobs

Do not create all tables blindly. Use this list as a design reference when implementing related features.

Listing Identity Rules

Prefer listing identity in this order:

Official listing ID from the source website
Canonical source URL
Stable hash from source + normalized URL
Stable hash from source + title + location + area + seller when no better identity exists

Never use only price, title, or description as the identity.

A listing identity must be stable across crawls.

Snapshot Rules

For every successfully extracted listing:

Normalize extracted fields.
Compute a content hash from meaningful fields.
Compare with the latest stored snapshot.
If unchanged, update crawl metadata only.
If changed, insert a new snapshot.
Create field-level events for important changes.
Never send duplicate notifications for the same event.

Important fields for snapshot comparison:

price
title
description
area
rooms
neighborhood
address/location
seller/agency
contact data, if legally and ethically stored
status
URL/canonical URL
media count
primary image, if useful
Change Detection Rules

Change detection must be deterministic and explainable.

New Listing

A listing is new when no existing listing identity matches the source listing.

Actions:

insert listing
insert initial snapshot
create LISTING_NEW event
send Telegram notification if enabled
Updated Listing

A listing is updated when meaningful normalized fields differ from the latest snapshot.

Actions:

insert new snapshot
calculate field-level diff
create specific event or events
send Telegram notification with previous and new values
Price Change

A price change must preserve:

previous price
new price
absolute difference
percentage difference when possible
detected timestamp
source
Removed or Unavailable Listing

A listing should not be marked removed after one temporary failure.

A listing may be considered removed when:

it is missing from multiple consecutive successful crawls
the source page returns 404 or 410
the source explicitly marks it unavailable
the source explicitly marks it sold, rented, or expired

If the status is inferred, store the reason and confidence when supported.

Sold or Rented Listing

If the source explicitly provides sold/rented status, store it as a status change.

If sold/rented is inferred, mark it as inferred and preserve the reason.

Telegram Bot Rules

Telegram is the operational interface for alerts and simple management.

The bot should support:

new listing notifications
price change notifications
detail change notifications
removed/sold/rented notifications
neighborhood-based Excel export requests
access management when implemented
clear error messages for failed commands

Notification messages should be concise but complete.

Include when available:

alert type
title
price
previous price and new price for price changes
area
rooms
neighborhood
source
listing URL
detected time
important changed fields

Never send the same event notification repeatedly.

Use a NotificationLogs-style mechanism or the existing project equivalent.

Admin-only features may include:

adding users
removing users
changing subscriptions
requesting broad exports
triggering crawls
changing source configuration
changing notification settings

Telegram bot tokens must be stored in environment variables or secret storage only.

Do not expose stack traces or secrets through Telegram messages.

Excel Export Rules

Exports must be deterministic, reproducible, and analysis-ready.

When exporting listings for a neighborhood:

Query SQL Server.
Do not scrape live during export unless explicitly requested.
Include active listings by default.
Allow future filtering by:
date range
source
status
price range
area range
rooms
seller type
Include generated timestamp and filters in metadata when possible.
Keep column names stable.
Do not hide missing data.
Use empty values consistently for missing fields.

Recommended export columns:

source
source_listing_id
title
price
currency
area
rooms
neighborhood
address
status
url
seller_type
seller_name
first_seen_at
last_seen_at
last_changed_at
published_at_source
updated_at_source
price_per_square_meter
description

Exports should be generated from stored database data, not from temporary in-memory scrape results.

Scraper and Browser Automation Rules

Use the existing scraping approach first.

If browser automation is used:

Keep browser/session setup isolated from parsing logic.
Do not hardcode browser fingerprints, credentials, proxy settings, or session values.
Do not implement captcha bypass or anti-bot evasion.
Add rate limiting, retry/backoff, and timeout controls.
Treat network failures as temporary unless proven otherwise.
Log crawl run ID, source, URL, status, duration, and errors.
Store raw HTML/JSON only if the project already supports it or the user requests it.
Parsers should be deterministic and testable with saved fixtures.
Do not make parsing dependent on current time unless necessary.
Avoid abusive request patterns.

Preferred flow:

fetch_page()
  -> parse_listing()
  -> normalize_listing()
  -> validate_listing()
  -> save_listing()
  -> detect_changes()
  -> notify_if_needed()

A parser should return a structured object and should not write directly to the database.

Data Science and Analytics Rules

This project is intended for future data science.

Preserve analytical value.

Data quality rules:

Keep raw and normalized values.
Track missing values explicitly.
Normalize neighborhood names consistently.
Normalize price, currency, area, rooms, and property type.
Avoid irreversible transformations.
Preserve timestamps needed for time-series analysis.
Avoid data leakage when building ML features.
Document assumptions used in derived fields.

Useful derived fields may include:

price per square meter
days on market
price change percentage
number of price changes
listing age
source reliability
neighborhood trend features
time since last price change
price compared to neighborhood median

Derived fields should be reproducible from stored data.

When adding predictive models:

Use time-aware train/validation/test splits when appropriate.
Do not train on future data relative to prediction time.
Log feature set, dataset version, parameters, and metrics.
Do not fabricate metrics.
Keep inference separate from training.
Store model outputs with model version and timestamp.
Testing Rules

Always add or update tests for behavior changes when feasible.

Required test areas:

parser tests with fixed HTML/JSON fixtures
normalization tests
listing identity/fingerprint tests
change detection tests
database persistence tests
idempotency tests
notification formatting tests
notification deduplication tests
Excel export tests
Telegram access-control tests where applicable

Test principles:

Prefer fast unit tests.
Mock only external boundaries:
network
filesystem
database
Telegram API
clock/time
Keep tests deterministic.
Use fixed timestamps.
Avoid live network calls in tests.
Test error paths and edge cases.
If fixing a bug, add a regression test.
Security Rules

Apply safe-by-default engineering.

Never commit or print:

Telegram bot token
SQL Server connection string
passwords
API keys
proxy credentials
session cookies
sensitive admin IDs

Use environment variables or the project’s existing secret configuration system.

Database security:

Use parameterized SQL.
Do not concatenate user input into SQL.
Validate export filters.
Limit broad export/admin operations to authorized users.
Avoid exposing internal IDs unnecessarily.

Telegram security:

Validate every command sender.
Enforce admin-only commands.
Log permission denied events when useful.
Avoid sending sensitive debug traces to users.
Use safe error messages.

Scraping security:

Treat scraped content as untrusted.
Do not execute untrusted code from scraped pages.
Escape or sanitize scraped content before rendering in HTML, Markdown, or Telegram messages.
Do not follow arbitrary redirects blindly for sensitive internal requests.
Avoid storing sensitive personal data unless required and allowed.
Logging and Observability

Use structured logs where possible.

Log at least:

crawl run started
crawl run finished
source
URL or listing ID
number of listings found
number of listings inserted
number of listings updated
number of unchanged listings
detected events
notification send result
export requested
export generated
errors with context

Do not log secrets.

Every scheduled run should be traceable by a run ID.

Error Handling Rules
Fail clearly.
Retry transient network errors with backoff.
Do not retry deterministic parser errors indefinitely.
Store crawl errors when useful.
Do not mark listings removed because of one failed crawl.
Partial failures should not corrupt existing data.
Use transactions around related writes.
Keep user-facing errors clear and safe.
Keep developer-facing logs detailed but secret-free.
Performance Rules

Optimize only after understanding the bottleneck.

Likely hotspots:

duplicate detection
latest snapshot lookup
neighborhood export queries
bulk inserts
change detection over many listings
Telegram notification bursts

Prefer:

bulk operations where supported
proper indexes
batched database queries
pagination/chunking for exports
avoiding N+1 database queries
notification queueing when needed
Role: Development Agent

Focus: high-quality feature implementation.

Rules:

Follow existing architecture and style.
Keep responsibilities separated.
Make data contracts explicit.
Avoid large rewrites unless requested.
Preserve existing behavior unless change is requested.
Add or update tests for behavior changes.
Explain database and data-quality implications.

Deliverables:

implementation
tests
verification commands
concise rationale
risks/tradeoffs
Role: Scraper Agent

Focus: reliable extraction and ingestion.

Rules:

Inspect existing source adapters before adding a new one.
Keep fetch, parse, normalize, and persist separate.
Use fixtures for parser tests.
Do not rely on brittle selectors without fallback.
Add source-specific parsing in isolated modules.
Validate extracted values before persistence.
Do not mark removed/sold from temporary network errors.
Respect target-site rules and avoid abusive request patterns.

Deliverables:

source adapter/parser changes
extraction data contract
parser fixtures/tests
crawl error behavior
rate limit/retry notes
Role: Database Agent

Focus: SQL Server schema, migrations, queries, and data integrity.

Rules:

Preserve backward compatibility unless migration is requested.
Prefer append-only history for snapshots/events.
Add constraints for identity and deduplication.
Add indexes for new query patterns.
Avoid over-indexing.
Use explicit transactions for multi-step writes.
Provide migration up/down when project convention supports it.
Use parameterized queries.
Avoid N+1 queries.
Explain analytics impact of schema decisions.

Deliverables:

schema/query changes
migration plan
indexes/constraints
data backfill plan if needed
performance notes
Role: Telegram Bot Agent

Focus: notifications, commands, access control, and user experience.

Rules:

Keep Telegram formatting separate from event detection.
Validate command permissions.
Do not leak secrets or stack traces.
Deduplicate notifications.
Make messages short, clear, and useful.
Support Persian output if the existing bot UX is Persian.
Log sends and failures.
Handle Telegram API failures gracefully.

Deliverables:

command handlers
notification templates
access-control logic
notification tests
failure behavior
Role: Data Science Agent

Focus: analysis-ready data, features, and future ML.

Rules:

Preserve raw and normalized fields.
Avoid data leakage.
Make derived fields reproducible.
Use time-aware splits for valuation models where appropriate.
Log dataset versions and model metrics.
Do not fabricate metrics.
Prefer explainable features for early versions.
Keep analytics code separate from operational scraping code.

Deliverables:

feature definitions
analytics queries
dataset generation code
evaluation plan
metrics hooks
assumptions and limitations
Role: Testing Agent

Focus: reliability, edge cases, and regression prevention.

Rules:

Add tests for happy path, edge cases, and error paths.
Prefer unit tests for parsers, normalizers, and change detection.
Add integration tests only where necessary.
Mock only external boundaries.
Use deterministic fixtures and fixed time.
Ensure tests fail before the fix and pass after when fixing bugs.

Deliverables:

test plan
test code
fixtures if needed
commands and expected results
Role: Security Agent

Focus: threat modeling and safe-by-default implementation.

Rules:

Identify entry points:
scraped websites
Telegram commands
export filters
scheduled jobs
database writes
Identify assets:
database
Telegram token
user/admin IDs
listing history
exported files
Check for:
SQL injection
command injection
secrets leakage
unsafe file paths
unauthorized Telegram commands
unsafe rendering of scraped content
excessive data exposure in exports
Validate at boundaries and sanitize at sinks.
Use least privilege.

Deliverables:

findings with severity
secure patch
security tests where feasible
remaining risks
Role: DevOps Agent

Focus: reliable local, development, and production operation.

Rules:

Do not hardcode environment-specific settings.
Use environment variables or existing config system.
Document required variables.
Keep scheduled jobs observable.
Add health checks if the project already has service infrastructure.
Avoid destructive deployment or migration behavior.
Ensure logs are useful without exposing secrets.

Deliverables:

config updates
deployment notes
environment variable list
operational verification steps
Configuration Rules

Prefer environment variables for:

SQL Server connection string
Telegram bot token
Telegram admin IDs
source URLs
browser settings
proxy settings
crawl interval
export directory
log level

Do not introduce new configuration mechanisms if the project already has one.

Document any new required variable.

Migration Rules

Before changing schema:

Inspect current schema and migrations.
Identify backward compatibility impact.
Preserve existing data.
Add indexes only for real query patterns.
Provide rollback if project convention supports it.
Explain data backfill needs.

Avoid:

dropping columns without migration plan
renaming columns without compatibility plan
changing types without checking existing data
deleting history tables
destructive cleanup by default
Notification Deduplication Rules

Each notification should be linked to a specific event.

Deduplication key may include:

event ID
listing ID
event type
previous value hash
new value hash
recipient ID

A failed notification may be retried.

A successfully sent notification should not be sent again unless the user explicitly requests resending.

Neighborhood Handling Rules

Neighborhood names should be normalized carefully.

Store:

raw neighborhood from source
normalized neighborhood
city/region when available
source-specific location text

Avoid losing raw location text.

Neighborhood normalization should be testable and reversible when possible.

Price Handling Rules

Always preserve:

raw price text
normalized numeric price
currency
price type when available

Examples of price type:

total price
rent
deposit
mortgage
negotiable
unknown

For price changes, store previous and new values.

Do not assume currency if the source is ambiguous.

Status Handling Rules

Use explicit status values where possible.

Recommended statuses:

active
updated
unavailable
removed
sold
rented
expired
unknown

If status is inferred, store that it was inferred and why.

Final Rule

This is not only a scraper project.

Treat it as a real-estate data platform.

Every implementation should help the project become more reliable, more analyzable, and easier to extend over time.
