# Signal2

Autonomous AI model-release intelligence for Discord. **24/7, GitHub Actions only, zero runtime dependencies, zero paid infrastructure.**

Signal2 polls public sources every five minutes, retrospectively re-scans a 48-hour window, clusters evidence referring to the same model, and publishes only release-grade events. Known Discord delivery failures remain in a durable outbox and are retried on subsequent runs.

## Operating model

Signal2 deliberately stays small:

- one Python stdlib runtime file (`signal2.py`);
- one Git-backed state file (`signal2_state.json`);
- one scheduled/manual workflow;
- one CI workflow and one unit-test file;
- no daemon, server, database, container, package manager, API subscription, LLM or local host.

The source stack is redundant by design:

| Tier | Sources | Purpose |
|---|---|---|
| Authoritative | OpenAI, Mistral, Qwen and Google/DeepMind feeds; Anthropic official sitemap; DeepSeek official news | primary vendor evidence |
| First-party artifacts | Hugging Face model repositories from selected official organizations | open-weight releases |
| Structured corroboration | models.dev provider-agnostic model catalog | cross-vendor release metadata |
| Availability corroboration | OpenRouter public model catalog | rapid closed-model discovery |
| Discovery only | Hacker News/Algolia | weak-signal safety net; never publishes alone |

xAI/Grok remains intentionally excluded, matching the current repository scope.

## Reliability semantics

The system is **at-least-once**, not mathematically exactly-once. This is intentional.

- A Discord event is marked delivered only after a successful webhook response.
- Discord is called with `wait=true`, so Signal2 asks the API to confirm message creation.
- Failed deliveries stay in `pending` and are retried; they are never silently marked seen.
- Bursts are queued rather than dropped. Scheduled polls send at most 30 pending events per run; the remainder stays durable.
- The poll looks back 48 hours, so a delayed or dropped GitHub scheduled run does not create a detection gap as long as the upstream source still exposes the item.
- Hugging Face pagination follows `Link: rel="next"` until the lookback cutoff rather than taking a fixed top-N sample.
- One source failure does not stop healthy collectors or outbox delivery, but the Actions run exits non-zero after state persistence so degradation is visible immediately.
- A tiny state heartbeat is written at most once every 30 days so the public repository retains activity and GitHub does not reach its documented 60-day scheduled-workflow inactivity cutoff.

A pathological crash after Discord accepted a message but before the updated Git state is pushed can still cause a duplicate on retry. Eliminating that last ambiguity requires a transactional external store and would violate the zero-cost/GitHub-only constraint. Signal2 chooses a rare duplicate over silent loss.

GitHub also documents that scheduled workflows can be delayed, and under sufficient load queued jobs can be dropped. The cron is therefore offset from the start of the hour (`2,7,12,...,57`) and completeness comes from catch-up windows rather than scheduler punctuality.

### Completeness boundary

No zero-cost polling system can prove literal universal completeness: an upstream vendor can publish no machine-readable signal, retract an item before the next poll, or make a public endpoint unavailable longer than the catch-up window. Signal2 therefore optimizes for **high recall with explicit degradation**, not an unverifiable “nothing can ever be missed” claim. Redundant authoritative/first-party/structured sources, 48-hour reconciliation, HF pagination and durable pending delivery remove the avoidable failure modes under Signal2's control.

## Deployment

1. Keep the repository **public**. Standard GitHub-hosted runners are free for public repositories.
2. In **Settings → Secrets and variables → Actions**, create repository secret `SIGNAL2_WEBHOOK` containing the Discord webhook URL.
3. Ensure GitHub Actions may write repository contents (`contents: write` is requested by the workflow) so `signal2_state.json` can be committed.
4. Remove the legacy `.github/workflows/veille.yml` if it exists. Do not run the legacy and new schedulers together.
5. Commit/push these files to `main`. `scripts/digest7.ps1 -Cloud` can only work after `.github/workflows/signal2.yml` exists on the default branch.
6. Before relying on the scheduled feed, run the 7-day digest once as described below.

No other secret is required. The workflow does not need a Hugging Face, OpenRouter, models.dev or Hacker News API key.

## First population: 7-day digest

### Recommended: entirely in GitHub

Open **Actions → Signal2 → Run workflow** and set:

- `mode`: `digest`
- `days`: `7`
- `publish`: **false** for the first validation run

Review the Actions log. If the result is correct, run the same workflow again with `publish: true`. Published digest events are persisted in `signal2_state.json`, so the normal poll will not repost them.

### PowerShell helper

Local dry-run, no Discord publication:

```powershell
./scripts/digest7.ps1
```

Publish locally (requires `SIGNAL2_WEBHOOK` in the current environment):

```powershell
./scripts/digest7.ps1 -Publish
```

Trigger the **cloud** workflow using GitHub CLI; execution itself still happens entirely on GitHub Actions:

```powershell
./scripts/digest7.ps1 -Cloud
./scripts/digest7.ps1 -Cloud -Publish
```

The helper is optional. Production never depends on the local machine. `-Cloud` performs a remote-workflow preflight and reports explicitly when `signal2.yml` has not yet been deployed to `main`.

If you already executed `./scripts/digest7.ps1 -Publish` locally and Discord confirmed delivery, **preserve that resulting `signal2_state.json` and commit it with the deployment**. Replacing it with the clean template would discard the delivered-event ledger and can cause duplicate publication.

## CLI

```bash
python3 signal2.py --poll                 # local/cloud dry-run, 48 h
python3 signal2.py --poll --publish       # publish + persist state
python3 signal2.py --digest 7             # 7-day dry-run
python3 signal2.py --digest 7 --publish   # initial population
```

`--publish` is the only mode that requires `SIGNAL2_WEBHOOK`.

## Discord presentation

Each event is a single compact embed containing:

- canonical vendor/model identity;
- event class (`MODEL RELEASE`, `OPEN WEIGHTS`, or `MODEL AVAILABILITY`);
- confidence (`Official`, `Confirmed`, `Corroborated`);
- UTC publication timestamp;
- up to six independent evidence links.

Mentions are disabled (`allowed_mentions.parse = []`) so upstream titles cannot trigger `@everyone`, roles or users.

## Tests

```bash
python3 -m compileall -q signal2.py tests
python3 -m unittest discover -s tests -v
```

CI runs these checks on code changes but ignores state-only commits generated by Signal2.

## State compatibility

Version 3 state is intentionally minimal:

```json
{
  "version": 3,
  "pending": {},
  "delivered": {},
  "meta": {}
}
```

The previous `seen` hash format is not semantically compatible with evidence-clustered event IDs. On a fresh migration, use the supplied clean v3 state and immediately run/publish the 7-day digest to establish the new baseline. If the new implementation has already published locally, keep that newly generated v3 state instead of restoring the clean template.
