"""Round 79: Tab0-driven model dropdowns (Fix A) and per-model agentic video fallback (Fix B).

Why both live together: the golden-set workbench (`Tab3_黄金集评测工作台`) can only compare Gemini
models if (a) an operator can *pick* a model that is not in the hardcoded list and (b) a model
without agentic video serving (`gemini-3.5-flash`, `gemini-3.1-pro-preview` on Vertex `global`,
probe 2026-10-20) does not fail every slice with
`400 INVALID_ARGUMENT: Agentic video processing is not enabled for this model`.

All tests are hermetic: Sheets API and Gemini are replaced with in-memory fakes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httplib2
import pytest
from google.genai import errors as genai_errors
from google.genai import types
from googleapiclient.errors import HttpError

from cctv_audit import agentic_auditor, gcp
from cctv_audit import prompt_manager as pm
from cctv_audit.agentic_auditor import (
    AGENTIC_NOT_ENABLED_MARKER,
    AgenticAuditor,
    WindowResult,
    effective_media_mode,
    is_agentic_not_enabled_error,
    mark_agentic_unsupported,
    wants_agentic_video,
)
from cctv_audit.prompt_manager import (
    EVAL_WORKBENCH_HEADERS,
    SCHEDULED_RULE_HEADERS,
    SHEET_RULE_HEADER,
    GoogleSheetsConfigClient,
    PromptModelConfig,
    build_master_sheet_validation_requests,
    collect_model_candidates_from_tab0,
    default_model_dropdown_options,
    model_dropdown_options,
)
from cctv_audit.video_ingestor import VideoSliceSegment

TAB0 = GoogleSheetsConfigClient.TAB0_TITLE
TAB2 = pm.TAB2_SCHEDULED_TITLE
TAB3 = pm.TAB3_EVAL_TITLE
TAB4 = pm.TAB4_SPOT_CHECK_TITLE
PROMPT_TAB = "Prompt_v2.5_V10全量17条标准版"

TAB0_HEADER = ["配置项 (Config_Key)", "当前生效值 (Active_Value)", "说明", "候选列表 (Candidates)"]
NEW_NOTE = "🆕 新模型可用待测试"


def _tab0_rows(*, candidates: str = "gemini-3.8-flash | gemini-2.5-flash", discovered: str = "") -> List[List[str]]:
    """Tab0 as the production master sheet lays it out (header + 3 pointer rows + optional runtime row)."""
    rows = [
        list(TAB0_HEADER),
        ["Active_Prompt_Version", PROMPT_TAB, "生效提示词分表", f"{PROMPT_TAB} | Prompt_v2.6_r02"],
        ["Active_Model_Version", "gemini-3.8-flash", "生效模型", candidates],
        ["Fallback_Model_Version", "gemini-3.8-flash", "回退模型", ""],
    ]
    if discovered:
        rows.append(["Candidate_Models_Discovered", discovered, "仅追加至候选列表（未自动切换 Active_Model_Version）"])
    return rows


@pytest.fixture(autouse=True)
def _reset_process_registries(monkeypatch: pytest.MonkeyPatch):
    """Both fixes keep per-process memory; every test starts from an empty one."""
    monkeypatch.setattr(agentic_auditor, "_AGENTIC_UNSUPPORTED_MODELS", set())
    monkeypatch.setattr(pm, "_VALID_SHEET_CACHE", {})
    monkeypatch.setattr(pm, "_SHEET_RANGE_CACHE", {})
    monkeypatch.setattr(agentic_auditor.config, "video_media_processing", "agentic")
    yield


# --------------------------------------------------------------------------------------------
# Fix A: model dropdown options come from Tab0
# --------------------------------------------------------------------------------------------


def test_collect_model_candidates_from_tab0_parses_notes_dedupes_and_ignores_non_gemini() -> None:
    rows = _tab0_rows(
        candidates="gemini-3.8-flash | gemini-2.5-flash",
        discovered=f"gemini-3.5-flash ({NEW_NOTE}) | gemini-3.1-pro-preview ({NEW_NOTE})",
    )
    rows.append(["Some_Other_Key", "AUTO_LATEST_FLASH", "", "gemini-3.5-flash（已测） | foo-1 | Gemini-3.9-flash |  | gemini-3.8-flash-lite-preview-09-2026 "])
    rows.append(["Active_Model_Version", "gemini-4.0-pro (🆕 新模型可用待测试)"])  # short row, column B only
    rows.append([])  # blank row inside A1:D30

    got = collect_model_candidates_from_tab0(rows)

    assert got == [
        "gemini-3.8-flash",  # Active_Model_Version column B (also in 候选列表, kept once)
        "gemini-2.5-flash",
        "gemini-3.5-flash",  # `(🆕 ...)` note stripped; `（已测）` duplicate collapsed
        "gemini-3.1-pro-preview",
        "gemini-3.8-flash-lite-preview-09-2026",
        "gemini-4.0-pro",
    ]
    # Non-models never leak into a strict dropdown: header text, AUTO_LATEST_FLASH, foo-1, the
    # capitalised typo (`^gemini-\d` is case-sensitive, like Vertex model ids), prompt tab names.
    assert not any(tok in got for tok in ("候选列表", "AUTO_LATEST_FLASH", "foo-1", "Gemini-3.9-flash", PROMPT_TAB))
    assert collect_model_candidates_from_tab0([]) == []
    assert collect_model_candidates_from_tab0([TAB0_HEADER]) == []


def test_model_dropdown_options_puts_baseline_first_then_tab0_candidates() -> None:
    baseline = default_model_dropdown_options()
    assert baseline[0] == pm.config.fallback_model_version
    assert baseline[1:] == ["gemini-3-flash-preview", "gemini-2.5-flash"]

    rows = _tab0_rows(discovered=f"gemini-3.5-flash ({NEW_NOTE})")
    opts = model_dropdown_options(rows)
    assert opts[: len(dict.fromkeys(baseline))] == list(dict.fromkeys(baseline))
    assert "gemini-3.5-flash" in opts
    assert len(opts) == len(set(opts))


def _sheet_meta() -> Dict[str, Any]:
    return {
        "sheets": [
            {"properties": {"title": TAB0, "sheetId": 10}},
            {"properties": {"title": PROMPT_TAB, "sheetId": 11}},
            {"properties": {"title": TAB2, "sheetId": 20}},
            {"properties": {"title": TAB3, "sheetId": 30}},
        ]
    }


def _one_of_list_values(reqs: List[Dict[str, Any]], *, sheet_id: int, start_row: int, start_col: int) -> List[str]:
    for r in reqs:
        sdv = r.get("setDataValidation")
        if not sdv:
            continue
        rng = sdv["range"]
        if rng["sheetId"] == sheet_id and rng["startRowIndex"] == start_row and rng["startColumnIndex"] == start_col:
            rule = sdv["rule"]
            assert rule["strict"] is True
            assert rule["condition"]["type"] == "ONE_OF_LIST"
            return [v["userEnteredValue"] for v in rule["condition"]["values"]]
    raise AssertionError(f"no ONE_OF_LIST rule on sheet {sheet_id} row {start_row} col {start_col}")


def test_validation_rules_include_tab0_candidates_in_tab0_b3b4_and_tab3_column_d() -> None:
    rows = _tab0_rows(discovered=f"gemini-3.5-flash ({NEW_NOTE}) | gemini-3.1-pro-preview ({NEW_NOTE})")
    reqs = build_master_sheet_validation_requests(_sheet_meta(), model_versions=model_dropdown_options(rows))

    tab0_models = _one_of_list_values(reqs, sheet_id=10, start_row=2, start_col=1)  # B3:B4
    tab3_models = _one_of_list_values(reqs, sheet_id=30, start_row=1, start_col=3)  # column D
    assert tab0_models == tab3_models
    for m in ("gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-2.5-flash"):
        assert m in tab3_models
    assert f"gemini-3.5-flash ({NEW_NOTE})" not in tab3_models
    assert len(tab3_models) == len(set(tab3_models))

    # Tab3 schema untouched: 10 columns, 5 column rules (C, D, clear E:G, H, I).
    assert len(EVAL_WORKBENCH_HEADERS) == 10
    tab3_reqs = [r for r in reqs if "setDataValidation" in r and r["setDataValidation"]["range"]["sheetId"] == 30]
    assert len(tab3_reqs) == 5

    # Without a model list the builder still emits the baseline (what callers without Tab0 get).
    baseline_only = _one_of_list_values(build_master_sheet_validation_requests(_sheet_meta()), sheet_id=30, start_row=1, start_col=3)
    assert baseline_only == list(dict.fromkeys(default_model_dropdown_options()))


class _FakeRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self, num_retries: int = 0) -> Any:
        return self._payload


class _FakeSheetsService:
    """Sheets v4 subset used by `ensure_master_sheet_validations`; every tab already migrated."""

    def __init__(self, tab0_rows: List[List[str]]) -> None:
        self.tab0_rows = tab0_rows
        self.tab0_error: Optional[BaseException] = None
        self.meta_gets = 0
        self.value_gets: List[str] = []
        self.batch_updates: List[Dict[str, Any]] = []
        self.sheet_meta = {
            "sheets": [
                {"properties": {"title": TAB0, "sheetId": 10}},
                {"properties": {"title": PROMPT_TAB, "sheetId": 11}},
                {"properties": {"title": TAB2, "sheetId": 20}},
                {"properties": {"title": TAB3, "sheetId": 30}},
                {"properties": {"title": TAB4, "sheetId": 40}},
            ]
        }

    def spreadsheets(self) -> "_FakeSheetsService":
        return self

    def values(self) -> "_FakeSheetsService":
        return self

    def get(self, *, spreadsheetId: str, range: Optional[str] = None, **_kw: Any) -> _FakeRequest:
        if range is None:  # spreadsheets().get(...) -> metadata
            self.meta_gets += 1
            return _FakeRequest(self.sheet_meta)
        self.value_gets.append(range)
        tab = range.split("!")[0].strip("'")
        if tab == TAB0:
            if self.tab0_error is not None:
                raise self.tab0_error
            return _FakeRequest({"values": [list(r) for r in self.tab0_rows]})
        if tab == TAB2:
            return _FakeRequest({"values": [list(SCHEDULED_RULE_HEADERS)]})
        if tab == TAB3:
            return _FakeRequest({"values": [list(EVAL_WORKBENCH_HEADERS)]})
        if tab == TAB4:
            return _FakeRequest({"values": [list(SHEET_RULE_HEADER)]})
        return _FakeRequest({"values": []})

    def batchUpdate(self, *, spreadsheetId: str, body: Dict[str, Any]) -> _FakeRequest:
        self.batch_updates.append(body)
        return _FakeRequest({})

    def update(self, **_kw: Any) -> _FakeRequest:  # pragma: no cover - no migration in these tests
        raise AssertionError("no values().update expected: fake tabs are already migrated")

    def clear(self, **_kw: Any) -> _FakeRequest:  # pragma: no cover
        raise AssertionError("no values().clear expected: fake tabs are already migrated")


def _tab3_model_options(batch_body: Dict[str, Any]) -> List[str]:
    return _one_of_list_values(batch_body["requests"], sheet_id=30, start_row=1, start_col=3)


def _http_error(status: int, message: str) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), f'{{"error": {{"code": {status}, "message": "{message}"}}}}'.encode())


def test_ensure_master_sheet_validations_reapplies_when_tab0_candidates_change(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSheetsService(_tab0_rows())
    client = GoogleSheetsConfigClient()
    monkeypatch.setattr(client, "_sheets_service", lambda: fake)
    sheet_id = "1ExampleMasterSopSheetId00000000000000000000"

    # Tick 1: full pass (metadata + migrations + validations); Tab0 candidates in the dropdown.
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    assert fake.meta_gets == 1 and len(fake.batch_updates) == 1
    first_opts = _tab3_model_options(fake.batch_updates[0])
    assert "gemini-3.8-flash" in first_opts and "gemini-2.5-flash" in first_opts
    assert "gemini-3.5-flash" not in first_opts
    assert pm._VALID_SHEET_CACHE[sheet_id] == tuple(model_dropdown_options(_tab0_rows()))

    # Tick 2: nothing changed -> one cheap Tab0 read, no metadata call, no batchUpdate.
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    assert fake.meta_gets == 1 and len(fake.batch_updates) == 1
    assert fake.value_gets[-1] == f"'{TAB0}'!A1:D30"

    # Tick 3: operator appends gemini-3.5-flash to the Tab0 候选列表 (and the runtime appended a
    # discovered row) -> signature differs -> dropdowns re-applied without a restart.
    fake.tab0_rows = _tab0_rows(
        candidates="gemini-3.8-flash | gemini-2.5-flash | gemini-3.5-flash",
        discovered=f"gemini-3.1-pro-preview ({NEW_NOTE})",
    )
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    assert fake.meta_gets == 2 and len(fake.batch_updates) == 2
    third_opts = _tab3_model_options(fake.batch_updates[1])
    assert "gemini-3.5-flash" in third_opts and "gemini-3.1-pro-preview" in third_opts
    assert pm._VALID_SHEET_CACHE[sheet_id] == tuple(third_opts)
    tab0_opts = _one_of_list_values(fake.batch_updates[1]["requests"], sheet_id=10, start_row=2, start_col=1)
    assert tab0_opts == third_opts

    # Tick 4: unchanged again -> skipped; force=True -> re-applied even though unchanged.
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    assert len(fake.batch_updates) == 2
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id, force=True)) is True
    assert fake.meta_gets == 3 and len(fake.batch_updates) == 3

    # gs:// workbooks are customer-owned and read-only for the runtime.
    assert asyncio.run(client.ensure_master_sheet_validations("gs://bucket/sop.xlsx")) is False
    assert len(fake.batch_updates) == 3


def test_ensure_master_sheet_validations_tab0_read_failures(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeSheetsService(_tab0_rows(candidates="gemini-3.8-flash | gemini-3.5-flash"))
    client = GoogleSheetsConfigClient()
    monkeypatch.setattr(client, "_sheets_service", lambda: fake)
    sheet_id = "sheet-with-flaky-api"

    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    applied = pm._VALID_SHEET_CACHE[sheet_id]
    assert "gemini-3.5-flash" in applied

    # A transient Sheets failure must not shrink the strict dropdowns to the baseline (which would
    # flag every Tab3 row naming gemini-3.5-flash): the tick is skipped and the memo kept.
    fake.tab0_error = _http_error(503, "backendError")
    with caplog.at_level(logging.WARNING, logger="cctv_audit.prompt_manager"):
        assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is False
    assert pm._VALID_SHEET_CACHE[sheet_id] == applied
    assert len(fake.batch_updates) == 1
    assert any("Could not apply master sheet validations" in rec.getMessage() for rec in caplog.records)

    # A missing Tab0 (400 Unable to parse range) is a stable state: baseline options only.
    fake.tab0_error = _http_error(400, "Unable to parse range: Tab0")
    assert asyncio.run(client.ensure_master_sheet_validations(sheet_id)) is True
    assert len(fake.batch_updates) == 2
    assert _tab3_model_options(fake.batch_updates[1]) == list(dict.fromkeys(default_model_dropdown_options()))
    assert pm._VALID_SHEET_CACHE[sheet_id] == tuple(dict.fromkeys(default_model_dropdown_options()))


# --------------------------------------------------------------------------------------------
# Fix B: per-model agentic capability fallback
# --------------------------------------------------------------------------------------------


def _not_enabled_error() -> genai_errors.ClientError:
    """Byte-for-byte shape of the live Vertex `global` response (probe 2026-10-20)."""
    return genai_errors.ClientError(
        400,
        {
            "error": {
                "code": 400,
                "message": "Agentic video processing is not enabled for this model",
                "status": "INVALID_ARGUMENT",
            }
        },
    )


def _ok_response() -> SimpleNamespace:
    return SimpleNamespace(
        parsed=WindowResult(findings=[]),
        text="",
        usage_metadata=SimpleNamespace(
            prompt_token_count=100, thoughts_token_count=20, candidates_token_count=10, total_token_count=130
        ),
    )


def _segment(idx: int = 0) -> VideoSliceSegment:
    return VideoSliceSegment(
        source_file_id=f"vid_{idx}",
        source_filename="GD2902390.mov",
        segment_index=idx,
        start_offset_sec=302.0 * idx,
        end_offset_sec=302.0 * (idx + 1),
        local_path=Path("/nonexistent/never_read.mp4"),
        gcs_uri=f"gs://bucket/eval/media/vid/seg_{idx}.mp4",
    )


def _is_agentic(part: types.Part) -> bool:
    return getattr(part, "media_processing", None) == types.MediaProcessing.AGENTIC


def test_is_agentic_not_enabled_error_and_transient_classification() -> None:
    exc = _not_enabled_error()
    assert AGENTIC_NOT_ENABLED_MARKER in str(exc)
    assert is_agentic_not_enabled_error(exc) is True
    # `.message` alone (an exception whose str() does not repeat the server message).
    class _Opaque(Exception):
        message = "Agentic video processing is not enabled for this model"

        def __str__(self) -> str:
            return "ClientError"

    assert is_agentic_not_enabled_error(_Opaque()) is True
    assert is_agentic_not_enabled_error(RuntimeError("400 INVALID_ARGUMENT. something else")) is False
    assert is_agentic_not_enabled_error(TimeoutError()) is False

    # Never retried with backoff: not transient even if the body happens to contain "500"/"503".
    assert gcp._is_transient_error(exc) is False
    assert gcp._is_transient_error(RuntimeError(f"{AGENTIC_NOT_ENABLED_MARKER} for this model (request id 503500)")) is False
    assert gcp._is_transient_error(RuntimeError("503 UNAVAILABLE")) is True
    assert gcp._is_transient_error(RuntimeError("400 INVALID_ARGUMENT. other")) is False


def test_agentic_not_enabled_falls_back_to_static_once_per_process(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    calls: List[SimpleNamespace] = []

    async def _fake_generate(*, model: str, contents: list, gen_config: Any, **kw: Any) -> SimpleNamespace:
        part = contents[0]
        calls.append(SimpleNamespace(model=model, agentic=_is_agentic(part), part=part, timeout_ms=kw.get("timeout_ms")))
        if model == "gemini-3.5-flash" and _is_agentic(part):
            raise _not_enabled_error()
        return _ok_response()

    monkeypatch.setattr(agentic_auditor, "generate_content_with_retry", _fake_generate)
    auditor = AgenticAuditor(gemini_concurrency=5)

    async def _analyze(model: str, idx: int):
        return await auditor.analyze_segment(
            audit_id="r79",
            folder_id="folder",
            segment=_segment(idx),
            prompt_cfg=PromptModelConfig(active_model_version=model, system_instruction="SYS"),
        )

    assert effective_media_mode("gemini-3.5-flash") == "agentic"  # nothing learned yet

    with caplog.at_level(logging.WARNING, logger="cctv_audit.agentic_auditor"):
        parsed, ledger = asyncio.run(_analyze("gemini-3.5-flash", 0))

    # Exactly one fallback: agentic attempt, then the same slice again with a static Part.
    assert [(c.model, c.agentic) for c in calls] == [("gemini-3.5-flash", True), ("gemini-3.5-flash", False)]
    assert calls[1].part.media_processing is None
    assert calls[1].part.file_data.file_uri == calls[0].part.file_data.file_uri
    assert calls[0].timeout_ms == calls[1].timeout_ms
    assert parsed.findings == [] and ledger.model_version_used == "gemini-3.5-flash"
    assert ledger.total_token_count == 130
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Model gemini-3.5-flash does not support agentic video; falling back to static sampling for the rest of this process"]

    # Learned for the rest of the process: later slices go straight to static (one call each).
    assert effective_media_mode("gemini-3.5-flash") == "static"
    assert wants_agentic_video("gemini-3.5-flash") is False
    asyncio.run(_analyze("gemini-3.5-flash", 1))
    assert [(c.model, c.agentic) for c in calls[2:]] == [("gemini-3.5-flash", False)]

    # A supporting model keeps agentic and is not marked.
    asyncio.run(_analyze("gemini-3.8-flash", 0))
    assert [(c.model, c.agentic) for c in calls[3:]] == [("gemini-3.8-flash", True)]
    assert effective_media_mode("gemini-3.8-flash") == "agentic"
    assert agentic_auditor._AGENTIC_UNSUPPORTED_MODELS == {"gemini-3.5-flash"}

    # Explicit switch wins over the registry either way; a non-3.x/4.x model was never agentic.
    assert effective_media_mode("gemini-3.8-flash", "static") == "static"
    assert effective_media_mode("gemini-4-argon") == "agentic"
    assert wants_agentic_video("gemini-4-argon") is True
    assert effective_media_mode("gemini-2.5-flash") == "static"
    assert effective_media_mode("gemini-3.5-flash", "agentic") == "static"  # marked -> still static


def test_other_errors_are_reraised_unchanged_and_static_mode_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[SimpleNamespace] = []

    async def _fake_generate(*, model: str, contents: list, gen_config: Any, **kw: Any) -> SimpleNamespace:
        calls.append(SimpleNamespace(model=model, agentic=_is_agentic(contents[0])))
        if len(calls) == 1:
            raise genai_errors.ClientError(400, {"error": {"code": 400, "message": "Request payload size exceeds the limit", "status": "INVALID_ARGUMENT"}})
        if len(calls) == 2:
            raise _not_enabled_error()
        return _ok_response()

    monkeypatch.setattr(agentic_auditor, "generate_content_with_retry", _fake_generate)
    auditor = AgenticAuditor(gemini_concurrency=5)

    async def _analyze(model: str) -> None:
        await auditor.analyze_segment(
            audit_id="r79",
            folder_id="folder",
            segment=_segment(0),
            prompt_cfg=PromptModelConfig(active_model_version=model, system_instruction="SYS"),
        )

    # 1. A different 400 propagates unchanged after one call; the model is not marked.
    with pytest.raises(genai_errors.ClientError, match="payload size"):
        asyncio.run(_analyze("gemini-3.1-pro-preview"))
    assert len(calls) == 1 and calls[0].agentic is True
    assert agentic_auditor._AGENTIC_UNSUPPORTED_MODELS == set()

    # 2. The not-enabled 400 on a request that was NOT agentic (switch set to static) is not ours
    #    to repair: it propagates unchanged and nothing is marked.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agentic_auditor.config, "video_media_processing", "static")
        with pytest.raises(genai_errors.ClientError, match=AGENTIC_NOT_ENABLED_MARKER):
            asyncio.run(_analyze("gemini-3.1-pro-preview"))
    assert len(calls) == 2 and calls[1].agentic is False
    assert agentic_auditor._AGENTIC_UNSUPPORTED_MODELS == set()

    # 3. `mark_agentic_unsupported` is idempotent and ignores empty names.
    mark_agentic_unsupported("")
    mark_agentic_unsupported("gemini-3.1-pro-preview")
    mark_agentic_unsupported("gemini-3.1-pro-preview")
    assert agentic_auditor._AGENTIC_UNSUPPORTED_MODELS == {"gemini-3.1-pro-preview"}
    asyncio.run(_analyze("gemini-3.1-pro-preview"))
    assert calls[2].agentic is False
