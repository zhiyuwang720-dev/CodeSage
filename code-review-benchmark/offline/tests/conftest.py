"""Pytest configuration and common fixtures."""

from __future__ import annotations

from collections import defaultdict
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
import types

# Ensure repository root is importable for code under test.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Provide a lightweight openpyxl stub so tests can import modules without
# triggering installation of the real dependency.
if "openpyxl" not in sys.modules:
    ColumnDim = types.SimpleNamespace
    _SAVED_WORKBOOKS: dict[str, StubWorkbook] = {}

    class StubWorksheet:
        def __init__(self):
            self.title = ""
            self._rows: list[list[str]] = []
            self.column_dimensions = defaultdict(lambda: ColumnDim(width=None))

        def append(self, row):
            self._rows.append(row)

        @property
        def max_row(self):
            return len(self._rows)

        def iter_rows(self):
            return self._rows

        def cell(self, row: int, column: int):
            value = self._rows[row - 1][column - 1]
            return types.SimpleNamespace(value=value)

        def __getitem__(self, cell_ref: str):
            col_letter = cell_ref[0].upper()
            row_index = int(cell_ref[1:])
            column_index = ord(col_letter) - ord("A") + 1
            return self.cell(row_index, column_index)

    class StubWorkbook:
        def __init__(self):
            self.active = StubWorksheet()

        def save(self, path):
            _SAVED_WORKBOOKS[str(path)] = self
            Path(path).write_text("stub")

    def load_workbook(path):
        workbook = _SAVED_WORKBOOKS[str(path)]
        return types.SimpleNamespace(active=workbook.active)

    stub_module = types.ModuleType("openpyxl")
    stub_module.__spec__ = ModuleSpec("openpyxl", loader=None)
    stub_module.Workbook = StubWorkbook
    stub_module.load_workbook = load_workbook
    sys.modules["openpyxl"] = stub_module
