from __future__ import annotations


class JsonSyntaxBroken(Exception):
    # 增量序列出现结构性破损（括号失配、字符串外非法闭合、根闭合后杂文本）。
    pass


class JsonSyntaxMonitor:
    """逐增量的 JSON 语法监控器（ADR 0008）：只做括号平衡 + 字符串转义状态机的
    结构性检查，负责流中尽早发现破损并断流；完整解析与 Schema 校验留在流尾。
    零依赖纯函数对象，可确定性单元测试。
    """

    _PAIR = {"}": "{", "]": "["}

    def __init__(self) -> None:
        self._stack: list[str] = []
        self._in_string = False
        self._escaped = False
        self._root_closed = False

    def feed(self, delta: str) -> None:
        for ch in delta:
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
                continue
            if self._root_closed:
                if not ch.isspace():
                    raise JsonSyntaxBroken("根容器闭合后出现额外内容")
                continue
            if ch == '"':
                self._in_string = True
            elif ch in "{[":
                self._stack.append(ch)
            elif ch in "}]":
                if not self._stack or self._stack[-1] != self._PAIR[ch]:
                    raise JsonSyntaxBroken(f"括号失配: 意外的 {ch!r}")
                self._stack.pop()
                if not self._stack:
                    self._root_closed = True
            # 数字/字面量/逗号/冒号/空白不做词法级判定（结构性监控边界）

    @property
    def balanced(self) -> bool:
        # 流结束时的完整性：根容器已闭合且不在字符串中
        return self._root_closed and not self._in_string and not self._stack
