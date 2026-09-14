"""Turning a list of records into a spreadsheet.

Reports build their own framed workbook (title, summary rows, charts); this is
the plainer case — the rows behind a list screen, exactly as filtered, so an
office can open them in Excel. Kept separate from the reports module because
``core.crud`` needs it and must not depend on a feature module.
"""

import csv
import io
from datetime import date, datetime
from typing import Any

#: Columns nobody wants in a spreadsheet: internal plumbing and foreign keys,
#: which are opaque ids rather than the names a human is looking for.
HIDDEN = {"id", "_id", "tenant_id", "is_deleted", "deleted_at", "created_by", "updated_by"}


def _readable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if not isinstance(v, (dict, list)))
    if value is None:
        return ""
    return value


def _flatten(doc: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """One level of nesting becomes ``contact.phone``; deeper is dropped."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key in HIDDEN or key.endswith("_id") or key.endswith("_ids"):
            continue
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            if not prefix:
                out.update(_flatten(value, f"{key}."))
            continue
        out[name] = _readable(value)
    return out


def label_for(key: str) -> str:
    return key.replace(".", " ").replace("_", " ").strip().capitalize()


def tabulate(docs: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Column keys in first-seen order, plus the flattened rows.

    Columns come from the union of the rows rather than the model, so a field
    every record leaves blank does not become an empty column.
    """
    rows = [_flatten(doc) for doc in docs]
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns, rows


def rows_to_csv(docs: list[dict[str, Any]]) -> str:
    columns, rows = tabulate(docs)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label_for(c) for c in columns])
    for row in rows:
        writer.writerow([row.get(c, "") for c in columns])
    return buffer.getvalue()


def rows_to_xlsx(docs: list[dict[str, Any]], title: str = "Export") -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    columns, rows = tabulate(docs)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31] or "Export"
    sheet.append([label_for(c) for c in columns])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([row.get(c, "") for c in columns])
    for index, key in enumerate(columns, start=1):
        widest = max([len(label_for(key))] + [len(str(r.get(key, ""))) for r in rows] or [10])
        sheet.column_dimensions[get_column_letter(index)].width = min(widest + 2, 40)
    sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
