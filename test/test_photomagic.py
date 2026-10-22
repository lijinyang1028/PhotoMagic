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
import logging
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import llm_handler
import log_setup
import settings
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


# ============================ 日志（INFO / 保留最近 5 次） ============================
class TestLogSetup(unittest.TestCase):
    def setUp(self):
        # 每个用例都从干净的日志状态开始，避免相互污染
        log_setup._configured = False
        log_setup._current_log_file = None
        log_setup._current_log_dir = None
        # 用系统临时目录，避免在仓库里留下测试产物。
        # 注意：这里用 os.mkdir 而不是 tempfile.mkdtemp —— 后者在 Windows 上
        # 会以 0700 的 ACL 建目录，在一些受限环境下反而写不进去。
        self.tmp = os.path.join(
            tempfile.gettempdir(),
            f"photomagic_logs_{os.getpid()}_{uuid.uuid4().hex[:8]}")
        os.mkdir(self.tmp)
        self.root = logging.getLogger()
        self._saved_level = self.root.level

    def tearDown(self):
        log_setup._remove_own_handlers(self.root)
        self.root.setLevel(self._saved_level)
        log_setup._configured = False
        log_setup._current_log_file = None
        log_setup._current_log_dir = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _flush(self):
        for handler in self.root.handlers:
            handler.flush()

    def test_writes_info_to_photomagic_dir(self):
        path = log_setup.setup_logging(
            log_dir=self.tmp, console=False, force=True)
        self.assertEqual(os.path.dirname(str(path)), self.tmp)
        self.assertTrue(os.path.basename(str(path)).startswith("photomagic_"))
        self.assertTrue(os.path.basename(str(path)).endswith(".log"))

        logging.getLogger("test.module").info("hello-info-中文")
        self._flush()
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("hello-info-中文", text)
        self.assertIn("INFO", text)
        self.assertIn("test.module", text)

    def test_default_level_is_info(self):
        log_setup.setup_logging(log_dir=self.tmp, console=False, force=True)
        self.assertEqual(self.root.level, logging.INFO)
        logger = logging.getLogger("test.level")
        logger.debug("debug-应被丢弃")
        logger.info("info-应保留")
        self._flush()
        text = Path(log_setup.get_log_file()).read_text(encoding="utf-8")
        self.assertIn("info-应保留", text)
        self.assertNotIn("debug-应丢弃", text)

    def test_prune_keeps_recent_five(self):
        # 造 8 个不同时间的日志，只应保留最新的 5 个
        created = []
        for i in range(8):
            p = Path(self.tmp) / f"photomagic_2024010{i}_000000_100{i}.log"
            p.write_text(f"log {i}", encoding="utf-8")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            created.append(p)

        removed = log_setup.prune_log_files(5, self.tmp)
        remaining = log_setup.list_log_files(self.tmp)

        self.assertEqual(len(remaining), 5)
        self.assertEqual(len(removed), 3)
        # 最旧的 3 个被删，最新的 5 个还在
        self.assertEqual(set(removed), set(created[:3]))
        self.assertEqual(remaining[0], created[7])

    def test_setup_keeps_only_five_runs(self):
        # 预置 6 个旧日志，再启动一次：总数仍为 5
        for i in range(6):
            p = Path(self.tmp) / f"photomagic_2024010{i}_000000_100{i}.log"
            p.write_text("old", encoding="utf-8")
            os.utime(p, (500_000 + i, 500_000 + i))

        log_setup.setup_logging(
            log_dir=self.tmp, console=False, keep=5, force=True)
        files = log_setup.list_log_files(self.tmp)
        self.assertEqual(len(files), 5)
        self.assertIn(Path(log_setup.get_log_file()), files)

    def test_repeated_setup_is_idempotent(self):
        first = log_setup.setup_logging(
            log_dir=self.tmp, console=False, force=True)
        second = log_setup.setup_logging(log_dir=self.tmp, console=False)
        self.assertEqual(str(first), str(second))

    def test_unwritable_dir_degrades_without_crashing(self):
        # 目录不可写时不改写到别处、也不抛异常：返回 None，只输出到控制台
        blocker = Path(self.tmp) / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")     # 文件冒充目录 -> mkdir 失败
        result = log_setup.setup_logging(
            log_dir=str(blocker / "logs"), console=False, force=True)
        self.assertIsNone(result)
        self.assertIsNone(log_setup.get_log_file())

    def test_default_dir_is_photomagic_logs(self):
        # 默认（且唯一）的落盘位置就是 ~/.photomagic/logs
        self.assertEqual(log_setup._resolve_dir(), Path(log_setup.LOG_DIR))
        self.assertEqual(Path(log_setup.LOG_DIR).name, "logs")
        self.assertEqual(Path(log_setup.LOG_DIR).parent.name, ".photomagic")


# ============================ 多套 API 配置方案 ============================
def _fake_keyring(store):
    """用 dict 模拟 keyring 的 get/set/delete_password。"""
    fake = mock.Mock()

    def _get(service, user):
        return store.get(user)

    def _set(service, user, value):
        store[user] = value

    def _delete(service, user):
        if user not in store:
            raise KeyError(user)
        del store[user]

    fake.get_password.side_effect = _get
    fake.set_password.side_effect = _set
    fake.delete_password.side_effect = _delete
    return fake


class TestApiProfiles(unittest.TestCase):
    """多套「模型 + API Key」配置方案：迁移、切换、隔离、校验、删除。"""

    def setUp(self):
        # 用系统临时目录，避免污染真实 ~/.photomagic（也不在仓库留产物）
        self.tmp = Path(tempfile.gettempdir()) / (
            f"photomagic_settings_{os.getpid()}_{uuid.uuid4().hex[:8]}")
        os.mkdir(self.tmp)
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

        self._patches = [
            mock.patch.object(settings, "CONFIG_DIR", self.tmp),
            mock.patch.object(settings, "SETTINGS_FILE",
                              self.tmp / "settings.json"),
            mock.patch.object(settings, "CUSTOM_PARAMS_FILE",
                              self.tmp / "custom_params.json"),
            mock.patch.object(settings, "LOG_DIR", self.tmp / "logs"),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

        settings._MEMORY_API_KEYS.clear()
        self.addCleanup(settings._MEMORY_API_KEYS.clear)

        self.keyring_store = {}
        self._keyring_patches = [
            mock.patch.object(settings, "keyring",
                              _fake_keyring(self.keyring_store)),
            mock.patch.object(settings, "keyring_available",
                              return_value=True),
        ]
        for patch in self._keyring_patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _write_settings(self, data):
        with open(settings.SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_legacy_single_config_becomes_default_profile(self):
        # 旧版 settings.json 只有顶层 api_base / model，读出来应自动成为
        # 一套「默认配置」，顶层字段与当前方案保持一致（旧代码无需修改）
        self._write_settings({
            "api_base": "https://old.example/v1",
            "model": "old-model",
            "parallel": 5,
        })
        data = settings.load_settings()
        self.assertEqual(len(data["api_profiles"]), 1)
        profile = data["api_profiles"][0]
        self.assertEqual(profile["id"], settings.DEFAULT_PROFILE_ID)
        self.assertEqual(profile["name"], settings.DEFAULT_PROFILE_NAME)
        self.assertEqual(profile["api_base"], "https://old.example/v1")
        self.assertEqual(profile["model"], "old-model")
        self.assertEqual(data["active_api_profile"],
                         settings.DEFAULT_PROFILE_ID)
        self.assertEqual(data["api_base"], "https://old.example/v1")
        self.assertEqual(data["model"], "old-model")
        self.assertEqual(data["parallel"], 5)

    def test_ensure_migrates_legacy_key_and_persists_profiles(self):
        # 启动迁移：旧配置写盘为方案，旧钥匙串条目迁移到默认方案并被清理
        self._write_settings({
            "api_base": "https://old.example/v1", "model": "old-model"})
        self.keyring_store[settings.KEYRING_USER] = "sk-legacy"

        self.assertTrue(settings.ensure_api_profiles())

        with open(settings.SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["active_api_profile"],
                         settings.DEFAULT_PROFILE_ID)
        self.assertEqual(saved["api_profiles"][0]["id"],
                         settings.DEFAULT_PROFILE_ID)
        self.assertEqual(self.keyring_store.get("api_key:default"),
                         "sk-legacy")
        self.assertNotIn(settings.KEYRING_USER, self.keyring_store)
        self.assertEqual(settings.get_api_key(), "sk-legacy")

        # 幂等：再次迁移不写盘、不再改动钥匙串
        self.assertFalse(settings.ensure_api_profiles())
        self.assertEqual(self.keyring_store.get("api_key:default"),
                         "sk-legacy")

    def test_legacy_key_readable_before_migration(self):
        # 还没迁移时，单一默认方案仍能读到旧钥匙串条目
        self.keyring_store[settings.KEYRING_USER] = "sk-legacy"
        self.assertEqual(settings.get_api_key(), "sk-legacy")
        self.assertFalse(settings.SETTINGS_FILE.exists())

    def test_adding_second_profile_migrates_legacy_key(self):
        # 首次新增第二套方案前，旧 Key 必须先落到默认方案，
        # 否则「单一方案回退」失效会让旧 Key 看起来丢失
        self.keyring_store[settings.KEYRING_USER] = "sk-legacy"
        profile = settings.save_api_profile("备用", "https://b/v1", "model-b")
        self.assertNotEqual(profile["id"], settings.DEFAULT_PROFILE_ID)
        self.assertEqual(self.keyring_store.get("api_key:default"),
                         "sk-legacy")
        self.assertEqual(settings.get_api_key(settings.DEFAULT_PROFILE_ID),
                         "sk-legacy")
        self.assertEqual(settings.get_api_key(profile["id"]), "")

    def test_multiple_profiles_keep_separate_models_and_keys(self):
        p1 = settings.save_api_profile(
            "官方", "https://a.example/v1", "model-a",
            profile_id=settings.DEFAULT_PROFILE_ID)
        p2 = settings.save_api_profile(
            "备用", "https://b.example/v1", "model-b")
        self.assertNotEqual(p1["id"], p2["id"])
        self.assertTrue(settings.set_api_key("key-a", p1["id"]))
        self.assertTrue(settings.set_api_key("key-b", p2["id"]))

        self.assertEqual(len(settings.list_api_profiles()), 2)
        # save_api_profile 总把保存的方案设为当前
        self.assertEqual(settings.get_active_profile_id(), p2["id"])
        self.assertEqual(settings.get_api_key(), "key-b")

        # 切换后顶层 api_base / model 跟随当前方案（旧代码直接读这两个字段）
        self.assertTrue(settings.set_active_api_profile(p1["id"]))
        data = settings.load_settings()
        self.assertEqual(data["api_base"], "https://a.example/v1")
        self.assertEqual(data["model"], "model-a")
        self.assertEqual(settings.get_api_key(), "key-a")
        self.assertEqual(settings.get_api_key(p2["id"]), "key-b")

        # 不存在的方案不能切换
        self.assertFalse(settings.set_active_api_profile("missing"))

    def test_profile_name_validation(self):
        settings.save_api_profile(
            "Official", "https://a.example/v1", "model-a",
            profile_id=settings.DEFAULT_PROFILE_ID)
        p2 = settings.save_api_profile(
            "备用", "https://b.example/v1", "model-b")

        with self.assertRaises(ValueError):
            settings.save_api_profile("   ", "https://x/v1", "m")
        with self.assertRaises(ValueError):
            settings.save_api_profile("备用", "https://x/v1", "m")
        with self.assertRaises(ValueError):
            # 名称重复不区分大小写
            settings.save_api_profile("official", "https://x/v1", "m")
        with self.assertRaises(ValueError):
            settings.rename_api_profile(p2["id"], "OFFICIAL")
        with self.assertRaises(ValueError):
            settings.save_api_profile("X", "https://x/v1", "m",
                                      profile_id="missing")
        with self.assertRaises(ValueError):
            settings.rename_api_profile(p2["id"], "  ")

        renamed = settings.rename_api_profile(p2["id"], "第二套")
        self.assertEqual(renamed["name"], "第二套")
        self.assertEqual(settings.get_api_profile(p2["id"])["name"], "第二套")

    def test_delete_profile_removes_key_and_reassigns_active(self):
        p1 = settings.save_api_profile(
            "官方", "https://a.example/v1", "model-a",
            profile_id=settings.DEFAULT_PROFILE_ID)
        p2 = settings.save_api_profile(
            "备用", "https://b.example/v1", "model-b")
        settings.set_api_key("key-a", p1["id"])
        settings.set_api_key("key-b", p2["id"])
        settings.set_active_api_profile(p1["id"])

        self.assertTrue(settings.delete_api_profile(p1["id"]))
        self.assertIsNone(settings.get_api_profile(p1["id"]))
        self.assertNotIn("api_key:default", self.keyring_store)
        self.assertIn(f"api_key:{p2['id']}", self.keyring_store)
        self.assertEqual(settings.get_active_profile_id(), p2["id"])
        self.assertEqual(settings.get_active_profile()["name"], "备用")

        # 至少保留一套方案，最后一套不允许删除
        self.assertFalse(settings.delete_api_profile(p2["id"]))
        self.assertEqual(len(settings.list_api_profiles()), 1)

    def test_memory_storage_when_keyring_unavailable(self):
        with mock.patch.object(settings, "keyring_available",
                               return_value=False):
            p1 = settings.save_api_profile(
                "官方", "https://a.example/v1", "model-a",
                profile_id=settings.DEFAULT_PROFILE_ID)
            p2 = settings.save_api_profile(
                "备用", "https://b.example/v1", "model-b")
            self.assertFalse(settings.set_api_key("mem-a", p1["id"]))
            self.assertFalse(settings.set_api_key("mem-b", p2["id"]))
            self.assertEqual(settings.get_api_key(p1["id"]), "mem-a")
            self.assertEqual(settings.get_api_key(p2["id"]), "mem-b")
            self.assertNotIn(f"api_key:{p1['id']}", self.keyring_store)
            self.assertFalse(settings.delete_api_key(p1["id"]))
            self.assertEqual(settings.get_api_key(p1["id"]), "")

    def test_normalize_repairs_bad_profile_data(self):
        # 手工编辑坏掉的 settings.json：重复 / 空 id、同名、非法 active
        # 都应在读取时被自动修复，不能让 UI 出现无法区分的方案
        self._write_settings({
            "api_profiles": [
                {"id": "dup", "name": "Same",
                 "api_base": "https://a.example/v1", "model": "m"},
                {"id": "dup", "name": "same",
                 "api_base": " https://b.example/v1 ", "model": "m2"},
                {"id": "", "name": "", "api_base": None, "model": None},
            ],
            "active_api_profile": "missing",
        })
        data = settings.load_settings()
        profiles = data["api_profiles"]
        self.assertEqual(len(profiles), 3)
        self.assertEqual(len({p["id"] for p in profiles}), 3)
        self.assertEqual(len({p["name"].casefold() for p in profiles}), 3)
        self.assertEqual(profiles[1]["api_base"], "https://b.example/v1")
        self.assertEqual(data["active_api_profile"], profiles[0]["id"])

    def test_keyring_read_failure_falls_back_to_memory(self):
        # 钥匙串瞬时读取失败时，本次会话写入内存的 Key 仍应可用
        settings.save_api_profile(
            "官方", "https://a.example/v1", "model-a",
            profile_id=settings.DEFAULT_PROFILE_ID)
        settings._MEMORY_API_KEYS[settings.DEFAULT_PROFILE_ID] = "mem-key"
        with mock.patch.object(settings.keyring, "get_password",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(settings.get_api_key(), "mem-key")

    def test_keyring_write_failure_key_still_usable_in_session(self):
        # 钥匙串写失败时 set_api_key 返回 False，但 Key 本次会话应可用
        settings.save_api_profile(
            "官方", "https://a.example/v1", "model-a",
            profile_id=settings.DEFAULT_PROFILE_ID)
        with mock.patch.object(settings.keyring, "set_password",
                               side_effect=RuntimeError("boom")):
            self.assertFalse(
                settings.set_api_key("mem-key", settings.DEFAULT_PROFILE_ID))
        self.assertEqual(settings.get_api_key(), "mem-key")

    def test_save_settings_keeps_profiles(self):
        profile = settings.save_api_profile("官方", "https://a/v1", "model-a")
        profile_ids = [p["id"] for p in settings.list_api_profiles()]

        data = settings.load_settings()
        data["parallel"] = 7
        settings.save_settings(data)

        reloaded = settings.load_settings()
        self.assertEqual(reloaded["parallel"], 7)
        self.assertEqual([p["id"] for p in reloaded["api_profiles"]],
                         profile_ids)
        self.assertEqual(reloaded["active_api_profile"], profile["id"])


if __name__ == "__main__":
    unittest.main()