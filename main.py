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
TTS_API_KEY = os.getenv("TTS_API_KEY", "e9690219-6f03-4a0f-906b-f29ce8bd3c45")

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
            await websocket.send(build_frame(EVENT_START_CONNECTION, payload="{}"))
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
            task_payload = json.dumps({"req_params": {"text": text}})
            await websocket.send(build_frame(EVENT_TASK_REQUEST, session_id=session_id, payload=task_payload))

            audio_data = b''
            while True:
                message = await websocket.recv()
                if not isinstance(message, bytes):
                    continue
                if len(message) < 12:
                    continue
                header = message[:8]
                event = struct.unpack('>I', header[4:8])[0]
                if event == EVENT_TTS_RESPONSE:
                    pos = 8
                    if len(message) >= pos + 4:
                        session_id_len = struct.unpack('>I', message[pos:pos+4])[0]
                        pos += 4 + session_id_len
                        if len(message) >= pos + 4:
                            payload_len = struct.unpack('>I', message[pos:pos+4])[0]
                            pos += 4
                            audio_data += message[pos:pos+payload_len]
                    else:
                        audio_data += message[24:]
                elif event == EVENT_SESSION_FINISHED:
                    break

            await websocket.send(build_frame(EVENT_FINISH_SESSION, session_id=session_id, payload="{}"))
            await websocket.send(build_frame(EVENT_FINISH_CONNECTION, payload="{}"))
            return audio_data, None
    except Exception as e:
        return None, str(e)

# ---------- UI ----------
def main(page: ft.Page):
    page.title = "灵溪"
    page.theme_mode = "light"
    page.padding = 10
    page.window_width = None
    page.window_height = None
    page.bgcolor = "#f5f5f5"

    audio_player = ft.Audio(src="")
    page.overlay.append(audio_player)

    def play_pcm_as_wav(pcm_data: bytes):
        if not pcm_data:
            return
        try:
            import wave
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                with wave.open(tmp.name, 'wb') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(24000)
                    wf.writeframes(pcm_data)
                audio_player.src = tmp.name
                audio_player.play()
                async def delete_later():
                    await asyncio.sleep(5)
                    try:
                        os.unlink(tmp.name)
                    except:
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
        win_w = page.width or page.window_width or 400

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
