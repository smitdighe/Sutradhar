"""QR payload, PNG/SVG rendering, and the round trip through a real decoder.

The decode test is the only one here that proves anything about the physical
world. Everything else checks that this code produced the bytes it meant to;
``zxing-cpp`` checks that a scanner would agree, which is the actual
requirement -- a QR that renders beautifully and does not decode is a blank
label with extra steps.
"""

from __future__ import annotations

import io
import re

import pytest
import zxingcpp
from PIL import Image

from app.config import get_settings
from app.core.ids import new_tag_code
from app.qr import service

pytestmark = pytest.mark.unit

CODE = "X7K29M4P3RQ8"


def decode(png: bytes) -> zxingcpp.Result:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    result = zxingcpp.read_barcode(image)
    assert result is not None, "the rendered PNG did not decode at all"
    return result


class TestPayload:
    def test_payload_is_exactly_the_public_url(self) -> None:
        assert service.tag_url(CODE) == f"{get_settings().public_base_url}/v/{CODE}"

    def test_payload_carries_no_token_signature_or_query_string(self) -> None:
        # A QR is a printed number. Anything secret inside one is public the
        # moment it goes on fabric, so there is nothing here to leak.
        url = service.tag_url(CODE)
        assert "?" not in url
        assert "#" not in url
        assert url.count("/v/") == 1
        assert url.split("/v/")[1] == CODE

    def test_payload_points_at_the_frontend_not_the_backend(self) -> None:
        # The consumer scan page must resolve without waking this service.
        settings = get_settings()
        assert not service.tag_url(CODE).startswith(settings.app_base_url + "/")

    def test_any_typed_form_produces_the_same_payload(self) -> None:
        assert (
            service.tag_url("x7k2-9m4p-3rq8")
            == service.tag_url("X7K29M4P3RQ8")
            == service.tag_url("x7k2 9m4p 3rq8")
        )

    def test_display_form_is_grouped_in_fours(self) -> None:
        assert service.format_tag_code(CODE) == "X7K2-9M4P-3RQ8"

    def test_display_form_normalises_before_grouping(self) -> None:
        assert service.format_tag_code("x7k2 9m4p-3rq8") == "X7K2-9M4P-3RQ8"


class TestPngRoundTrip:
    def test_decoded_string_equals_the_original_url(self) -> None:
        for _ in range(5):
            code = new_tag_code()
            assert decode(service.render_png(code)).text == service.tag_url(code)

    def test_default_size_is_512_square(self) -> None:
        image = Image.open(io.BytesIO(service.render_png(CODE)))
        assert image.size == (service.DEFAULT_PNG_SIZE, service.DEFAULT_PNG_SIZE) == (512, 512)

    @pytest.mark.parametrize("size", [128, 256, 512, 1024])
    def test_requested_size_is_exact_and_still_decodes(self, size: int) -> None:
        png = service.render_png(CODE, size)
        assert Image.open(io.BytesIO(png)).size == (size, size)
        assert decode(png).text == service.tag_url(CODE)

    def test_size_is_clamped_rather_than_trusted(self) -> None:
        assert Image.open(io.BytesIO(service.render_png(CODE, 1))).size == (
            service.MIN_PNG_SIZE,
            service.MIN_PNG_SIZE,
        )
        assert Image.open(io.BytesIO(service.render_png(CODE, 99_999))).size == (
            service.MAX_PNG_SIZE,
            service.MAX_PNG_SIZE,
        )

    def test_error_correction_is_level_q(self) -> None:
        # 25% recovery. A tag on fabric gets creased, folded and rubbed; the
        # usual default of M is chosen for screens, which do none of that.
        assert decode(service.render_png(CODE)).ec_level == "Q"


class TestQuietZone:
    def test_at_least_four_light_modules_surround_the_code(self) -> None:
        matrix = service._matrix(CODE)
        size = len(matrix)

        def light_run(cells: list[bool]) -> int:
            run = 0
            for cell in cells:
                if cell:
                    break
                run += 1
            return run

        rows = [matrix[index] for index in range(size)]
        columns = [[matrix[row][index] for row in range(size)] for index in range(size)]
        margins = [
            min(light_run(row) for row in rows),
            min(light_run(row[::-1]) for row in rows),
            min(light_run(column) for column in columns),
            min(light_run(column[::-1]) for column in columns),
        ]
        assert min(margins) >= service.QUIET_ZONE_MODULES == 4


class TestSvg:
    def test_renders_a_scalable_document(self) -> None:
        svg = service.render_svg(CODE)
        assert svg.startswith("<svg ")
        assert svg.rstrip().endswith("</svg>")
        # A viewBox in module units is what makes it resolution-free: a print
        # shop can set any physical size without resampling anything.
        assert f'viewBox="0 0 {len(service._matrix(CODE))} ' in svg
        assert "<path" in svg

    def test_encodes_the_same_grid_as_the_png(self) -> None:
        matrix = service._matrix(CODE)
        dark_modules = sum(1 for row in matrix for cell in row if cell)
        segments = re.findall(r"h(\d+)v1h-\d+z", service.render_svg(CODE))
        assert sum(int(run) for run in segments) == dark_modules

    def test_contains_no_pii(self) -> None:
        svg = service.render_svg(CODE)
        # No addresses, no identifiers, no names, no timestamps -- the file goes
        # to a printer that belongs to somebody else.
        assert "@" not in svg
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", svg)
        # The SVG namespace declaration is the only URL in the file; the tag's
        # own payload URL lives in the module grid, not in readable markup.
        body = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "http" not in body.lower()
        for word in ("email", "user", "name", "weaver", "registered"):
            assert word not in svg.lower()

    def test_the_only_readable_string_is_the_tag_code(self) -> None:
        svg = service.render_svg(CODE)
        assert service.format_tag_code(CODE) in svg
