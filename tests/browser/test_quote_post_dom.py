"""Quote posts open X's quote composer from the exact target and keep its media."""
import pytest
from PIL import Image

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")

QUOTED = "https://x.com/person/status/123"


def quote_document(*, preview_author="person", quote_option=True, upload=True, reposted=False):
    preview = ('<div role="link"><span>Sterling</span><span>@' + preview_author + '</span>'
               '<div>The exact original post</div></div>') if preview_author else ""
    option = ('<a role="menuitem" href="/compose/post" id="quote-option">Quote</a>'
              if quote_option else "")
    file_input = ('''<input type="file" multiple onchange="
          document.documentElement.dataset.uploadCount=String(this.files.length);
          document.querySelector('[data-testid=attachments]').hidden=false;">
        <div data-testid="attachments" hidden>Clip thumbnail</div>''' if upload else "")
    control = "unretweet" if reposted else "retweet"
    return '''
      <main data-testid="primaryColumn">
        <article data-testid="tweet">
          <a href="/person/status/123"><time>Earlier</time></a>
          <div data-testid="User-Name"><span>Sterling</span><span>@person</span></div>
          <div data-testid="tweetText">The exact original post</div>
          <button data-testid="''' + control + '''" id="open-repost">Repost</button>
        </article>
      </main>
      <div role="menu" id="repost-menu" hidden>
        <div role="menuitem" data-testid="retweetConfirm" onclick="
          document.documentElement.dataset.repostClicks='1';">Repost</div>
        ''' + option + '''
      </div>
      <div role="dialog" id="quote-modal" hidden>
        <div contenteditable="true" data-testid="tweetTextarea_0" id="quote-text"></div>
        ''' + preview + file_input + '''
        <button data-testid="tweetButton" disabled>Post</button>
      </div>
      <script>
        document.querySelector('#open-repost').addEventListener('click', () => {
          document.querySelector('#repost-menu').hidden=false;
        });
        const quote=document.querySelector('#quote-option');
        if (quote) quote.addEventListener('click', event => {
          event.preventDefault();
          document.querySelector('#repost-menu').hidden=true;
          history.pushState({}, '', '/compose/post');
          document.querySelector('#quote-modal').hidden=false;
        });
        const button=document.querySelector('[data-testid=tweetButton]');
        document.querySelector('#quote-text').addEventListener('input', () => button.disabled=false);
        button.addEventListener('click', () => {
          document.documentElement.dataset.sendClicks='1';
          document.documentElement.dataset.submittedText=document.querySelector('#quote-text').innerText;
          document.querySelector('#quote-modal').remove();
          const toast=document.createElement('div'); toast.dataset.testid='toast';
          toast.innerHTML='Your post was sent <a href="/me/status/124">View</a>';
          document.body.appendChild(toast);
        });
      </script>
    '''


@pytest.mark.parametrize("reposted", [False, True])
async def test_quote_post_submits_text_and_media_in_the_quote_composer(app, tmp_path, reposted):
    path = tmp_path / "clip.png"
    Image.new("RGB", (10, 10), "blue").save(path)
    app.document("/person/status/123", quote_document(reposted=reposted))
    result = await app.adapter.post("My take", media=[str(path)], quote_tweet_url=QUOTED)
    assert result["success"] and result["evidence"]["tweet_id"] == "124"
    assert result["evidence"]["quote_of"] == "123"
    assert await app.clicks() == 1
    html = app.page.locator("html")
    assert await html.get_attribute("data-upload-count") == "1"
    assert await html.get_attribute("data-submitted-text") == "My take"
    assert await html.get_attribute("data-repost-clicks") is None


@pytest.mark.parametrize("preview_author", ["", "other"])
async def test_composer_without_the_quoted_post_stops_before_typing_or_send(app, preview_author):
    app.document("/person/status/123", quote_document(preview_author=preview_author))
    with pytest.raises(BrowserActionError, match="tweet_not_found"):
        await app.adapter.post("My take", quote_tweet_url=QUOTED)
    assert await app.page.locator("#quote-text").inner_text() == ""
    assert await app.clicks() == 0


async def test_missing_file_input_stops_the_quote_before_send(app, tmp_path):
    path = tmp_path / "clip.png"
    Image.new("RGB", (10, 10), "blue").save(path)
    app.document("/person/status/123", quote_document(upload=False))
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.post("My take", media=[str(path)], quote_tweet_url=QUOTED)
    assert await app.clicks() == 0


async def test_repost_menu_without_quote_never_reposts_or_sends(app):
    app.document("/person/status/123", quote_document(quote_option=False))
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.post("My take", quote_tweet_url=QUOTED)
    assert await app.page.locator("html").get_attribute("data-repost-clicks") is None
    assert await app.clicks() == 0
