"""无损 JSON 边界测试:快照、拒绝表、冻结语义。

覆盖 DSH json.spec.ts 的核心断言面:合法值通过、非法值拒绝、
快照与源分离、产物不可变。
"""

import math
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1]  # 包目录 core/session
_ROOT = Path(__file__).resolve().parents[4]  # 仓库根(CodeSage)
sys.path.insert(0, str(_CORE))

from core.session.src.json import (  # noqa: E402
    FrozenDict,
    FrozenList,
    is_json_value,
    snapshot_json_value,
)


def test_scalars_round_trip():
    assert is_json_value(None)
    assert is_json_value(True)
    assert is_json_value(False)
    assert is_json_value("文本")
    assert is_json_value(42)
    assert is_json_value(-17)
    assert is_json_value(3.5)
    assert is_json_value(0)
    assert is_json_value(0.0)  # 正的零合法


def test_nested_containers_round_trip():
    value = {"a": [1, {"b": "x"}], "c": None, "d": [True, False]}
    assert is_json_value(value)
    snap = snapshot_json_value(value)
    assert snap == value
    assert isinstance(snap, FrozenDict)
    assert isinstance(snap["a"], FrozenList)
    assert isinstance(snap["a"][1], FrozenDict)


def test_rejects_negative_zero():
    assert not is_json_value(-0.0)
    assert snapshot_json_value(-0.0) is None
    assert is_json_value(0.0)
    # 嵌套里的负零同样拒绝
    assert not is_json_value({"x": [-0.0]})


def test_rejects_non_finite():
    for bad in (math.inf, -math.inf, math.nan):
        assert not is_json_value(bad)
        assert snapshot_json_value(bad) is None


def test_rejects_exotic_types():
    assert not is_json_value(bytes([1]))
    assert not is_json_value({1, 2})
    assert not is_json_value((1, 2))
    assert not is_json_value(1 + 2j)


def test_rejects_class_instances():
    class Point:
        def __init__(self):
            self.x = 1

    assert not is_json_value(Point())
    assert not is_json_value({"p": Point()})


def test_rejects_subclassed_containers():
    class MyList(list):
        pass

    class MyDict(dict):
        pass

    assert not is_json_value(MyList([1]))
    assert not is_json_value(MyDict({"a": 1}))


def test_rejects_non_string_keys():
    assert not is_json_value({1: "a"})
    assert not is_json_value({None: "a"})


def test_rejects_circular():
    lst = []
    lst.append(lst)
    assert not is_json_value(lst)
    d = {}
    d["self"] = d
    assert not is_json_value(d)
    a = []
    b = [a]
    a.append(b)  # 跨引用环
    assert not is_json_value(a)


def test_shared_reference_is_legal():
    # 同一对象出现两处是共享,不是环 —— 合法
    shared = {"x": 1}
    assert is_json_value([shared, shared])
    snap = snapshot_json_value([shared, shared])
    assert snap == [shared, shared]
    assert snap[0] is not shared  # 快照分离


def test_snapshot_is_detached():
    value = {"list": [1, 2], "nested": {"k": "v"}}
    snap = snapshot_json_value(value)
    value["list"].append(3)
    value["nested"]["k"] = "changed"
    value["new"] = "added"
    assert snap == {"list": [1, 2], "nested": {"k": "v"}}


def test_frozen_structures_reject_writes():
    value = {"list": [1, {"k": "v"}], "plain": "ok"}
    snap = snapshot_json_value(value)
    # 顶层与嵌套都冻结
    for target in (snap, snap["list"], snap["list"][1]):
        for mutate in (
            lambda o: o.__setitem__("k", "v"),
            lambda o: o.clear(),
            lambda o: o.pop("k", None),
        ):
            try:
                mutate(target)
                raise AssertionError("frozen structure accepted a write")
            except TypeError:
                pass
    try:
        snap["list"].append(3)
        raise AssertionError("frozen list accepted append")
    except TypeError:
        pass
    try:
        snap["list"][0] = 9
        raise AssertionError("frozen list accepted item assignment")
    except TypeError:
        pass


def test_frozen_reenters_as_input():
    # 快照产物可以回流(种子来自已冻结的 events)
    snap = snapshot_json_value({"a": [1]})
    again = snapshot_json_value(snap)
    assert again == {"a": [1]}
    assert isinstance(again, FrozenDict)
    assert isinstance(again["a"], FrozenList)


def test_deep_nesting():
    value = {"a": {"b": {"c": {"d": {"e": [1, [2, [3]]]}}}}}
    assert is_json_value(value)
    assert snapshot_json_value(value) == value
