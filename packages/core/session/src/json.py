"""无损 JSON:校验、快照、冻结 —— 会话数据可持久化的边界。

会话日志是唯一事实源,而事件数据必须能逐字节往返 JSON。DSH 用
Lossless-JSON 定义这条边界:只有 JSON 无损往返的值(标量、数组、
普通对象)才有资格进入日志;BigInt、undefined、Infinity、NaN、
负零、Map/Set/Date、类实例、循环引用、稀疏数组都会被拒绝 ——
它们要么在 JSON 序列化时丢失信息,要么会在重放时产生与写入时
不同的结构。校验失败的地方必须是写入点(append),而不是落盘时。

本模块同时提供三种能力,对应 DSH 的三层防护:

1. is_json_value —— 纯校验:一个值是否跨 JSON 往返无损;
2. snapshot_json_value —— 校验 + 拷贝一次递归完成:读取一遍就
   同时完成校验与快照,状态型 getter 不可能在校验与拷贝之间换值
   (DSH 注释的原话:读一遍,而非两遍);
3. 冻结结构 —— 快照产物直接以不可变形态交付:任何后续写操作抛
   TypeError,模拟 JS 的 deepFreeze 语义。Python 没有内建冻结,
   FrozenDict/FrozenList 以 dict/list 子类实现 —— isinstance 成立、
   比较与 JSON 序列化照常,唯独"写入"被拒。

遍历是迭代式的(显式任务栈):嵌套深度只受内存限制,不受调用栈
限制(DSH 的 walkJsonValue 同样的选择,防深嵌套爆栈)。祖先集合
按对象身份(id)追踪环引用 —— 拒绝循环,但不误伤共享引用
(同一对象出现在两处是合法的,只有自己包含自己才非法)。
"""

from __future__ import annotations

import math

__all__ = [
    "FrozenDict",
    "FrozenList",
    "is_json_value",
    "snapshot_json_value",
]


class FrozenDict(dict):
    """接纳后不可变的字典:任何写操作抛 TypeError。

    语义对应 JS 的 Object.freeze(dict 子类身份保持 isinstance(dict)
    成立,== 与 json.dumps 照常),但写入被拒绝 —— 消费方改不动
    已入日志的事件。构造与内部填充走基类通道,不受拦截。
    """

    __frozen__ = True

    def __setitem__(self, key, value) -> None:  # noqa: D401
        raise TypeError("frozen dict does not support item assignment")

    def __delitem__(self, key) -> None:
        raise TypeError("frozen dict does not support item deletion")

    def clear(self) -> None:
        raise TypeError("frozen dict does not support clear")

    def pop(self, key, default=None):  # noqa: D401
        raise TypeError("frozen dict does not support pop")

    def popitem(self):
        raise TypeError("frozen dict does not support popitem")

    def setdefault(self, key, default=None):
        # 语义对齐 dict.setdefault:键已存在时返回现值(读,合法);
        # 键缺失时需要写入,拒绝。
        if key in self:
            return self[key]
        raise TypeError("frozen dict does not support setdefault of a missing key")

    def update(self, *args, **kwargs) -> None:  # noqa: D401
        raise TypeError("frozen dict does not support update")

    def __ior__(self, other):
        raise TypeError("frozen dict does not support |= update")


class FrozenList(list):
    """接纳后不可变的列表:任何写操作抛 TypeError。

    与 FrozenDict 同理,是快照产物的不可变外壳:身份与序列化照常,
    写入被拒。
    """

    __frozen__ = True

    def __setitem__(self, index, value) -> None:  # noqa: D401
        raise TypeError("frozen list does not support item assignment")

    def __delitem__(self, index) -> None:  # noqa: D401
        raise TypeError("frozen list does not support item deletion")

    def append(self, value) -> None:  # noqa: D401
        raise TypeError("frozen list does not support append")

    def extend(self, values) -> None:  # noqa: D401
        raise TypeError("frozen list does not support extend")

    def insert(self, index, value) -> None:  # noqa: D401
        raise TypeError("frozen list does not support insert")

    def remove(self, value) -> None:  # noqa: D401
        raise TypeError("frozen list does not support remove")

    def pop(self, index: int = -1):
        raise TypeError("frozen list does not support pop")

    def clear(self) -> None:  # noqa: D401
        raise TypeError("frozen list does not support clear")

    def sort(self, *args, **kwargs) -> None:  # noqa: D401
        raise TypeError("frozen list does not support sort")

    def reverse(self) -> None:  # noqa: D401
        raise TypeError("frozen list does not support reverse")

    def __iadd__(self, other):
        raise TypeError("frozen list does not support in-place concatenation")


def _is_frozen(value: object) -> bool:
    """是否为我们的冻结结构之一(快照产物,允许作为输入回流)。"""
    return type(value) in (FrozenDict, FrozenList) or getattr(type(value), "__frozen__", False) is True


def _is_plain_list(value: object) -> bool:
    """精确 list 或我们的 FrozenList:拒绝任意其他 list 子类。

    DSH 拒绝 Array 子类与伪造原型 —— 子类可能携带 JSON 会丢弃的
    额外属性或行为;FrozenList 是我们自己造的安全类型,作为输入
    回流(种子来自已冻结的 events)时放行。
    """
    return type(value) is list or isinstance(value, FrozenList)


def _is_plain_dict(value: object) -> bool:
    """精确 dict 或我们的 FrozenDict(理由同上)。"""
    return type(value) is dict or isinstance(value, FrozenDict)


# ---- 迭代式遍历 ----

# 任务栈元组形状:
#   ("visit", value, destination)        —— 校验/快照一个值,写往 destination
#   ("array-item", source, index, target) —— 访问数组第 index 项
#   ("object-property", source, key, target) —— 访问对象第 key 个属性
#   ("leave", obj)                       —— 离开一个容器,撤销它的祖先登记
# destination = None 表示纯校验(detach=False);否则是快照写入目标。
# 快照目标恒为我们的冻结结构,填充走基类通道(dict.__setitem__ /
# list.append),绕开冻结类重写的拦截 —— 这是快照构建的合法内部通道。


def _walk_json_value(value, detach: bool):
    """一次遍历完成无损 JSON 校验与(可选)快照物化。

    返回:detach 时是快照(冻结结构)或 None(值不合法);纯校验时
    是 True 或 None。
    """
    ancestors = set()  # 环检测:已进入路径上的容器身份
    root = None

    def assign(destination, item):
        """把快照子项写入目的地(冻结结构构造期通道)。"""
        if destination is None:
            return
        kind, target, key = destination
        if kind == "root":
            nonlocal root
            root = item
        elif kind == "array":
            list.append(target, item)
        else:
            dict.__setitem__(target, key, item)

    tasks = [("visit", value, ("root", None, None) if detach else None)]
    while tasks:
        task = tasks.pop()
        kind = task[0]
        if kind == "leave":
            ancestors.discard(id(task[1]))
            continue
        if kind == "array-item":
            _source, index, target = task[1], task[2], task[3]
            tasks.append(("visit", _source[index], ("array", target, None) if target is not None else None))
            continue
        if kind == "object-property":
            _source, key, target = task[1], task[2], task[3]
            tasks.append(("visit", _source[key], ("object", target, key) if target is not None else None))
            continue

        current = task[1]
        destination = task[2]
        if current is None:
            assign(destination, None)
            continue
        if isinstance(current, bool) or isinstance(current, str):
            assign(destination, current)
            continue
        if isinstance(current, int):
            # Python int 无界且无负零语义;bool 已前置拦截。
            assign(destination, current)
            continue
        if isinstance(current, float):
            # JS Number 的语义边界:有限值且不是负零。
            if not math.isfinite(current) or (current == 0 and math.copysign(1.0, current) < 0):
                return None
            assign(destination, current)
            continue
        if not isinstance(current, (list, dict)):
            # 其余一切(BigInt 无对应,但 bytes/set/tuple/complex/datetime/
            # 类实例/… )都进不了 JSON —— 拒绝。
            return None
        if id(current) in ancestors:
            return None

        if isinstance(current, list):
            if not _is_plain_list(current):
                return None
            if detach:
                target = FrozenList()
                assign(destination, target)
            else:
                target = None
            ancestors.add(id(current))
            tasks.append(("leave", current))
            for index in range(len(current) - 1, -1, -1):
                tasks.append(("array-item", current, index, target))
            continue

        # dict:键必须是字符串(JSON 对象键;非 str 键会被 JSON 丢弃)。
        if not _is_plain_dict(current) or any(not isinstance(k, str) for k in current.keys()):
            return None
        if detach:
            target = FrozenDict()
            assign(destination, target)
        else:
            target = None
        ancestors.add(id(current))
        tasks.append(("leave", current))
        for key in list(current.keys())[::-1]:
            tasks.append(("object-property", current, key, target))

    return root if detach else True


def snapshot_json_value(value):
    """校验并分离一份无损 JSON 快照。

    一次递归读取同时完成校验与拷贝(状态型 getter 无法在校验与
    拷贝之间换值);遍历是迭代式的,合法嵌套只受内存限制。快照
    产物是冻结结构(见模块 docstring):接纳即不可变,对应 DSH
    的 snapshotJsonValue + deepFreeze 两步。

    接受:普通数组、普通对象、JSON 标量。
    拒绝:稀疏、循环、异类、负零、非有限值。
    返回:分离的快照;值不能无损 JSON 序列化时返回 None。
    """
    return _walk_json_value(value, True)


def is_json_value(value) -> bool:
    """测试与 snapshot_json_value 相同的无损 JSON 边界,但不分离。

    只观察自有可枚举字符串键;toJSON 不参与、getter 照常执行 ——
    持久化边界用快照器,纯测试用本函数。
    """
    return _walk_json_value(value, False) is True
