"""Native loopback-only proof that WebRTC cannot send unproxied UDP."""
import asyncio
import importlib
import os

import pytest

from xuse.browser.sessions import PatchrightSessionPool, PlaywrightSessionPool


class PacketCounter(asyncio.DatagramProtocol):
    def __init__(self):
        self.count = 0

    def datagram_received(self, data, addr):
        self.count += 1


async def gather(browser, port):
    context = await browser.new_context()
    try:
        page = await context.new_page()
        await page.evaluate("""async port => {
            const peer = new RTCPeerConnection({iceServers:[{urls:`stun:127.0.0.1:${port}`}]});
            try {
                peer.createDataChannel('synthetic');
                await peer.setLocalDescription(await peer.createOffer());
                await new Promise(resolve => setTimeout(resolve, 2000));
            } finally { peer.close(); }
        }""", port)
    finally:
        await context.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", os.environ.get("XUSE_TEST_BROWSER_DRIVERS", os.environ.get("XUSE_TEST_BROWSER_DRIVER", "patchright,playwright")).split(","))
async def test_managed_browser_blocks_udp_with_a_working_native_baseline(driver, make_config_loader, tmp_path):
    if driver not in {"patchright", "playwright"}:
        pytest.fail("Unsupported browser driver")
    try:
        api = importlib.import_module(f"{driver}.async_api")
    except ModuleNotFoundError:
        if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
            pytest.fail("Required browser driver unavailable")
        pytest.skip("Optional browser driver unavailable")
    channel = os.environ.get("XUSE_TEST_BROWSER_CHANNEL", "chrome")
    # Match the CI fixtures: "chromium" selects the installed default bundled
    # headless executable; named installed channels are explicit overrides.
    options = {"headless": True}
    settings = {"mcp": {"browser_backend": driver, "browser_headless": True}}
    if channel != "chromium":
        options["channel"] = channel
        settings["mcp"]["browser_channel"] = channel
    pool_type = PatchrightSessionPool if driver == "patchright" else PlaywrightSessionPool
    pool = pool_type(make_config_loader(settings=settings), lock_directory=tmp_path / "locks")
    transport, counter = await asyncio.get_running_loop().create_datagram_endpoint(PacketCounter, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    try:
        async with api.async_playwright() as runtime:
            try:
                baseline = await runtime.chromium.launch(**options)
            except Exception:
                if os.environ.get("XUSE_REQUIRE_BROWSER_TESTS") == "1":
                    pytest.fail("Required native browser unavailable")
                pytest.skip("Native browser unavailable")
            try:
                await gather(baseline, port)
            finally:
                await baseline.close()
        assert counter.count > 0, "The local UDP baseline must work to prove containment."
        counter.count = 0
        protected = await pool._get_browser()
        await gather(protected, port)
        assert counter.count == 0, "Managed browser sent UDP outside the configured HTTP route."
    finally:
        await pool.close_all()
        transport.close()
