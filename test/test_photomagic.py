"""
PhotoMagic 代码变更的单元测试（标准库 unittest，无额外依赖）。

覆盖：
- rt_processor.generate_pp3：参数表 -> .pp3（键名修正、钳位、分段合并、Enabled、忽略未知键）
- rt_processor.run_rawtherapee：失败时把 stderr 抛出
- rt_processor.check_rt_cli
- llm_handler.LLMClient.request_json：JSON 鲁棒解析
- 模块衔接：LLM 返回的 dict -> generate_pp3

运行： python -m unittest test_photomagic       （或 python -m unittest 自动发现）
"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import llm_handler
from llm_handler import LLMClient
from rt_processor import generate_pp3, run_rawtherapee, check_rt_cli, PARAMS


def render(params):
    """生成 pp3 并返回其文本内容，便于断言。"""
    fd, path = tempfile.mkstemp(suffix=".pp3")
    os.close(fd)
    generate_pp3(params, path)
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestGeneratePP3(unittest.TestCase):
    def test_empty_params_only_version(self):
        out = render({})
        self.assertIn("[Version]", out)
        self.assertNotIn("[Exposure]", out)  # 没有参数就不该出现工具段

    def test_exposure_key_is_compensation(self):
        # 回归：旧代码写成 Exposure=，RT 会忽略；正确键是 Compensation
        out = render({"exposure": 0.7})
        self.assertIn("Compensation=0.7", out)
        self.assertNotIn("\nExposure=", out)

    def test_contrast_saturation_grouped_in_exposure(self):
        # 回归：旧代码放进 RT 不存在的 [Lab Adjustments] 段
        out = render({"contrast": 25, "saturation": 10})
        self.assertIn("[Exposure]", out)
        self.assertIn("Contrast=25", out)
        self.assertIn("Saturation=10", out)
        self.assertNotIn("[Lab Adjustments]", out)

    def test_shadows_highlights_section_and_enabled(self):
        # 回归：段名是 & 不是 /，且必须 Enabled=true 才生效
        out = render({"highlights": 40, "shadows": 30})
        self.assertIn("[Shadows & Highlights]", out)
        self.assertNotIn("[Shadows/Highlights]", out)
        self.assertIn("Enabled=true", out)
        self.assertIn("Highlights=40", out)
        self.assertIn("Shadows=30", out)

    def test_clamping_to_range(self):
        out = render({"exposure": 99, "contrast": -999, "saturation": 5})
        self.assertIn("Compensation=3.0", out)
        self.assertIn("Contrast=-100", out)

    def test_unknown_keys_ignored(self):
        out = render({"exposure": 1.0, "totally_unknown": 1, "WhiteBalance": {}})
        self.assertNotIn("totally_unknown", out)
        self.assertNotIn("WhiteBalance", out)

    def test_enabled_only_where_needed(self):
        out = render({"exposure": 1, "temperature": 5500, "tint": 1.0, "highlights": 10})
        exposure_block = out.split("[Exposure]")[1].split("[", 1)[0]
        wb_block = out.split("[White Balance]")[1].split("[", 1)[0]
        self.assertNotIn("Enabled", exposure_block)
        self.assertNotIn("Enabled", wb_block)

    def test_white_balance_defaults(self):
        out = render({"temperature": 5600, "tint": 1.05})
        self.assertIn("Setting=Custom", out)
        self.assertIn("Temperature=5600", out)
        self.assertIn("Green=1.05", out)

    def test_sharpening_companion_keys(self):
        # 反卷积锐化除强度外还需要 Method 与迭代次数
        out = render({"sharpen_amount": 80})
        self.assertIn("Method=rld", out)
        self.assertIn("DeconvIterations=40", out)
        self.assertIn("DeconvAmount=80", out)
        self.assertIn("Enabled=true", out)

    def test_param_count_expanded(self):
        self.assertGreater(len(PARAMS), 6)


class TestRunRawTherapee(unittest.TestCase):
    def _fake(self, returncode, stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode,
                                           stdout="", stderr=stderr)

    def test_raises_with_stderr_on_failure(self):
        # 失败时必须把 RT 的报错带出来，否则无法排查
        with mock.patch("rt_processor.subprocess.run",
                        return_value=self._fake(1, "boom")):
            with self.assertRaises(RuntimeError) as ctx:
                run_rawtherapee("in.raw", "p.pp3", tempfile.mkdtemp())
        self.assertIn("boom", str(ctx.exception))

    def test_no_raise_on_success(self):
        with mock.patch("rt_processor.subprocess.run",
                        return_value=self._fake(0)):
            run_rawtherapee("in.raw", "p.pp3", tempfile.mkdtemp())


class TestCheckRtCli(unittest.TestCase):
    def test_found(self):
        with mock.patch("rt_processor.shutil.which",
                        return_value="/usr/bin/rawtherapee-cli"):
            self.assertTrue(check_rt_cli())

    def test_not_found(self):
        with mock.patch("rt_processor.shutil.which", return_value=None):
            self.assertFalse(check_rt_cli())


class _FakeResp:
    """模拟 requests 的响应对象。"""
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def parse(content):
    """mock 掉网络请求与图片加载，仅测 request_json 的 JSON 解析。"""
    with mock.patch.object(llm_handler, "requests") as m_req, \
         mock.patch.object(llm_handler, "load_image_base64_from_raw",
                           return_value="AAAA"):
        m_req.post.return_value = _FakeResp(content)
        return LLMClient("http://x", "key", "m").request_json("s", "u", "fake.jpg")


class TestRequestJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse('{"exposure": 0.5, "contrast": 20}'),
                         {"exposure": 0.5, "contrast": 20})

    def test_json_fenced_with_label(self):
        self.assertEqual(parse('```json\n{"exposure": 0.5}\n```'),
                         {"exposure": 0.5})

    def test_json_fenced_without_label(self):
        self.assertEqual(parse('```\n{"exposure": 0.5}\n```'),
                         {"exposure": 0.5})

    def test_json_with_surrounding_prose(self):
        content = '建议如下：\n{"exposure": 0.5, "saturation": 10}\n希望有帮助'
        self.assertEqual(parse(content), {"exposure": 0.5, "saturation": 10})

    def test_no_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse("抱歉，我无法分析这张照片。")


class TestPipeline(unittest.TestCase):
    """模块衔接：LLM 返回的 dict 能否正确喂给 generate_pp3。"""
    def test_llm_output_flows_into_pp3(self):
        params = parse('```json\n{"exposure": 1.0, "highlights": 50}\n```')
        out = render(params)
        self.assertIn("Compensation=1.0", out)
        self.assertIn("[Shadows & Highlights]", out)
        self.assertIn("Highlights=50", out)


if __name__ == "__main__":
    unittest.main()
