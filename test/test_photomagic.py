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

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import llm_handler
from llm_handler import LLMClient
from rt_processor import generate_pp3, run_rawtherapee, check_rt_cli, get_params
from grouping import (
    assign_group, build_group_system_prompt, build_group_user_prompt, group_of,
    merge_group_params, next_group_name, normalize_group_response,
    partition_targets, prune_groups, ungroup,
)


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
        #self.assertGreater(len(PARAMS), 6)
        self.assertGreater(len(get_params()), 6)


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
    """
    模拟 requests 的流式响应对象。

    与旧版 mock 的区别：
    - 增加了 status_code（request_json 会读它）
    - 增加了 iter_content()（request_json 用 stream=True 逐块读取）
    - 增加了 close()（request_json 在 finally 中调用）
    - 内部把返回体预序列化成完整 JSON，再在 iter_content 中按 chunk_size 切片，
      与真实 requests.Response 的行为一致
    """
    def __init__(self, content, status_code=200):
        self._content = content
        self.status_code = status_code
        self._body = json.dumps({
            "choices": [{"message": {"content": content}}]
        }).encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=4096):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass

    def json(self):
        return json.loads(self._body.decode("utf-8"))


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


# ============================ 组图（统一风格） ============================
class TestGroupData(unittest.TestCase):
    def test_assign_and_lookup(self):
        groups = assign_group({}, ["a.CR2", "b.CR2"], "组 1")
        self.assertEqual(group_of(groups, "a.CR2"), "组 1")
        self.assertIsNone(group_of(groups, "z.CR2"))

    def test_file_belongs_to_single_group(self):
        # 重新分组时应从旧组摘出来，避免一张图出现在两个组里
        groups = assign_group({}, ["a.CR2"], "组 1")
        groups = assign_group(groups, ["a.CR2"], "组 2")
        self.assertEqual(group_of(groups, "a.CR2"), "组 2")
        self.assertNotIn("组 1", groups)

    def test_assign_does_not_mutate_input(self):
        original = {}
        assign_group(original, ["a.CR2"], "组 1")
        self.assertEqual(original, {})

    def test_ungroup_removes_and_prunes_empty(self):
        groups = assign_group({}, ["a.CR2", "b.CR2"], "组 1")
        groups = ungroup(groups, ["a.CR2", "b.CR2"])
        self.assertEqual(groups, {})

    def test_prune_groups_drops_missing_files(self):
        groups = assign_group({}, ["a.CR2", "b.CR2"], "组 1")
        groups = prune_groups(groups, ["a.CR2"])
        self.assertEqual(groups, {"组 1": ["a.CR2"]})

    def test_next_group_name_avoids_collision(self):
        self.assertEqual(next_group_name({}), "组 1")
        self.assertEqual(next_group_name({"组 1": ["a"]}), "组 2")
        self.assertEqual(next_group_name({"组 2": ["a"]}), "组 1")


class TestPartitionTargets(unittest.TestCase):
    def setUp(self):
        self.groups = assign_group({}, ["a.CR2", "b.CR2"], "组 1")

    def test_disabled_unify_is_one_batch_per_file(self):
        batches = partition_targets(["a.CR2", "b.CR2"], self.groups, False)
        self.assertEqual(batches, [(None, ["a.CR2"]), (None, ["b.CR2"])])

    def test_enabled_unify_merges_group_members(self):
        batches = partition_targets(["a.CR2", "b.CR2"], self.groups, True)
        self.assertEqual(batches, [("组 1", ["a.CR2", "b.CR2"])])

    def test_unassigned_files_stay_individual(self):
        batches = partition_targets(["x.CR2", "a.CR2", "b.CR2", "y.CR2"],
                                    self.groups, True)
        self.assertEqual(batches, [
            (None, ["x.CR2"]),
            ("组 1", ["a.CR2", "b.CR2"]),
            (None, ["y.CR2"]),
        ])

    def test_only_checked_members_are_sent(self):
        batches = partition_targets(["a.CR2"], self.groups, True)
        self.assertEqual(batches, [("组 1", ["a.CR2"])])

    def test_two_groups_keep_order(self):
        groups = assign_group(self.groups, ["c.CR2", "d.CR2"], "组 2")
        batches = partition_targets(["c.CR2", "a.CR2", "d.CR2", "b.CR2"],
                                    groups, True)
        self.assertEqual(batches, [
            ("组 2", ["c.CR2", "d.CR2"]),
            ("组 1", ["a.CR2", "b.CR2"]),
        ])


class TestNormalizeGroupResponse(unittest.TestCase):
    PATHS = ["/photos/a.CR2", "/photos/b.CR2"]

    def test_style_with_positional_photos(self):
        raw = {"style": {"contrast": 15},
               "photos": [{"exposure": 0.3}, {}]}
        style, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(style, {"contrast": 15})
        self.assertEqual(per_image, [{"exposure": 0.3}, {}])

    def test_photos_keyed_by_filename(self):
        raw = {"style": {"contrast": 10},
               "photos": {"B.CR2": {"exposure": -0.5}}}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(per_image, [{}, {"exposure": -0.5}])

    def test_photos_keyed_by_index_string(self):
        raw = {"style": {}, "photos": {"0": {"exposure": 1.0},
                                       "1": {"exposure": -1.0}}}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(per_image, [{"exposure": 1.0}, {"exposure": -1.0}])

    def test_entries_with_file_field_match_by_name(self):
        raw = {"style": {"saturation": 5},
               "photos": [{"file": "b.CR2", "params": {"exposure": 0.7}},
                          {"file": "a.CR2", "params": {"exposure": -0.2}}]}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(per_image, [{"exposure": -0.2}, {"exposure": 0.7}])

    def test_bare_list_response(self):
        raw = [{"exposure": 0.1}, {"exposure": 0.2}]
        style, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(style, {})
        self.assertEqual(per_image, [{"exposure": 0.1}, {"exposure": 0.2}])

    def test_short_list_is_padded(self):
        raw = {"style": {"contrast": 5}, "photos": [{"exposure": 0.1}]}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(len(per_image), 2)
        self.assertEqual(per_image[1], {})

    def test_long_list_is_truncated(self):
        raw = {"photos": [{"exposure": 1}, {"exposure": 2}, {"exposure": 3}]}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(len(per_image), 2)

    def test_alias_keys(self):
        raw = {"shared": {"contrast": 8}, "images": [{"exposure": 0.4}, {}]}
        style, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(style, {"contrast": 8})
        self.assertEqual(per_image[0], {"exposure": 0.4})

    def test_plain_params_dict_treated_as_style(self):
        raw = {"contrast": 12, "saturation": 6}
        style, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(style, {"contrast": 12, "saturation": 6})
        self.assertEqual(per_image, [{}, {}])

    def test_garbage_returns_empty(self):
        style, per_image = normalize_group_response("not a json shape",
                                                    self.PATHS)
        self.assertEqual(style, {})
        self.assertEqual(per_image, [{}, {}])

    def test_non_dict_entries_ignored(self):
        raw = {"style": {}, "photos": ["nonsense", 3, None, {"exposure": 0.5}]}
        _, per_image = normalize_group_response(raw, self.PATHS)
        self.assertEqual(per_image, [{}, {}])


class TestMergeGroupParams(unittest.TestCase):
    def test_per_image_overrides_style(self):
        merged = merge_group_params({"contrast": 10, "saturation": 5},
                                    {"contrast": 25})
        self.assertEqual(merged, {"contrast": 25, "saturation": 5})

    def test_empty_extra_keeps_style(self):
        self.assertEqual(merge_group_params({"contrast": 10}, {}),
                         {"contrast": 10})

    def test_inputs_not_mutated(self):
        style = {"contrast": 10}
        merge_group_params(style, {"contrast": 1})
        self.assertEqual(style, {"contrast": 10})


class TestGroupPrompts(unittest.TestCase):
    def test_group_user_prompt_numbers_files_in_order(self):
        text = build_group_user_prompt("请给统一风格",
                                       ["/x/a.CR2", "/x/b.NEF", "/x/c.ARW"])
        self.assertIn("共 3 张照片", text)
        self.assertIn("第 1 张：a.CR2", text)
        self.assertIn("第 3 张：c.ARW", text)
        self.assertLess(text.index("第 1 张"), text.index("第 2 张"))
        self.assertIn("请给统一风格", text)

    def test_group_system_suffix_declares_contract(self):
        prompt = build_group_system_prompt("BASE")
        self.assertTrue(prompt.startswith("BASE"))
        self.assertIn('"style"', prompt)
        self.assertIn('"photos"', prompt)
        self.assertIn("长度必须等于", prompt)


class TestRequestJsonMulti(unittest.TestCase):
    def _call(self, content, paths, method="multi"):
        with mock.patch.object(llm_handler, "requests") as m_req, \
             mock.patch.object(llm_handler, "load_image_base64_from_raw",
                               return_value="AAAA") as m_load:
            m_req.post.return_value = _FakeResp(content)
            client = LLMClient("http://x", "key", "m")
            if method == "multi":
                result = client.request_json_multi("sys", "u", paths)
            else:
                result = client.request_json("sys", "u", paths[0])
        return result, m_req, m_load

    def test_sends_one_image_part_per_path_in_order(self):
        _, m_req, m_load = self._call(
            '{"style": {}, "photos": []}', ["a.CR2", "b.CR2", "c.CR2"])
        content = m_req.post.call_args.kwargs["json"]["messages"][1]["content"]
        images = [c for c in content if c["type"] == "image_url"]
        self.assertEqual(len(images), 3)
        self.assertEqual([c["image_url"]["url"] for c in images],
                         ["data:image/jpeg;base64,AAAA"] * 3)
        self.assertEqual(m_load.call_count, 3)
        called = [c.args[0] for c in m_load.call_args_list]
        self.assertEqual(called, ["a.CR2", "b.CR2", "c.CR2"])
        # 文本在前，图片依次在后
        self.assertEqual(content[0]["type"], "text")

    def test_group_response_parsed(self):
        raw, _, _ = self._call(
            '{"style": {"contrast": 9}, "photos": [{"exposure": 0.2}, {}]}',
            ["a.CR2", "b.CR2"])
        style, per_image = normalize_group_response(raw, ["a.CR2", "b.CR2"])
        self.assertEqual(style, {"contrast": 9})
        self.assertEqual(per_image[0], {"exposure": 0.2})

    def test_empty_paths_rejected(self):
        with self.assertRaises(ValueError):
            LLMClient("http://x", "key", "m").request_json_multi("s", "u", [])

    def test_single_request_json_still_sends_one_image(self):
        # 回归：单张调用（照片评价页）行为不变
        result, m_req, m_load = self._call('{"exposure": 0.5}', ["a.CR2"],
                                           method="single")
        content = m_req.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(len([c for c in content
                              if c["type"] == "image_url"]), 1)
        self.assertEqual(result, {"exposure": 0.5})
        self.assertEqual(m_load.call_count, 1)


class TestGroupPipeline(unittest.TestCase):
    """组图返回 -> 合并 -> generate_pp3 的端到端衔接。"""
    def test_style_and_tweak_reach_pp3(self):
        raw = {"style": {"contrast": 18, "saturation": 8},
               "photos": [{"exposure": 0.5}, {"exposure": -0.5}]}
        paths = ["a.CR2", "b.CR2"]
        style, per_image = normalize_group_response(raw, paths)
        out_a = render(merge_group_params(style, per_image[0]))
        out_b = render(merge_group_params(style, per_image[1]))
        self.assertIn("Contrast=18", out_a)
        self.assertIn("Compensation=0.5", out_a)
        self.assertIn("Compensation=-0.5", out_b)
        self.assertIn("Saturation=8", out_b)


if __name__ == "__main__":
    unittest.main()