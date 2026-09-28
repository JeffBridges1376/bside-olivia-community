"""The native mailbox patch recognizes silence without a failed/pending reply."""
import shutil
import subprocess

import pytest

from runtime.personal_chat import _patch_companion_settings_base as patch


SOURCE = '''
let bt=(e=>(e[e.FAILED=5]="FAILED",e))(bt||{});
function Wn(e,t,s){return t===bt.FAILED?s===co.REJECTED?"audit_fail":"error":e===Yt.TEXT?"text":"video"}
function bn(e){const t=e.letterStatus===bt.FAILED||e.replyType!==Yt.NONE&&e.letterStatus===bt.REPLIED;return{isUnread:e.isRead===0,received:t?{type:Wn(e.replyType,e.letterStatus,e.auditStatus)}:void 0}}
function p1(e){const s=e.letterStatus===bt.FAILED||e.replyType!==Yt.NONE&&e.letterStatus===bt.REPLIED;return{isUnread:e.isRead===0,received:s?{type:Wn(e.replyType,e.letterStatus,e.auditStatus)}:void 0}}
function render(A){return A.type==="error"?n("button",{},"retry"):!A.modelValue&&A.type!=="video"?n("video",{},"waiting"):n("textarea",{},A.modelValue)}
'''


def test_silent_patch_is_idempotent_and_executes_terminal_native_branch(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    transformed = patch._repair_native_silent_reply(SOURCE)
    assert patch._repair_native_silent_reply(transformed) == transformed
    # The fixture has let rather than the bundle's surrounding var declaration.
    transformed = transformed.replace('let bt=', 'var bt=')
    script = '''const assert=require('node:assert/strict');
const co={REJECTED:3},Yt={NONE:0,TEXT:1};
const n=(tag,props,text)=>({tag,props,text});
''' + transformed + '''
const input={letterStatus:6,replyType:0,isRead:0};
for(const project of [bn,p1]){
 const row=project(input);
 assert.equal(row.isUnread,false);
 assert.equal(row.received.type,'no_reply');
 const view=render({type:row.received.type,modelValue:''});
 assert.equal(view.tag,'div');
 assert.equal(view.props.role,'status');
 assert.equal(view.text,'本轮不回复');
}
assert.equal(render({type:'error'}).tag,'button');
assert.equal(render({modelValue:''}).tag,'video');
assert.equal([1,2,3].includes(input.letterStatus),false);
'''
    target = tmp_path / 'silent.cjs'
    target.write_text(script, encoding='utf-8')
    completed = subprocess.run([node, str(target)], capture_output=True, text=True, encoding='utf-8')
    assert completed.returncode == 0, completed.stderr


def test_standard_mailbox_patch_applies_silent_extension(tmp_path):
    root = tmp_path / 'assets'
    root.mkdir()
    main = root / 'main-31595bd3.js'
    source = SOURCE + patch.MAILBOX_WRITE_ANCHOR_0627
    main.write_text(source, encoding='utf-8')
    assert patch._repair_mailbox_write_access(tmp_path) == 'PATCHED'
    first = main.read_text(encoding='utf-8')
    assert 'A.type==="no_reply"?' in first and 'e[e.NO_REPLY=6]' in first
    assert patch._repair_mailbox_write_access(tmp_path) == 'ALREADY_PATCHED'
    assert main.read_text(encoding='utf-8') == first
