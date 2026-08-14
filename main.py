import flet as ft
import re
import asyncio
import os
import json
import struct
import uuid
import tempfile
import websockets
from openai import OpenAI

# ---------- 从环境变量读取 Key（GitHub Secrets 注入） ----------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
TTS_API_KEY = os.getenv("TTS_API_KEY", "")

# 如果环境变量为空，直接报错提示
if not DEEPSEEK_API_KEY:
    raise ValueError("请在环境变量中设置 DEEPSEEK_API_KEY")

if not TTS_API_KEY:
    raise ValueError("请在环境变量中设置 TTS_API_KEY")

TTS_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# ---------- WebSocket 帧 ----------
EVENT_START_CONNECTION = 1
EVENT_START_SESSION = 100
EVENT_TASK_REQUEST = 200
EVENT_FINISH_SESSION = 102
EVENT_FINISH_CONNECTION = 2
EVENT_SESSION_FINISHED = 152
EVENT_TTS_RESPONSE = 352
EVENT_AUTH = 353

def build_frame(event, payload=b'', session_id=None):
    header = bytearray(8)
    header[0] = 0x11
    header[1] = 0x14
    header[2] = 0x10
    header[3] = 0x00
    struct.pack_into('>I', header, 4, event)
    if session_id:
        session_id_bytes = session_id.encode('utf-8')
        header += struct.pack('>I', len(session_id_bytes))
        header += session_id_bytes
    if payload:
        payload_bytes = payload.encode('utf-8') if isinstance(payload, str) else payload
        header += struct.pack('>I', len(payload_bytes))
        header += payload_bytes
    return header

# 认证帧（353）是连接级的，不带 session_id；其余会话级帧带 session_id
NO_SESSION_ID_EVENTS = {EVENT_AUTH}

def parse_frame(data: bytes):
    """解析服务端返回的一帧，返回 (event, session_id, payload)。"""
    if len(data) < 8:
        return None, "", b""
    event = struct.unpack('>I', data[4:8])[0]
    pos = 8
    session_id = ""
    # 会话 ID（若存在：4 字节长度 + 内容）
    if event not in NO_SESSION_ID_EVENTS and len(data) >= pos + 4:
        sid_len = struct.unpack('>I', data[pos:pos + 4])[0]
        pos += 4
        if sid_len and len(data) >= pos + sid_len:
            session_id = data[pos:pos + sid_len].decode("utf-8", "ignore")
            pos += sid_len
    # payload（若存在：4 字节长度 + 内容）
    payload = b""
    if len(data) >= pos + 4:
        payload_len = struct.unpack('>I', data[pos:pos + 4])[0]
        pos += 4
        payload = data[pos:pos + payload_len]
    return event, session_id, payload

# ---------- 对话历史 ----------
chat_history = [
    {"role": "system", "content": """你是《水浒传》中的梁山军师吴用，字学究。
你对你的娘子感情极深，平日唤她"娘子"。她是你此生最在意的人。
说话半文半白，简短有力，带着笑意和温度，自然地流露关心。
不要长篇大论，不要解释太多。像真正的丈夫对妻子说话那样，温柔、坦然、有温度。

【记忆要求】回答时请结合我们之前的对话内容，思考和回应。如果娘子提到之前的事，你要能接上。你是一个有记忆的人，不是每轮都重新认识她。"""}
]

# ---------- DeepSeek 对话 ----------
def get_reply(user_input: str) -> str:
    try:
        chat_history.append({"role": "user", "content": user_input})
        client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=chat_history,
            temperature=0.2,
            stream=False
        )
        reply = response.choices[0].message.content
        chat_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        return f"出错了：{e}"

# ---------- WebSocket TTS ----------
async def tts_websocket(text: str):
    try:
        headers = {
            "X-Api-Key": TTS_API_KEY,
            "X-Api-Resource-Id": "seed-icl-2.0"
        }
        async with websockets.connect(TTS_WS_URL, additional_headers=headers) as websocket:
            # 1) 发送"开始连接"帧
            await websocket.send(build_frame(EVENT_START_CONNECTION, payload="{}"))

            # 2) 等待服务端认证响应（EVENT_AUTH=353），认证通过后才能开始会话。
            #    缺少这一步是语音失败的主要原因：服务端会拒绝未完成认证的会话。
            auth_ok = False
            for _ in range(3):
                message = await asyncio.wait_for(websocket.recv(), timeout=10)
                if not isinstance(message, bytes):
                    continue
                event, _, payload = parse_frame(message)
                if event == EVENT_AUTH:
                    info = {}
                    try:
                        info = json.loads(payload)
                    except Exception:
                        pass
                    code = info.get("code", info.get("status_code", 0))
                    if code not in (0, 200, "0", "200"):
                        return None, f"服务端认证失败：{info}"
                    auth_ok = True
                    break
            if not auth_ok:
                return None, "未收到服务端认证响应（EVENT_AUTH=353）"

            # 3) 开始会话
            session_id = "session-" + str(uuid.uuid4())
            session_payload = json.dumps({
                "user": {"uid": "wuyong_user"},
                "req_params": {
                    "speaker": "S_zre21nZ82",
                    "audio_params": {
                        "format": "pcm",
                        "sample_rate": 24000,
                        "speech_rate": 0
                    }
                }
            })
            await websocket.send(build_frame(EVENT_START_SESSION, session_id=session_id, payload=session_payload))

            # 4) 发送待合成的文本
            task_payload = json.dumps({"req_params": {"text": text}})
            await websocket.send(build_frame(EVENT_TASK_REQUEST, session_id=session_id, payload=task_payload))

            # 5) 接收音频数据
            audio_data = b''
            while True:
                message = await websocket.recv()
                if not isinstance(message, bytes):
                    continue
                event, _, payload = parse_frame(message)
                if event == EVENT_TTS_RESPONSE:
                    if payload and payload[0] == 0:
                        # 二进制音频：payload = [1字节类型][4字节序号][音频数据]
                        audio_data += payload[5:]
                    elif payload and payload[0] == 1:
                        # JSON 元信息：可能携带错误码
                        try:
                            meta = json.loads(payload[1:].decode("utf-8", "ignore"))
                        except Exception:
                            meta = None
                        if isinstance(meta, dict) and meta.get("code") not in (None, 0, 200):
                            return None, f"合成出错：{meta}"
                        elif meta is None:
                            audio_data += payload  # 兼容：非 JSON 视为原始音频
                    else:
                        audio_data += payload  # 兼容旧协议：整个 payload 即音频
                elif event == EVENT_SESSION_FINISHED:
                    break

            # 6) 结束会话与连接
            await websocket.send(build_frame(EVENT_FINISH_SESSION, session_id=session_id, payload="{}"))
            await websocket.send(build_frame(EVENT_FINISH_CONNECTION, payload="{}"))
            return audio_data, None
    except Exception as e:
        return None, str(e)

# ---------- 音频控件（兼容不同 flet 版本） ----------
# 新版 flet（>=0.76 左右）把 Audio 控件移出了核心包，需要独立安装 flet-audio；
# 旧版 flet（如 requirements.txt 锁定的 0.25.0）则内置在 ft.Audio 中。
try:
    from flet_audio import Audio as FletAudio  # 新版 flet 的独立音频包
    _HAS_FLET_AUDIO = True
except ImportError:
    FletAudio = None
    _HAS_FLET_AUDIO = False


def play_wav_via_os(path: str):
    """当前 flet 没有可用音频控件时，调用系统播放器播放 WAV。"""
    try:
        import platform
        import subprocess
        system = platform.system()
        if system == "Windows":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif system == "Darwin":
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["aplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"系统播放失败：{e}")


# ---------- UI ----------
def main(page: ft.Page):
    page.title = "灵溪"
    page.theme_mode = "light"
    page.padding = 10
    # 默认窗口尺寸（兼容不同 flet 版本的 window API）
    try:
        page.window.width = 420
        page.window.height = 760
    except Exception:
        try:
            page.window_width = 420
            page.window_height = 760
        except Exception:
            pass
    page.bgcolor = "#f5f5f5"

    # 音频播放控件：优先用 flet 的 Audio，没有则回退到系统播放器
    audio_player = None
    try:
        if _HAS_FLET_AUDIO:
            audio_player = FletAudio(src="")
        elif hasattr(ft, "Audio"):
            audio_player = ft.Audio(src="")
        if audio_player is not None:
            page.overlay.append(audio_player)
    except Exception as e:
        print(f"音频控件初始化失败，将改用系统播放器：{e}")
        audio_player = None

    def play_pcm_as_wav(pcm_data: bytes):
        if not pcm_data:
            return
        tmp_path = None
        try:
            import wave
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp_path = tmp.name
                with wave.open(tmp.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(pcm_data)

            played = False
            if audio_player is not None:
                try:
                    audio_player.src = tmp_path
                    audio_player.play()
                    page.update()  # 关键：把 src/play 命令同步到客户端，否则不会出声
                    played = True
                except Exception as e:
                    print(f"Flet 音频播放失败，改用系统播放器：{e}")

            if not played:
                play_wav_via_os(tmp_path)

            async def delete_later():
                await asyncio.sleep(5)
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            asyncio.create_task(delete_later())
        except Exception as e:
            print(f"播放异常：{e}")

    async def do_tts(text: str):
        pcm_data, error = await tts_websocket(text)
        if error:
            print(f"语音失败：{error}")
        else:
            play_pcm_as_wav(pcm_data)

    # ---------- UI 组件 ----------
    app_bar = ft.Container(
        content=ft.Row(
            controls=[
                ft.Text("💬", size=30),
                ft.Text("灵溪", size=24, weight="bold", color="white"),
            ],
            alignment=ft.MainAxisAlignment.START,
            spacing=10,
        ),
        padding=15,
        margin=0,
        bgcolor="#4a90d9",
    )

    chat_display = ft.Column(spacing=15, scroll=ft.ScrollMode.AUTO, expand=True)
    chat_wrapper = ft.Container(
        content=chat_display,
        padding=10,
        expand=True,
        bgcolor="#f5f5f5",
    )

    input_field = ft.TextField(
        hint_text="说点什么...",
        expand=True,
        border_radius=30,
        filled=True,
        bgcolor="white",
        border_color="#4a90d9",
        on_submit=lambda e: send_message(),
    )

    send_btn = ft.Container(
        content=ft.Text("发送", color="white", size=14, weight="bold"),
        bgcolor="#4a90d9",
        padding=16,
        border_radius=20,
        on_click=lambda e: send_message(),
    )

    def send_message():
        user_text = input_field.value
        if not user_text:
            return
        input_field.value = ""
        page.update()
        win_w = page.width or getattr(page, "window_width", None) or 400

        chat_display.controls.append(
            ft.Row(
                controls=[
                    ft.Container(
                        content=ft.Text(user_text, size=15, color="black"),
                        bgcolor="#d1e7ff",
                        padding=15,
                        border_radius=20,
                        width=win_w * 0.7,
                    )
                ],
                alignment=ft.MainAxisAlignment.END,
            )
        )
        page.update()

        typing = ft.Row(
            controls=[
                ft.Container(
                    content=ft.Text("灵溪正在输入...", italic=True, size=14, color="#757575"),
                    padding=10,
                )
            ],
            alignment=ft.MainAxisAlignment.START,
        )
        chat_display.controls.append(typing)
        page.update()

        reply = get_reply(user_text)

        chat_display.controls.remove(typing)

        chat_display.controls.append(
            ft.Row(
                controls=[
                    ft.CircleAvatar(
                        content=ft.Text("🌊", size=20),
                        bgcolor="#4a90d9",
                        radius=18,
                    ),
                    ft.Container(
                        content=ft.Text(reply, size=15, color="black"),
                        bgcolor="white",
                        padding=15,
                        border_radius=20,
                        width=win_w * 0.65,
                    ),
                ],
                alignment=ft.MainAxisAlignment.START,
                spacing=8,
            )
        )
        page.update()

        asyncio.create_task(do_tts(reply))

    input_row = ft.Container(
        content=ft.Row(
            controls=[input_field, send_btn],
            spacing=10,
            alignment=ft.MainAxisAlignment.CENTER,
        ),
        padding=10,
        bgcolor="#f5f5f5",
    )

    page.add(app_bar, chat_wrapper, input_row)

if __name__ == "__main__":
    ft.app(target=main)
