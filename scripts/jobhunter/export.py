"""`export`: CSV copies of every Sheet tab in exports/ (design 1.3.8), built from the same row builders.

One file per table tab (`<Tab title>.csv` with the human headers), plus `Limits and settings.csv` and
`Dashboard.csv`. Dates are written in the person's time zone; links as their URL; any value that starts
with =, +, @ or a minus sign gets a leading apostrophe so a spreadsheet program never runs it as a formula. Files are
written atomically with mode 600 (they hold personal data).
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import tempfile

from . import paths
from . import sheets_labels as L
from . import sheets_rows as R
from . import status as S
from .errors import Denied


def _cell(typ: str, v, ctx: R.RowCtx) -> str:
    if v is None:
        return ""
    if typ == "datetime":
        return S.fmt_local(v, ctx.tz) or str(v)
    if typ == "date":
        if isinstance(v, str) and len(v) == 10:   # already the local calendar date
            try:
                d = _dt.datetime.strptime(v, "%Y-%m-%d")
                return "%d %s %d" % (d.day, d.strftime("%b"), d.year)
            except ValueError:
                return v
        return S.fmt_local(v, ctx.tz, with_time=False) or str(v)
    if typ == "link":
        return str(v.get("url") or "") if isinstance(v, dict) else str(v)
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return L.safe_cell(str(v))


def _write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".export-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_all(conn, out_dir: str | None = None, config: dict | None = None) -> dict:
    ctx = R.RowCtx.load(config)
    out_dir = os.path.realpath(out_dir) if out_dir else paths.exports_dir()
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise Denied("E_VALIDATION", "the export folder is a file", data={"path": out_dir})
    files = {}
    for tab in L.TABLE_ORDER:
        cols = L.columns(tab)
        data = []
        for rid, row, _stamp in R.build_rows_stamped(conn, tab, None, ctx):
            data.append([_cell(c[2], rid if c[0] == "id" else row.get(c[0]), ctx) for c in cols])
        path = os.path.join(out_dir, "%s.csv" % L.TAB_TITLES[tab])
        _write_csv(path, [c[1] for c in cols], data)
        files[tab] = {"path": path, "rows": len(data)}
    settings = [[L.safe_cell(str(x)) for x in r[:4]] for r in S.settings_rows(conn, ctx.config)]
    path = os.path.join(out_dir, "%s.csv" % L.TAB_TITLES["settings"])
    _write_csv(path, ["Setting", "Value", "Used today", "What it means"], settings)
    files["settings"] = {"path": path, "rows": len(settings)}
    dash = S.dashboard(conn, ctx.config)
    rows = [[L.safe_cell(r[0])] + [str(x) for x in r[1:]] for r in dash["activity"]["rows"]]
    path = os.path.join(out_dir, "%s.csv" % L.TAB_TITLES["dashboard"])
    _write_csv(path, ["Activity"] + dash["activity"]["columns"], rows)
    files["dashboard"] = {"path": path, "rows": len(rows)}
    return {"out_dir": out_dir, "files": files}
