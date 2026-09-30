# gitcrawl Legal Gates Checklist

Spec: `design/spec.md` FR-011. Findings: `findings/05-research-gaps.md` A10.
Review cadence: re-verify quarterly (robots.txt, API version pin, token owners, trademark naming) before any crawl/serve/scrape step.

## 1. API-only default

- [ ] All discovery uses `api.github.com` REST/GraphQL only (API-only is the default; no HTML fallback).
- [ ] HTML scraping or HTML fallback stays **disabled** unless a written legal review approves it.
- [ ] Before any HTML fallback is ever enabled: complete section 2 (`robots.txt` check record) and attach the review outcome.

## 2. `robots.txt` check record (required before any HTML fallback)

Record the actual result of `curl -s https://github.com/robots.txt` for the `/search` path on the run date.
If the network check cannot run, write `not run` in the Result column and keep HTML fallback disabled.

| Date | Command | Result | `/search` directive | Decision |
|---|---|---|---|---|
| 2026-09-30 | `curl -s https://github.com/robots.txt` | fetched, HTTP 200 | `User-agent: *` section disallows `/search` (`Disallow: /search$`, `Disallow: /search/advanced`; query URLs also disallowed via `Disallow: /*?q=*`) | HTML fallback stays disabled |

- [ ] Each future run date has its own row (or `not run`).
- [ ] Any `Disallow` covering `/search` keeps the HTML fallback disabled.

## 3. Token ownership and handling

Token-ownership log **template** (log fingerprints only — **never the raw token**):

| Owner (account) | Token type | Scope | Created | Purpose | Fingerprint sha256[:12] | Retired |
|---|---|---|---|---|---|---|
| _example-owner_ | fine-grained PAT | `metadata:read` (public repos) | 2026-09-30 | dev / golden-org discovery | _12-hex-fingerprint_ | — |

Rules:

- [ ] Tokens load from environment only: `GITHUB_TOKEN` (single) or `GITHUB_TOKENS` (comma-separated list). Never hardcoded, never in config, bundles, or run artifacts.
- [ ] Logs and reports use fingerprint-only (`sha256(token).hexdigest()[:12]` via `src/lib/gh_client.py:token_fingerprint`); raw tokens are never logged, printed, or committed.
- [ ] No token sharing across owners to evade rate limits (GitHub ToS §H: tokens may not be shared to exceed rate limitations; doing so risks suspension). Rotation is allowed only across accounts we own and consent to, with the token hash logged.
- [ ] Least privilege: fine-grained PAT with `metadata:read` for public data (no `repo` scope). Never embed an OAuth `client_secret`; GitHub App installs are deferred to scale-up.
- [ ] On token incident (any `401`, bad-credential `403` lockout, or SAML SSO `partial-results`): loud failure and runbook — never silently serve filtered results; back off, then stop.
- [ ] Secret scanning: `.env` stays gitignored; no secrets in commits or run bundles.

## 4. Resale, spam, and PII

- [ ] Resale/high-throughput trigger: any paid access, resale, or high-throughput serving pauses the feature and starts a ToS §H subscription review before it proceeds.
- [ ] No resale or sale of PII; no bulk outreach, recruiter use, or spam.
- [ ] Forbid collecting `user:email`; store only allowlisted fields; no commit author/committer emails anywhere.
- [ ] No spam creation (automated content, fake engagement, bulk messaging).

## 5. Deletion, GDPR/CCPA purge window, PII minimization

- [ ] `GET /repos/{o}/{r}` returning `404` (deleted or privatized) creates a tombstone in-run (`deleted_at` set, audit-log `repo.destroy`) and quarantines the row from serve.
- [ ] Tombstoned rows are hard-purged after the retention window (proposed 30 days; confirm with legal review before serve).
- [ ] Access/erase requests: upstream via GitHub (`privacy@github`), local stored rows via the operator runbook within the same window.
- [ ] PII minimization: no commit emails stored; only allowlisted repo/owner fields persisted; raw location strings kept only for geo resolution.

## 6. Trademark-safe naming

- [ ] Product name, UI copy, and metadata imply no GitHub endorsement or affiliation.
- [ ] No Invertocat/Octocat marks or GitHub logos (respect `GITHUB®`/`OCTOCAT®` per the Logo Policy and Brand Toolkit).
- [ ] `User-Agent` identifies the tool (`gitcrawl/…`) and links no GitHub branding.
- [ ] Naming/branding review gates any public UI ship.

## 7. Researcher/archivist carve-outs

- [ ] No researcher or archivist exception is claimed: continuous commercial crawling (public, non-personal) fits neither GitHub's Researcher nor Archivist terms, so the API-only default and all gates above apply without exception.
