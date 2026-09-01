# Signal2

![cycle](https://github.com/FeelTheFonk/signal2/actions/workflows/veille.yml/badge.svg)

Autonomous 24/7 watch for **AI model releases**. Runs entirely on GitHub
Actions — every 15 minutes, forever, for free — and publishes clean,
structured alerts to a Discord channel.

**Zero dependencies. Zero API keys. Zero cost.**

## Detection — four redundant layers

| Layer | Sources | Signal |
|---|---|---|
| Official | OpenAI, Mistral, Qwen, Google DeepMind newsfeeds + Anthropic, DeepSeek newsrooms | model releases & vendor announcements |
| Open weights | Hugging Face — 11 organizations (DeepSeek, Qwen, Z.ai, Moonshot, Meta, Mistral, OpenAI, BFL, Google, NVIDIA, Microsoft) | new public checkpoints |
| Code | GitHub release events — 9 organizations | tags & releases |
| Community | Hacker News (Algolia API), score-gated | strong resonance — safety net for anything the above misses |

## Nothing is ever missed, nothing is ever duplicated

- Every item is fingerprinted; state is committed back to the repo after each
  cycle, so consecutive runs never re-post.
- A 7-day catch-up window means that even after an outage, everything that
  happened in the meantime is recovered on the next cycle — silently skipping
  anything already reported.
- Rate-limit-proof: ETag-cached GitHub calls, conditional requests, per-domain
  backoff, per-cycle fetch budgets.

## Design

- Single file, Python stdlib only — no supply chain, nothing to install.
- Multi-vendor dedup (identical news submitted several times collapses to the
  highest-signal item).
- Relevance filter tuned to model releases: product names × release verbs,
  open-weights phrasing — vendor marketing noise is dropped.
- Publication format: one embed per item, vendor-colored, consistent fields,
  releases always reported first.

## Local usage (development only)

```bash
python signal2.py --test        # dry run, nothing sent
python signal2.py --poll        # one live cycle
python signal2.py --digest 7    # retrospective digest
python signal2.py --launch-msg  # presentation message
```

Do not run `--loop` locally while Actions is active: state would diverge and
cause duplicates. The repository is the single source of truth.

## Files

- `signal2.py` — the entire system
- `signal2_state.json` — dedup state, committed by each run
- `signal2_config.json` — local config (gitignored; webhook lives in the
  `SIGNAL2_WEBHOOK` repository secret)
