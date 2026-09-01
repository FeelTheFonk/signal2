import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

import signal2

UTC = timezone.utc


class Signal2CoreTests(unittest.TestCase):
    def test_parse_datetime_accepts_iso_and_http_dates(self):
        self.assertEqual(signal2.parse_datetime("2026-08-31T12:34:56Z").tzinfo, UTC)
        self.assertEqual(signal2.parse_datetime("Mon, 31 Aug 2026 12:34:56 GMT").tzinfo, UTC)

    def test_parse_link_next_extracts_cursor_url(self):
        header = '<https://huggingface.co/api/models?cursor=abc>; rel="next", <x>; rel="prev"'
        self.assertEqual(
            signal2.parse_link_next(header),
            "https://huggingface.co/api/models?cursor=abc",
        )

    def test_release_filter_requires_release_semantics_for_general_feed(self):
        self.assertTrue(signal2.looks_like_release("Introducing GPT-6"))
        self.assertTrue(signal2.looks_like_release("Claude Opus 6 is now available"))
        self.assertFalse(signal2.looks_like_release("How we improved datacenter cooling"))

    def test_vendor_detection_excludes_xai(self):
        self.assertIsNone(signal2.detect_vendor("Grok 5 released by xAI"))
        self.assertEqual(signal2.detect_vendor("Introducing Claude Opus 6"), "Anthropic")


    def test_extract_model_id_stops_before_sentence_prose(self):
        self.assertEqual(signal2.extract_model_id("Claude Opus 6 is now available", "Anthropic"), "Claude Opus 6")
        self.assertEqual(signal2.extract_model_id("Introducing GPT-6.1 Mini today", "OpenAI"), "GPT-6.1 Mini")

    def test_candidate_key_is_stable_across_secondary_provider_variants(self):
        a = signal2.make_candidate(
            source="models.dev",
            vendor="Anthropic",
            title="Claude Opus 6",
            url="https://models.dev/anthropic/claude-opus-6",
            date=datetime(2026, 8, 31, tzinfo=UTC),
            model_id="anthropic/claude-opus-6",
            trust=85,
        )
        b = signal2.make_candidate(
            source="openrouter",
            vendor="Anthropic",
            title="Anthropic: Claude Opus 6 (free)",
            url="https://openrouter.ai/anthropic/claude-opus-6:free",
            date=datetime(2026, 8, 31, tzinfo=UTC),
            model_id="anthropic/claude-opus-6:free",
            trust=65,
        )
        self.assertEqual(signal2.canonical_model_key(a), signal2.canonical_model_key(b))

    def test_merge_candidates_aggregates_evidence_for_same_model(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        candidates = [
            signal2.make_candidate("models.dev", "OpenAI", "GPT-6", "https://models.dev/gpt6", now, "openai/gpt-6", 85),
            signal2.make_candidate("openrouter", "OpenAI", "OpenAI: GPT-6", "https://openrouter.ai/gpt6", now, "openai/gpt-6", 65),
        ]
        events = signal2.merge_candidates(candidates)
        self.assertEqual(len(events), 1)
        self.assertEqual({e["source"] for e in events[0]["evidence"]}, {"models.dev", "openrouter"})
        self.assertEqual(events[0]["confidence"], "confirmed")

    def test_publish_policy_rejects_hn_only_but_accepts_authoritative(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        hn = signal2.merge_candidates([
            signal2.make_candidate("hn", "OpenAI", "GPT-6 launched", "https://news.ycombinator.com/item?id=1", now, None, 25)
        ])[0]
        official = signal2.merge_candidates([
            signal2.make_candidate("official", "OpenAI", "Introducing GPT-6", "https://openai.com/gpt-6", now, None, 100)
        ])[0]
        self.assertFalse(signal2.should_publish(hn))
        self.assertTrue(signal2.should_publish(official))


    def test_cloud_helper_preflights_remote_workflow_before_dispatch(self):
        helper = (Path(__file__).resolve().parents[1] / "scripts" / "digest7.ps1").read_text(encoding="utf-8")
        self.assertIn("gh workflow view signal2.yml", helper)
        self.assertIn("not deployed on GitHub main", helper)

    def test_run_status_surfaces_source_or_delivery_degradation(self):
        self.assertEqual(signal2.run_status(delivery_failures=0, source_errors=[]), 0)
        self.assertEqual(signal2.run_status(delivery_failures=1, source_errors=[]), 1)
        self.assertEqual(signal2.run_status(delivery_failures=0, source_errors=["models.dev: HTTP 500"]), 1)

    def test_state_keeps_failed_delivery_pending(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        event = signal2.merge_candidates([
            signal2.make_candidate("official", "OpenAI", "Introducing GPT-6", "https://openai.com/gpt-6", now, "openai/gpt-6", 100)
        ])[0]
        state = signal2.new_state()
        signal2.enqueue_events(state, [event])

        class FailingSender:
            def send(self, _event):
                return False, None, "HTTP 503"

        result = signal2.deliver_outbox(state, FailingSender(), limit=10, sleep_fn=lambda _: None)
        self.assertEqual(result["delivered"], 0)
        self.assertIn(event["key"], state["pending"])
        self.assertNotIn(event["key"], state["delivered"])
        self.assertEqual(state["pending"][event["key"]]["attempts"], 1)

    def test_state_marks_only_confirmed_delivery_as_delivered(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        event = signal2.merge_candidates([
            signal2.make_candidate("official", "OpenAI", "Introducing GPT-6", "https://openai.com/gpt-6", now, "openai/gpt-6", 100)
        ])[0]
        state = signal2.new_state()
        signal2.enqueue_events(state, [event])

        class SuccessfulSender:
            def send(self, _event):
                return True, "123456", None

        result = signal2.deliver_outbox(state, SuccessfulSender(), limit=10, sleep_fn=lambda _: None)
        self.assertEqual(result["delivered"], 1)
        self.assertNotIn(event["key"], state["pending"])
        self.assertEqual(state["delivered"][event["key"]]["message_id"], "123456")

    def test_enqueue_is_idempotent(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        event = signal2.merge_candidates([
            signal2.make_candidate("models.dev", "Google", "Gemini 4 Pro", "https://models.dev/gemini4", now, "google/gemini-4-pro", 85)
        ])[0]
        state = signal2.new_state()
        self.assertEqual(signal2.enqueue_events(state, [event]), 1)
        self.assertEqual(signal2.enqueue_events(state, [event]), 0)
        self.assertEqual(len(state["pending"]), 1)


    def test_monthly_heartbeat_changes_state_only_when_due(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)
        state = signal2.new_state()
        self.assertTrue(signal2.touch_heartbeat(state, now=now, interval_days=30))
        self.assertFalse(signal2.touch_heartbeat(state, now=now + timedelta(days=29), interval_days=30))
        self.assertTrue(signal2.touch_heartbeat(state, now=now + timedelta(days=30), interval_days=30))

    def test_prune_delivered_keeps_recent_and_removes_old(self):
        state = signal2.new_state()
        now = datetime(2026, 9, 1, tzinfo=UTC)
        state["delivered"] = {
            "old": {"at": (now - timedelta(days=200)).isoformat(), "message_id": "1", "title": "old"},
            "new": {"at": (now - timedelta(days=2)).isoformat(), "message_id": "2", "title": "new"},
        }
        signal2.prune_delivered(state, now=now, days=120)
        self.assertNotIn("old", state["delivered"])
        self.assertIn("new", state["delivered"])

    def test_modelsdev_parser_accepts_direct_canonical_mapping(self):
        payload = {
            "anthropic/claude-opus-5": {
                "name": "Claude Opus 5",
                "release_date": "2026-07-24",
                "open_weights": False,
            }
        }
        rows = signal2.parse_modelsdev(payload)
        self.assertEqual(rows[0]["model_id"], "anthropic/claude-opus-5")
        self.assertEqual(rows[0]["vendor"], "Anthropic")

    def test_modelsdev_parser_accepts_provider_agnostic_list(self):
        payload = {
            "models": {
                "anthropic": {
                    "claude-opus-6": {
                        "name": "Claude Opus 6",
                        "release_date": "2026-08-31",
                        "open_weights": False,
                    }
                }
            }
        }
        items = signal2.parse_modelsdev(payload)
        self.assertEqual(items[0]["model_id"], "anthropic/claude-opus-6")
        self.assertEqual(items[0]["vendor"], "Anthropic")


    def test_modelsdev_provider_catalog_preserves_creator_from_qualified_model_id(self):
        payload = {
            "openrouter": {
                "models": {
                    "anthropic/claude-opus-6": {
                        "name": "Claude Opus 6",
                        "release_date": "2026-08-31",
                    }
                }
            }
        }
        items = signal2.parse_modelsdev(payload)
        self.assertEqual(items[0]["model_id"], "anthropic/claude-opus-6")
        self.assertEqual(items[0]["vendor"], "Anthropic")

    def test_modelsdev_skips_unattributable_aggregator_alias(self):
        payload = {
            "openrouter": {
                "models": {
                    "mystery-model": {
                        "name": "Mystery Model",
                        "release_date": "2026-08-31",
                    }
                }
            }
        }
        self.assertEqual(signal2.parse_modelsdev(payload), [])

    def test_modelsdev_parser_accepts_flat_data_list(self):
        payload = {"data": [{"id": "google/gemini-4-pro", "name": "Gemini 4 Pro", "release_date": "2026-08-30"}]}
        items = signal2.parse_modelsdev(payload)
        self.assertEqual(items[0]["model_id"], "google/gemini-4-pro")

    def test_rss_parser_handles_rss_and_atom(self):
        rss = b'''<?xml version="1.0"?><rss><channel><item><title>Introducing GPT-6</title><link>https://x/1</link><pubDate>Mon, 31 Aug 2026 12:00:00 GMT</pubDate></item></channel></rss>'''
        atom = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Qwen4 released</title><link href="https://x/2"/><updated>2026-08-31T12:00:00Z</updated></entry></feed>'''
        self.assertEqual(signal2.parse_feed(rss)[0]["url"], "https://x/1")
        self.assertEqual(signal2.parse_feed(atom)[0]["url"], "https://x/2")

    def test_atomic_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "state.json")
            state = signal2.new_state()
            state["pending"]["x"] = {"event": {"key": "x"}, "attempts": 0, "last_error": None}
            signal2.save_state(path, state)
            self.assertEqual(signal2.load_state(path)["pending"]["x"]["event"]["key"], "x")

    def test_modelsdev_collector_uses_provider_agnostic_catalog(self):
        calls = []
        payload = {"data": [{"id": "anthropic/claude-opus-5", "name": "Claude Opus 5", "release_date": "2026-08-31", "open_weights": False}]}

        def fake_get(url, **_kwargs):
            calls.append(url)
            return payload, {}

        rows = signal2.collect_modelsdev(datetime(2026, 8, 30, tzinfo=UTC), http_get_json=fake_get)
        self.assertEqual(calls, ["https://models.dev/models.json"])
        self.assertEqual(rows[0]["model_id"], "anthropic/claude-opus-5")

    def test_openrouter_parser_uses_created_and_canonical_slug(self):
        payload = {"data": [{"id": "anthropic/claude-opus-6:free", "canonical_slug": "anthropic/claude-opus-6", "name": "Anthropic: Claude Opus 6", "created": 1788134400}]}
        items = signal2.parse_openrouter(payload)
        self.assertEqual(items[0]["model_id"], "anthropic/claude-opus-6")
        self.assertEqual(items[0]["vendor"], "Anthropic")

    def test_anthropic_sitemap_collector_keeps_recent_release_pages(self):
        xml = (
            b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b'<url><loc>https://www.anthropic.com/news/introducing-claude-opus-6</loc><lastmod>2026-08-31</lastmod></url>'
            b'<url><loc>https://www.anthropic.com/news/company-update</loc><lastmod>2026-08-31</lastmod></url>'
            b'<url><loc>https://www.anthropic.com/news/introducing-claude-sonnet-4</loc><lastmod>2026-07-01</lastmod></url>'
            b'</urlset>'
        )

        def fake_get(url, **_kwargs):
            self.assertEqual(url, "https://www.anthropic.com/sitemap.xml")
            return xml, {}

        rows = signal2.collect_anthropic(datetime(2026, 8, 30, tzinfo=UTC), http_get_bytes=fake_get)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["vendor"], "Anthropic")
        self.assertEqual(rows[0]["model_id"], "Claude Opus 6")
        self.assertEqual(rows[0]["url"], "https://www.anthropic.com/news/introducing-claude-opus-6")

    def test_anthropic_family_detection_includes_fable_and_mythos(self):
        self.assertEqual(signal2.detect_vendor("Introducing Fable 6"), "Anthropic")
        self.assertTrue(signal2.looks_like_release("Introducing Mythos 6"))
        self.assertFalse(signal2.looks_like_release("Improving Fable 5 biology safeguards"))

    def test_deepseek_news_parser_derives_date_from_url(self):
        html = b'<a href="/news/news260831/">DeepSeek V5 Release</a>'
        items = signal2.parse_deepseek_news(html)
        self.assertEqual(items[0]["date"], datetime(2026, 8, 31, tzinfo=UTC))
        self.assertEqual(items[0]["vendor"], "DeepSeek")

    def test_candidate_window_does_not_keep_undated_secondary_noise(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)
        items = [
            signal2.make_candidate("hn", "OpenAI", "GPT-6 released", "https://x/1", None, None, 25),
            signal2.make_candidate("official", "OpenAI", "GPT-6 released", "https://x/2", None, None, 100),
        ]
        kept = signal2.filter_window(items, now - timedelta(days=7), now=now)
        self.assertEqual([x["source"] for x in kept], ["official"])

    def test_embed_never_allows_mentions_and_has_evidence(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        event = signal2.merge_candidates([
            signal2.make_candidate("official", "OpenAI", "Introducing GPT-6 @everyone", "https://openai.com/gpt6", now, "openai/gpt-6", 100),
            signal2.make_candidate("models.dev", "OpenAI", "GPT-6", "https://models.dev/gpt6", now, "openai/gpt-6", 85),
        ])[0]
        payload = signal2.discord_payload(event, username="Signal2")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 1)
        self.assertIn("Evidence", {f["name"] for f in payload["embeds"][0]["fields"]})

    def test_hf_broad_org_uploads_are_discovery_unless_core_family(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)

        def microsoft_get(_url, **_kwargs):
            return ([
                {"id": "microsoft/SQuadGen", "createdAt": "2026-08-31T10:00:00Z", "tags": [], "pipeline_tag": "text-generation"},
                {"id": "microsoft/Phi-5-mini", "createdAt": "2026-08-31T09:00:00Z", "tags": [], "pipeline_tag": "text-generation"},
            ], {})

        rows = signal2.collect_hf_org("microsoft", now - timedelta(days=7), http_get_json=microsoft_get)
        by_title = {row["title"]: row for row in rows}
        self.assertEqual(by_title["SQuadGen"]["trust"], 55)
        self.assertEqual(by_title["Phi-5-mini"]["trust"], 90)

        noise_event = signal2.merge_candidates([by_title["SQuadGen"]])[0]
        self.assertFalse(signal2.should_publish(noise_event))

    def test_hf_broad_org_upload_can_publish_when_independently_corroborated(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)
        hf = signal2.make_candidate(
            "huggingface", "NVIDIA", "DeepSeek-V4-Pro-0813-NVFP4",
            "https://huggingface.co/nvidia/DeepSeek-V4-Pro-0813-NVFP4",
            now, "nvidia/DeepSeek-V4-Pro-0813-NVFP4", 55, kind="open_weights",
        )
        secondary = signal2.make_candidate(
            "openrouter", "NVIDIA", "DeepSeek-V4-Pro-0813-NVFP4",
            "https://openrouter.ai/nvidia/deepseek-v4-pro-0813-nvfp4",
            now, "nvidia/DeepSeek-V4-Pro-0813-NVFP4", 65, kind="availability",
        )
        event = signal2.merge_candidates([hf, secondary])[0]
        self.assertTrue(signal2.should_publish(event))

    def test_vendor_branding_is_presentation_only_and_preserves_event_identity(self):
        self.assertEqual(signal2.vendor_from_slug("zhipuai"), "Zhipuai")
        self.assertEqual(signal2.vendor_from_slug("inclusionai"), "Inclusionai")
        self.assertEqual(signal2.display_vendor("Zhipuai"), "Z.ai (Zhipu)")
        self.assertEqual(signal2.display_vendor("Inclusionai"), "InclusionAI")

        now = datetime(2026, 9, 1, tzinfo=UTC)
        event = signal2.merge_candidates([
            signal2.make_candidate(
                "models.dev", "Zhipuai", "GLM-5.3-Flash",
                "https://models.dev/models/zhipuai/glm-5.3-flash",
                now, "zhipuai/glm-5.3-flash", 85,
            )
        ])[0]
        self.assertEqual(event["cluster_id"], "zhipuai:zhipuai-glm-5-3-flash")

    def test_hf_collector_follows_link_until_cutoff(self):
        now = datetime(2026, 9, 1, tzinfo=UTC)
        calls = []
        pages = {
            "first": (
                [{"id": "mistralai/New-Model", "createdAt": "2026-08-31T10:00:00Z", "tags": ["license:apache-2.0"], "pipeline_tag": "text-generation"}],
                {"Link": '<second>; rel="next"'},
            ),
            "second": (
                [{"id": "mistralai/Old-Model", "createdAt": "2026-07-01T10:00:00Z", "tags": [], "pipeline_tag": "text-generation"}],
                {},
            ),
        }
        def fake_get(url, **_kwargs):
            calls.append(url)
            return pages["first" if len(calls) == 1 else "second"]
        items = signal2.collect_hf_org("mistralai", now - timedelta(days=7), http_get_json=fake_get)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["model_id"], "mistralai/New-Model")
        self.assertEqual(calls, [calls[0], "second"])

    def test_queue_preserves_old_pending_when_new_events_arrive(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        state = signal2.new_state()
        old = signal2.merge_candidates([signal2.make_candidate("official", "OpenAI", "GPT-5", "https://x/5", now, "openai/gpt-5", 100)])[0]
        new = signal2.merge_candidates([signal2.make_candidate("official", "OpenAI", "GPT-6", "https://x/6", now, "openai/gpt-6", 100)])[0]
        signal2.enqueue_events(state, [old])
        signal2.enqueue_events(state, [new])
        self.assertEqual(set(state["pending"]), {old["key"], new["key"]})


if __name__ == "__main__":
    unittest.main()
