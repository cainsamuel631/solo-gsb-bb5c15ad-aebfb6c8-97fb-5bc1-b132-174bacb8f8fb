"""核心解析 / 断句算法测试（纯标准库，无需 Web 依赖）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analyzer import analyze, render_webvtt
from app.parser import (
    Cue,
    detect_lang,
    format_timestamp,
    join_tokens,
    parse_subtitles,
    parse_timestamp,
    tokenize,
    wrap_lines,
)

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


class TestTimestamp(unittest.TestCase):
    def test_parse_vtt(self):
        self.assertEqual(parse_timestamp("00:01:02.500"), 62500)

    def test_parse_srt_comma(self):
        self.assertEqual(parse_timestamp("01:02:03,250"), 3723250)

    def test_parse_short_form(self):
        self.assertEqual(parse_timestamp("00:00.5"), 500)

    def test_roundtrip(self):
        for ms in (0, 1, 999, 62500, 3_723_250):
            self.assertEqual(parse_timestamp(format_timestamp(ms)), ms)


class TestParser(unittest.TestCase):
    def test_parse_vtt(self):
        cues = parse_subtitles(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000\nHello world\n\n"
            "00:00:02.000 --> 00:00:03.000 align:start\n第二行\n"
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].text, "Hello world")
        self.assertEqual(cues[0].start_ms, 1000)
        self.assertEqual(cues[1].text, "第二行")

    def test_parse_srt_with_index(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:02,000\nabc\n\n"
            "2\n00:00:02,500 --> 00:00:03,500\ndef ghi\n"
        )
        cues = parse_subtitles(srt)
        self.assertEqual([c.text for c in cues], ["abc", "def ghi"])

    def test_strip_tags_and_note(self):
        vtt = (
            "WEBVTT\n\nNOTE skip me\n\n"
            "1\n00:00:01.000 --> 00:00:02.000\n<b>Bold</b> <c.yellow>x</c>\n"
        )
        cues = parse_subtitles(vtt)
        self.assertEqual(cues[0].text, "Bold x")

    def test_multiline_cue_preserved(self):
        cues = parse_subtitles(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nline one\nline two\n"
        )
        self.assertEqual(cues[0].text, "line one\nline two")


class TestTokenize(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(tokenize("Hello 世界 world"), ["Hello", "世", "界", "world"])

    def test_join_roundtrip(self):
        for text in ["Hello world foo", "你好世界", "Hello 世界 test", "don't stop rock-roll"]:
            self.assertEqual(join_tokens(tokenize(text)), text)

    def test_wrap(self):
        lines = wrap_lines("This is a fairly long English sentence", 12)
        self.assertTrue(all(len(ln) <= 12 for ln in lines))

    def test_wrap_cjk(self):
        lines = wrap_lines("一二三四五六七八九十", 4)
        self.assertEqual(lines, ["一二三四", "五六七八", "九十"])


class TestDetect(unittest.TestCase):
    def test_lang(self):
        self.assertEqual(detect_lang("这是中文句子"), "zh")
        self.assertEqual(detect_lang("これは日本語のテキストです"), "ja")
        self.assertEqual(detect_lang("이것은 한국어입니다"), "ko")
        self.assertEqual(detect_lang("plain english text"), "en")


class TestAnalyzer(unittest.TestCase):
    def test_clean_input_no_issues(self):
        cues = [
            Cue(0, 2000, "一切正常。"),
            Cue(2200, 4500, "这也很正常。"),
        ]
        r = analyze(cues, max_chars=21, min_dwell_ms=1000, cps_max=17)
        self.assertEqual(r.stats["error_count"], 0)
        self.assertEqual(r.language, "zh")

    def test_overlong_detected(self):
        cues = [Cue(0, 6000, "这是一个明显超过每行字数上限的超长中文句子必须被拆开处理才行")]
        r = analyze(cues, max_chars=10, min_dwell_ms=500, cps_max=17)
        # DP 应把它拆成多条 cue；文本总量守恒
        revised_text = "".join(c.text.replace("\n", "") for c in r.cues)
        self.assertIn("这是一个", revised_text)
        # 时间轴在重排后仍在原范围内
        self.assertEqual(r.cues[0].start_ms, 0)
        self.assertEqual(r.cues[-1].end_ms, 6000)

    def test_short_dwell_flags_issue(self):
        cues = [Cue(0, 300, "短句")]
        r = analyze(cues, max_chars=21, min_dwell_ms=1000, cps_max=17)
        types = {i.type for i in r.issues}
        self.assertIn("dwell", types)

    def test_speed_issue(self):
        cues = [Cue(0, 1000, "一二三四五六七八九十一二三四五六七八九十")]  # 30 字/秒
        r = analyze(cues, max_chars=40, min_dwell_ms=500, cps_max=17)
        types = {i.type for i in r.issues}
        self.assertIn("speed", types)

    def test_cross_cue_reflow_preserves_tokens(self):
        # 两条紧邻 cue，第一条过载、第二条很空：边界应被重分配
        cues = [
            Cue(0, 2000, "一二三四五六七八九十一二三四五六七八九十一二三四"),
            Cue(2050, 6000, "字"),
        ]
        r = analyze(cues, max_chars=12, min_dwell_ms=800, cps_max=12)
        # 守恒基准：逐 cue 分词后拼接（不跨 cue 粘合拉丁词）
        before = [t for c in cues for t in tokenize(c.text)]
        after = [
            t for c in r.cues for t in tokenize(c.text.replace("\n", " "))
        ]
        self.assertEqual(before, after)
        # 切分点时间必须单调不减
        for a, b in zip(r.cues, r.cues[1:]):
            self.assertLessEqual(a.end_ms, b.end_ms)
            self.assertLessEqual(a.start_ms, b.start_ms)

    def test_unsolvable_minimal_segment(self):
        # 时间轴短到无法同时满足停留与语速：应标出最小区段并给出原因
        cues = [Cue(0, 300, "一二三四五六七八九十一二三四五六七八九十")]
        r = analyze(cues, max_chars=40, min_dwell_ms=2000, cps_max=10)
        self.assertGreater(r.stats["error_count"], 0)
        for iss in r.issues:
            if iss.severity == "error":
                self.assertIn("reason_summary", iss.detail)
                self.assertIn("original_cue_range", iss.detail)
                break

    def test_render_webvtt(self):
        cues = [Cue(0, 2000, "你好世界")]
        r = analyze(cues)
        vtt = render_webvtt(r)
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:00.000 --> 00:00:02.000", vtt)
        self.assertIn("你好世界", vtt)
        # 产物可被重新解析
        again = parse_subtitles(vtt)
        self.assertEqual(len(again), 1)

    def test_sample_files(self):
        for name in ("sample_en.vtt", "sample_zh.srt"):
            with open(os.path.join(SAMPLE_DIR, name), encoding="utf-8") as f:
                cues = parse_subtitles(f.read())
            self.assertGreater(len(cues), 3, name)
            r = analyze(cues, max_chars=21, min_dwell_ms=1000, cps_max=17)
            self.assertGreater(r.stats["revised_cue_count"], 0)
            vtt = render_webvtt(r)
            self.assertEqual(len(parse_subtitles(vtt)), len(r.cues))

    def test_property_random_inputs(self):
        """随机输入下 token 守恒、时间单调、VTT 可回环。"""
        import random

        random.seed(42)
        alphabet = "中英文混排hello测试abc，。 "
        for trial in range(60):
            n = random.randint(1, 10)
            src, t = [], 0
            for _ in range(n):
                t += random.choice([0, 50, 250, 800])
                dur = random.randint(200, 4000)
                txt = "".join(random.choice(alphabet) for _ in range(random.randint(1, 36)))
                src.append(Cue(t, t + dur, txt))
                t += dur
            r = analyze(
                src,
                max_chars=random.choice([8, 15, 21, 42]),
                min_dwell_ms=random.choice([300, 1000, 2000]),
                cps_max=random.choice([10, 17, 30]),
            )
            before = [tok for c in src for tok in tokenize(c.text)]
            after = [
                tok for c in r.cues for tok in tokenize(c.text.replace("\n", " "))
            ]
            self.assertEqual(before, after, f"token mismatch in trial {trial}")
            for a, b in zip(r.cues, r.cues[1:]):
                self.assertLessEqual(a.start_ms, b.start_ms)
                self.assertLessEqual(a.end_ms, b.end_ms)
            self.assertEqual(len(parse_subtitles(render_webvtt(r))), len(r.cues))


if __name__ == "__main__":
    unittest.main(verbosity=2)
