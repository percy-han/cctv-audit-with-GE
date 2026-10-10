"""Hermetic fakes of the Sheets v4 / Drive v3 discovery clients used by eval/golden_sheet.py tests."""

from __future__ import annotations

from typing import Any


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries: int = 0):
        return self.fn()


class HttpErrorLike(Exception):
    def __init__(self, status: int, text: str = ""):
        super().__init__(text or f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


def _trim(row: list[str]) -> list[str]:
    row = list(row)
    while row and row[-1] == "":
        row.pop()
    return row


class FakeSheets:
    """``tabs`` = {title: 2-D values}. values().get trims trailing empty cells / rows like the API."""

    def __init__(self, tabs: dict[str, list[list[str]]], fail_status: int | None = None):
        self.tabs = {k: [list(r) for r in v] for k, v in tabs.items()}
        self.fail_status = fail_status
        self.writes: list[tuple[str, Any]] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def _tab_of(self, a1: str) -> str:
        tab = a1.split("!")[0]
        return tab[1:-1] if tab.startswith("'") else tab

    def get(self, spreadsheetId: str, range: str | None = None, fields: str | None = None):  # noqa: A002
        def run():
            if self.fail_status:
                raise HttpErrorLike(self.fail_status)
            if range is None:  # spreadsheets().get
                return {"sheets": [{"properties": {"title": t}} for t in self.tabs]}
            tab = self._tab_of(range)
            if tab not in self.tabs:
                raise HttpErrorLike(400, f"Unable to parse range: {range}")
            rows = [_trim(r) for r in self.tabs[tab]]
            while rows and not rows[-1]:
                rows.pop()
            return {"values": rows}
        return _Call(run)

    def batchUpdate(self, spreadsheetId: str, body: dict):  # noqa: N802
        def run():
            self.writes.append(("batchUpdate", body))
            for req in body.get("requests", []):
                if "addSheet" in req:
                    self.tabs[req["addSheet"]["properties"]["title"]] = []
            return {}
        return _Call(run)

    def clear(self, spreadsheetId: str, range: str, body: dict):  # noqa: A002
        def run():
            self.writes.append(("clear", range))
            self.tabs[self._tab_of(range)] = []
            return {}
        return _Call(run)

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: dict):  # noqa: A002, N803
        def run():
            self.writes.append(("update", range, body))
            self.tabs[self._tab_of(range)] = [list(r) for r in body["values"]]
            return {}
        return _Call(run)


class FakeDrive:
    """``folders`` = {folder_id: [{"id","name","videoMediaMetadata"?}]} (listed in this order, 2 per page)."""

    def __init__(self, folders: dict[str, list[dict[str, Any]]], sheet_meta: dict[str, Any] | None = None):
        self.folders = folders
        self.sheet_meta = sheet_meta or {"name": "Manual Audit Result", "modifiedTime": "2026-10-10T00:00:00Z",
                                         "version": "42"}

    def files(self):
        return self

    def get(self, fileId: str, fields: str, supportsAllDrives: bool = False):  # noqa: N803
        return _Call(lambda: dict(self.sheet_meta))

    def list(self, q: str, fields: str, pageSize: int, pageToken: str | None, supportsAllDrives: bool,  # noqa: N803
             includeItemsFromAllDrives: bool):  # noqa: N803
        folder = q.split("'")[1]

        def run():
            if folder not in self.folders:
                raise HttpErrorLike(404)
            files = self.folders[folder]
            start = int(pageToken or 0)
            page = {"files": files[start:start + 2]}
            if start + 2 < len(files):
                page["nextPageToken"] = str(start + 2)
            return page
        return _Call(run)


def drive_files_from_manifest(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Drive listing that reproduces a manifest's folders (reverse order to prove sorting is ours)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for f in manifest["folders"]:
        files = []
        for v in f["videos"]:
            meta = {}
            if "duration_sec" in v:
                meta["durationMillis"] = str(int(round(v["duration_sec"] * 1000)))
            if "width" in v:
                meta.update(width=v["width"], height=v["height"])
            files.append({"id": v["file_id"], "name": v["filename"], "videoMediaMetadata": meta})
        out[f["folder_id"]] = list(reversed(files))
    return out
