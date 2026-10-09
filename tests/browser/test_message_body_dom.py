"""Observed authored-text/timestamp separation in modern message bodies."""
import pytest

from test_messaging_dom import app, native_browser  # noqa: F401
from test_messaging_send_dom import CHAT_URL, modern_chat_script


pytestmark = pytest.mark.asyncio(loop_scope="module")
AUTHORED = "Authored time 8:18 PM\n9:00 AM\nhttps://example.test/ 🐦 اردو"


def observed_body_script(behavior="complete", multi_focus=False):
    document = modern_chat_script(behavior, existing_text=AUTHORED).replace(
        '.body{white-space:pre-wrap}', '.body,.whitespace-pre-wrap{white-space:pre-wrap}',
    ).replace(
        "body.className='body';body.textContent=text;",
        """body.className='relative flex justify-end px-4 py-2 rounded-chat bg-gray-50';
        const contents=document.createElement('div');contents.className='text-text relative inline-flex flex-wrap items-end gap-2 overflow-hidden';
        const holder=document.createElement('span');holder.className='max-w-full text-body font-normal text-[color:inherit]';
        const authored=document.createElement('span');authored.dir='auto';authored.className='font-chirp max-w-full whitespace-pre-wrap break-words text-inherit text-body font-normal';
        const content=document.createElement('span');content.className='font-chirp max-w-full whitespace-pre-wrap break-words text-inherit text-[length:inherit] font-inherit';content.textContent=text;authored.append(content);
        const spacer=document.createElement('span');spacer.setAttribute('aria-hidden','true');spacer.className='user-select-none inline-block pl-2 opacity-0';spacer.innerHTML='<div class="text-gray700 justify-start flex items-center ml-auto shrink-0 gap-1"><span class="max-w-full text-subtext3">8:18 PM</span></div>';
        const stamp=document.createElement('div');stamp.className='absolute bottom-0 inset-e-0';stamp.innerHTML='<div class="text-gray700 justify-start flex items-center ml-auto shrink-0 gap-1"><span class="max-w-full text-subtext3">8:18 PM</span></div>';
        holder.append(authored,spacer);contents.append(holder,stamp);body.append(contents);""")
    if multi_focus:
        document = document.replace('row.append(focus);', '''row.append(focus);
          const preview=document.createElement('div');preview.setAttribute('data-dm-message-focus-target','');
          preview.textContent='Separate preview';row.append(preview);''')
    return document


@pytest.mark.parametrize("multi_focus", [False, True])
async def test_native_observed_body_keeps_authored_times_and_excludes_timestamp_decorations(app, multi_focus):
    app.document("/i/chat/10-20", observed_body_script(multi_focus=multi_focus))
    await app.page.goto(CHAT_URL)
    result = await app.adapter.get_conversation(CHAT_URL)
    assert result["messages"][0]["text"] == AUTHORED
    scope = await app.adapter._conversation_scope()
    assert (await app.adapter._message_snapshot(scope))[0]["text"] == AUTHORED


@pytest.mark.parametrize("multi_focus", [False, True])
async def test_native_observed_body_confirms_exact_authored_text_with_timestamp_decorations(app, multi_focus):
    app.document("/i/chat/10-20", observed_body_script(multi_focus=multi_focus))
    await app.page.goto(CHAT_URL)
    result = await app.adapter.send_message("@alice", AUTHORED)
    assert result["success"] and await app.clicks() == 1
