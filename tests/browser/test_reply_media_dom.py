"""Reply attachment uses the same verified native composer as text replies."""
import pytest
from PIL import Image

from xuse.browser.errors import BrowserActionError
from test_post_dom import nested_reply_document
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_reply_upload_and_exact_text_are_submitted_once(app, tmp_path):
    path = tmp_path / "chart.png"
    Image.new("RGB", (10, 10), "blue").save(path)
    document = nested_reply_document()
    upload = '''<input type="file" multiple onchange="
      document.documentElement.dataset.uploadCount=String(this.files.length);
      document.querySelector('[data-testid=attachments]').hidden=false;">
      <div data-testid="attachments" hidden>Chart thumbnail</div>'''
    document = document.replace('<div data-testid="toolBar">', upload + '<div data-testid="toolBar">')
    app.document("/person/status/123", document)
    result = await app.adapter.reply("https://x.com/person/status/123", "Context with a chart", media=[str(path)])
    assert result["success"] and result["evidence"]["reply_to"] == "123"
    assert await app.clicks() == 1
    assert await app.page.locator("html").get_attribute("data-upload-count") == "1"
    assert await app.page.locator("html").get_attribute("data-submitted-text") == "Context with a chart"


async def test_missing_reply_attachment_stops_before_typing_or_send(app, tmp_path):
    app.document("/person/status/123", nested_reply_document())
    with pytest.raises(BrowserActionError, match="media_invalid"):
        await app.adapter.reply("https://x.com/person/status/123", "Do not send", media=[str(tmp_path / "missing.png")])
    assert await app.clicks() == 0
    assert await app.page.get_by_test_id("tweetTextarea_0").nth(1).inner_text() == ""
