"""Files outside the requested document scope do not create permission failures."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from course_resource_download.platforms.stuonline import process_file


@pytest.mark.parametrize(
    "filetype, permission, status",
    [
        ("mp4", "1", "skipped_video"),
        ("mp4", None, "skipped_video"),
        ("zip", None, "skipped_non_document"),
    ],
)
def test_excluded_types_do_not_request_metadata(filetype, permission, status, tmp_path):
    page = Mock()
    entry = {}
    row = {"ftype": filetype, "candownload": permission, "name": "excluded"}
    process_file(
        page,
        row,
        "",
        str(tmp_path),
        SimpleNamespace(include_video=False, list_only=False),
        {},
        entry,
    )
    assert entry["status"] == status
    page.evaluate.assert_not_called()
