# -*- coding: utf-8 -*-
"""离线验证 main.py 的 TTS 帧构建 / 解析 / 认证握手逻辑（不联网）。"""
import asyncio
import json
import os
import struct
import sys

os.environ.setdefault("DEEPSEEK_API_KEY", "test-deepseek-key")
os.environ.setdefault("TTS_API_KEY", "test-tts-key")

import main as app  # noqa: E402


def make_server_frame(event, session_id, payload: bytes, with_sid=True):
    """按协议构造服务端帧（与客户端 build_frame 相同结构）。"""
    h = bytearray(8)
    h[0], h[1], h[2], h[3] = 0x11, 0x14, 0x10, 0x00
    struct.pack_into(">I", h, 4, event)
    if with_sid:
        sid = session_id.encode()
        h += struct.pack(">I", len(sid)) + sid
    h += struct.pack(">I", len(payload)) + payload
    return bytes(h)


class FakeWS:
    """模拟服务端：按剧本依次回帧。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self.script:
            raise Exception("no more frames")
        return self.script.pop(0)


class FakeConnect:
    """模拟 websockets.connect 的返回值：既支持 async with 也支持 await。"""

    def __init__(self, script):
        self.script = script

    async def __aenter__(self):
        return FakeWS(self.script)

    async def __aexit__(self, *a):
        return False

    def __await__(self):
        async def _inner():
            return self
        return _inner().__await__()


def fake_connect(url, **kwargs):
    return FakeConnect(SCRIPT)


# ---------- 用例 1：正常流程 ----------
def build_success_script(sid):
    auth = json.dumps({"code": 200, "message": "success"}).encode()
    audio1 = b"\x00" + struct.pack(">I", 0) + b"AAAA"  # 类型0 + 序号 + 音频
    audio2 = b"\x00" + struct.pack(">I", 1) + b"BBBB"
    return [
        make_server_frame(app.EVENT_AUTH, "", auth, with_sid=False),
        make_server_frame(app.EVENT_TTS_RESPONSE, sid, audio1),
        make_server_frame(app.EVENT_TTS_RESPONSE, sid, audio2),
        make_server_frame(app.EVENT_SESSION_FINISHED, sid, b""),
    ]


async def run_case(name, script, expect_audio=b"AAAABBBB", expect_err=None):
    global SCRIPT
    SCRIPT = script
    app.websockets.connect = fake_connect
    data, err = await app.tts_websocket("你好，娘子")
    if expect_err is not None:
        ok = err is not None and expect_err in err
        print(f"[{name}] 期望错误包含 '{expect_err}' -> {'PASS' if ok else 'FAIL'}, err={err!r}")
        return
    if data == expect_audio and err is None:
        print(f"[{name}] 音频拼接正确 ({len(data)}B) -> PASS")
    else:
        print(f"[{name}] FAIL: data={data!r} err={err!r} (期望 {expect_audio!r})")


async def main_test():
    # 用例 1：完整正常流程（认证->两帧音频->会话结束）
    sid = "session-test-1"
    await run_case("正常流程", build_success_script(sid), expect_audio=b"AAAABBBB")

    # 用例 2：认证失败（code != 200）
    sid = "session-test-2"
    bad_auth = [make_server_frame(app.EVENT_AUTH, "", json.dumps({"code": 401, "message": "bad key"}).encode(), with_sid=False)]
    await run_case("认证失败", bad_auth, expect_err="认证失败")

    # 用例 3：连收几帧都不是认证帧 -> 报"未收到认证响应"
    sid = "session-test-3"

    async def no_auth_case():
        global SCRIPT
        # 服务端返回 3 个非认证帧（如未知事件），随后无帧可读
        SCRIPT = [make_server_frame(999, "", b"x", with_sid=False)] * 3
        app.websockets.connect = fake_connect
        data, err = await app.tts_websocket("x")
        ok = err is not None and "EVENT_AUTH" in err
        print(f"[无认证响应] 期望错误含 EVENT_AUTH -> {'PASS' if ok else 'FAIL'}, err={err!r}")

    await no_auth_case()

    # 用例 4：JSON 元信息里带错误码
    sid = "session-test-4"
    err_meta = [
        make_server_frame(app.EVENT_AUTH, "", json.dumps({"code": 200}).encode(), with_sid=False),
        make_server_frame(app.EVENT_TTS_RESPONSE, sid, b"\x01" + json.dumps({"code": 400, "msg": "bad"}).encode()),
    ]
    await run_case("JSON错误码", err_meta, expect_err="合成出错")

    # 用例 5：parse_frame 边界
    ev, sid2, payload = app.parse_frame(b"")
    assert (ev, sid2, payload) == (None, "", b""), "空帧解析失败"
    frame = make_server_frame(352, "s1", b"DATA")
    ev, sid2, payload = app.parse_frame(frame)
    assert ev == 352 and sid2 == "s1" and payload == b"DATA", f"parse_frame 失败: {(ev, sid2, payload)}"
    print("[parse_frame 边界] PASS")

    # 用例 6：环境探测（本机 flet 0.86 无 ft.Audio）
    print(f"[环境] _HAS_FLET_AUDIO={app._HAS_FLET_AUDIO}, hasattr(ft,'Audio')={hasattr(app.ft, 'Audio')} "
          f"-> 将使用系统播放器兜底（winsound）")


asyncio.run(main_test())
print("ALL DONE")
