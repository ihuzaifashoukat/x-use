"""Native standalone posts select the innermost verified visible composer."""
import pytest

from xuse.browser.errors import BrowserActionError
from test_messaging_dom import app, native_browser  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="module")


def nested_post(*, hidden_outer=True, duplicate=False, human_draft=False, duplicate_textbox=False):
    outer = ' style="visibility:hidden"' if hidden_outer else ''
    inner = ' style="visibility:visible"' if hidden_outer else ''
    draft = "Human draft" if human_draft else ""
    return '''<div role="dialog" id="outer"''' + outer + '''>
      <div role="dialog" id="composer"''' + inner + '''>
        <div contenteditable="true" data-testid="tweetTextarea_0" id="text">''' + draft + '''</div>
        <button data-testid="tweetButton" disabled>Post</button>
      </div></div><script>
      const field=document.querySelector('#text');const button=document.querySelector('[data-testid=tweetButton]');
      field.addEventListener('input',()=>button.disabled=false);
      button.addEventListener('click',()=>{
        document.documentElement.dataset.sendClicks='1';document.documentElement.dataset.submittedText=field.innerText;
        document.querySelector('#outer').remove();const toast=document.createElement('div');toast.dataset.testid='toast';
        toast.innerHTML='Your post was sent <a href="/fixture/status/124">View</a>';document.body.append(toast);
      });
      ''' + ("document.body.append(document.querySelector('#outer').cloneNode(true));" if duplicate else "") + '''
      ''' + ("field.after(field.cloneNode(true));" if duplicate_textbox else "") + '''
      </script>'''


@pytest.mark.parametrize("hidden_outer", [True, False])
async def test_redirected_post_uses_visible_inner_composer_and_submits_once(app, hidden_outer):
    app.document("/compose/tweet", nested_post(hidden_outer=hidden_outer) +
                 "<script>history.replaceState({}, '', '/compose/post');</script>")
    result = await app.adapter.post("Reviewed standalone post")
    assert result["success"] and result["evidence"]["tweet_id"] == "124"
    assert await app.clicks() == 1
    assert await app.page.locator("html").get_attribute("data-submitted-text") == "Reviewed standalone post"


async def test_native_nonvisible_outer_matches_live_visibility_and_breaks_first_role_wait(app):
    # A zero-size outer dialog remains in the role query while its positioned
    # inner dialog is visible. ClientRects length alone misses this distinction.
    body = nested_post(hidden_outer=False).replace(
        'id="outer"', 'id="outer" style="width:0;height:0"').replace(
        'id="composer"', 'id="composer" style="position:fixed;top:20px;left:20px;width:350px;min-height:150px"')
    app.document("/compose/tweet", body +
                 "<script>history.replaceState({}, '', '/compose/post');</script>")
    await app.page.goto("https://x.com/compose/tweet")
    roles = app.page.get_by_role("dialog")
    assert await roles.count() == 2
    assert [await roles.nth(index).is_visible() for index in range(2)] == [False, True]
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter._wait_visible(roles, timeout_ms=200)
    assert await app.page.locator("#text").inner_text() == ""
    assert await app.clicks() == 0


@pytest.mark.parametrize("option,reason", [("duplicate", "unsupported_dom"),
                                         ("duplicate_textbox", "unsupported_dom"),
                                         ("human_draft", "composer_not_empty")])
async def test_ambiguous_or_nonempty_standalone_composer_never_types_or_submits(app, option, reason):
    app.document("/compose/tweet", nested_post(**{option: True}))
    with pytest.raises(BrowserActionError, match=reason):
        await app.adapter.post("Reviewed standalone post")
    assert await app.clicks() == 0
    assert "Reviewed standalone post" not in await app.page.locator("body").inner_text()


async def test_standalone_post_route_change_after_typing_stops_before_policy_and_submit(app):
    app.document("/compose/tweet", nested_post() + '''<script>
      document.querySelector('#text').addEventListener('input',()=>history.replaceState({}, '', '/home'));
      </script>''')
    policy_checks = []
    app.adapter.action_validator = lambda: policy_checks.append(True)
    with pytest.raises(BrowserActionError, match="unsupported_dom"):
        await app.adapter.post("Reviewed standalone post")
    assert policy_checks == [] and await app.clicks() == 0
