# Public People workflow — content engine

## Recruiter workflow
1. Open a JD, confirm professional criteria, generate strategies.
2. Review the shared public MUST clauses and each strategy angle. Save edits and approve the new config. Old configs need “Soạn truy vấn Exa” and review before searching.
3. Select location and strategies, inspect the exact query, search People.
4. Select up to 20 sources and fetch available content. Inspect source status, cached/live metadata, truncation warnings and company/social context. Text is not guaranteed to be 100% of LinkedIn.
5. Confirm the budget-compatible group. An unavailable source needs an explicit excerpt opt-in or a user-provided PDF. PDF attachment does not ingest a candidate or contact anyone.
6. Start one AI batch; only validated final results are displayed. Changing TopK reads saved results.
7. Review criterion quotes, coverage and UNKNOWNs. Label sources yourself; labels are not created by the system.

## Evidence and scoring
Original fetched text is retained. Long paragraphs are divided losslessly into stable chunks up to 3,000 characters. Repeated paragraphs are deduplicated in the evidence catalog. Company/social context remains in the input but cannot be cited as professional evidence. Classification is conservative and does not prove the remaining text is accurate.
AI supplies criterion statuses, source-local IDs, exact quotes and verification questions. Backend rejects wrong IDs, invalid quotes and context-only citations. It does not mathematically prove the quote entails the criterion; recruiter review remains necessary.
Score uses the existing approved weights and full denominator. UNKNOWN earns no confirmed credit and stays distinct from NOT_MET. Coverage and unknown MUST counts explain missing data. A score is not a hiring probability.

## Recovery and cost
Each step has a durable ID and immutable snapshot. Cached content is reused for 24 hours. Refresh is explicit. Retry keeps available sources and runs only unresolved URLs; network/429/5xx attempts are capped at three. HTTP 200 with failed URL statuses is not successful extraction.
Provider-reported cost is recorded; unknown costs stay unknown. Assessment usage reports tokens and timing, not an invented price. No automatic provider fallback or invisible multi-batch splitting.
A failed or interrupted assessment preserves its inputs. After three attempts inspect exported diagnostics before changing input/model. Export uses an allowlist and omits keys, runtime configuration and private job source notes; it still contains professional profile data and should only be shared with authorized reviewers.

## Maintenance
Run the existing start-pilot launcher on localhost port 9652. Check maintenance readiness against http://127.0.0.1:9652. Wait until idle, take a PostgreSQL custom-format backup, restore it to a separate database and compare source/config/history counts before upgrades. Alembic migration 0007 adds content fetches and public-source review labels; old assessments remain readable.
Keep runtime and backups outside Git. Do not deploy this localhost build online by just changing the bind address; online access controls require a separate deployment plan.

## Validation boundaries
The benchmark endpoint reports precision only among human-labeled returned sources, and evidence errors. It cannot infer recall over the entire Internet or certify the policy from zero labels. Five functional JD runs demonstrate operation, not recruitment accuracy. A full-versus-excerpt comparison holds JD, criteria, provider, prompt and scorer constant but remains exploratory and subject to model variation.

