import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))

import http_safety
import server


class UrlSafetyTests(unittest.TestCase):
    def test_valid_bilibili_urls_without_dns(self):
        cases = {
            "https://www.bilibili.com/video/BV1abc": "https://www.bilibili.com/video/BV1abc",
            "http://b23.tv/abc": "http://b23.tv/abc",
            "https://bilibili.com": "https://bilibili.com/",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(
                    http_safety.validate_bilibili_url(value, resolve_host=False),
                    expected,
                )

    def test_invalid_bilibili_urls_without_dns(self):
        cases = [
            "",
            "javascript:alert(1)",
            "https://example.com/video/BV1abc",
            "https://bilibili.com.evil.example/video/BV1abc",
            "https://user:pass@www.bilibili.com/video/BV1abc",
            "https://www.bilibili.com:444/video/BV1abc",
            "https://127.0.0.1/video/BV1abc",
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(http_safety.UnsafeUrlError):
                    http_safety.validate_bilibili_url(value, resolve_host=False)


class RangeTests(unittest.TestCase):
    def test_three_supported_range_forms(self):
        self.assertEqual(http_safety.parse_single_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(http_safety.parse_single_range("bytes=6-", 10), (6, 9))
        self.assertEqual(http_safety.parse_single_range("bytes=-3", 10), (7, 9))

    def test_invalid_and_unsatisfiable_ranges(self):
        cases = [
            None,
            "items=0-1",
            "bytes=0-1,3-4",
            "bytes=abc-def",
            "bytes=5-4",
            "bytes=-0",
            "bytes=10-",
        ]
        self.assertIsNone(http_safety.parse_single_range(cases[0], 10))
        for value in cases[1:]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    http_safety.parse_single_range(value, 10)


class IdentifyTests(unittest.TestCase):
    @staticmethod
    def synced_lyrics():
        return [server.lyric_line(index * 20, f"第{index}句测试歌词内容") for index in range(10)]

    def test_identify_uses_full_fusion_before_sampled_for_synced_online_lyrics(self):
        online_lyrics = self.synced_lyrics()
        aligned_lyrics = [server.lyric_line(line["seconds"] + 5, line["text"]) for line in online_lyrics]
        correction = {"mode": "asr_word_sequence_timeline", "aligned_to_video": True}
        attempt = {"stage": "speech_anchors", "status": "ok"}
        view = {"title": "测试歌曲", "cid": 123, "duration": 180}
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1TEST"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=[]), \
             patch.object(server, "fetch_lyrics_from_netease", return_value=(online_lyrics, {"provider": "netease", "duration": 180})), \
             patch.object(server, "fetch_lyrics_from_lrclib") as lrclib, \
             patch.object(server, "align_candidate_lyrics", return_value=(aligned_lyrics, correction)), \
             patch.object(server, "calibrate_synced_lyrics") as sampled, \
             patch.object(server, "transcribe_audio_anchors", return_value=([{"text": "anchor"}], {"provider": "faster_whisper"}, [attempt])) as full, \
             patch.object(server, "attempt_video_recognition") as video_recognition, \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1TEST")

        self.assertEqual(status, 200)
        self.assertEqual(payload["lyrics"], aligned_lyrics)
        self.assertEqual(payload["lyricsMeta"]["correction"]["mode"], "asr_word_sequence_timeline")
        self.assertEqual(payload["videoOffsetSeconds"], 0)
        self.assertEqual(payload["recognitionAttempts"], [attempt])
        sampled.assert_not_called()
        full.assert_called_once_with("https://www.bilibili.com/video/BV1TEST", "zh")
        lrclib.assert_not_called()
        video_recognition.assert_not_called()

    def test_sampled_unavailable_keeps_original_synced_lyrics(self):
        online_lyrics = self.synced_lyrics()
        correction = {"mode": "unverified", "aligned_to_video": False, "reason": "missing_sampled_anchors"}
        attempt = {"stage": "sampled_speech_anchors", "status": "missing_tool"}
        view = {"title": "测试歌曲", "cid": 123, "duration": 180}
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1TEST"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=[]), \
             patch.object(server, "fetch_lyrics_from_netease", return_value=(online_lyrics, {"provider": "netease", "duration": 180})), \
             patch.object(server, "fetch_lyrics_from_lrclib"), \
             patch.object(server, "calibrate_synced_lyrics", return_value=(online_lyrics, correction, [attempt])), \
             patch.object(server, "transcribe_audio_anchors", return_value=([], None, [])) as full, \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1TEST")

        self.assertEqual(status, 200)
        self.assertEqual(payload["lyrics"], online_lyrics)
        self.assertEqual(payload["lyricsMeta"]["correction"]["mode"], "unverified")
        self.assertEqual(payload["recognitionAttempts"], [attempt])
        self.assertTrue(payload["lyricsMeta"]["correction"].get("fallback"))
        full.assert_called_once()

    def test_multiple_providers_share_one_full_whisper_and_lrclib_can_succeed(self):
        wrong = self.synced_lyrics()
        right = [server.lyric_line(index * 6, f"正确第{index}句歌词") for index in range(10)]
        aligned = [server.lyric_line(10 + index * 7, line["text"]) for index, line in enumerate(right)]
        rejected = {"mode": "rejected", "aligned_to_video": False, "reason": "low_text_similarity"}
        accepted = {"mode": "recognized_word_timeline", "aligned_to_video": True}
        view = {"title": "测试歌曲", "cid": 123, "duration": 180}
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1TEST"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=[]), \
             patch.object(server, "fetch_lyrics_from_netease", return_value=(wrong, {"provider": "netease", "duration": 180})), \
             patch.object(server, "fetch_lyrics_from_lrclib", return_value=(right, {"provider": "lrclib", "mode": "text_only"})), \
             patch.object(server, "transcribe_audio_anchors", return_value=([{"text": "anchor"}], {"provider": "faster_whisper"}, [])) as full, \
             patch.object(server, "align_candidate_lyrics", side_effect=[(wrong, rejected), (aligned, accepted)]), \
             patch.object(server, "calibrate_synced_lyrics", return_value=(wrong, {"mode": "unverified", "aligned_to_video": False}, [])), \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1TEST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["lyricsSource"], "lrclib")
        self.assertEqual(payload["lyrics"], aligned)
        full.assert_called_once_with("https://www.bilibili.com/video/BV1TEST", "zh")

    def test_aligned_fallback_beats_first_unverified_candidate(self):
        netease = self.synced_lyrics()
        lrclib = [server.lyric_line(index * 20, f"另一版第{index}句歌词内容") for index in range(10)]
        shifted = [server.lyric_line(line["seconds"] + 3, line["text"]) for line in lrclib]
        view = {"title": "测试歌曲", "cid": 123, "duration": 180}
        corrections = [
            (netease, {"mode": "unverified", "aligned_to_video": False, "reason": "no_matches"}, []),
            (shifted, {"mode": "sampled_fixed_offset", "aligned_to_video": True, "avg_similarity": 0.804, "matched_count": 7, "window_count": 3}, []),
        ]
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1TEST"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=[]), \
             patch.object(server, "fetch_lyrics_from_netease", return_value=(netease, {"provider": "netease", "duration": 180})), \
             patch.object(server, "fetch_lyrics_from_lrclib", return_value=(lrclib, {"provider": "lrclib", "duration": 180})), \
             patch.object(server, "transcribe_audio_anchors", return_value=([], None, [])), \
             patch.object(server, "calibrate_synced_lyrics", side_effect=corrections), \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1TEST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["lyricsSource"], "lrclib")
        self.assertEqual(payload["lyrics"], shifted)

    def test_two_aligned_fallbacks_use_quality_score(self):
        weak = {"correction": {"mode": "sampled_fixed_offset", "aligned_to_video": True, "avg_similarity": 0.79, "matched_count": 5, "window_count": 2}}
        strong = {"correction": {"mode": "sampled_fixed_offset", "aligned_to_video": True, "avg_similarity": 0.84, "matched_count": 7, "window_count": 3}}
        self.assertGreater(server.synced_fallback_quality(strong, "lrclib"), server.synced_fallback_quality(weak, "netease"))

    def test_bilibili_subtitles_call_no_whisper(self):
        subtitles = self.synced_lyrics()
        view = {"title": "测试歌曲", "cid": 123, "duration": 180}
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1TEST"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=subtitles), \
             patch.object(server, "calibrate_synced_lyrics") as sampled, \
             patch.object(server, "transcribe_audio_anchors") as full, \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1TEST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["lyricsSource"], "bilibili_subtitle")
        sampled.assert_not_called()
        full.assert_not_called()

    def test_recognition_attempts_accumulate_across_fallbacks(self):
        speech_attempt = {"stage": "speech_anchors", "status": "no_result"}
        video_attempt = {"stage": "video_subtitle", "status": "no_result"}
        view = {"title": "没有公开歌词的测试歌曲", "cid": 123, "duration": 180}
        with patch.object(server, "normalize_bilibili_url", return_value="https://www.bilibili.com/video/BV1UNKNOWN"), \
             patch.object(server, "fetch_video_view", return_value=view), \
             patch.object(server, "fetch_subtitle_lines", return_value=[]), \
             patch.object(server, "fetch_lyrics_from_netease", return_value=([], None)), \
             patch.object(server, "fetch_lyrics_from_lrclib", return_value=([], None)), \
             patch.object(server, "get_known_lyrics", return_value=[]), \
             patch.object(server, "transcribe_audio_anchors", return_value=([], None, [speech_attempt])) as whisper, \
             patch.object(server, "attempt_video_recognition", return_value=([], None, [video_attempt])), \
             patch.object(server, "get_safe_video_stream_url", return_value=None), \
             patch.object(server, "cached_video_path", return_value=ROOT / ".missing-test-video"):
            payload, status = server.identify("https://www.bilibili.com/video/BV1UNKNOWN")

        self.assertEqual(status, 200)
        self.assertEqual(payload["recognitionAttempts"], [speech_attempt, video_attempt])
        self.assertEqual(payload["lyricsSource"], "no_public_subtitle")
        whisper.assert_called_once()


class RecognizedTimelineAlignmentTests(unittest.TestCase):
    @staticmethod
    def anchor(start, text, words=True, quality=True):
        item = {"start": start, "end": start + max(1.0, len(text) * 0.35), "seconds": start, "text": text}
        if words:
            item["words"] = [{"word": text, "start": item["start"], "end": item["end"]}]
        if quality:
            item.update({"no_speech_prob": 0.05, "avg_logprob": -0.2, "compression_ratio": 1.0})
        return item

    def test_word_timeline_keeps_online_text_and_handles_segment_shapes(self):
        lyrics = [
            server.lyric_line(0, "春风吹过长街"),
            server.lyric_line(6, "灯火照亮归途"),
            server.lyric_line(12, "我们继续向前直到天明"),
        ]
        anchors = [
            self.anchor(10, "春风吹过长街灯火照亮归途"),
            self.anchor(24, "我们继续向前"),
            self.anchor(27, "直到天明"),
        ]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertEqual(meta["mode"], "asr_word_sequence_timeline")
        self.assertEqual([line["text"] for line in aligned], [line["text"] for line in lyrics])
        self.assertAlmostEqual(aligned[0]["seconds"], 10, places=2)
        self.assertGreater(aligned[1]["seconds"], aligned[0]["seconds"])
        self.assertAlmostEqual(aligned[2]["seconds"], 24, places=2)

    def test_intro_is_skipped_and_missing_line_is_interpolated(self):
        lyrics = [server.lyric_line(index * 6, text) for index, text in enumerate([
            "第一句歌词内容", "中间遗漏的一句歌词", "第三句歌词内容", "最后一句歌词内容",
        ])]
        anchors = [
            self.anchor(1, "欢迎收看今天的节目"),
            self.anchor(10, "第一句歌词内容"),
            self.anchor(30, "第三句歌词内容"),
            self.anchor(40, "最后一句歌词内容"),
        ]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertTrue(meta["aligned_to_video"])
        self.assertEqual(meta["interpolated_count"], 1)
        self.assertGreater(aligned[1]["seconds"], aligned[0]["seconds"])
        self.assertLess(aligned[1]["seconds"], aligned[2]["seconds"])

    def test_repeated_chorus_uses_distinct_rounds(self):
        texts = ["主歌第一句", "相同副歌短句", "副歌收尾句", "第二段主歌", "相同副歌短句", "副歌收尾句"]
        lyrics = [server.lyric_line(index * 5, text) for index, text in enumerate(texts)]
        anchors = [self.anchor(time, text) for time, text in zip([10, 20, 25, 60, 80, 85], texts)]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertTrue(meta["aligned_to_video"])
        self.assertAlmostEqual(aligned[1]["seconds"], 20, places=1)
        self.assertAlmostEqual(aligned[4]["seconds"], 80, places=1)

    def test_long_song_sparse_or_front_loaded_matches_are_rejected(self):
        lyrics = [server.lyric_line(index * 5, f"第{index}句独特歌词内容") for index in range(30)]
        sparse = [self.anchor(10 + index * 5, lyrics[index]["text"]) for index in range(3)]
        _, sparse_meta = server.align_lyrics_to_recognized_timeline(lyrics, sparse)
        self.assertEqual(sparse_meta["mode"], "unverified")
        front_loaded = [self.anchor(10 + index * 3, lyrics[index]["text"]) for index in range(18)]
        front_loaded.extend([self.anchor(150, "完全无关的识别文本"), self.anchor(250, "仍然无关的识别文本")])
        _, front_meta = server.align_lyrics_to_recognized_timeline(lyrics, front_loaded)
        self.assertEqual(front_meta["mode"], "unverified")

    def test_low_asr_quality_and_large_missing_run_are_rejected(self):
        lyrics = [server.lyric_line(index * 5, f"第{index}句歌词内容") for index in range(12)]
        poor = [self.anchor(10 + index * 5, line["text"], quality=False) for index, line in enumerate(lyrics)]
        for anchor in poor:
            anchor.update({"no_speech_prob": 0.55, "avg_logprob": -0.95, "compression_ratio": 2.3})
        _, poor_meta = server.align_lyrics_to_recognized_timeline(lyrics, poor)
        self.assertEqual(poor_meta["reason"], "low_asr_quality")
        anchors = [self.anchor(10 + index * 5, lyrics[index]["text"]) for index in (0, 1, 2, 8, 9, 10, 11)]
        _, gap_meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertEqual(gap_meta["mode"], "unverified")
        self.assertGreaterEqual(gap_meta["max_unmatched_run"], 5)

    def test_cantonese_like_errors_accept_with_global_evidence_and_segment_fallback(self):
        lyrics = [server.lyric_line(index * 6, text) for index, text in enumerate([
            "仍然倚在失眠夜望天边星宿", "仍然听见小提琴如泣似诉再挑逗", "为何只剩一弯月留在我的天空",
            "这晚以后音讯隔绝", "人如天上的明月是不可拥有", "情如曲过只遗留无可挽救再分别",
        ])]
        recognized = [
            "仍然依在失眠夜望天边星宿", "仍然听见小提琴如哭似诉再挑逗", "为何只剩一弯月留在我天空",
            "这晚以后音信隔绝", "人如天上明月是不可拥有", "情如曲过只遗留无法挽救再分别",
        ]
        anchors = [self.anchor(10 + index * 12, text, words=False) for index, text in enumerate(recognized)]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertEqual(meta["mode"], "asr_segment_sequence_timeline")
        self.assertTrue(meta["aligned_to_video"])
        self.assertEqual([line["text"] for line in aligned], [line["text"] for line in lyrics])

    def test_single_line_and_multiple_segments_form_one_line(self):
        lyrics = [server.lyric_line(0, "春风吹过长街灯火照亮归途")]
        anchors = [self.anchor(12, "春风吹过长街"), self.anchor(15, "灯火照亮归途")]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertEqual(meta["mode"], "asr_word_sequence_timeline")
        self.assertAlmostEqual(aligned[0]["seconds"], 12, places=1)
        self.assertGreater(aligned[0]["end"], 15)

    def test_same_sentence_multiple_times_follows_global_order(self):
        texts = ["同一句歌词", "中间不同歌词", "同一句歌词", "结尾歌词"]
        lyrics = [server.lyric_line(index * 30, text) for index, text in enumerate(texts)]
        anchors = [self.anchor(time, text) for time, text in zip([8, 35, 68, 92], texts)]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertTrue(meta["aligned_to_video"])
        self.assertAlmostEqual(aligned[0]["seconds"], 8, places=1)
        self.assertAlmostEqual(aligned[2]["seconds"], 68, places=1)

    def test_wrong_original_lrc_times_are_only_weak_prior(self):
        texts = ["第一句完整歌词", "第二句完整歌词", "第三句完整歌词", "第四句完整歌词"]
        lyrics = [server.lyric_line(180 - index * 40, text) for index, text in enumerate(texts)]
        anchors = [self.anchor(10 + index * 15, text) for index, text in enumerate(texts)]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertTrue(meta["aligned_to_video"])
        self.assertEqual([round(line["seconds"]) for line in aligned], [10, 25, 40, 55])

    def test_only_middle_matches_rejects_unbounded_edges(self):
        lyrics = [server.lyric_line(index * 6, f"第{index}句完整歌词内容") for index in range(8)]
        anchors = [self.anchor(20 + index * 8, lyrics[index]["text"]) for index in range(1, 7)]
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertFalse(meta["aligned_to_video"])
        self.assertEqual(meta["reason"], "unbounded_edge_interpolation")
        self.assertEqual(aligned, lyrics)

    def test_low_similarity_high_coverage_is_rejected(self):
        lyrics = [server.lyric_line(index * 6, f"春风灯火归途第{index}章") for index in range(10)]
        anchors = [self.anchor(10 + index * 8, f"春分灯塔归图第{index}张") for index in range(10)]
        _, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertEqual(meta["mode"], "unverified")
        self.assertEqual(meta["reason"], "low_text_similarity")

    def test_long_synthetic_input_has_bounded_candidates(self):
        lyrics = [server.lyric_line(index * 5, f"这是第{index}句唯一合成歌词内容") for index in range(50)]
        anchors = [self.anchor(10 + index * 6, line["text"]) for index, line in enumerate(lyrics)]
        units = server.anchor_time_units(anchors)
        rows = [server.lyric_span_candidates(index, line["text"], units) for index, line in enumerate(lyrics)]
        self.assertTrue(all(len(row) <= 28 for row in rows))
        aligned, meta = server.align_lyrics_to_recognized_timeline(lyrics, anchors)
        self.assertTrue(meta["aligned_to_video"])
        self.assertEqual(len(aligned), 50)


class SampledAlignmentTests(unittest.TestCase):
    @staticmethod
    def matches(scale=1.0, offset=5.0, similarity=0.95):
        times = [10, 30, 55, 85, 115, 145, 175]
        return [{
            "lyric_seconds": value,
            "audio_seconds": scale * value + offset,
            "similarity": similarity,
            "window_index": min(2, index // 2),
        } for index, value in enumerate(times)]

    @staticmethod
    def temporal_consensus_matches():
        values = [
            (35.618, 38.36, 0.7272727272727273, 0),
            (40.181, 42.7, 0.7384615384615385, 0),
            (44.739, 47.5, 0.7272727272727273, 0),
            (53.878, 56.64, 1.0, 0),
            (130.088, 132.68, 0.7384615384615385, 1),
            (193.391, 198.54, 0.7250000000000001, 2),
            (202.849, 207.34, 0.72, 2),
        ]
        return [{
            "lyric_seconds": lyric_seconds,
            "audio_seconds": audio_seconds,
            "similarity": similarity,
            "window_index": window_index,
        } for lyric_seconds, audio_seconds, similarity, window_index in values]

    @staticmethod
    def phrase_matches(lyrics, anchors, duration=None):
        duration = duration or max(line["seconds"] for line in lyrics)
        return server.match_sampled_anchors_to_lyrics(lyrics, anchors, duration, duration)

    def test_suffix_anchor_maps_to_in_line_time(self):
        lyrics = [
            server.lyric_line(193.391, "曾沿着雪路浪游 为何为好事泪流"),
            server.lyric_line(202.849, "何不把悲哀感觉假设是来自你虚构"),
        ]
        matches = self.phrase_matches(lyrics, [{"seconds": 198.54, "text": "为何为好事泪流", "window_index": 2}], 220)
        self.assertEqual(len(matches), 1)
        self.assertGreater(matches[0]["phrase_start_ratio"], 0.45)
        self.assertGreater(matches[0]["expected_lyric_seconds"], 197.0)
        self.assertLess(abs(matches[0]["audio_seconds"] - matches[0]["expected_lyric_seconds"]), 1.6)

    def test_prefix_anchor_stays_near_line_start(self):
        lyrics = [server.lyric_line(30, "春风吹过长街灯火渐明"), server.lyric_line(38, "下一句歌词内容")]
        matches = self.phrase_matches(lyrics, [{"seconds": 32, "text": "春风吹过长街", "window_index": 0}], 60)
        self.assertEqual(matches[0]["phrase_start_ratio"], 0)
        self.assertEqual(matches[0]["expected_lyric_seconds"], 30)

    def test_mixed_phrase_positions_recover_linear_model(self):
        scale, offset = 1.018, 2.4
        lyrics = [
            server.lyric_line(20, "晨光照进窗前故事开始"),
            server.lyric_line(50, "穿过漫长街道终于见到你"),
            server.lyric_line(80, "抬头看见星光落在肩上"),
            server.lyric_line(110, "旧日回忆随风慢慢远去"),
            server.lyric_line(140, "我们继续向前不再回头"),
            server.lyric_line(170, "最后一句唱给远方的你"),
            server.lyric_line(180, "收尾歌词"),
        ]
        specs = [(0, "晨光照进"), (0, "故事开始"), (1, "穿过漫长"), (2, "落在肩上"), (3, "旧日回忆"), (4, "不再回头"), (5, "唱给远方的你")]
        anchors = []
        for index, (lyric_index, text) in enumerate(specs):
            span = server.best_phrase_span(lyrics[lyric_index]["text"], text)
            phrase_time = lyrics[lyric_index]["seconds"] + span["start_ratio"] * server.usable_lyric_line_duration(lyrics, lyric_index)
            anchors.append({"seconds": scale * phrase_time + offset, "text": text, "window_index": min(2, index // 2)})
        fit = server.robust_timeline_models(self.phrase_matches(lyrics, anchors, 180))
        self.assertEqual(fit["mode"], "sampled_linear_timeline")
        self.assertAlmostEqual(fit["scale"], scale, places=3)
        self.assertAlmostEqual(fit["offset"], offset, places=2)

    def test_long_instrumental_gap_is_clamped(self):
        lyrics = [server.lyric_line(10, "前半句歌词后半句歌词"), server.lyric_line(70, "间奏以后歌词")]
        matches = self.phrase_matches(lyrics, [{"seconds": 16, "text": "后半句歌词", "window_index": 0}], 80)
        self.assertLessEqual(matches[0]["expected_lyric_seconds"] - 10, 7.15)

    def test_credit_lines_are_not_matches_or_duration_boundaries(self):
        lyrics = [
            server.lyric_line(10, "开头歌词后半段"),
            server.lyric_line(12, "作词：某某"),
            server.lyric_line(18, "下一句歌词"),
        ]
        anchors = [
            {"seconds": 14, "text": "后半段", "window_index": 0},
            {"seconds": 15, "text": "作词某某", "window_index": 0},
        ]
        matches = self.phrase_matches(lyrics, anchors, 30)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["lyric_index"], 0)
        self.assertGreater(matches[0]["expected_lyric_seconds"], 12)

    def test_two_non_overlapping_phrases_on_same_line_are_kept(self):
        lyrics = [server.lyric_line(10, "春风吹过长街灯火照亮归途"), server.lyric_line(20, "下一句歌词")]
        anchors = [
            {"seconds": 11, "text": "春风吹过", "window_index": 0},
            {"seconds": 16, "text": "灯火照亮归途", "window_index": 0},
        ]
        matches = self.phrase_matches(lyrics, anchors, 30)
        self.assertEqual(len(matches), 2)
        self.assertEqual([item["lyric_index"] for item in matches], [0, 0])
        self.assertLess(matches[0]["expected_lyric_seconds"], matches[1]["expected_lyric_seconds"])

    def test_overlapping_duplicate_phrase_is_not_counted_twice(self):
        lyrics = [server.lyric_line(10, "春风吹过长街灯火照亮归途"), server.lyric_line(20, "下一句歌词")]
        anchors = [
            {"seconds": 11, "text": "春风吹过长街", "window_index": 0},
            {"seconds": 11.4, "text": "吹过长街", "window_index": 0},
        ]
        matches = self.phrase_matches(lyrics, anchors, 30)
        self.assertEqual(len(matches), 1)

    def test_sample_windows(self):
        self.assertEqual(server.build_anchor_sample_windows(59), [])
        self.assertEqual(server.build_anchor_sample_windows(100), [
            {"start": 12.0, "end": 28.0, "index": 0},
            {"start": 42.0, "end": 58.0, "index": 1},
            {"start": 72.0, "end": 88.0, "index": 2},
        ])
        self.assertEqual(server.build_anchor_sample_windows(120), [
            {"start": 12.0, "end": 36.0, "index": 0},
            {"start": 48.0, "end": 72.0, "index": 1},
            {"start": 84.0, "end": 108.0, "index": 2},
        ])
        self.assertEqual(server.build_anchor_sample_windows(60), [
            {"start": 4.0, "end": 20.0, "index": 0},
            {"start": 22.0, "end": 38.0, "index": 1},
            {"start": 40.0, "end": 56.0, "index": 2},
        ])

    def test_fixed_offset_fit(self):
        fit = server.robust_timeline_models(self.matches(offset=7.0))
        self.assertEqual(fit["mode"], "sampled_fixed_offset")
        self.assertAlmostEqual(fit["offset"], 7.0)

    def test_linear_scale_fit(self):
        noisy = self.matches(scale=1.02, offset=3.0)
        noisy[1]["audio_seconds"] += 0.15
        noisy[4]["audio_seconds"] -= 0.12
        fit = server.robust_timeline_models(noisy)
        self.assertEqual(fit["mode"], "sampled_linear_timeline")
        self.assertEqual(fit["confidence_basis"], "text_and_time")
        self.assertAlmostEqual(fit["scale"], 1.02, places=3)
        self.assertAlmostEqual(fit["offset"], 3.0, places=1)

    def test_low_text_similarity_with_temporal_consensus_accepts_linear(self):
        fit = server.robust_timeline_models(self.temporal_consensus_matches())
        self.assertEqual(fit["mode"], "sampled_linear_timeline")
        self.assertEqual(fit["confidence_basis"], "temporal_consensus")
        self.assertAlmostEqual(fit["scale"], 1.0109417, places=5)
        self.assertLessEqual(fit["rmse"], 0.65)
        self.assertLessEqual(fit["mae"], 0.45)
        self.assertLessEqual(fit["max_residual"], 1.25)

    def test_temporal_consensus_rejects_large_residuals(self):
        matches = self.temporal_consensus_matches()
        matches[1]["audio_seconds"] += 1.2
        matches[4]["audio_seconds"] -= 1.2
        fit = server.robust_timeline_models(matches)
        self.assertEqual(fit["mode"], "unverified")

    def test_temporal_consensus_requires_strong_text_anchor(self):
        matches = self.temporal_consensus_matches()
        matches[3]["similarity"] = 0.82
        fit = server.robust_timeline_models(matches)
        self.assertEqual(fit["mode"], "unverified")

    def test_temporal_consensus_requires_three_windows(self):
        matches = self.temporal_consensus_matches()
        for item in matches:
            if item["window_index"] == 2:
                item["window_index"] = 1
        fit = server.robust_timeline_models(matches)
        self.assertEqual(fit["mode"], "unverified")

    def test_single_outlier_is_rejected(self):
        matches = self.matches(offset=4.0)
        matches[3]["audio_seconds"] += 30
        fit = server.robust_timeline_models(matches)
        self.assertEqual(fit["mode"], "sampled_fixed_offset")
        self.assertEqual(fit["matched_count"], 6)
        self.assertAlmostEqual(fit["offset"], 4.0)

    def test_low_confidence_falls_back(self):
        fit = server.robust_timeline_models(self.matches(similarity=0.7)[:3])
        self.assertEqual(fit["mode"], "unverified")

    def test_transform_is_nonnegative_and_strictly_monotonic(self):
        lyrics = [
            {"seconds": 0, "time": "00:00", "text": "a", "end": 0.1},
            {"seconds": 0, "time": "00:00", "text": "b", "end": 0.1},
            {"seconds": 1, "time": "00:01", "text": "c", "end": 1.1},
        ]
        transformed = server.apply_timeline_transform(lyrics, 1.0, -5.0)
        seconds = [line["seconds"] for line in transformed]
        self.assertGreaterEqual(seconds[0], 0)
        self.assertTrue(all(right > left for left, right in zip(seconds, seconds[1:])))
        self.assertTrue(all(line["end"] > line["seconds"] for line in transformed))

    def test_full_cache_is_reused_for_samples(self):
        windows = server.build_anchor_sample_windows(180)
        full = {"segments": [
            {"start": 30, "end": 32, "seconds": 30, "text": "测试歌词", "no_speech_prob": 0},
            {"start": 90, "end": 92, "seconds": 90, "text": "另一句歌词", "no_speech_prob": 0},
        ], "meta": {"provider": "faster_whisper"}}
        with patch.object(server, "load_cached_speech", return_value=full), \
             patch.object(server, "load_cached_sampled_speech") as sampled_cache, \
             patch.dict(sys.modules, {"faster_whisper": Mock()}):
            segments, meta, attempts = server._transcribe_sampled_audio_anchors("url", 180)
        self.assertEqual(len(segments), 2)
        self.assertEqual(meta["cache"], "full")
        self.assertEqual(attempts[0]["status"], "cached_full")
        sampled_cache.assert_not_called()

    def test_empty_segments_are_rejected_and_not_saved(self):
        self.assertFalse(server.cached_segments_are_valid([]))
        windows = server.build_anchor_sample_windows(180)
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "SPEECH_CACHE_DIR", Path(tmp)):
            self.assertFalse(server.save_cached_speech("url", {"version": 3, "segments": [], "meta": {"model": "tiny", "languageHint": "auto"}}))
            self.assertFalse(server.save_cached_sampled_speech("url", windows, [], {"model": "tiny", "languageHint": "auto"}))
            self.assertFalse(server.cached_speech_path("url").exists())
            self.assertFalse(server.cached_sampled_speech_path("url").exists())

    def test_full_cache_language_identity_and_empty_cache_rejection(self):
        segment = {"start": 1, "end": 2, "seconds": 1, "text": "测试", "words": [{"word": "测试", "start": 1, "end": 2}], "no_speech_prob": 0.05, "avg_logprob": -0.2, "compression_ratio": 1.0}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "SPEECH_CACHE_DIR", Path(tmp)), \
             patch.object(server, "resolve_whisper_model", return_value="tiny"):
            payload = {"version": 3, "segments": [segment], "meta": {"model": "tiny", "languageHint": "zh"}}
            self.assertTrue(server.save_cached_speech("url", payload))
            self.assertEqual(server.load_cached_speech("url", "zh")["version"], 3)
            self.assertIsNone(server.load_cached_speech("url", None))
            server.cached_speech_path("url").write_text(json.dumps({"version": 3, "segments": [], "meta": {"model": "tiny", "languageHint": "zh"}}), encoding="utf-8")
            self.assertIsNone(server.load_cached_speech("url", "zh"))

    def test_empty_model_result_is_no_result_and_not_cached(self):
        model = Mock()
        model.transcribe.return_value = (iter([]), SimpleNamespace(language="ru", duration=10))
        with patch.object(server, "load_cached_speech", return_value=None), \
             patch.object(server, "extract_audio_wav", return_value=Path("audio.wav")), \
             patch.object(server, "resolve_whisper_model", return_value="tiny"), \
             patch.object(server, "get_whisper_model", return_value=model), \
             patch.object(server, "save_cached_speech") as save, \
             patch.dict(sys.modules, {"faster_whisper": Mock()}):
            segments, meta, attempts = server._transcribe_audio_anchors("url", "zh")
        self.assertEqual(segments, [])
        self.assertEqual(meta["language"], "ru")
        self.assertEqual(attempts[0]["status"], "no_result")
        save.assert_not_called()
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "zh")

    def test_language_hint_inference(self):
        self.assertEqual(server.infer_lyrics_language_hint([{"text": "这是中文粤语歌词内容"}]), "zh")
        self.assertIsNone(server.infer_lyrics_language_hint([{"text": "This is an English lyric line"}]))

    def test_sampled_cache_model_mismatch_is_rejected(self):
        windows = server.build_anchor_sample_windows(180)
        segment = {"start": 30, "end": 32, "seconds": 30, "text": "测试", "no_speech_prob": 0.05, "avg_logprob": -0.2, "compression_ratio": 1.0}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "SPEECH_CACHE_DIR", Path(tmp)), \
             patch.object(server, "resolve_whisper_model", return_value="tiny"):
            server.save_cached_sampled_speech("url", windows, [segment], {"model": "small", "languageHint": "zh"})
            self.assertIsNone(server.load_cached_sampled_speech("url", windows, "zh"))


class HttpConfigurationTests(unittest.TestCase):
    def test_cors_origin_matches_web_server(self):
        self.assertEqual(
            server.ALLOWED_ORIGINS,
            {"http://127.0.0.1:4190", "http://localhost:4190"},
        )
        handler = server.Handler.__new__(server.Handler)
        handler.headers = Message()
        handler.headers["Origin"] = "http://127.0.0.1:4190"
        self.assertEqual(handler._cors_origin(), "http://127.0.0.1:4190")
        handler.headers.replace_header("Origin", "http://127.0.0.1:4191")
        self.assertIsNone(handler._cors_origin())

    def test_server_defaults_to_loopback(self):
        self.assertEqual(server.HOST, "127.0.0.1")

    def test_video_stream_url_is_relative_for_local_and_proxied_clients(self):
        with patch.object(server, "cached_video_path") as cached, \
             patch.object(server, "get_yt_dlp_command", return_value=["yt-dlp"]), \
             patch.object(server, "get_ffmpeg_path", return_value="ffmpeg"):
            cached.return_value.exists.return_value = False
            value = server.get_safe_video_stream_url(
                "https://www.bilibili.com/video/BV1TEST",
                [],
            )
        self.assertEqual(
            value,
            "/api/video?url=https%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV1TEST",
        )

    def test_bilibili_media_url_allows_only_official_media_hosts(self):
        self.assertTrue(server._is_allowed_bilibili_media_url("https://upos.example.bilivideo.com/video.m4s"))
        self.assertTrue(server._is_allowed_bilibili_media_url("https://upos.example.bilivideo.cn/audio.m4s"))
        for value in (
            "https://example.com/video.m4s",
            "https://bilivideo.com.evil.example/video.m4s",
            "https://user:pass@upos.example.bilivideo.com/video.m4s",
            "https://upos.example.bilivideo.com:444/video.m4s",
            "file:///tmp/video.m4s",
        ):
            with self.subTest(value=value):
                self.assertFalse(server._is_allowed_bilibili_media_url(value))

    def test_fetch_bilibili_media_streams_prefers_h264_and_highest_audio_bandwidth(self):
        view_payload = {"code": 0, "data": {"cid": 123}}
        play_payload = {
            "code": 0,
            "data": {
                "dash": {
                    "video": [
                        {"baseUrl": "https://v1.bilivideo.com/video.m4s", "codecs": "hev1", "bandwidth": 900},
                        {
                            "baseUrl": "https://third-party.example/video.m4s",
                            "backupUrl": ["https://v2.bilivideo.com/video.m4s"],
                            "codecs": "avc1.64001F",
                            "bandwidth": 500,
                        },
                    ],
                    "audio": [
                        {"baseUrl": "https://a1.bilivideo.com/audio.m4s", "bandwidth": 100},
                        {
                            "baseUrl": "https://third-party.example/audio.m4s",
                            "backup_url": ["https://a2.bilivideo.com/audio.m4s"],
                            "bandwidth": 200,
                        },
                    ],
                }
            },
        }
        with patch.object(server, "fetch_video_view", return_value=view_payload["data"]), \
             patch.object(server, "request_json", return_value=play_payload):
            streams = server.fetch_bilibili_media_streams("https://www.bilibili.com/video/BV1TEST")
        self.assertEqual(streams["video_url"], "https://v2.bilivideo.com/video.m4s")
        self.assertEqual(streams["audio_url"], "https://a2.bilivideo.com/audio.m4s")

    def test_request_body_limits(self):
        self.assertEqual(server.MAX_JSON_BODY_BYTES, 1024 * 1024)
        self.assertEqual(server.MAX_LYRICS_BYTES, 512 * 1024)
        self.assertLess(server.MAX_LYRICS_BYTES, server.MAX_JSON_BODY_BYTES)


if __name__ == "__main__":
    unittest.main()
