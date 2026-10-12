import csv
import gzip
import io
import json
from datetime import date
from html import escape

import httpx
import pytest
from openpyxl import Workbook
from super_partner.benchmarks import (
    AA_KEY_ENV,
    AA_PUBLIC_URL,
    AA_URL,
    BFCL_DATA_URL,
    BFCL_FIELDS,
    BFCL_URL,
    SUPERCLUE_FIELDS,
    SUPERCLUE_URL,
    BenchmarkCatalog,
    match_results,
    parse_aa,
    parse_aa_public,
    parse_bfcl,
    parse_superclue,
)
from super_partner.config import settings
from super_partner.db import Database
from super_partner.models import ModelProfile


def bfcl_csv(name="Sample-1 (FC)", score="72.5%"):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["Model", "Organization", *BFCL_FIELDS])
    writer.writeheader()
    writer.writerow(
        {"Model": name, "Organization": "OpenAI", "Overall Acc": score, "Live Acc": "0%"}
    )
    return buffer.getvalue()


BFCL_PAGE = "<title>Berkeley Function Calling (BFCL) V4</title>Last Updated: 2026-09-15"


def workbook_bytes(name="Qwen3.8-Max(max)", score=71.48):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "总排行榜"
    sheet.append(["模型名称", "机构", *SUPERCLUE_FIELDS, "发布日期"])
    sheet.append([name, "阿里巴巴", score, 77.19, 86.08, None, 43.81, 68.42, 90.94, "2026.8.6"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def aa_body(page=1, more=False, version=4.3, name="Sample-1 (high)"):
    return {
        "intelligence_index_version": version,
        "pagination": {"page": page, "has_more": more, "total_pages": 2 if more else page},
        "data": [
            {
                "name": name,
                "slug": "sample-1",
                "model_creator": {"name": "OpenAI"},
                "evaluations": {
                    "artificial_analysis_intelligence_index": 52.3,
                    "artificial_analysis_coding_index": 0,
                    "artificial_analysis_agentic_index": None,
                },
            }
        ],
    }


def aa_public_html(rows=None):
    if rows is None:
        rows = [("Sample-1 (high)", "OpenAI", "52", "sample-1-high")]
    body = "".join(
        f"<tr><td>{escape(creator)}</td><td>{escape(score)}</td>"
        f'<td><div>{escape(name)}</div></td><td><a href="/models/{escape(slug)}">Model</a>'
        f'<a href="/models/{escape(slug)}/providers">Providers</a></td></tr>'
        for name, creator, score, slug in rows
    )
    return (
        '<table><thead><tr><th colspan="4">Features</th></tr><tr><th>Creator</th>'
        "<th>Artificial Analysis Intelligence Index</th><th>Model</th>"
        "<th>Further Analysis</th></tr></thead><tbody>" + body + "</tbody></table>"
    )


def aa_detail_row(**updates):
    return {
        "slug": "sample-1-high",
        "shortName": "Sample-1 (high)",
        "name": "Sample-1 (High)",
        "modelCreatorName": "OpenAI",
        "intelligenceIndex": 52.3,
        "scicode": 0.625,
        "terminalBench40": 0.5,
        "hle": 0.3,
        "critpt": 0.1,
        "lcr": 0.8,
        "omniscienceAccuracy": 0.7,
        "omniscienceNonHallucination": 0.6,
        **updates,
    }


def aa_chart_scripts(rows):
    stream = 'f:I["module"]\na:' + json.dumps(["$", "$L36", None, {"models": rows}]) + "\n"
    midpoint = len(stream) // 2
    return "".join(
        "<script>self.__next_f.push(" + json.dumps([1, part]) + ")</script>"
        for part in (stream[:midpoint], stream[midpoint:])
    )


def model(identifier="openai:sample-1", **kwargs):
    return ModelProfile(
        id=identifier,
        provider=identifier.split(":")[0],
        display_name=identifier.split(":")[1],
        **kwargs,
    )


def test_parsers_keep_original_scores_dates_variants_and_missing_values():
    bfcl = parse_bfcl(bfcl_csv(), BFCL_PAGE)[0]
    assert bfcl.model_key == "Sample-1" and bfcl.variant == "FC"
    assert bfcl.version == "BFCL V4" and bfcl.published_at == date(2026, 9, 15)
    assert [metric.value for metric in bfcl.metrics] == [72.5, 0]
    sc = parse_superclue(workbook_bytes(), "2026年7月", SUPERCLUE_URL)[0]
    assert sc.variant == "max" and sc.published_at == date(2026, 8, 6)
    assert len(sc.metrics) == 6  # Missing scientific reasoning isn't silently replaced with zero.
    aa = parse_aa(aa_body())[0]
    assert aa.published_at is None
    assert aa.variant == "high" and aa.metrics[0].unit == "index"
    assert [metric.value for metric in aa.metrics] == [52.3, 0]


@pytest.mark.parametrize("score", ["101%", "-1%", "nan", "inf", "not a score"])
def test_invalid_percentages_are_rejected(score):
    with pytest.raises(ValueError):
        parse_bfcl(bfcl_csv(score=score), BFCL_PAGE)


def test_schema_changes_and_empty_payloads_are_rejected():
    with pytest.raises(ValueError):
        parse_bfcl("Model,Overall Acc\nDemo,1", BFCL_PAGE)
    with pytest.raises(ValueError):
        parse_bfcl(bfcl_csv(), "no version")
    with pytest.raises(ValueError):
        parse_superclue(workbook_bytes(score=101), "2026年7月", SUPERCLUE_URL)


def test_matching_preserves_snapshots_provider_configuration_and_ambiguity():
    rows = parse_bfcl(bfcl_csv("Sample-1-20260101 (FC)"), BFCL_PAGE)
    rows += parse_bfcl(bfcl_csv("Sample-1-20260101 (Prompt)"), BFCL_PAGE)
    correct = model("openai:sample-1-20260101")
    candidates = [
        correct,
        model(),
        model("openai:sample-1-mini"),
        model("google:sample-1-20260101"),
    ]
    matched = match_results(candidates, rows)
    assert list(matched) == [correct.id]
    assert {row["variant"] for row in matched[correct.id]} == {"FC", "Prompt"}
    assert correct.scores is None and not correct.routing_ready
    rows = parse_bfcl(bfcl_csv("Friendly Name (FC)"), BFCL_PAGE)
    candidates[0].display_name = "Friendly Name"
    candidates[1].display_name = "Friendly Name"
    assert not match_results(candidates, rows)
    candidates[0].display_name = "Other Name"
    assert match_results(candidates, rows)[candidates[1].id][0]["matched_by"] == "display_name"


def test_scores_older_than_model_release_do_not_transfer_to_new_snapshot():
    rows = parse_bfcl(bfcl_csv(), BFCL_PAGE)
    assert not match_results([model(release_date=date(2026, 9, 16))], rows)
    assert model().id in match_results([model(release_date=date(2026, 9, 14))], rows)


def test_public_aa_retains_visible_precision_variants_notes_and_zero():
    rows = parse_aa_public(
        aa_public_html(
            [
                ("Sample-1 (high)", "OpenAI", "52", "sample-1-high"),
                ("Sample-1 (low)", "OpenAI", "0", "sample-1-low"),
                ("Sample-2 (max with fallback)", "Anthropic", "33 *", "sample-2"),
                ("Sample-3 (max)", "OpenAI", "--", "sample-3"),
            ]
        )
    )
    assert len(rows) == 3
    assert [row.metrics[0].value for row in rows] == [52, 0, 33]
    assert [row.variant for row in rows] == ["high", "low", "max with fallback"]
    assert rows[2].metrics[0].note and "*" in rows[2].metrics[0].note
    assert all(row.metrics[0].unit == "index" for row in rows)
    assert all(row.collection_method == "public_page" for row in rows)
    assert all(row.published_at is None and "网页未标版本" in row.version for row in rows)
    assert rows[0].data_url == AA_PUBLIC_URL
    assert rows[0].source_url == "https://artificialanalysis.ai/models/sample-1-high"


def test_public_aa_reads_split_chart_json_and_scales_real_subscores():
    page = aa_public_html() + aa_chart_scripts([aa_detail_row(critpt=0, lcr=None)])
    row = parse_aa_public(page)[0]
    scores = {metric.key: metric for metric in row.metrics}
    assert scores["artificial_analysis_intelligence_index"].value == 52
    assert scores["artificial_analysis_intelligence_index"].unit == "index"
    assert scores["aa_scicode"].value == 62.5
    assert scores["aa_scicode"].benchmark == "SciCode"
    assert scores["aa_scicode"].unit == "percent"
    assert scores["aa_critpt"].value == 0
    assert "aa_lcr" not in scores
    assert len(row.metrics) == 7  # Headline plus six reported dimensions; missing is not zero.


@pytest.mark.parametrize(
    "updates",
    [
        {"slug": "sample-1-low"},
        {"shortName": "Sample-1 (low)", "name": "Sample-1 (Low)"},
        {"modelCreatorName": "Anthropic"},
        {"intelligenceIndex": 59},
    ],
)
def test_aa_subscores_must_match_slug_provider_setting_and_headline(updates):
    page = aa_public_html() + aa_chart_scripts([aa_detail_row(**updates)])
    assert len(parse_aa_public(page)[0].metrics) == 1


def test_ambiguous_chart_records_are_not_attached_and_scripts_are_not_executed():
    page = aa_public_html() + aa_chart_scripts([aa_detail_row(), aa_detail_row()])
    assert len(parse_aa_public(page)[0].metrics) == 1
    page = aa_public_html() + '<script>self.__next_f.push([1, eval("untrusted")])</script>'
    assert len(parse_aa_public(page)[0].metrics) == 1


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), True, "75%"])
def test_aa_subscores_reject_invalid_ratio_units(value):
    with pytest.raises(ValueError):
        parse_aa_public(aa_public_html() + aa_chart_scripts([aa_detail_row(scicode=value)]))


def test_parenthesized_snapshot_and_edition_are_not_inference_settings():
    rows = parse_aa_public(
        aa_public_html(
            [
                ("Sample-1 (0902)", "OpenAI", "45", "sample-1-0902"),
                ("Sample-1 (June 2026)", "OpenAI", "26", "sample-1-june"),
            ]
        )
    )
    assert not match_results([model()], rows)
    assert all(row.variant == "来源未注明配置" for row in rows)
    assert all("(" in row.model_key for row in rows)


def test_duplicate_public_labels_require_distinct_model_identity():
    rows = parse_aa_public(
        aa_public_html(
            [
                ("Sample-1", "OpenAI", "11", "sample-1-reasoning"),
                ("Sample-1", "OpenAI", "10 *", "sample-1-instruct"),
            ]
        )
    )
    assert len({row.id for row in rows}) == 2
    assert not match_results([model()], rows)
    assert match_results([model("openai:sample-1-instruct")], rows)


@pytest.mark.parametrize(
    "page",
    [
        "<html>Access unavailable</html>",
        aa_public_html().replace("Artificial Analysis Intelligence Index", "New metric"),
        aa_public_html().replace("52", "not a number"),
        aa_public_html().replace('/models/sample-1-high"', 'https://example.com/model"'),
        aa_public_html().replace("</table>", ""),
        aa_public_html([]),
    ],
)
def test_public_aa_rejects_schema_changes_and_incomplete_pages(page):
    with pytest.raises(ValueError):
        parse_aa_public(page)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.delenv(AA_KEY_ENV, raising=False)
    monkeypatch.setattr(settings, "artificial_analysis_api_key", None)
    return BenchmarkCatalog(Database(tmp_path / "benchmarks.db"))


def public_response(request):
    assert "authorization" not in request.headers and "x-api-key" not in request.headers
    url = str(request.url)
    if url == AA_PUBLIC_URL:
        return httpx.Response(200, text=aa_public_html())
    if url == BFCL_URL:
        return httpx.Response(200, text=BFCL_PAGE)
    if url == BFCL_DATA_URL:
        return httpx.Response(200, text=bfcl_csv())
    if url == SUPERCLUE_URL:
        return httpx.Response(200, text='<script src="/assets/index-123.js"></script>')
    if url.endswith("index-123.js"):
        return httpx.Response(200, text='import("./HomePage-abc.js")')
    if url.endswith("HomePage-abc.js"):
        return httpx.Response(200, text='import"./charts-main-xyz.js"')
    if url.endswith("charts-main-xyz.js"):
        return httpx.Response(200, text='loadExcelData("2026年10月")')
    if request.url.path.endswith("2026年10月.xlsx"):
        return httpx.Response(200, content=workbook_bytes())
    raise AssertionError(f"Unexpected public request {request.url}")


async def test_live_source_discovery_cache_persistence_and_model_matching(catalog):
    async with httpx.AsyncClient(transport=httpx.MockTransport(public_response)) as client:
        await catalog.refresh(client)
    status = catalog.status([model(), model("qwen:qwen3.8-max")])
    assert status["matched_models"] == 2
    assert status["sources"]["bfcl"]["status"] == "ok"
    assert status["sources"]["superclue"]["status"] == "ok"
    assert status["sources"]["artificial_analysis"]["status"] == "ok"
    assert status["sources"]["artificial_analysis"]["configured"] is False
    assert status["sources"]["artificial_analysis"]["collection_method"] == "public_page"
    assert any(result.version == "SuperCLUE 2026年10月" for result in catalog.results())
    restarted = BenchmarkCatalog(catalog.db)
    assert restarted.match_models([model()]) == catalog.match_models([model()])


async def test_source_failure_retains_previous_scores_without_leaking_error_body(catalog):
    async with httpx.AsyncClient(transport=httpx.MockTransport(public_response)) as client:
        await catalog.refresh(client)
    previous = catalog.match_models([model()])
    previous_aa = catalog.cache["artificial_analysis"]["results"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="secret-key"))
    ) as client:
        await catalog.refresh(client)
    assert catalog.match_models([model()]) == previous
    assert catalog.cache["bfcl"]["status"] == "error"
    assert catalog.cache["bfcl"]["last_success_at"]
    assert "secret-key" not in json.dumps(catalog.status([model()]))
    assert catalog.cache["artificial_analysis"]["status"] == "error"
    assert catalog.cache["artificial_analysis"]["results"] == previous_aa


async def test_aa_pagination_keeps_key_on_its_official_host(catalog, monkeypatch):
    monkeypatch.setenv(AA_KEY_ENV, "test-secret-key")
    pages = []

    def respond(request):
        if str(request.url) == AA_PUBLIC_URL:
            return public_response(request)
        if request.url.host != "artificialanalysis.ai":
            return public_response(request)
        assert request.headers["x-api-key"] == "test-secret-key"
        assert str(request.url).startswith(AA_URL)
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200, json=aa_body(page=page, more=page == 1, name=f"Sample-{page} (high)")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await catalog.refresh(client)
    assert pages == [1, 2]
    assert catalog.cache["artificial_analysis"]["status"] == "ok"
    assert catalog.cache["artificial_analysis"]["count"] == 2
    assert "test-secret-key" not in json.dumps(catalog.cache)
    assert catalog.cache["artificial_analysis"]["collection_method"] == "api"


async def test_api_indices_get_public_subscores_without_exposing_api_key(catalog, monkeypatch):
    monkeypatch.setenv(AA_KEY_ENV, "test-secret-key")

    def respond(request):
        if str(request.url) == AA_PUBLIC_URL:
            assert "x-api-key" not in request.headers
            return httpx.Response(
                200,
                text=aa_public_html()
                + aa_chart_scripts(
                    [
                        aa_detail_row(slug="sample-1"),
                    ]
                ),
            )
        if request.url.host == "artificialanalysis.ai":
            return httpx.Response(200, json=aa_body())
        return public_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await catalog.refresh(client)
    assert catalog.cache["artificial_analysis"]["status"] == "ok"
    assert catalog.cache["artificial_analysis"]["with_details"] == 1
    result = catalog.cache["artificial_analysis"]["results"][0]
    assert len(result["metrics"]) == 9
    assert result["metrics"][0]["value"] == 52.3


async def test_missing_public_chart_page_keeps_successful_api_indices(catalog, monkeypatch):
    monkeypatch.setenv(AA_KEY_ENV, "test-secret-key")

    def respond(request):
        if str(request.url) == AA_PUBLIC_URL:
            return httpx.Response(503)
        if request.url.host == "artificialanalysis.ai":
            return httpx.Response(200, json=aa_body())
        return public_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await catalog.refresh(client)
    assert catalog.cache["artificial_analysis"]["status"] == "ok"
    assert catalog.cache["artificial_analysis"]["with_details"] == 0
    assert len(catalog.cache["artificial_analysis"]["results"][0]["metrics"]) == 2


async def test_incomplete_or_mixed_version_aa_pages_do_not_replace_cache(catalog, monkeypatch):
    monkeypatch.setenv(AA_KEY_ENV, "test-secret-key")
    previous = parse_aa(aa_body())[0].model_dump(mode="json")
    catalog.cache["artificial_analysis"] = {"results": [previous]}

    def respond(request):
        if request.url.host != "artificialanalysis.ai":
            return public_response(request)
        page = int(request.url.params["page"])
        return httpx.Response(
            200, json=aa_body(page=page, more=page == 1, version=4.3 if page == 1 else 5)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await catalog.refresh(client)
    assert catalog.cache["artificial_analysis"]["status"] == "error"
    assert catalog.cache["artificial_analysis"]["results"] == [previous]


async def test_redirects_do_not_forward_aa_credentials(catalog, monkeypatch):
    monkeypatch.setenv(AA_KEY_ENV, "test-secret-key")
    seen = []

    def respond(request):
        seen.append(str(request.url))
        if request.url.host == "artificialanalysis.ai":
            return httpx.Response(302, headers={"location": "https://example.com/untrusted"})
        return public_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await catalog.refresh(client)
    assert catalog.cache["artificial_analysis"]["status"] == "error"
    assert not any("example.com" in url for url in seen)


async def test_compressed_responses_are_decoded_once(catalog):
    text = "公开评测数据"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=gzip.compress(text.encode()),
                headers={"content-encoding": "gzip", "content-type": "text/plain; charset=utf-8"},
            )
        )
    ) as client:
        response = await catalog._get(client, SUPERCLUE_URL)
    assert response.text == text
    assert "content-encoding" not in response.headers
