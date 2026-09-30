"""
组图（多图统一风格）逻辑。

本模块只包含纯函数，不依赖 PyQt6 / 网络，便于单元测试：
  - 分组数据的增删查（groups 是 {组名: [文件路径, ...]}）
  - 把勾选的文件按分组切分成"一次性提交给 LLM"的批次
  - 解析组图模式的 LLM 返回（style + 逐张微调）
  - 组图提示词拼装

组图模式的约定格式：

    {
      "style":  { ...整套照片共用的参数... },
      "photos": [ { ...第 1 张的差异化微调... }, { ...第 2 张... }, ... ]
    }

其中 "photos" 与发送顺序一一对应，元素可为 {}。解析层对常见的
变体（photos 为字典、缺少 style、直接返回数组等）做了兼容。
"""
from pathlib import Path

# 单次请求包含过多图片时，token 成本与超时风险都会上升
GROUP_SOFT_LIMIT = 8

# style / 逐张参数 可能使用的别名键
_STYLE_KEYS = ("style", "shared", "common", "common_style", "global",
               "base", "base_style", "统一风格", "共享参数", "统一参数")
_ITEMS_KEYS = ("photos", "images", "adjustments", "per_image", "per_photo",
               "items", "results", "list", "图片", "逐张参数")
# 单个条目里真正装参数的键
_PARAM_KEYS = ("params", "parameters", "adjustments", "settings", "values",
               "参数", "微调")
# 条目里标识文件名的键
_FILE_KEYS = ("file", "filename", "file_name", "name", "path", "photo",
              "图片", "文件名")


# ----------------------------- 分组数据 -----------------------------
def group_of(groups: dict, path: str):
    """返回文件所属组名，未分组返回 None。"""
    for name, members in (groups or {}).items():
        if path in members:
            return name
    return None


def assign_group(groups: dict, paths, name: str) -> dict:
    """把 paths 归入组 name（返回新的 groups，不改动入参）。"""
    name = (name or "").strip()
    result = {k: list(v) for k, v in (groups or {}).items()}
    if not name:
        return result

    targets = list(paths or [])
    # 先从其它组里摘出来，保证一个文件只属于一个组
    for key in list(result):
        result[key] = [p for p in result[key] if p not in targets]
    members = result.setdefault(name, [])
    for p in targets:
        if p not in members:
            members.append(p)
    return _prune(result)


def ungroup(groups: dict, paths) -> dict:
    """把 paths 从所有组中移除（返回新的 groups）。"""
    targets = set(paths or [])
    result = {k: [p for p in v if p not in targets]
              for k, v in (groups or {}).items()}
    return _prune(result)


def prune_groups(groups: dict, existing_paths) -> dict:
    """丢弃已不在列表中的文件（以及因此变空的组）。"""
    valid = set(existing_paths or [])
    result = {k: [p for p in v if p in valid] for k, v in (groups or {}).items()}
    return _prune(result)


def _prune(groups: dict) -> dict:
    return {k: v for k, v in groups.items() if v}


def next_group_name(groups: dict) -> str:
    """生成一个不冲突的默认组名，如 "组 1"。"""
    existing = set((groups or {}).keys())
    i = 1
    while f"组 {i}" in existing:
        i += 1
    return f"组 {i}"


# ----------------------------- 批次切分 -----------------------------
def partition_targets(targets, groups: dict, unify_enabled: bool):
    """
    把待处理文件切分成提交给 LLM 的批次。

    返回 [(组名或 None, [路径, ...]), ...]，保持 targets 的原有顺序：
      - unify_enabled=False：每张图一个批次（组名一律为 None）
      - unify_enabled=True ：同一组的文件合成一个批次，未分组文件各自一批
    """
    paths = list(targets or [])
    if not unify_enabled:
        return [(None, [p]) for p in paths]

    batches = []
    index = {}
    for p in paths:
        name = group_of(groups, p)
        if name is None:
            batches.append((None, [p]))
            continue
        if name not in index:
            index[name] = len(batches)
            batches.append((name, []))
        batches[index[name]][1].append(p)
    return batches


def merge_group_params(style: dict, extra: dict) -> dict:
    """统一风格 + 单张微调；单张给出的键优先。"""
    merged = dict(style or {})
    for k, v in (extra or {}).items():
        merged[k] = v
    return merged


# ----------------------------- 返回解析 -----------------------------
def _as_param_dict(value):
    """把一个条目转成参数字典；无法识别返回 None。"""
    if not isinstance(value, dict):
        return None
    for key in _PARAM_KEYS:
        inner = value.get(key)
        if isinstance(inner, dict):
            return dict(inner)
    # 条目本身就是参数（去掉纯标识用的键后仍为空则视为 {}）
    return {k: v for k, v in value.items() if k.lower() not in _FILE_KEYS}


def _entry_name(value):
    if not isinstance(value, dict):
        return None
    for key in _FILE_KEYS:
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def normalize_group_response(raw, paths):
    """
    把 LLM 的组图返回规整成 (style, [逐张参数, ...])。

    - style：整套照片共用的参数字典
    - 逐张参数：长度恒等于 len(paths)，缺失的位置补 {}

    兼容多种返回形态；无法识别时返回 ({}, [{}, ...])。
    """
    paths = list(paths or [])
    style = {}
    per_image = [{} for _ in paths]

    items = None

    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in _STYLE_KEYS:
            value = raw.get(key)
            if isinstance(value, dict):
                style = dict(value)
                break

        for key in _ITEMS_KEYS:
            value = raw.get(key)
            if isinstance(value, (list, dict)):
                items = value
                break

        if items is None:
            # 既没有 style 也没有 photos：把整个对象当作统一风格
            if not style:
                style = dict(raw)
            return style, per_image
    else:
        return style, per_image

    if isinstance(items, dict):
        _fill_from_mapping(items, paths, per_image)
    else:
        _fill_from_sequence(items, paths, per_image)
    return style, per_image


def _fill_from_sequence(seq, paths, per_image):
    for i, entry in enumerate(seq):
        params = _as_param_dict(entry)
        if params is None:
            continue
        name = _entry_name(entry)
        idx = None
        if name is not None:
            idx = _match_by_name(name, paths)
        if idx is None:
            idx = i
        if 0 <= idx < len(per_image):
            per_image[idx] = params


def _fill_from_mapping(mapping, paths, per_image):
    """photos 是字典时：优先按文件名匹配，其次按数字键当作下标。"""
    for key, entry in mapping.items():
        params = _as_param_dict(entry)
        if params is None:
            continue
        idx = _match_by_name(str(key), paths)
        if idx is None:
            try:
                idx = int(str(key))
            except ValueError:
                idx = None
        if idx is not None and 0 <= idx < len(per_image):
            per_image[idx] = params


def _match_by_name(name, paths):
    """按文件名（忽略大小写与目录）匹配下标；匹配不到返回 None。"""
    name = name.strip()
    if not name:
        return None
    target = Path(name).name.lower()
    for i, p in enumerate(paths):
        if Path(p).name.lower() == target:
            return i
    return None


# ----------------------------- 提示词 -----------------------------
GROUP_SYSTEM_SUFFIX = """

【组图模式 · 重要】
这一次我会按顺序发给你多张照片，它们属于同一组，需要统一的整体风格。
请严格返回如下 JSON 结构：
{
  "style": { ...整套照片共用的参数... },
  "photos": [ { ...第 1 张的差异微调，可为 {}... }, { ...第 2 张... } ]
}
要求：
1. "photos" 的长度必须等于我发送的照片数量，顺序与我给出的编号一致，不要包含文件名。
2. 整套共用的风格放在 "style"；某张照片独有的偏差（如曝光补偿、白平衡）放在对应下标的 "photos" 项里，且只写偏离统一风格的键。
3. 某张不需要单独调整时，对应下标写 {}。photos 数组不可省略。
4. 仍然只返回 JSON，不要输出任何其他文字。"""


def build_group_system_prompt(system_prompt: str) -> str:
    return (system_prompt or "") + GROUP_SYSTEM_SUFFIX


def build_group_user_prompt(base_user_prompt: str, paths) -> str:
    """把"第 N 张：文件名"的清单拼进 user prompt，让编号与图片顺序对应。"""
    paths = list(paths or [])
    lines = [
        f"本次共 {len(paths)} 张照片，属于同一组，请给出统一风格并逐张微调。",
        "照片顺序（与我随消息发送的图片顺序一致）：",
    ]
    for i, p in enumerate(paths, start=1):
        lines.append(f"第 {i} 张：{Path(p).name}")

    base = (base_user_prompt or "").strip()
    if base:
        lines.append("")
        lines.append(f"补充要求：{base}")
    return "\n".join(lines)
