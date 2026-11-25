# Source registry

`config/sources.yaml` is code-reviewed configuration, validated on load by
`models/source.py::SourceRegistry.validate_registry`. A malformed registry is a
startup error, not a warning.

## Policy

- 24 sources, all seven categories covered, 22 Tier A + 2 Tier B.
- RSS/Atom, the GitHub REST releases API and the Hugging Face Hub API only. No
  unrestricted scraper, no JavaScript rendering in V1.
- **No Tier C source is enabled.** By policy a Tier C-only discovery is capped at
  0.49 confidence (`processing/confidence.py::CAP_TIER_C_ONLY`) and can never pass
  the gate, so subscribing to one would burn quota producing rows that are always
  discarded. The Tier C gate is implemented and unit-tested
  (`tests/integration/test_pipeline.py::test_tier_c_only_discovery_is_discarded`).
  Add a C source only together with A/B corroboration for the same topics.

## Verification of every endpoint

Each URL was probed on **2026-11-23** with the project's own HTTP path: HTTP status,
feedparse result, entry count, and JSON detection. Only endpoints that returned a
parsable body were kept.

| Source ID | Category | Tier | Method | HTTP | Entries at probe | Notes |
|---|---|---|---|---|---|---|
| `openai_news` | ai_models | A | rss | 200 | 657 | capped by `max_items: 30` |
| `google_ai_blog` | ai_models | A | rss | 200 | 20 | |
| `deepmind_blog` | ai_models | A | rss | 200 | 100 | |
| `hf_blog` | ai_models | A | rss | 200 | 916 | publisher-side announcements |
| `microsoft_research` | ai_models | A | rss | 200 | 10 | research lab feed |
| `github_blog` | ai_coding_tools | A | rss | 200 | 10 | |
| `github_copilot_changelog` | ai_coding_tools | A | rss | 200 | 10 | canonical Copilot changelog |
| `vscode_updates` | ai_coding_tools | A | rss | 200 | 50 | AI features ship here first |
| `replit_blog` | ai_coding_tools | A | rss | 200 | 269 | |
| `crewai_releases` | ai_agents | A | github_releases | 200 | JSON | release artefacts |
| `autogen_releases` | ai_agents | A | github_releases | 200 | JSON | |
| `openai_agents_releases` | ai_agents | A | github_releases | 200 | JSON | |
| `replicate_blog` | image_video_ai | A | rss | 200 | 125 | |
| `fal_blog` | image_video_ai | A | rss | 200 | 15 | |
| `comfyui_releases` | image_video_ai | A | github_releases | 200 | JSON | local-workflow ecosystem |
| `hf_image_models` | image_video_ai | A | huggingface_models | 200 | JSON | substance-gated, see below |
| `elevenlabs_blog` | audio_speech_ai | A | rss | 200 | 50 | |
| `suno_blog` | audio_speech_ai | A | rss | 200 | 50 | generative audio |
| `whisper_releases` | audio_speech_ai | A | github_releases | 200 | JSON | speech recognition reference |
| `linear_changelog` | productivity_tools | A | rss | 200 | 49 | continuous AI features |
| `slack_engineering` | productivity_tools | A | rss | 200 | 8 | |
| `zapier_blog` | productivity_tools | A | rss | 200 | 25 | automation platform |
| `techcrunch_ai` | ai_industry_updates | B | rss | 200 | 20 | reporting, not primary evidence |
| `verge_ai` | ai_industry_updates | B | rss | 200 | 10 | reporting, not primary evidence |

## Candidates rejected at probe time

Documented so nobody re-adds a dead URL:

| Candidate | Result | Reason |
|---|---|---|
| `https://ai.meta.com/blog/rss/` | 404 | no feed at that path |
| `https://mistral.ai/news/feed.xml` | 404 | no feed |
| `https://cursor.com/changelog/rss` | 404 | changelog is a rendered page |
| `https://windsurf.com/changelog/rss` | 200, 0 entries | HTML, not a feed |
| `https://blog.langchain.dev/rss/` | 200, 0 entries | not a feed |
| `https://runwayml.com/blog/rss.xml` | 404 | |
| `https://stability.ai/news?format=rss` | 200, 0 entries | JS-driven page |
| `https://www.assemblyai.com/blog/rss.xml` | 200, 0 entries | not a feed |
| `https://www.notion.com/blog/rss.xml` | 404 | |
| `https://venturebeat.com/category/ai/feed/` | 429 | rate-limited at probe; would need backoff and may stay hostile |
| `https://www.anthropic.com/news/rss.xml` | 404 | Anthropic publishes no public feed |
| `https://groq.com/blog/rss.xml` | 404 | |
| `https://deepgram.com/blog/rss.xml` | 404 | |
| `https://www.heygen.com/blog/rss.xml` | 404 | |
| `https://lumalabs.ai/news` | 200, 0 entries | JS page |

Candidates that probed fine but are **not** in V1 (deliberate, to stay inside
15-25 sources): `nvidia_ai_blog`, `together_blog`, `arstechnica`,
`hf_models_new`, `hf_audio_models`. Add them by editing `sources.yaml` and running
`ai-radar sources --validate`.

## The Hugging Face substance gate

The Hub's `sort=createdAt` listing is dominated by throwaway uploads: during the
live probe, the newest ten `text-to-image` repositories were individual test
uploads with zero likes and zero downloads. Alerting on those would make the
product useless.

`hf_image_models` therefore requires `min_likes: 3` and `min_downloads: 200` before
an entry is considered, and the collector reports how many rows it filtered. The
trade-off is stated plainly: a brand-new model that has not accumulated usage yet
is missed. Relax the gate in `config/sources.yaml` if you would rather see the
long tail.

## Conditional requests

RSS collection sends `If-None-Match` / `If-Modified-Since` from the stored
`etag` / `last_modified`. A `304` is treated as a successful, unchanged poll: the
checkpoint advances, no entries are re-processed. Validators are stored per source
in the `sources` table.

## Health

Every collection attempt updates `sources.health_status`:
`healthy` on success, `degraded` after one failure, `failing` after three
consecutive failures. `ai-radar maintenance` reports unhealthy sources and alerts
when three or more are degraded.

## Adding a source

1. Probe it first — an endpoint that returns HTML with 200 is not a feed.
2. Add the entry with a stable `id` (`^[a-z0-9]+(_[a-z0-9]+)*$`), real tier, and a
   `notes` line recording why it belongs.
3. Run `ai-radar sources --validate` (must print `"problems": []`).
4. Run `ai-radar collect --source <id>` to see parsed entries before enabling it.
