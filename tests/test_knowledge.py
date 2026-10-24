"""Knowledge metadata traversal, attachment ownership, and no player navigation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from course_resource_download.platforms import aicourse as ai
from course_resource_download.platforms import knowledge as kg


def catalog():
    return [
        {
            "name": "Theme",
            "nodes": [],
            "children": [
                {
                    "name": "Section",
                    "nodes": [
                        {"knowledgeId": "one", "knowledgeName": "One"},
                        {"knowledgeId": "two", "knowledgeName": "Two"},
                    ],
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://remote.example/cdp",
        "file:///tmp/cdp",
        "ws://user:pass@localhost:9333",
        "ws://localhost:9333?token=secret",
    ],
)
def test_remote_or_credentialed_cdp_refused(url):
    with pytest.raises(ValueError):
        kg.validate_cdp_endpoint(url)


def test_catalog_identity_and_path():
    nodes = kg.flatten_catalog(catalog())
    assert [n["knowledgeId"] for n in nodes] == ["one", "two"]
    assert nodes[0]["chapter"] == "Theme/Section"
    broken = catalog()
    broken[0]["children"][0]["nodes"][1]["knowledgeId"] = "one"
    with pytest.raises(ValueError, match="Duplicate"):
        kg.flatten_catalog(broken)


def test_node_failure_keeps_other_resources_and_checkpoints():
    page = Mock()
    page.evaluate.side_effect = [
        True,
        catalog(),
        [
            {"name": "note", "url": "https://example.invalid/a.pdf", "canDownload": False},
            {"name": "ebook", "type": 21},
        ],
        ValueError("Unavailable node"),
    ]
    resources, records, snapshots = [], [], []
    kg.harvest_knowledge(
        page,
        SimpleNamespace(course_id="c", class_id="x"),
        resources,
        records,
        lambda: snapshots.append([r["status"] for r in records]),
    )
    assert [r["status"] for r in records] == ["ok", "failed"]
    assert [ai.resource_entry(r)["status"] for r in resources] == [
        "permission_denied",
        "skipped_ebook",
    ]
    assert snapshots == [["ok", "pending"], ["ok", "failed"]]
    page.goto.assert_not_called()
    page.click.assert_not_called()


def test_video_cover_does_not_merge_distinct_videos():
    page = Mock()
    page.evaluate.side_effect = [
        True,
        [{"name": "Theme", "nodes": [{"knowledgeId": "one"}]}],
        [
            {
                "resourceId": key,
                "type": 22,
                "url": "https://example.invalid/cover.jpg",
                "isVideo": True,
            }
            for key in ("first", "second")
        ],
    ]
    resources = []
    kg.harvest_knowledge(page, SimpleNamespace(course_id="c", class_id="x"), resources, [])
    entries = [ai.resource_entry(r) for r in ai.deduplicate_resources(resources)]
    assert [r["resourceId"] for r in entries] == ["first", "second"]
    assert all(r["status"] == "skipped_video" and r["url"] is None for r in entries)


@pytest.mark.parametrize("mode", ["success", "failure", "late_tasks"])
def test_cdp_main_uses_own_tab_and_leaves_browser_open(tmp_path, monkeypatch, mode):
    factory = MagicMock()
    playwright = factory.return_value.__enter__.return_value
    browser = playwright.chromium.connect_over_cdp.return_value
    context = Mock()
    browser.contexts = [context]
    page = context.new_page.return_value
    page.url = ai.BASE
    page.evaluate.side_effect = (
        RuntimeError("Metadata unavailable")
        if mode == "failure"
        else [True, catalog(), [{"name": "ebook", "type": 21}], []]
    )
    callback = None

    def register(event, cb):
        nonlocal callback
        callback = cb

    def goto(*args, **kwargs):
        response = Mock(url=ai.BASE + "/list-knowledge-theme")
        response.text.return_value = '{"code":200,"data":{}}'
        callback(response)

    page.on.side_effect = register
    page.goto.side_effect = goto
    if mode == "late_tasks":

        def late_response(duration):
            response = Mock(url=ai.BASE + "/get-project-task-path-cards")
            response.text.return_value = '{"data":{"teachTasks":[]}}'
            callback(response)

        page.wait_for_timeout.side_effect = late_response
    monkeypatch.setattr(ai, "sync_playwright", factory)
    assert ai.main(
        [
            "--course-id",
            "c",
            "--class-id",
            "x",
            "--out",
            str(tmp_path),
            "--cdp-endpoint",
            "ws://127.0.0.1:9333",
            "--list-only",
        ]
    ) == (1 if mode == "failure" else 0)
    browser.close.assert_not_called()
    browser.new_context.assert_not_called()
    context.close.assert_not_called()
    page.close.assert_called_once()
    assert page.goto.call_count == 1
    assert "/singleCourse/knowledgeStudy/" in page.goto.call_args.args[0]
    report = json.loads((tmp_path / "_discovery.json").read_text(encoding="utf-8"))
    if mode == "failure":
        assert report["tasks"][0]["status"] == "failed"
    elif mode == "late_tasks":
        page.evaluate.assert_not_called()
        assert report["statistics"] == {}
    else:
        assert len(report["tasks"]) == 2
        assert report["statistics"] == {"skipped_ebook": 1}
