# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Host tests against a device running the dectnrp-spinel sample, reached
through the URL given with --transport."""

import pytest
from integration import scenarios


@pytest.fixture
def transport_url(request):
    url = request.config.getoption("--transport")
    if url is None:
        pytest.skip("needs --transport")
    return url


@pytest.fixture
async def radio(transport_url):
    async with scenarios.open_radio(transport_url) as radio:
        yield radio
