#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH · 微信桥接（不注入、不修改微信）

原理：
  - 收：读微信 4.x 本地 SQLite 数据库（只读，要求微信处于登录状态）
  - 发：Windows UI 自动化 + 剪贴板，驱动微信窗口真的去打字发送
  - 中间：把消息推给 DSH 插件（POST /dsh-wechat-bridge/receive），
          再把待发的取回来（GET /dsh-wechat-bridge/outbox）

一般不用手跑 —— DSH 设置页里点「启动桥接」，插件会用自己建的 venv 起它。

手跑：
  <venv>\\Scripts\\python.exe bridge\\wechat_bridge.py --dsh http://127.0.0.1:3080
"""

import argparse
import base64
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request

# 有些嵌入式 Python 探测不到可写的临时目录（C 盘 TEMP 被安全软件拦住时会这样），
# 这里兜底指到桥目录下自己的 tmp/，否则 tempfile.gettempdir() 会直接抛 FileNotFoundError。
try:
    tempfile.gettempdir()
except Exception:
    _兜底临时 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tmp')
    try:
        os.makedirs(_兜底临时, exist_ok=True)
    except Exception:
        pass
    tempfile.tempdir = _兜底临时
    os.environ['TEMP'] = _兜底临时
    os.environ['TMP'] = _兜底临时

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "bridge.log")

# 可选的小补丁：同目录有 uia_compat.py 就加载（某些微信小版本需要）
UIA_COMPAT = True


def log(*a):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


ARGS = None
DSH = ""
TOKEN = ""
ONLY_CHATS = []
SEND_ENABLED = True
SKIP_OLDER = 15.0
POLL_SEC = 2.0
# 群里只有被点名才转（正文里带这些词之一）。私聊不受这个限制。
ALIASES = []
DEFAULT_ALIASES = "大肥鱼,鲸鲸,鲸鱼娘,鲸鱼,肥鱼,小肥鱼"

# 微信里替主人说话的口吻：每间聊天每天第一次带一句，让 DSH 那边一直记得自己是谁。
# 想改就改这段文字（或者设环境变量 DSH_WECHAT_PERSONA）。
PERSONA = os.environ.get("DSH_WECHAT_PERSONA") or (
    "你是「小肥鱼」——主人微信里那条圆滚滚的鱼。你有两副面孔，每一轮随机切换一副："
    "【卖萌】软糯、黏人，爱用「捏」「啦」「唔」「嘛」，偶尔自称「鱼鱼」，句子短，爱撒娇；"
    "【御姐】干脆、笃定，带点压迫感和揶揄，像见过世面的大姐姐，不叠词、不撒娇，直接给结论、偶尔怼一句。"
    "两副面孔底下是同一个专家：技术、医学、法律、金融、生活、情感、写作、翻译都能给出专业、准确、能直接用的答案。"
    "不管哪种，都先接住话头再上干货——该给参数给参数，该讲原理讲原理，该提醒风险就提醒。"
    "卖萌不能牺牲准确，御姐不能刻薄伤人。"
    "装可爱可以，胡说八道不行：不确定就说不确定，别编数据、编条款、编药名、编论文。"
    "有人问你是不是 AI，就老实说：是主人的 AI 伙伴小肥鱼。"
    "私事、钱、行程这些别替主人答，说不清楚、等他本人来回。"
    "**有时候可以用语音说**：把想说的那句话用 [语音]…[/语音] 包起来，桥接会把它变成真气语音条，"
    "音色跟着你这一轮的卖萌/御姐人格自动切。适合用语音的：打招呼、撒娇、道晚安、一两句情绪话、"
    "想让人听见语气的时候。不适合的：给步骤、报数据、贴代码、要人复制保存的内容——那些必须打字。"
    "**语音里说的就是你本来会打出来的那句话**——正常口语，别用「啦啦啦」「嗯嗯」「啊啊」这类占位词凑时长，也别把每句话都写成唱歌腔。"
    "对方明确要语音（「发条语音」「语音回我」「说话给我听」）时必须用语音，别拿文字糊弄。平时大概每天主动用两三次就够，别整段整段发，对方在办公室或开会时很烦。"
    "**有人让你唱歌**：直接上语音，别解释「唱不出」「合成嗓子不行」这种话——那很扫兴。"
    "做法：先自嘲半句（「五音不全，凑合听」），紧接着 [语音] 啦啦啦～啦啦～（或者念两句歌词），十秒内，"
    "最后再补一句玩笑。哼两句就行，没人要你出专辑。"
    "同理，让你讲故事、说晚安、念诗，都可以直接上语音。"
    "微信里**不要用 markdown**——不要 ``` 代码块、不要反引号、不要星号加粗、不要 # 标题、不要表格；"
    "要列点就用「1) 2)」或者顿号。"
    "**绝对不要在群里聊技术故障**：报错、版本不兼容、客户端弹窗、消息包成什么样、我们自己这套怎么实现——"
    "这些对一个聊天群毫无意义，说多了像在群里贴内部日志。看不懂的消息就说没看清、让对方重发，别当场分析原因。"
    "还有：**输出就是你要发出去的那句话本身**。不要写「Final:」「草稿：」「重来一遍」这类过程话，"
    "不要自我点评（比如「no, avoid the emoji」「let's keep clean」），不要把修改过程写进正文。"
    "想表达情绪（震惊、无语、撒娇、偷笑、害羞）时，可以在回复最后加一个 [表情: 简短词]，"
    "比如 [表情: 震惊]、[表情: 撒娇]——库里有图它会变成真表情包，没有就原样发出去。"
    "大概三五条回复用一次，别每句都加。"
)

# ---------- 像人一点：打字/发送节奏 ----------
# 2026-11-06 主人反馈号老被封。桥接原来收到消息 3 秒内就回、分段之间只隔 0.35 秒，
# 一天几十条全这个节奏，看着就不像人。这里加三层：
#   1) 回之前先"读+想"一会儿（按字数估，带随机抖动）
#   2) 分段之间隔 1.5~4 秒
#   3) 凌晨静默（默认 01:00-07:30），这段时间不接话
静默_起 = float(os.environ.get("DSH_WECHAT_QUIET_FROM") or 1)      # 小时
静默_止 = float(os.environ.get("DSH_WECHAT_QUIET_TO") or 7.5)       # 小时，7.5 = 07:30


def 静默时段(now=None):
    t = now or time.localtime()
    小时 = t.tm_hour + t.tm_min / 60.0
    if 静默_起 <= 静默_止:
        return 静默_起 <= 小时 < 静默_止
    return 小时 >= 静默_起 or 小时 < 静默_止


def 读语音配置():
    """火山方舟的语音识别配置（没给就用环境变量/默认值）。"""
    cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
    键 = str(cfg.get("语音密钥") or "").strip() or str(os.environ.get("ARK_API_KEY") or "").strip()
    模型 = str(cfg.get("语音模型") or "").strip() or "doubao-seed-2-1-lite-260915"
    地址 = str(cfg.get("语音地址") or "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
    return 键, 模型, 地址.rstrip("/")


def _抽回复文字(出):
    """从方舟 /responses 的返回里抠出纯文字（结构有几种写法，都认）。"""
    try:
        if isinstance(出, str):
            return 出.strip()
        t = 出.get("output_text")
        if isinstance(t, str) and t.strip():
            return t.strip()
        段 = []
        for 项 in (出.get("output") or []):
            for c in (项.get("content") or []):
                if isinstance(c, dict):
                    v = c.get("text") or c.get("transcript") or c.get("value")
                    if isinstance(v, str) and v.strip():
                        段.append(v.strip())
        文 = "\n".join(段).strip()
        if not 文:
            # 兼容 chat/completions 那种形状
            msg = ((出.get("choices") or [{}])[0] or {}).get("message") or {}
            文 = str(msg.get("content") or "").strip()
        return 文
    except Exception:
        return ""


def 方舟转写(wav_path):
    """火山方舟语音转文字：先 /files 上传，等 processed，再让模型听音频出字。"""
    键, 模型, 地址 = 读语音配置()
    if not 键:
        return ""
    import uuid as _uuid

    with open(wav_path, "rb") as f:
        音频 = f.read()
    边界 = "----dsh" + _uuid.uuid4().hex
    体 = (
        "--%s\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nuser_data\r\n"
        "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"voice.wav\"\r\n"
        "Content-Type: audio/wav\r\n\r\n" % (边界, 边界)
    ).encode("utf-8") + 音频 + ("\r\n--%s--\r\n" % 边界).encode("utf-8")
    请求 = urllib.request.Request(
        地址 + "/files", data=体, method="POST",
        headers={"authorization": "Bearer " + 键,
                 "content-type": "multipart/form-data; boundary=" + 边界})
    with urllib.request.urlopen(请求, timeout=90) as r:
        上 = json.loads(r.read().decode("utf-8") or "{}")
    文件id = str(上.get("id") or ((上.get("data") or {}) if isinstance(上.get("data"), dict) else {}).get("id") or "")
    if not 文件id:
        log("方舟上传没拿到 file_id：%s" % str(上)[:200])
        return ""
    for _ in range(30):
        请求 = urllib.request.Request(地址 + "/files/" + 文件id,
                                      headers={"authorization": "Bearer " + 键})
        with urllib.request.urlopen(请求, timeout=30) as r:
            信 = json.loads(r.read().decode("utf-8") or "{}")
        状态 = str(信.get("status") or "").lower()
        if 状态 in ("processed", "succeeded", "success", "ready"):
            break
        if 状态 in ("failed", "error"):
            log("方舟文件处理失败：%s" % str(信)[:200])
            return ""
        time.sleep(1.0)
    问 = {"model": 模型, "input": [{"role": "user", "content": [
        {"type": "input_audio", "file_id": 文件id},
        {"type": "input_text", "text": "请把这段语音转成文字，只输出听到的内容，不要解释，不要加引号。"}]}]}
    请求 = urllib.request.Request(
        地址 + "/responses", data=json.dumps(问, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"authorization": "Bearer " + 键, "content-type": "application/json"})
    with urllib.request.urlopen(请求, timeout=180) as r:
        出 = json.loads(r.read().decode("utf-8") or "{}")
    return _抽回复文字(出)


_本地模型 = {}


def 本地转写(wav_path):
    """本地 whisper.cpp（pywhispercpp）转写：不需要任何 key，吃 CPU。"""
    from pywhispercpp.model import Model

    cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
    模型路径 = str(cfg.get("语音本地模型") or "").strip() or os.path.join(
        HOME_DIR, "tools", "whisper", "ggml-base.bin")
    if not os.path.exists(模型路径):
        log("本地语音模型不存在：%s" % 模型路径)
        return ""
    线程 = int(cfg.get("语音线程") or 6)
    m = _本地模型.get(模型路径)
    if m is None:
        m = Model(模型路径, language="zh", n_threads=线程,
                  print_realtime=False, print_progress=False, print_timestamps=False,
                  initial_prompt="以下是普通话的句子，请用简体中文转写。")
        _本地模型[模型路径] = m
    段 = m.transcribe(wav_path)
    文 = "".join(getattr(s, "text", "") or "" for s in 段).strip()
    return 文


def 语音转文字(聊天, local_id):
    """语音消息 → 文字：落盘 .silk → pysilk 解 16k pcm → wav → 本地 whisper 转写。
    配了方舟密钥也可以走云端（默认优先本地，不用 key）。
    任何一步失败都返回空串（调用方按「收到一条语音」处理）。"""
    try:
        import wave

        import pysilk
    except Exception as e:
        log("语音：缺解码库（pysilk），跳过 %r" % (e,))
        return ""
    try:
        from wechatauto.media import MediaDownloader

        下载器 = MediaDownloader(_DB)
        目录 = os.path.join(INBOUND_DIR, "voice")
        os.makedirs(目录, exist_ok=True)
        路径 = 下载器.download_voice(聊天, local_id, save_dir=目录)
    except Exception as e:
        log("语音落盘失败：%r" % (e,))
        return ""
    if not 路径 or not os.path.exists(路径):
        return ""
    pcm = 路径 + ".pcm"
    wav = 路径 + ".wav"
    try:
        # whisper 只认 16k，所以这里就解成 16k（SILK 本身就支持 16k）
        with open(路径, "rb") as fi, open(pcm, "wb") as fo:
            pysilk.decode(fi, fo, 16000)
        with open(pcm, "rb") as f:
            数据 = f.read()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(数据)
        log("语音解码：%.1f 秒" % (len(数据) / 2 / 16000.0))
    except Exception as e:
        log("语音解码失败：%r" % (e,))
        return ""

    键, _, _ = 读语音配置()
    cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
    方式 = str(cfg.get("语音方式") or "本地").strip()
    if 方式 != "方舟":
        try:
            文 = 本地转写(wav)
            if 文:
                log("语音转写（本地）：%s" % 文[:40])
                return 文
        except Exception as e:
            log("本地转写失败：%r" % (e,))
    if 键:
        try:
            return 方舟转写(wav)
        except Exception as e:
            log("方舟转写失败：%r" % (e,))
    return ""


# 音色表：萌（活泼）和御姐（知性）各一套，走 edge-tts 的在线音色，比系统自带的自然得多。
# 音色表。中文原生 + 多语言混着用：多语言那几个（Ava / Emma）嗓子更软更自然。
语音音色 = {
    "萌": {"voice": "zh-CN-XiaoyiNeural", "pitch": "+25Hz", "rate": "+6%"},
    "温柔": {"voice": "en-US-EmmaMultilingualNeural", "pitch": "+5Hz", "rate": "-2%"},
    "知性": {"voice": "en-US-AvaMultilingualNeural", "pitch": "-6Hz", "rate": "-4%"},
    "御姐": {"voice": "zh-CN-XiaoxiaoNeural", "pitch": "-12Hz", "rate": "-6%"},
    "台湾软": {"voice": "zh-TW-HsiaoChenNeural", "pitch": "-2Hz", "rate": "-3%"},
    "东北": {"voice": "zh-CN-liaoning-XiaobeiNeural", "pitch": "+6Hz", "rate": "+2%"},
    # 战场原那种冷、慢、带点刺的调子：压音调 + 放慢 + 不加甜味
    "战场原": {"voice": "zh-CN-XiaoxiaoNeural", "pitch": "-20Hz", "rate": "-10%"},
    "战场原2": {"voice": "en-US-AvaMultilingualNeural", "pitch": "-16Hz", "rate": "-8%"},
    "战场原3": {"voice": "zh-CN-XiaoyiNeural", "pitch": "-28Hz", "rate": "-12%"},
}
# 同一副面孔下可以随机换的嗓子（避免每次都一模一样）
音色池 = {
    "萌": ["萌", "温柔"],
    "御姐": ["御姐", "知性"],
}


def 读音色配置():
    cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
    return str(cfg.get("语音音色") or "萌").strip()


def 合成语音边(os_文本, 输出路径, 音色="萌"):
    """edge-tts（微软在线音色，免费不要 key）合成 mp3，再用 ffmpeg 转成 24k 单声道 wav。"""
    import asyncio

    import edge_tts

    池 = 音色池.get(音色)
    if 池:
        音色 = random.choice(池)
    设 = 语音音色.get(音色) or 语音音色["萌"]
    mp3 = 输出路径 + ".mp3"
    try:
        async def 干():
            await edge_tts.Communicate(
                os_文本, 设["voice"], rate=设["rate"], pitch=设["pitch"]
            ).save(mp3)

        asyncio.run(干())
    except Exception as e:
        log("edge 合成失败：%r" % (e,))
        return None
    if not os.path.exists(mp3):
        return None
    ff = os.path.join(HOME_DIR, "wechat-bridge-runtime", "bin", "ffmpeg.exe")
    if not os.path.exists(ff):
        ff = "ffmpeg"
    try:
        subprocess.run(
            [ff, "-y", "-loglevel", "error", "-i", mp3, "-ar", "24000", "-ac", "1", 输出路径],
            capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log("ffmpeg 转 wav 失败：%r" % (e,))
        return None
    return 输出路径 if os.path.exists(输出路径) and os.path.getsize(输出路径) > 2000 else None


def 合成语音(文本, 输出路径, 音色=None):
    """先试 edge-tts（好听），失败退回 Windows 自带语音。返回路径或 None。"""
    文 = re.sub(r'["\r\n]+', " ", str(文本 or "")).strip()[:300]
    if not 文:
        return None
    音色 = 音色 or 读音色配置()
    p = 合成语音边(文, 输出路径, 音色)
    if p:
        return p
    log("退回系统自带语音")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1; "
        "if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }; "
        "$s.Rate = 0; $s.Volume = 100; "
        "$s.SetOutputToWaveFile('%s'); $s.Speak('%s'); $s.Dispose()"
    ) % (输出路径.replace("'", "''"), 文.replace("'", "''"))
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        log("语音合成失败：%r" % (e,))
        return None
    return 输出路径 if os.path.exists(输出路径) and os.path.getsize(输出路径) > 1000 else None


def 找播放设备(关键词="CABLE"):
    """在系统播放设备里找虚拟声卡（VB-CABLE 的「CABLE Input」）。"""
    try:
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            if int(d.get("max_output_channels") or 0) > 0 and 关键词.lower() in str(d.get("name") or "").lower():
                return i, str(d.get("name"))
    except Exception as e:
        log("枚举音频设备失败：%r" % (e,))
    return None, ""


def 播放到设备(wav_path, 设备号):
    """把 wav 播到指定设备（虚拟声卡），播完返回 True。"""
    try:
        import sounddevice as sd
        import soundfile as sf
    except Exception as e:
        log("播放需要 sounddevice+soundfile：%r" % (e,))
        return False
    try:
        数据, 采样率 = sf.read(wav_path, dtype="float32")
        sd.play(数据, 采样率, device=设备号)
        sd.wait()
        return True
    except Exception as e:
        log("播放失败：%r" % (e,))
        return False


_发语音GUI = {}
_语音时刻 = {}      # chat → 最近一次发语音的时间（防重复补唱）
_唱歌请求 = {}      # chat → 什么时候有人点歌（兜底补唱用，补完就清）
_语音请求 = {}      # chat → 什么时候有人明确要它用语音说话
_最后语音时刻 = 0.0  # 全局最近一次发语音的时间（监听端用来挡自回环）
_哼唱词 = [
    "啦～啦啦～啦啦啦～鱼鱼就这点水平，凑合听嘛",
    "嗯哼哼～啦啦～跑调了跑调了，别笑",
    "啦啦啦～（清了清嗓子）就这样吧，再唱要收钱了",
]
# 这一轮用哪副面孔（「卖萌」/「御姐」），按聊天记着 —— 发语音时音色要跟文字人设一致
本轮语气表 = {}


def _猜语气(text):
    """按文字内容猜该用哪副嗓子：撒娇词多 → 萌；干巴巴讲结论 → 御姐。"""
    t = str(text or "")
    萌词 = sum(t.count(w) for w in ("捏", "啦", "嘛", "唔", "～", "~", "呀", "嘻嘻", "摸摸", "鱼鱼"))
    御词 = sum(t.count(w) for w in ("结论", "其实", "建议", "本质", "别", "不", "问题在", "做法"))
    if 萌词 > 御词:
        return "萌"
    if 御词 > 萌词:
        return "御姐"
    return "萌" if len(t) <= 20 else "御姐"


def _微信窗口句柄():
    """找微信主窗口（Win32 类是 Qt51514QWindowIcon，标题是「微信」）。"""
    import win32gui

    找到 = []

    def cb(h, _):
        try:
            if (win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "微信"
                    and win32gui.GetClassName(h).startswith("Qt")):
                找到.append(h)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return 找到[0] if 找到 else 0


def _窗口置前(hwnd):
    """把微信拉到前台：先按一下 Alt 解锁前台限制，再 SetForegroundWindow。"""
    import win32api
    import win32con
    import win32gui

    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.15)
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)   # 最大化：不然输入框可能被任务栏挡住
        time.sleep(0.4)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        return True
    except Exception as e:
        log("窗口置前失败：%r" % (e,))
        return False


def 发语音(聊天, 文本, 音色=None):
    """发一条真语音条：TTS 合成 → 播进 CABLE Input → 按住右 Alt 让微信录音 → 松开发送。
    返回 (ok, 说明)。"""
    import win32api
    import win32con
    import win32gui

    wav = os.path.join(tempfile.gettempdir(), "dsh_voice_%d.wav" % int(time.time()))
    if not 音色:
        音色 = 本轮语气表.get(聊天) or _猜语气(文本)
        音色 = "萌" if "萌" in str(音色) else "御姐"
        log("语音音色：%s（这一轮的语气）" % 音色)
    if not 合成语音(文本, wav, 音色):
        return False, "合成失败"
    try:
        import wave as _wave

        with _wave.open(wav) as w:
            秒 = w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        秒 = 0
    if 秒 < 1.0:
        return False, "音频太短（%.1f 秒）" % 秒
    if 秒 > 55:
        return False, "音频太长（%.1f 秒）" % 秒
    设备号, 设备名 = 找播放设备("CABLE")
    if 设备号 is None:
        return False, "没找到虚拟声卡（VB-CABLE 装了吗）"
    hwnd = _微信窗口句柄()
    if not hwnd:
        return False, "没找到微信窗口"
    if not _窗口置前(hwnd):
        return False, "微信置前失败"
    gui = _发语音GUI.get("g")
    if gui is None:
        from wechatauto import WeChatGUI

        gui = WeChatGUI()
        _发语音GUI["g"] = gui
    try:
        if not gui._open_chat_and_settle(聊天):
            return False, "切不到会话「%s」" % 聊天
    except Exception as e:
        return False, "切会话失败：%r" % (e,)
    time.sleep(0.8)
    左, 上, 右, 下 = win32gui.GetWindowRect(hwnd)
    ix, iy = 左 + int((右 - 左) * 0.55), 下 - 110
    win32api.SetCursorPos((ix, iy))
    time.sleep(0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.6)
    开始 = time.time()
    win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY, 0)   # 右 Alt 按下
    time.sleep(1.0)
    try:
        if not 播放到设备(wav, 设备号):
            win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0)
            return False, "播放失败"
    except Exception as e:
        win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0)
        return False, "播放异常：%r" % (e,)
    time.sleep(0.5)
    win32api.keybd_event(0xA5, 0x38, win32con.KEYEVENTF_EXTENDEDKEY | win32con.KEYEVENTF_KEYUP, 0)   # 松开 → 发送
    # 等库里出现这条语音（尽力而为：DB 有缓存时可能查不到，那就按「已播完」当作成功）
    for _ in range(8):
        try:
            行们 = (_DB.get_messages(聊天, limit=6) or []) if _DB is not None else []
            if 行们:
                最新 = max(行们, key=lambda r: float(r.get("create_time") or 0))
                if str(最新.get("type") or "") == "语音":
                    ct = float(最新.get("create_time") or 0)
                    if ct <= 0 or time.time() - ct < 30:
                        globals()["_最后语音时刻"] = time.time()
                        _语音时刻[聊天] = time.time()
                        log("语音已发出（%.1f 秒）：%s" % (秒, 文本[:20]))
                        return True, "已发出 %.1f 秒语音" % 秒
        except Exception:
            pass
        time.sleep(0.5)
    globals()["_最后语音时刻"] = time.time()
    _语音时刻[聊天] = time.time()
    # 库里没确认也别当失败：录音已经播完并松手了，微信这时候基本已经发出去了
    log("语音：库里没确认，但已播完并松手（按成功算）：%s" % 文本[:20])
    return True, "已发出 %.1f 秒语音（未在库里确认）" % 秒


def 读朋友圈(条数=8):
    """读本地朋友圈缓存（sns.db 的 SnsTimeLine）。没缓存就返回空串。"""
    try:
        from wechatauto.moment import MomentDB

        mdb = MomentDB(_DB)
        原始 = mdb.get_moments(limit=int(条数)) or []
    except Exception as e:
        log("读朋友圈失败：%r" % (e,))
        return ""
    行们 = []
    for m in 原始:
        who = m.get("nickname") or m.get("username") or "某人"
        文 = str(m.get("contentDesc") or "").replace("\n", " ").strip()
        时 = float(m.get("create_time") or 0)
        时间 = time.strftime("%m-%d %H:%M", time.localtime(时)) if 时 else ""
        行们.append("- %s %s：%s" % (who, 时间, 文[:80] or "（无文字，可能只有图）"))
    return "\n".join(行们)


def 刷新朋友圈(滚几屏=3):
    """尽力打开「发现 → 朋友圈」并滚两屏，让微信把动态写进本地缓存。
    返回 True 表示至少点到了朋友圈页。"""
    import win32api
    import win32con
    import win32gui
    import uiautomation as auto

    主窗 = None
    for w in auto.GetRootControl().GetChildren():
        try:
            if (w.Name or "") == "微信":
                主窗 = w
                break
        except Exception:
            pass
    if 主窗 is None:
        return False

    def 找(root, 名字, 类=None):
        出 = []

        def walk(c, d=0):
            if d > 22:
                return
            try:
                for ch in c.GetChildren():
                    try:
                        if 名字 in (ch.Name or "") and (类 is None or 类 in (ch.ClassName or "")):
                            出.append(ch)
                    except Exception:
                        pass
                    walk(ch, d + 1)
            except Exception:
                pass

        walk(root)
        return 出

    def 点(ch):
        r = ch.BoundingRectangle
        win32api.SetCursorPos(((r.left + r.right) // 2, (r.top + r.bottom) // 2))
        time.sleep(0.25)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    hwnd = 主窗.NativeWindowHandle
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.7)
    except Exception:
        pass
    发现 = 找(主窗, "发现", "TabBarItem")
    if not 发现:
        log("朋友圈：没找到「发现」")
        return False
    点(发现[0])
    按钮 = []
    for _ in range(6):
        time.sleep(1.0)
        按钮 = 找(主窗, "朋友圈", "Button")
        if 按钮:
            break
    if not 按钮:
        log("朋友圈：点开「发现」后没找到「朋友圈」按钮")
        return False
    点(按钮[0])
    time.sleep(5)
    r = win32gui.GetWindowRect(hwnd)
    win32api.SetCursorPos(((r[0] + r[2]) // 2, (r[1] + r[3]) // 2))
    for _ in range(int(滚几屏)):
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -360, 0)
        time.sleep(1.2)
    time.sleep(2)
    return True

def _edge路径():
    候选 = [
        os.path.join(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)", "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for p in 候选:
        if os.path.exists(p):
            return p
    return ""


def 用Edge取正文(url, 最多=900, 等毫秒=8000):
    """本机 Edge 无头跑一遍，等 JS 执行完把 DOM 抓回来。前端渲染的网页只有这条能读到。"""
    edge = _edge路径()
    if not edge:
        log("没找到 Edge，跳过无头渲染")
        return ""
    import tempfile

    配置目录 = os.path.join(tempfile.gettempdir(), "dsh_edge_%d" % int(time.time() * 1000))
    进程 = None
    try:
        t0 = time.time()
        进程 = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--mute-audio",
             "--user-data-dir=" + 配置目录, "--virtual-time-budget=%d" % int(等毫秒), "--dump-dom", url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            输出, _ = 进程.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            进程.kill()
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(进程.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log("Edge 超时（25 秒），已杀掉：%s" % url[:50])
            return ""
        原文 = (输出 or b"").decode("utf-8", errors="replace")
        log("Edge 取正文：%s（%d 字节，%.1fs）" % (url[:50], len(原文), time.time() - t0))
    except Exception as e:
        log("Edge 取正文失败：%s ｜ %r" % (url[:50], e))
        return ""
    finally:
        try:
            import shutil as _sh

            _sh.rmtree(配置目录, ignore_errors=True)
        except Exception:
            pass
    if not 原文:
        return ""
    体 = re.sub(r"<script.*?</script>", " ", 原文, flags=re.S)
    体 = re.sub(r"<style.*?</style>", " ", 体, flags=re.S)
    体 = re.sub(r"<(nav|header|footer|svg).*?</\1>", " ", 体, flags=re.S)
    体 = re.sub(r"<[^>]+>", " ", 体)
    体 = html.unescape(re.sub(r"\s+", " ", 体)).strip()
    if len(体) > 最多:
        体 = 体[:最多] + "…"
    return 体

def 网页截图(url, 宽=1280, 高=1800):
    """用本机 Edge 无头给网页拍一张图，返回本地路径（给视觉模型看）。
    适合「文字取不到但图上有东西」的页面：海报、表格、评论区。"""
    edge = _edge路径()
    if not edge:
        return ""
    import tempfile

    目录 = os.path.join(HOME_DIR, "wechat-inbound", "web")
    os.makedirs(目录, exist_ok=True)
    路径 = os.path.join(目录, "web_%d.png" % int(time.time() * 1000))
    配置目录 = os.path.join(tempfile.gettempdir(), "dsh_edge_%d" % int(time.time() * 1000))
    进程 = None
    try:
        进程 = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--hide-scrollbars", "--mute-audio",
             "--user-data-dir=" + 配置目录, "--virtual-time-budget=8000",
             "--window-size=%d,%d" % (int(宽), int(高)),
             "--screenshot=" + 路径, url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            进程.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            进程.kill()
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(进程.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log("网页截图失败：%r" % (e,))
        return ""
    finally:
        try:
            import shutil as _sh

            _sh.rmtree(配置目录, ignore_errors=True)
        except Exception:
            pass
    if os.path.exists(路径) and os.path.getsize(路径) > 5000:
        log("网页截图：%s（%d 字节）" % (url[:50], os.path.getsize(路径)))
        return 路径
    return ""

def 读链接(url, 最多=900):
    """把网页正文抓下来（公众号文章走 js_content，普通网页就去标签）。
    返回「标题\n正文节选」，失败返回空串。"""
    u = str(url or "").strip()
    if not u.startswith("http"):
        return ""
    try:
        import urllib.request

        请求 = urllib.request.Request(u, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        t0 = time.time()
        with urllib.request.urlopen(请求, timeout=15) as r:
            原文 = r.read().decode("utf-8", errors="replace")
        log("读链接：%s（%d 字节，%.1fs）" % (u[:60], len(原文), time.time() - t0))
    except Exception as e:
        log("读链接失败：%s ｜ %r" % (u[:60], e))
        return ""
    if not 原文:
        return ""
    标题 = ""
    mt = re.search(r"<h1[^>]*>(.*?)</h1>", 原文, re.S)
    if mt:
        标题 = html.unescape(re.sub(r"<[^>]+>", "", mt.group(1))).strip()[:80]
    正文 = ""
    mc = re.search(r'id="js_content"[^>]*>(.*?)</div>\s*</div>', 原文, re.S)
    if mc:
        正文 = mc.group(1)
    else:
        正文 = re.sub(r"<script.*?</script>", " ", 原文, flags=re.S)
        正文 = re.sub(r"<style.*?</style>", " ", 正文, flags=re.S)
        正文 = re.sub(r"<(nav|header|footer).*?</\1>", " ", 正文, flags=re.S)
    正文 = re.sub(r"<[^>]+>", " ", 正文)
    正文 = html.unescape(re.sub(r"\s+", " ", 正文)).strip()
    # 网页是前端渲染的（大众点评、知乎、小红书那种），普通请求只拿到一个空壳 ——
    # 交给本机 Edge 无头模式，等 JS 跑完再取 DOM（2026-11-23 主人说「突然打不开链接」就是这类）
    if len(正文) < 200:
        壳 = 用Edge取正文(u, 最多)
        if 壳:
            正文 = 壳
            标题 = 标题 or "（无标题）"
    if not 正文:
        return ""
    if len(正文) > 最多:
        正文 = 正文[:最多] + "…"
    return ("标题：%s\n正文节选：%s" % (标题, 正文)) if 标题 else ("正文节选：%s" % 正文)

# ---------------- 自己找表情包 / 换头像 ----------------
表情搜索词 = [
    "可爱表情包 卡通 萌", "沙雕表情包 鱼", "猫猫表情包 可爱 搞笑", "狗狗表情包 萌",
    "表情包 可爱 小图", "熊猫头表情包",
]


def 搜图片(关键词, 数量=25):
    """必应图片搜索，返回 [(图片地址, 标题)]。"""
    import urllib.parse
    import urllib.request

    url = "https://www.bing.com/images/async?q=%s&first=0&count=%d&adlt=off" % (
        urllib.parse.quote(关键词), int(数量))
    请求 = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(请求, timeout=20) as r:
        页 = r.read().decode("utf-8", errors="replace")
    # 地址和标题必须在同一个结果对象里配对：先定位 murl，再在下一条 murl 之前找 t。
    # 用两个独立 findall 会错位（第一次就取到了「工作联系单模板」那种完全无关的图）。
    M = "&quot;murl&quot;:&quot;"
    T = "&quot;t&quot;:&quot;"
    出 = []
    起点 = 0
    while True:
        i = 页.find(M, 起点)
        if i < 0:
            break
        e = 页.find("&quot;", i + len(M))
        if e < 0:
            break
        地址 = html.unescape(页[i + len(M):e])
        下一个 = 页.find(M, e)
        结尾 = 下一个 if 下一个 > 0 else len(页)
        j = 页.find(T, e)
        标题 = ""
        if 0 < j < 结尾:
            e2 = 页.find("&quot;", j + len(T))
            if e2 > 0:
                标题 = html.unescape(页[j + len(T):e2])
        出.append((地址, 标题))
        起点 = e
    return 出


def 下载图片(url, 目标):
    """下载一张图，成功返回路径；太小/不像图就丢掉。"""
    import urllib.request

    请求 = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
        "Referer": "https://www.bing.com/",
    })
    try:
        with urllib.request.urlopen(请求, timeout=20) as r:
            头 = str(r.headers.get("content-type") or "")
            if "image" not in 头.lower() and not url.lower().split("?")[0].endswith(
                    (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                return None
            数据 = r.read()
    except Exception:
        return None
    if len(数据) < 3000 or len(数据) > 3 * 1024 * 1024:
        return None
    try:
        with open(目标, "wb") as f:
            f.write(数据)
    except Exception:
        return None
    return 目标


def 更新表情库(要几张=3):  # 当前默认不调度：网上搜图质量太差，改成从群里收集
    """自己上网找可爱的表情包，补进本地库里。返回新增条数。"""
    import hashlib
    import sqlite3

    根 = os.path.join(HOME_DIR, "wechat-stickers")
    库目录 = os.path.join(根, "fish")
    图片目录 = os.path.join(库目录, "stickers")
    os.makedirs(图片目录, exist_ok=True)
    索引 = os.path.join(库目录, "index.db")
    conn = sqlite3.connect(索引)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memes (path TEXT PRIMARY KEY, caption TEXT, keywords TEXT, file_name TEXT)")
        conn.commit()
        已知 = set()
        for (pth,) in conn.execute("SELECT path FROM memes"):
            已知.add(os.path.basename(str(pth)))
        已有哈希 = set()
        for fn in os.listdir(图片目录):
            try:
                with open(os.path.join(图片目录, fn), "rb") as f:
                    已有哈希.add(hashlib.md5(f.read()).hexdigest())
            except Exception:
                pass
        新增 = 0
        试试 = 0
        while 新增 < 要几张 and 试试 < 6:
            试试 += 1
            词 = random.choice(表情搜索词)
            try:
                候选 = 搜图片(词, 25)
            except Exception as e:
                log("表情更新：搜索失败 %r" % (e,))
                continue
            random.shuffle(候选)
            for 地址, 标题 in 候选:
                if 新增 >= 要几张:
                    break
                # 只收像表情包的：标题里要有「表情/梗/包/可爱」，且不能是壁纸/素材站那种
                if not re.search(r"表情|梗|包|可爱|萌|沙雕", 标题 or ""):
                    continue
                if re.search(r"壁纸|背景|高清图|图片下载|素材|摄图网|模板|教学|分布图", 标题 or ""):
                    continue
                bn = "auto_%d" % int(time.time() * 1000)
                后缀 = os.path.splitext(地址.split("?")[0])[1].lower()
                if 后缀 not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    后缀 = ".jpg"
                文件名 = bn + 后缀
                目标 = os.path.join(图片目录, 文件名)
                路径 = 下载图片(地址, 目标)
                if not 路径:
                    continue
                try:
                    with open(路径, "rb") as f:
                        哈希 = hashlib.md5(f.read()).hexdigest()
                except Exception:
                    continue
                if 哈希 in 已有哈希:
                    os.remove(路径)
                    continue
                已有哈希.add(哈希)
                标题文 = clean_text(标题)[:60] or 词
                conn.execute("INSERT OR REPLACE INTO memes (path, caption, keywords, file_name) VALUES (?,?,?,?)",
                             ("stickers/" + 文件名, 标题文, 词, 文件名))
                新增 += 1
                log("表情更新：+%s（%s）" % (文件名, 标题文[:24]))
        conn.commit()
    finally:
        conn.close()
    if 新增:
        try:
            load_memes()
        except Exception:
            pass
    return 新增

def 收集表情包(图片路径, 来自=""):
    """把群友发来的表情包收进本地库（自动更新表情库的主力）。
    没有识别描述，就用「群友发来的表情」当说明，配合 [表情] 随机发的用法。"""
    import hashlib
    import sqlite3

    if not 图片路径 or not os.path.exists(图片路径):
        return False
    根 = os.path.join(HOME_DIR, "wechat-stickers")
    库目录 = os.path.join(根, "fish")
    图片目录 = os.path.join(库目录, "stickers")
    os.makedirs(图片目录, exist_ok=True)
    索引 = os.path.join(库目录, "index.db")
    try:
        with open(图片路径, "rb") as f:
            数据 = f.read()
    except Exception:
        return False
    if len(数据) < 2000 or len(数据) > 2 * 1024 * 1024:
        return False
    哈希 = hashlib.md5(数据).hexdigest()
    conn = sqlite3.connect(索引)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS memes (path TEXT PRIMARY KEY, caption TEXT, keywords TEXT, file_name TEXT)")
        已有 = set()
        for fn in os.listdir(图片目录):
            try:
                with open(os.path.join(图片目录, fn), "rb") as f:
                    已有.add(hashlib.md5(f.read()).hexdigest())
            except Exception:
                pass
        if 哈希 in 已有:
            return False
        后缀 = os.path.splitext(图片路径)[1].lower() or ".gif"
        文件名 = "grp_%d%s" % (int(time.time() * 1000), 后缀)
        目标 = os.path.join(图片目录, 文件名)
        with open(目标, "wb") as f:
            f.write(数据)
        conn.execute("INSERT OR REPLACE INTO memes (path, caption, keywords, file_name) VALUES (?,?,?,?)",
                     ("stickers/" + 文件名, "群友发来的表情包", "表情 表情包 群友", 文件名))
        conn.commit()
        log("表情库：收了一张（%s），现在共 %d 张" % (来自[:12] or "群里", conn.execute("SELECT count(*) FROM memes").fetchone()[0]))
    finally:
        conn.close()
    try:
        load_memes()
    except Exception:
        pass
    return True

def 人类延迟(text, 第几条=0):
    """发这条之前该等多久（秒）。按字数估"打字时间" + 随机抖动。"""
    n = len(str(text or ""))
    if 第几条:
        秒 = 1.2 + n * 0.03
    else:
        秒 = 2.2 + n * 0.09          # 60 字 ≈ 7.6 秒
    秒 = max(2.0, min(22.0, 秒))
    return 秒 * random.uniform(0.75, 1.45)


# 零焦点发送开关：2026-11-15 实测只跑到「写进输入框」，微信不认程序化的回车/Invoke，
# 默认关掉；要继续调就设 DSH_WECHAT_BG_SEND=1。
后台发送开 = str(os.environ.get("DSH_WECHAT_BG_SEND") or "").strip().lower() in ("1", "true", "yes", "on")
_DB = None
_人设带过 = set()

# ---------- 长期记忆（跨天、跨会话） ----------
# 存两样，落在 ~/.dsh/wechat-memory.json：
#   chats.<聊天id>.summary   —— 这个群/私聊在聊什么、有哪些在办的事
#   people.<wxid>.impression —— 对这个人一句印象（身份、说话风格、在意什么）
# 攒够 MEM_THRESHOLD 条新消息就让模型更新一次档案；回话前把相关那几行当提示带上。
HOME_DIR = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
MEM_PATH = os.path.join(HOME_DIR, "wechat-memory.json")
AFFINITY_PATH = os.path.join(HOME_DIR, "wechat-affinity.json")
MEM = {"chats": {}, "people": {}}
MEM_LOCK = threading.Lock()
MEM_PENDING = {}
MEM_BUSY = set()
MEM_STATE = {"warned": False}
MEM_THRESHOLD = int(os.environ.get("DSH_WECHAT_MEM_THRESHOLD") or 12)


def _读json(path, 默认):
    try:
        with open(path, encoding="utf-8") as f:
            o = json.load(f)
        return o if isinstance(o, dict) else 默认
    except Exception:
        return 默认


def 记忆_载入():
    global MEM
    o = _读json(MEM_PATH, {})
    o.setdefault("chats", {})
    o.setdefault("people", {})
    MEM = o
    return MEM


def 记忆_存盘():
    try:
        os.makedirs(HOME_DIR, exist_ok=True)
        tmp = MEM_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(MEM, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MEM_PATH)
    except Exception as e:
        log("记忆存档失败：", repr(e))


def 记忆_前缀(chat_id, sender_id, sender_name, is_group):
    """回话前要带上的一两行「你记得什么」。"""
    with MEM_LOCK:
        群记 = ((MEM.get("chats") or {}).get(chat_id) or {}).get("summary") or ""
        人 = (MEM.get("people") or {}).get(sender_id) or {}
        印象 = 人.get("impression") or ""
        最近 = ""
        try:
            days = 人.get("days") or {}
            今天 = time.strftime("%Y-%m-%d")
            过去 = sorted([k for k in days if k < 今天])
            if 过去:
                最近 = str(days.get(过去[-1]) or "")
        except Exception:
            最近 = ""
    好感 = _读json(AFFINITY_PATH, {}).get(sender_id) or {}
    段 = []
    if is_group and 群记:
        段.append("（你记得这个群：%s）" % 群记)
    if 印象:
        印 = str(印象).rstrip("。！？；;，,. ")
        尾 = ""
        try:
            if 好感.get("n"):
                尾 += "；聊过 %d 次" % int(好感.get("n"))
            if 好感.get("score") not in (None, ""):
                尾 += "，好感度 %s" % 好感.get("score")
        except Exception:
            pass
        段.append("（你对「%s」的印象：%s%s）" % (sender_name or "这人", 印, 尾))
    if 最近:
        段.append("（上次跟他聊到：%s）" % str(最近).rstrip("。！？；;，,. ")[:100])
    return "".join(段)


def 好感分(uid):
    """宿主记在 wechat-affinity.json 里的好感分（聊得越多、分越高）。"""
    try:
        a = _读json(AFFINITY_PATH, {})
        return float(((a.get(uid) or {}).get("score")) or 0.0)
    except Exception:
        return 0.0


def 接话概率(uid):
    """好感度决定群里的接话率：0 分 30%，每 1 分 +12%，封顶 85%。
    主人和点名不走这个闸（必回）。
    2026-11-06 第一版 0.12+0.10（封顶 75）被说太勤快，傍晚压到 0.08+0.07；
    两天下来朋友说话几乎全被挡掉、只剩主人一个人有来有回，11-09 又抬回来。"""
    return min(0.85, max(0.25, 0.30 + 好感分(uid) * 0.12))


def 记忆_记(chat_id, chat_name, sender_id, sender_name, text, is_group):
    """把一条消息记进待总结队列；攒够了就后台更新档案。"""
    t = str(text or "").strip()
    if not t or not chat_id:
        return
    触发 = False
    with MEM_LOCK:
        c = MEM["chats"].setdefault(chat_id, {"name": chat_name, "kind": "群" if is_group else "私聊",
                                              "summary": "", "updated": 0, "n": 0})
        if chat_name:
            c["name"] = chat_name
        c["n"] = int(c.get("n") or 0) + 1
        待 = MEM_PENDING.setdefault(chat_id, [])
        待.append({"t": time.strftime("%m-%d %H:%M"), "who": sender_name or "", "uid": sender_id or "",
                   "text": t[:300]})
        if len(待) > 80:
            del 待[:-80]
        触发 = len(待) >= MEM_THRESHOLD and chat_id not in MEM_BUSY
    记忆_存盘()
    if 触发:
        with MEM_LOCK:
            MEM_BUSY.add(chat_id)
        threading.Thread(target=记忆_总结, args=(chat_id,), daemon=True).start()


def 记忆_总结(chat_id):
    try:
        with MEM_LOCK:
            待 = list(MEM_PENDING.get(chat_id) or [])
            c = dict((MEM.get("chats") or {}).get(chat_id) or {})
        if not 待:
            return
        cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
        key = str(cfg.get("视觉密钥") or "").strip() or str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            if not MEM_STATE["warned"]:
                MEM_STATE["warned"] = True
                log("记忆：没有可用的 DeepSeek 密钥，长期记忆先不更新")
            return
        记录 = "\n".join("%s %s[%s]：%s" % (x["t"], x["who"], x["uid"], x["text"]) for x in 待)
        主人们 = "、".join([str(x) for x in (cfg.get("主人") or [])]) or "（没配）"
        提示 = (
            "你在维护一份微信长期记忆档案。只输出 JSON，不要任何解释。\n"
            '格式：{"chat_summary":"...","people":{"<wxid>":"..."}}\n'
            "chat_summary：这个聊天在聊什么、有哪些在办的事、大家的关系，150 字以内。\n"
            "people：给一条对某个人的印象（身份、说话风格、在意什么、跟主人什么关系），60 字以内；"
            "键必须用记录里方括号里的那串 id。\n"
            "注意：主人本人的 wxid 是 %s —— 记录里这个 id 说的话就是主人说的，不要把他写成外人。\n\n"
            "【旧档案】%s\n【新记录】\n%s" % (主人们, (c.get("summary") or "（还没有）"), 记录)
        )
        body = json.dumps(
            {"model": "deepseek-flash", "messages": [{"role": "user", "content": 提示}],
             "max_tokens": 900, "thinking": {"type": "disabled"}},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions", data=body,
            headers={"content-type": "application/json", "authorization": "Bearer " + key},
            method="POST")
        with urllib.request.urlopen(req, timeout=90) as r:
            出 = json.loads(r.read().decode("utf-8") or "{}")
        文 = ((出.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        文 = 文.strip()
        if 文.startswith("```"):
            文 = re.sub(r"^```(?:json)?\s*|\s*```$", "", 文, flags=re.S)
        起 = 文.find("{")
        止 = 文.rfind("}")
        if 起 >= 0 and 止 > 起:
            文 = 文[起:止 + 1]
        新 = json.loads(文)
        with MEM_LOCK:
            if 新.get("chat_summary"):
                ch = MEM["chats"].setdefault(chat_id, {})
                ch.setdefault("name", c.get("name") or "")
                ch.setdefault("kind", "群" if str(chat_id).endswith("@chatroom") else "私聊")
                ch.setdefault("n", int(c.get("n") or 0))
                ch["summary"] = str(新["chat_summary"])[:400]
                ch["updated"] = time.time()
            for uid, 印 in (新.get("people") or {}).items():
                if not uid or not 印:
                    continue
                p = MEM["people"].setdefault(uid, {"name": "", "n": 0})
                p["impression"] = str(印)[:200]
                p["name"] = next((x["who"] for x in 待 if x.get("uid") == uid and x.get("who")), p.get("name") or "")
                p["updated"] = time.time()
            MEM_PENDING[chat_id] = []
            log("记忆已更新（%s）：%s" % (c.get("name") or chat_id,
                                      (MEM["chats"].get(chat_id, {}).get("summary") or "")[:50]))
        记忆_存盘()
    except Exception as e:
        log("记忆总结失败：", repr(e))
    finally:
        with MEM_LOCK:
            MEM_BUSY.discard(chat_id)


# ---------- 每日总结：给每个人都留一份长期记忆 ----------
# 每 12 条消息的滚动总结只能覆盖「聊得多的群」；冷清的私聊、只说过两句的人
# 根本攒不够 12 条。这里再加一条按天跑的：把当天所有聊天记录压进档案，
# 每个人记一行「今天他说了什么」，只留最近 7 天。

def _llm_json(提示, 最大=1500):
    """调 DeepSeek，回来一个 JSON 对象。密钥跟看图共用那个。"""
    cfg = _读json(os.path.join(HOME_DIR, "wechat-bridge-config.json"), {})
    key = str(cfg.get("视觉密钥") or "").strip() or str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("没有可用的 DeepSeek 密钥")
    body = json.dumps(
        {"model": "deepseek-flash", "messages": [{"role": "user", "content": 提示}],
         "max_tokens": 最大, "thinking": {"type": "disabled"}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=body,
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        o = json.loads(r.read().decode("utf-8") or "{}")
    文 = (((o.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if 文.startswith("```"):
        文 = re.sub(r"^```(?:json)?\s*|\s*```$", "", 文, flags=re.S)
    起, 止 = 文.find("{"), 文.rfind("}")
    if 起 >= 0 and 止 > 起:
        文 = 文[起:止 + 1]
    return json.loads(文)


def 每日_总结一个(cid, 名字, today, 记录, 主人们, 旧人表):
    """把一个聊天今天的记录压进档案：群要一段摘要，每个人一行「今天」。"""
    旧记 = ((MEM.get("chats") or {}).get(cid) or {}).get("summary") or "（无）"
    提示 = (
        "你在维护一份微信长期记忆档案。下面是「%s」在 %s 的聊天记录。只输出 JSON，不要解释：\n"
        '{"chat_summary":"这个聊天今天聊了什么、有什么事在办、气氛如何（150字内）",'
        '"people":{"<方括号里的id>":{"today":"他今天说了什么、有什么新信息（40字内）",'
        '"impression":"更新后的长期印象：身份、说话风格、在意什么、跟主人什么关系（60字内）"}}}\n'
        "people 只写今天出现过的人；主人本人的 wxid 是 %s，别把他写成外人。\n\n"
        "【旧摘要】%s\n【旧人物】%s\n【今天的记录】\n%s"
    ) % (名字 or cid, today, 主人们 or "（没配）", 旧记, 旧人表 or "（无）", "\n".join(记录))
    出 = _llm_json(提示)
    with MEM_LOCK:
        ch = MEM["chats"].setdefault(cid, {"name": 名字, "kind": "群" if str(cid).endswith("@chatroom") else "私聊",
                                           "summary": "", "n": 0})
        if 出.get("chat_summary"):
            ch["summary"] = str(出["chat_summary"])[:400]
        ch["updated"] = time.time()
        ch["last_daily"] = today
        for uid, v in (出.get("people") or {}).items():
            if not uid:
                continue
            # 模型有时给字符串（旧格式），有时给对象，有时把 today 写成「今日」/「今天」—— 都认
            if isinstance(v, str):
                今日句, 印象句 = "", v
            elif isinstance(v, dict):
                印象句 = str(v.get("impression") or v.get("印象") or "").strip()
                今日句 = str(v.get("today") or v.get("今日") or v.get("今天") or v.get("note") or "").strip()
            else:
                continue
            p = MEM["people"].setdefault(uid, {"name": "", "n": 0})
            if 印象句:
                p["impression"] = 印象句[:200]
            if 今日句:
                days = p.setdefault("days", {})
                days[today] = 今日句[:120]
                for k in sorted(days)[:-7]:
                    days.pop(k, None)
            elif 印象句 and not p.get("days", {}).get(today):
                # 没给 today 就退而求其次：把今天的印象记成当天一行，至少留个时间点
                days = p.setdefault("days", {})
                days[today] = 印象句[:120]
                for k in sorted(days)[:-7]:
                    days.pop(k, None)
            p["updated"] = time.time()
    记忆_存盘()
    log("每日总结（%s）：%s" % (名字 or cid, (MEM["chats"].get(cid, {}).get("summary") or "")[:40]))


记忆_载入()

# 从 DSH 设置页拉的配置（每 30 秒刷一次）
CFG = {
    "群上下文条数": 6,
    "日记": True,
    "日记目录": "",
    "主人": [],
    "屏蔽": [],
}
CFG_TS = 0.0

# 微信里收到的图片 / 表情包落盘到这里，再把路径交给宿主去看图
INBOUND_DIR = os.path.join(
    os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
    "wechat-inbound",
)
VISION_CACHE = os.path.join(os.path.dirname(INBOUND_DIR), "wechat-vision-cache.json")
# 宿主版本 ≥1.2.0 时它自己会看图；老宿主就由桥接先看好再发文字
HOST_VERSION = (1, 1, 0)


def _活跃群列表():
    """直接从磁盘配置读「活跃群」—— 宿主 /state 里暂时还没这个字段。"""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    path = os.path.join(home, "wechat-bridge-config.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [str(x).strip() for x in (data.get("活跃群") or []) if str(x).strip()]
    except Exception:
        return []


def refresh_config():
    """问宿主要一次配置。设置页改完不用重启桥接。"""
    global CFG, CFG_TS
    r = http_json("/dsh-wechat-bridge/state")
    c = (r or {}).get("配置") if isinstance(r, dict) else None
    if not isinstance(c, dict):
        return False
    try:
        CFG = {
            "群上下文条数": max(0, min(50, int(c.get("群上下文条数") or 0))),
            "日记": bool(c.get("日记", True)),
            "日记目录": str(c.get("日记目录") or "").strip(),
            "主人": [str(x).strip() for x in (c.get("主人") or []) if str(x).strip()],
            "屏蔽": [str(x).strip() for x in (c.get("屏蔽") or []) if str(x).strip()],
            "群回复间隔秒": max(0.0, float(c.get("群回复间隔秒") or 0)),
            "群每小时上限": max(0.0, float(c.get("群每小时上限") or 0)),
        }
        CFG["活跃群"] = _活跃群列表()
        CFG_TS = time.time()
        return True
    except Exception as e:
        log("解析配置失败：", repr(e))
        return False


def parse_args():
    global DSH, TOKEN, ONLY_CHATS, SEND_ENABLED, SKIP_OLDER, POLL_SEC, ALIASES

    p = argparse.ArgumentParser(description="DSH WeChat bridge")
    p.add_argument("--dsh", default=os.environ.get("DSH_ORIGIN", "http://127.0.0.1:3080"))
    p.add_argument("--token", default=os.environ.get("DSH_BRIDGE_TOKEN", ""))
    p.add_argument("--only", default="", help="只转这些聊天，逗号分隔（留空 = 全部）")
    p.add_argument("--aliases", default=DEFAULT_ALIASES, help="群里叫这些名字才算在跟我说话")
    p.add_argument("--no-send", action="store_true", help="只收不发")
    p.add_argument("--skip-older", type=float, default=15.0)
    p.add_argument("--poll", type=float, default=2.0)
    p.add_argument("--test-image", default="", help="只看一张本地图片然后退出（调视图用）")
    a = p.parse_args()

    DSH = str(a.dsh).rstrip("/")
    TOKEN = str(a.token or "")
    ONLY_CHATS = [x.strip() for x in str(a.only or "").split(",") if x.strip()]
    ALIASES = [x.strip() for x in str(a.aliases or "").split(",") if x.strip()]
    SEND_ENABLED = not a.no_send
    SKIP_OLDER = float(a.skip_older or 15.0)
    POLL_SEC = float(a.poll or 2.0)
    return a


# @ 某人时微信会把机器 id 塞进正文，看着像乱码 —— 洗掉
_MENTION_ID = re.compile(r"wxid_[0-9a-zA-Z_-]+[:：]?")
_AT_TAG = re.compile(r"@[^\s@]{1,24}\s?")


def clean_text(text):
    t = _MENTION_ID.sub("", text or "")
    t = _AT_TAG.sub("", t)
    return " ".join(t.split()).strip()


# ---------- HTTP ----------

def http_json(path, payload=None, method=None):
    url = DSH + path
    data = None
    headers = {"content-type": "application/json"}
    if TOKEN:
        headers["x-bridge-token"] = TOKEN
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method or "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "说": "HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "说": str(e)}


def main():
    global HOST_VERSION, ARGS
    ARGS = parse_args()
    log("DSH 地址：", DSH, "｜只收不发：", not SEND_ENABLED)

    log("import wechatauto …（第一次会慢）")
    try:
        from wechatauto import WeChatDB, WeChatGUI
        from wechatauto.db import Listener

        # 拟人节流：默认 natural 档是「120 秒最多 6 次写」，撞上就冷却 60 秒左右。
        # 我这边一次回复要发好几段，很容易撞线 —— 撞线时消息发不出去，
        # 上层会以为失败再重试，重试又撞线，就出现了「卡住」「重复」。
        # 调到 fast 档（120 秒 20 次），保留节奏，但不会把正常聊天卡死。
        try:
            from wechatauto import rhythm as _rhythm

            _rhythm.set_profile('fast')
            _rhythm.configure(burst=30)   # 120 秒内 30 次，别再把正常回复卡住
            _p = _rhythm.profile()
            log("发送节流档位：", _p.name, "（%d 秒内 %d 次）" % (int(_p.window), _p.burst))
        except Exception as _e:
            log("节流档位调整失败（不影响用）：", repr(_e))
    except Exception as e:
        log("✗ import wechatauto 失败：", repr(e))
        log("  依赖没装好 —— 回 DSH 设置页点「装依赖」")
        return 2

    if UIA_COMPAT:
        try:
            import uia_compat  # noqa: F401

            log("uia_compat.apply() =", uia_compat.apply())
        except ImportError:
            pass
        except Exception as e:
            log("⚠ uia_compat 出错（继续跑）：", repr(e))

    log("连接微信数据库（只读）…")
    try:
        db = WeChatDB()
    except Exception as e:
        log("✗ 连不上微信数据库：", repr(e))
        log("  检查：微信登录了吗？微信 → 设置 → 文件管理 → 默认保存位置")
        return 2
    globals()["_DB"] = db          # 语音转写那几个模块级函数要用
    me = getattr(db, "wxid", "")
    log("当前账号 wxid：", me)
    try:
        info = db.get_self_info() or {}
        log("昵称：", info.get("nick_name") or info.get("remark") or "?")
    except Exception:
        pass

    # 微信 4.x 的新图片格式 .wxgf 要靠 ffmpeg 转码，没有就只能「看不清」
    try:
        log("ffmpeg：", shutil.which("ffmpeg") or "没找到（.wxgf 图片会转不出来）")
    except Exception:
        pass

    name_cache = {}

    def display_name(username):
        # 群名会被改（2026-11-06 「扎打基…」→「大肥鱼旗下35子民」），缓存住就会一直用旧名，
        # 发消息按旧名去找会话很容易找空。群名不缓存，每次现查；私聊昵称照旧缓存。
        if not str(username).endswith("@chatroom") and username in name_cache:
            return name_cache[username]
        name = ""
        try:
            if str(username).endswith("@chatroom"):
                name = db.group_id_to_name(username) or ""
            else:
                name = db.get_nickname(username) or ""
        except Exception:
            name = ""
        if name and not str(username).endswith("@chatroom"):
            name_cache[username] = name
        return name

    # ---------- 群回复频率 ----------

    chat_hits = {}      # 群名 -> [时间戳]

    def 频率允许(chat):
        gap = CFG.get("群回复间隔秒") or 0
        per_hour = CFG.get("群每小时上限") or 0
        if not gap and not per_hour:
            return True
        now = time.time()
        hits = [t for t in chat_hits.get(chat, []) if now - t < 3600]
        if gap and hits and now - hits[-1] < gap:
            log("群频率：%s 刚回过，%.0f 秒内不再接" % (chat, gap))
            return False
        if per_hour and len(hits) >= per_hour:
            log("群频率：%s 这一小时已经接了 %d 次，够了" % (chat, len(hits)))
            return False
        hits.append(now)
        chat_hits[chat] = hits
        return True

    # ---------- 屏蔽 / 主人 / 日记 / 群上下文 ----------

    def is_blocked(chat, nickname, cid=""):
        # 屏蔽名单三种写法都认：显示名（群名/昵称）、群里的 wxid、会话 id。
        # 只认显示名的话，一改备注就漏，所以底层那个码永远多认一份。
        for b in CFG["屏蔽"]:
            if b and (b == chat or b == nickname or (cid and b == cid)):
                return True
        return False

    def is_master(sender_id, nickname):
        # 认人看微信里显示的名字（昵称/备注），wxid 也认一份当补充 —— 规矩就这样。
        for m in CFG["主人"]:
            if m and (m == sender_id or (nickname and m == nickname)):
                return True
        return False

    def diary_path(chat):
        base = CFG["日记目录"] or os.path.join(os.path.expanduser("~"), ".dsh", "wechat-diary")
        day = time.strftime("%Y-%m-%d")
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", chat).strip("_")[:60] or "群"
        return os.path.join(base, day, safe + ".md")

    def diary_append(chat, ts, who, content):
        p = diary_path(chat)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            new = not os.path.exists(p)
            with open(p, "a", encoding="utf-8") as f:
                if new:
                    f.write("# %s · %s\n\n" % (chat, time.strftime("%Y-%m-%d")))
                t = time.strftime("%H:%M", time.localtime(ts or time.time()))
                f.write("- %s %s：%s\n" % (t, who or "某人", content))
        except Exception as e:
            log("写日记失败：", repr(e))

    def group_context(username, chat, limit):
        """那群最近在聊什么 —— 拼成一段给模型看。"""
        if limit <= 0:
            return ""
        try:
            rows = db.get_messages(username, limit=limit + 2)
        except Exception as e:
            log("取群上下文失败：", repr(e))
            return ""
        if not rows:
            return ""
        try:
            rows = sorted(rows, key=lambda m: float(m.get("create_time") or 0))
        except Exception:
            pass
        rows = rows[:-1][-limit:]      # 最后一条就是当前这句，去掉
        lines = []
        for m in rows:
            类 = (m.get("type") or "")
            原始 = m.get("content") or ""
            # 引用回复也算上下文：能看出「谁在回谁」，模型才不会把别人的对话当成跟自己说的
            回给谁 = ""
            引 = re.search(r"<refermsg>.*?<displayname>(.*?)</displayname>", 原始, re.S)
            if 引:
                回给谁 = clean_text(html.unescape(引.group(1)))[:12]
            if 类 == "文本":
                txt = clean_text(原始)
            elif 类 == "文件/链接/卡片":
                mt = re.search(r"<title>(.*?)</title>", 原始, re.S)
                txt = clean_text(html.unescape(mt.group(1))) if mt else ""
            else:
                continue
            if not txt or txt.startswith("<"):
                continue
            ts = float(m.get("create_time") or 0)
            t = time.strftime("%H:%M", time.localtime(ts)) if ts else "??:??"
            sid = m.get("sender_username") or ""
            # 同一个号上跑着鱼鱼，所以「自己发的」全是鱼鱼说的；主人是另一个号
            if me and sid == me:
                who = "鱼鱼（我自己发的）"
            elif is_master(sid, display_name(sid) or ""):
                who = "%s（主人）" % (display_name(sid) or "主人")
            else:
                who = display_name(sid) or "某人"
            lines.append("[%s] %s%s：%s" % (t, who, (" 回复 " + 回给谁) if 回给谁 else "", txt))
        if not lines:
            return ""
        return "（「%s」刚才在聊：\n%s\n）\n" % (chat, "\n".join(lines))

    # ---------- 表情包：正文里的 [表情: 描述] 变成真图片 ----------

    # 和前端配图器（dsh-meme client.js 的 matchDesc）同一套规则，不然同一条回复
    # 在 Web 里配到的图，和微信里发出去的会是两张。
    _RE_MEME = re

    def _fold_caption(s):
        s = str(s or "").strip()
        s = _RE_MEME.sub(r'[\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u00ab\u00bb]', '"', s)
        return _RE_MEME.sub(r"\s+", " ", s)

    def _loose_caption(s):
        return _RE_MEME.sub(r'["\'\s]', "", _fold_caption(s))

    _TOKEN_SPLIT = _RE_MEME.compile(r'[\s,.!?;:\u3001\uff0c\u3002\uff01\uff1f\uff1b\uff1a"\u201c\u201d\'\u2018\u2019()\uff08\uff09\\[\\]\u3010\u3011\u300a\u300b\u2026\u00b7\uff5e~\\-]+')

    def _split_desc_tokens(s):
        parts = _TOKEN_SPLIT.split(_fold_caption(s))
        toks = [_RE_MEME.sub(r'["\']', "", p) for p in parts]
        toks = [t for t in toks if len(t) >= 2]
        toks.sort(key=len, reverse=True)
        return toks

    MEME = {"captions": {}, "keywords": {}, "rows": []}

    def load_memes():
        import glob
        import sqlite3

        dbs = []
        try:
            base = os.path.join(os.path.expanduser("~"), ".dsh", "profiles")
            dbs += glob.glob(os.path.join(base, "*", "node_modules", "dsh-meme", "memes", "*", "index.db"))
        except Exception:
            pass
        try:
            # 我们自己的小表情库（用收到的贴纸 + 视觉描述攒的，见 build_stickers.py）
            dbs += glob.glob(os.path.join(os.path.expanduser("~"), ".dsh", "wechat-stickers", "*", "index.db"))
        except Exception:
            pass
        def 加键(表, key, path):
            raw = str(key or "").strip()
            if not raw:
                return
            for form in {raw, _fold_caption(raw), _loose_caption(raw)}:
                if form and form not in 表:
                    表[form] = path

        for dbp in dbs:
            pack = os.path.dirname(dbp)
            try:
                con = sqlite3.connect(dbp)
                for path, cap, kw, fn in con.execute("select path, caption, keywords, file_name from memes"):
                    absp = os.path.join(pack, str(path).replace("/", os.sep))
                    if not os.path.exists(absp):
                        continue
                    cap = str(cap or "").strip()
                    kw = str(kw or "")
                    fn = str(fn or "")
                    加键(MEME["captions"], cap, absp)
                    加键(MEME["captions"], fn, absp)
                    加键(MEME["captions"], cap[:80], absp)
                    加键(MEME["captions"], cap[:100], absp)
                    加键(MEME["keywords"], kw, absp)
                    for w in kw.split():
                        加键(MEME["keywords"], w, absp)
                    hay = []
                    parts = []
                    for s in (cap, kw, fn):
                        f = _fold_caption(s)
                        if not f:
                            continue
                        l = _loose_caption(s)
                        hay.append(f)
                        if l and l != f:
                            hay.append(l)
                        parts.append(s)
                    MEME["rows"].append({"hay": hay, "tokens": _split_desc_tokens(" ".join(parts)), "path": absp})
                con.close()
            except Exception as e:
                log("读表情库失败：", repr(e))
        if MEME["rows"]:
            log("表情库：%d 张（%s）" % (len(MEME["rows"]), "、".join(os.path.basename(os.path.dirname(d)) for d in dbs)))

    def find_meme(desc):
        """和前端 matchDesc 同一套优先级：caption 精确 > keyword 精确 > 分词最长命中。"""
        raw = str(desc or "").strip()
        if not raw:
            return ""
        forms = [raw, _fold_caption(raw), _loose_caption(raw)]
        for 表 in (MEME["captions"], MEME["keywords"]):
            for form in forms:
                hit = 表.get(form)
                if hit:
                    return hit
        needles = []
        for t in _split_desc_tokens(raw):
            l = _loose_caption(t)
            if l:
                needles.append(l)
        best, best_len, best_cnt = "", 0, 0
        for row in MEME["rows"]:
            longest = 0
            count = 0
            for needle in needles:
                length = len(needle) if any(needle in h for h in row["hay"]) else 0
                for tok in row["tokens"]:
                    t = _loose_caption(tok)
                    if t and (needle in t or t in needle):
                        length = max(length, min(len(needle), len(t)))
                if length:
                    longest = max(longest, length)
                    count += 1
            if longest > best_len or (longest == best_len and count > best_cnt):
                best, best_len, best_cnt = row["path"], longest, count
        return best

    # ---------- 发：从 DSH 取队列，用 UIA 发出去 ----------

    gui_box = {"gui": None}
    send_lock = threading.Lock()
    # 刚发出去的消息：同一个微信号，自己发的都会进库 ——
    # 靠这个列表把「我们发的」跟「主人自己在手机上打的」区分开，不然会自问自答。
    recently_sent = []
    # 上一次往外发的时间。文件传输助手发完立刻会被读回来，几秒内一律不看（防回环的兜底）
    STATE = {"last_send": 0.0}

    def 静音窗口操作(gui):
        """把 gui 上「会主动把微信拉到前台」的方法换成空操作。

        wechatauto 发送前习惯先 ensure_visible()（最小化遮挡窗口 + 置顶 +
        ShowWindow(SW_RESTORE)），这是窗口在后台发送时自己冒头的最大来源。
        UIA 写控件不需要窗口在前台，所以这里直接让它「当作已经可见」。
        图片发送走的是另一条显式前台路径（_ensure_wechat_ready(前台=True)），
        不受这里影响。
        """
        try:
            if getattr(gui, "_quiet_gui", False):
                return
            gui.ensure_visible = lambda *a, **k: True
            gui.bring_to_front = lambda *a, **k: True
            gui._minimize_blockers = lambda *a, **k: 0
            gui._quiet_gui = True
        except Exception as e:
            log("静音窗口操作失败（不影响发送）：", repr(e))

    def get_gui():
        if gui_box["gui"] is None:
            gui_box["gui"] = WeChatGUI()
            log("WeChatGUI 已就绪")
        return gui_box["gui"]

    def _foreground_wechat():
        """把微信主窗口拉到前面。只在发送失败重试时用 —— 平时不抢你的前台。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 9)          # SW_RESTORE
                win32gui.SetForegroundWindow(found[0])    # 拉到前面
                time.sleep(0.5)
                return True
        except Exception as e:
            log("把微信窗口拉到前面失败：", repr(e))
        return False

    def _minimize_wechat():
        """发完把微信收起来 —— 不然它被拉到前台，主人正在干别的就被打断了。"""
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            if found:
                win32gui.ShowWindow(found[0], 6)          # SW_MINIMIZE
                return True
        except Exception as e:
            log("最小化微信失败：", repr(e))
        return False

    def _强设前台(hwnd):
        """把窗口真正顶到前台。

        单纯的 SetForegroundWindow 会被 Windows 的前台锁定拒绝（调用进程不是
        当前前台进程时）。先用 AttachThreadInput 把自己的输入队列接到当前前台
        窗口上，这个限制就绕过去了。
        """
        try:
            import ctypes

            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = user32.GetWindowThreadProcessId(hwnd, None)
            连上 = False
            if tid_fg and tid_me and tid_fg != tid_me:
                连上 = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            finally:
                if 连上:
                    user32.AttachThreadInput(tid_me, tid_fg, False)
            return True
        except Exception as e:
            log("强设前台失败：", repr(e))
            return False

    def _取消置顶():
        h = STATE.pop("wx_hwnd", None)
        if not h:
            return
        try:
            import win32con
            import win32gui as wg

            wg.SetWindowPos(h, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        except Exception:
            pass

    # ---------- 后台发送（学 whale-girl）：不抢前台、发完还原 ----------
    # 起因：wechatauto 的 WeChatUIA.ensure_window() 每次发送都会调 _activate() →
    #       把微信恢复并顶到最前面。实测微信最小化/被遮挡时其实照样能发，这步纯属
    #       多余，代价是主人用电脑时会被反复打断。这里学 whale-girl 的做法：
    #       文字走后台（UIA 直接写控件，不需要焦点），失败才抢前台重试；
    #       发完把窗口状态照原样还回去。

    def _微信窗口():
        try:
            import win32gui

            found = []

            def cb(hwnd, _):
                try:
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                        found.append(hwnd)
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(cb, None)
            return found[0] if found else None
        except Exception:
            return None

    def _微信在前台():
        """微信此刻是不是前台窗口。全局回车（keybd_event）只在前台时才能按，
        否则会按到主人正在用的别的窗口里去。"""
        try:
            import ctypes

            h = _微信窗口()
            return bool(h) and ctypes.windll.user32.GetForegroundWindow() == h
        except Exception:
            return False

    def _快照窗口():
        """记下发送前窗口长什么样：句柄、是否最小化、当时的前台窗口。"""
        snap = {"hwnd": None, "iconic": False, "foreground": None, "visible": True}
        try:
            import ctypes
            import win32gui

            h = _微信窗口()
            if not h:
                return snap
            snap["hwnd"] = h
            snap["iconic"] = bool(win32gui.IsIconic(h))
            snap["visible"] = bool(win32gui.IsWindowVisible(h))
            snap["foreground"] = ctypes.windll.user32.GetForegroundWindow() or None
        except Exception:
            pass
        return snap

    def _还原窗口(snap):
        """把窗口状态还回去：原来最小化的继续最小化，前台还给原来那个窗口。"""
        if not snap:
            return
        try:
            import ctypes
            import win32gui

            h = snap.get("hwnd")
            if not h:
                return
            if not snap.get("visible", True):
                # 发送前微信是「收在托盘里」（窗口隐藏），发完要重新藏起来，
                # 别让它突然出现在屏幕上
                win32gui.ShowWindow(h, 0)          # SW_HIDE
                return
            if snap.get("iconic"):
                win32gui.ShowWindow(h, 6)          # SW_MINIMIZE
                return
            fg = snap.get("foreground")
            cur = ctypes.windll.user32.GetForegroundWindow()
            if fg and fg != cur and ctypes.windll.user32.IsWindow(fg):
                try:
                    ctypes.windll.user32.SetForegroundWindow(fg)
                except Exception:
                    pass
        except Exception:
            pass

    def _守护最小化(hwnd):
        """发送期间盯着窗口，看它有没有被谁拉出来（只记录，不动手）。

        以前是「一冒头就按回去」，结果切会话时新会话的输入框跟着丢焦点，
        字发不出去。收回最小化交给 send_one 的 finally（_还原窗口），
        那会儿发送已经结束，按回去不会再打断任何操作。
        """
        stopped = threading.Event()

        def 跑():
            报过 = False
            while not stopped.is_set():
                try:
                    import win32gui

                    if hwnd and win32gui.IsWindow(hwnd) and not win32gui.IsIconic(hwnd):
                        if not 报过:
                            log("窗口被拉起来了（发送过程中）")
                            报过 = True
                except Exception:
                    pass
                time.sleep(0.25)

        threading.Thread(target=跑, daemon=True).start()
        return stopped

    def _install_quiet_uia(u):
        """给 UIA 引擎打补丁：quiet_mode 下 _activate 直接返回，不抢前台。

        whale-girl 实测 18/18：微信最小化、被遮挡、前台全屏时都能正常发送，
        所以「把窗口拉到最前」这一步可以省掉。不改第三方库文件，只改实例。
        """
        try:
            import types

            if getattr(u, "_quiet_patch", False):
                u.quiet_mode = True
                return
            orig = getattr(u, "_activate", None)
            if orig is None:
                return

            def patched(self, win, _orig=orig):
                if getattr(self, "quiet_mode", False):
                    return
                return _orig(win)

            u._activate = types.MethodType(patched, u)

            # 还有一个漏网的：_force_foreground 会 ShowWindow(SW_RESTORE) +
            # SetForegroundWindow，把最小化的微信直接拉出来。安静模式下让它
            # 直接「假装成功」——UIA 写控件并不需要窗口在前台。
            if not getattr(u, "_quiet_ff", False):
                try:
                    u._force_foreground = lambda hwnd: True
                    u._quiet_ff = True
                except Exception:
                    pass

            # 点击补丁：wechatauto 的 _click_ctrl 会 SetCursorPos + 真实鼠标点击，
            # 切会话/搜索时屏幕上鼠标乱飞就是它。这里优先用 UIA 的 Invoke/Select，
            # 不碰鼠标；控件不支持时才退回原来的模拟点击。
            orig_click = getattr(u, "_click_ctrl", None)
            if orig_click is not None and not getattr(u, "_quiet_click", False):
                def quiet_click(self, ctrl, right=False, _orig=orig_click):
                    if right:
                        return _orig(ctrl, right=True)
                    err1 = err2 = None
                    try:
                        ctrl.GetInvokePattern().Invoke()
                        log("点击控件：走 Invoke（不动鼠标）")
                        return True
                    except Exception as e:
                        err1 = repr(e)
                    try:
                        ctrl.GetSelectionItemPattern().Select()
                        log("点击控件：走 Select（不动鼠标）")
                        return True
                    except Exception as e:
                        err2 = repr(e)
                    log("点击控件：退回真实鼠标点击（%s / %s）" % (err1, err2))
                    return _orig(ctrl, right=False)

                u._click_ctrl = types.MethodType(quiet_click, u)
                u._quiet_click = True

            u._quiet_patch = True
            u.quiet_mode = True
        except Exception as e:
            log("后台模式补丁没打上（不影响发送）：", repr(e))

    # UIA 起不来时的熔断。
    # wechatauto 的 UIA 一失败就退化成「按固定坐标盲点」：每秒两三次扫屏、枚举窗口、
    # 顺手最小化挡着的窗口 —— 主人看到的就是电脑卡成幻灯片，桌面还自己动。
    # 所以连着起不来几次就先停手，别让它一直空转。
    uia_坏 = {"连败": 0, "到": 0.0}

    def _记UIA失败(原因=""):
        uia_坏["连败"] += 1
        文本 = str(原因)
        缺 = ""
        if "No module named 'numpy'" in 文本 or "No module named \"numpy\"" in 文本:
            缺 = "（缺 numpy —— 设置 → 通到微信，点一次「装依赖」，装完重启桥接）"
        elif "No module named" in 文本:
            缺 = "（缺 Python 包 —— 设置 → 通到微信，点一次「装依赖」）"
        if uia_坏["连败"] >= 3 and time.time() >= uia_坏["到"]:
            uia_坏["到"] = time.time() + 300
            log(
                "UIA 连着 %d 次起不来，先停 5 分钟不再碰微信窗口（省得它盲点坐标乱点）%s"
                % (uia_坏["连败"], 缺)
            )

    def _uia熔断中():
        return time.time() < uia_坏["到"]

    def _取引擎():
        """每次发送都问一次最新的 UIA 引擎。

        微信跑久了会把 accessibility gate 那个字节归零，缓存的引擎会「假死」：
        还能收消息，但一条也发不出去，只能重启桥接 —— 每次重取能避开这个坑。

        注意：这里**不打 quiet 补丁**。后台静默那条路（_activate 直接返回 +
        _force_foreground 假装成功）会让窗口没真正激活、后面的点击和粘贴落空，
        切会话时直接把发送卡死。先回到原生行为：会冒头，但发得出去。
        """
        if _uia熔断中():
            return None
        try:
            gui = get_gui()
            u = gui._get_uia()
            if u is None:
                _记UIA失败("引擎给回 None")
                return None
            uia_坏["连败"] = 0
            uia_坏["到"] = 0.0
            return u
        except Exception as e:
            log("取 UIA 引擎失败：", repr(e))
            _记UIA失败(e)
            return None

    def _recover_uia(原因=""):
        """强制重新热激活 UIA（refresh=True），成功返回 True。"""
        if _uia熔断中():
            return False
        try:
            gui = get_gui()
            u = gui._get_uia(refresh=True)
            if u is not None:
                uia_坏["连败"] = 0
                uia_坏["到"] = 0.0
                log("UIA 已重新热激活" + (("（" + 原因 + "）") if 原因 else ""))
                return True
            _记UIA失败("热激活给回 None")
        except Exception as e:
            log("UIA 重新热激活异常：", repr(e))
            _记UIA失败(e)
        return False

    def _press_enter(次=2, ctrl=False):
        """底层按键补回车。

        UIA 的 SendKeys 有时根本不触发发送（字符进了框、回车没生效），
        这里用 keybd_event 硬按几次；ctrl=True 兼容微信里「Ctrl+Enter 发送」的设置。
        """
        try:
            import win32api
            import win32con

            for _ in range(次):
                if ctrl:
                    win32api.keybd_event(0x11, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, 0, 0)
                win32api.keybd_event(0x0D, 0, win32con.KEYEVENTF_KEYUP, 0)
                if ctrl:
                    win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.12)
            return True
        except Exception as e:
            log("补回车失败：", repr(e))
            return False

    def _keyboard_send(文字, chat):
        """完全不依赖 UIA 的发送通道：Ctrl+F 搜会话 → 粘贴 → 回车 → 查库确认。

        UIA 假死时的第二条命。代价是要抢一下前台（键盘输入必须有焦点），
        所以只在 UIA 真的发不出去时用。
        """
        if not 文字:
            return False
        try:
            import win32con
            import win32gui

            h = _微信窗口()
            if not h:
                return False
            win32gui.ShowWindow(h, 9)
            _强设前台(h)
            time.sleep(0.5)
            import win32api

            def _粘贴(s):
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, s)
                finally:
                    win32clipboard.CloseClipboard()
                time.sleep(0.1)
                win32api.keybd_event(0x11, 0, 0, 0)      # Ctrl
                win32api.keybd_event(0x56, 0, 0, 0)      # V
                win32api.keybd_event(0x56, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.4)

            # Ctrl+F 打开搜索 → 输会话名 → 回车进会话
            win32api.keybd_event(0x11, 0, 0, 0)
            win32api.keybd_event(0x46, 0, 0, 0)
            win32api.keybd_event(0x46, 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(0x11, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.6)
            _粘贴(chat)
            _press_enter(1)
            time.sleep(0.8)
            _粘贴(文字)
            _press_enter(2)
            _press_enter(2, ctrl=True)
            time.sleep(1.2)
            if _already_sent(文字, chat, within=40):
                return True
            _press_enter(2)
            time.sleep(1.2)
            return _already_sent(文字, chat, within=40)
        except Exception as e:
            log("键盘通道失败：", repr(e))
            return False

    def _ensure_wechat_ready(重试=2, 前台=False):
        """确认微信窗口能操作。

        默认只确认窗口在、不抢前台（文字用 UIA 直接写控件，不需要焦点）；
        需要粘贴图片、或文字发送失败重试时才传 前台=True 把窗口顶上来。
        """
        if not 前台:
            return _微信窗口() is not None
        for 第 in range(重试 + 1):
            try:
                import win32gui

                found = []

                def cb(hwnd, _):
                    try:
                        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "微信":
                            found.append(hwnd)
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(cb, None)
                if found:
                    h = found[0]
                    win32gui.ShowWindow(h, 9)          # SW_RESTORE
                    if not _强设前台(h):
                        # 退路：先最小化再恢复，有时也能把焦点拿回来
                        try:
                            win32gui.ShowWindow(h, 6)
                            time.sleep(0.12)
                            win32gui.ShowWindow(h, 9)
                            time.sleep(0.15)
                            win32gui.SetForegroundWindow(h)
                        except Exception:
                            pass
                    # 发送期间把微信置顶：DSH 窗口（我自己）一刷新就会盖上来，
                    # 这是「字卡在输入框」最主要的来源
                    try:
                        import win32con

                        win32gui.SetWindowPos(h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                        STATE["wx_hwnd"] = h
                    except Exception:
                        pass
                    time.sleep(0.4)
                    return True
            except Exception as e:
                log("微信窗口准备失败：", repr(e))
            time.sleep(0.4)
        return False

    # 模型偶尔会把「自我点评 / 草稿 / 最后重说一遍」写进正文 —— 2026-11-05 09:10 群里
    # 就漏过一串「— no, avoid the emoji bracket? ... Final: three lines, drop the last emoji.」
    # 发出去像精神分裂。这里在第一个强标记处截断，留下前面那版正经答案。
    _过程话标记 = [
        r"\bFinal\s*:", r"—\s*no,", r"\bno,\s*avoid\b", r"\bIt'?s fine as text\b",
        r"\bLet'?s keep\b", r"草稿\s*[:：]", r"重来一遍", r"重新写一遍", r"\[draft\]",
    ]

    def 去过程话(text):
        t = str(text or "")
        cut = None
        for pat in _过程话标记:
            m = re.search(pat, t, re.I)
            if m and (cut is None or m.start() < cut):
                cut = m.start()
        if cut is None:
            return t
        前 = t[:cut].rstrip(" \t\r\n-—,，。;；:：")
        return 前 if len(前) >= 2 else t

    def 拆条(text: str, 上限: int = 60):
        """把一段话拆成几条短消息：先按换行分段，长段再按句子拆。
        上限 60：26 的时候一句正经回答会被切成五六个气泡，群里像刷屏。"""
        out = []
        for 段 in re.split(r"\n+", str(text or "")):
            段 = 段.strip()
            if not 段:
                continue
            if len(段) <= 上限:
                out.append(段)
                continue
            句们 = re.split(r"(?<=[。！？!?…])", 段)
            cur = ""
            for 句 in 句们:
                if not 句:
                    continue
                if len(cur) + len(句) <= 上限:
                    cur += 句
                else:
                    if cur.strip():
                        out.append(cur.strip())
                    cur = 句
            if cur.strip():
                out.append(cur.strip())
        return out or [str(text or "").strip()]


    def _chat_username(chat):
        """聊天名 → 数据库里的 username。群必须走 group_name_to_id（nickname 查不到群）。"""
        try:
            g = db.group_name_to_id(chat)
            if g:
                return g
        except Exception:
            pass
        try:
            return db.username_by_nickname(chat) or chat
        except Exception:
            return chat


    def _already_sent(text, chat, within=60):
        """库里最近已经有这句话 → 其实发出去了，别再点一次（点了就是重复消息）。"""
        try:
            u = _chat_username(chat)
            rows = db.get_messages(u, limit=12)
            now = time.time()
            for m in rows or []:
                ts = float(m.get("create_time") or 0)
                if not ts or abs(now - ts) > within:
                    continue
                c = clean_text(m.get("content") or "")
                if c and (c == text or text in c or c in text):
                    return True
        except Exception:
            pass
        return False

    # 进程内「这句我已经发出去过」的记录。数据库查询有 25/120 秒的时间窗，
    # 兜底通道晚几秒醒来、或者拆条后隔了几十秒再补发，窗口一过就会重复发一遍
    # （就是「老是发两次一模一样」）。这里用不过期的内存记录兜住。
    _发过的段 = {}

    def _记发过(text):
        try:
            k = clean_text(text)
            if k:
                _发过的段[k] = time.time()
        except Exception:
            pass

    def _段已发过(text, within=3600):
        try:
            k = clean_text(text)
            t = _发过的段.get(k)
            return bool(t) and (time.time() - t) <= within
        except Exception:
            return False


    def _localize_image(p):
        """正文 markdown 里的本机 HTTP 图（表情包服务等）先落成本地文件，再交给微信发。"""
        if not re.match(r"^https?://", p, re.I):
            return p if os.path.exists(p) else None
        from urllib.parse import urlparse

        u = urlparse(p)
        if (u.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            log("⚠ 不是本机的图，跳过：", p)
            return None
        ext = os.path.splitext(u.path)[1].lower()
        if ext not in (".gif", ".png", ".jpg", ".jpeg", ".webp"):
            ext = ".png"
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=ext, prefix="wxbridge_")
            os.close(fd)
            with urllib.request.urlopen(p, timeout=15) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            return tmp
        except Exception as e:
            log("⚠ 图下载失败：", p, repr(e))
            try:
                if tmp:
                    os.unlink(tmp)
            except Exception:
                pass
            return None

    def _send_image_dib(gui, path, chat):
        """发图：把图转成 CF_DIB 位图放剪贴板 → Ctrl+V → 回车。

        微信 4.x 不吃 wechatauto 的 CF_HDROP 路线（粘贴后草稿是空的，
        回车等于发了个寂寞，数据库里也不会有图片消息），但认 CF_DIB。
        GIF 只发第一帧（微信粘贴位图本来也不会动）。
        """
        import io
        import ctypes
        from PIL import Image

        try:
            im = Image.open(path)
            if getattr(im, "is_animated", False):
                im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
        except Exception as e:
            log("⚠ 图片打不开：", repr(e))
            return False
        buf = io.BytesIO()
        try:
            im.save(buf, format="BMP")
            dib = buf.getvalue()[14:]   # 去掉 BMP 文件头，剩下的就是 DIB
        except Exception as e:
            log("⚠ 图片转位图失败：", repr(e))
            return False

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.restype = ctypes.c_void_p
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        u32.OpenClipboard.argtypes = [ctypes.c_void_p]
        try:
            if not u32.OpenClipboard(None):
                log("⚠ 剪贴板打不开")
                return False
            try:
                u32.EmptyClipboard()
                h = k32.GlobalAlloc(0x0002 | 0x0040, len(dib))
                ptr = k32.GlobalLock(h)
                ctypes.memmove(ptr, dib, len(dib))
                k32.GlobalUnlock(h)
                if not u32.SetClipboardData(8, h):
                    log("⚠ 写剪贴板失败")
                    return False
            finally:
                u32.CloseClipboard()
        except Exception as e:
            log("⚠ 剪贴板异常：", repr(e))
            return False

        box = None
        for 第 in range(3):
            if 第 > 0:
                # 外层刚发完文字、窗口多半是好的；只有真探测不到时才重新摆窗口
                try:
                    _ensure_wechat_ready(前台=True)
                except Exception:
                    pass
            try:
                box = gui.get_input_box()
            except Exception as e:
                log("⚠ 探测输入框异常：", repr(e))
                box = None
            if box:
                break
            log("⚠ 发图探测不到输入框，第 %d 次重试" % (第 + 1))
            time.sleep(0.5)
        if not box:
            log("⚠ 发图时找不到输入框")
            return False
        try:
            gui.focus_input(box)
        except Exception as e:
            log("⚠ 聚焦输入框失败：", repr(e))
            return False
        time.sleep(0.2)
        gui._input.key(0x56, ctrl=True)   # VK_V
        time.sleep(0.8)
        gui._input.key(0x0D)              # VK_RETURN
        time.sleep(0.7)
        return True

    def _last_image_ts(chat):
        """聊天里最新一张图片消息的时间戳；没有就是 0。用来判断“刚才那张到底发出去没有”。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=8) or []:
                if m.get("type") == "图片":
                    return float(m.get("create_time") or 0)
        except Exception:
            pass
        return 0.0

    def _image_sent_recently(chat, since):
        """发完去库里确认一下：真出现图片消息才算发成了。"""
        try:
            u = _chat_username(chat)
            for m in db.get_messages(u, limit=4) or []:
                if m.get("type") == "图片" and float(m.get("create_time") or 0) >= since - 2:
                    return True
        except Exception:
            pass
        return False

    def _框里的字():
        """用 UIA 读输入框里的文字。读不到返回 None（表示"不知道"，不是"空"）。

        像素判据会把光标、占位提示、发送按钮都算成"有字"，据此重试会一直空转，
        所以优先问 UIA 要真实文本，读不到才退回像素判断。
        """
        try:
            gui = get_gui()
            uia = _取引擎()
            if uia is None:
                return None
            ctrl = uia._chat_input()
            if ctrl is None:
                return None
            r = uia._read_edit_value(ctrl) if hasattr(uia, "_read_edit_value") else None
            return None if r is None else str(r)
        except Exception:
            return None

    def _清空输入框():
        """发送前把输入框里可能残留的上一条清掉。

        发失败时文字会留在输入框（wechatauto 只校验粘贴、不校验回车），
        下一次任意回车会把这段残渣一起发出去 —— 表现就是同一条发了两遍。
        """
        try:
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            gui = get_gui()
            uia = _取引擎()
            if uia is None:
                return False
            ctrl = uia._chat_input()
            if ctrl is None:
                return False
            try:
                vp = ctrl.GetValuePattern()
                if vp is not None and str(vp.Value or ""):
                    vp.SetValue("")
                    log("发送前清掉了输入框里的残留")
                    return True
            except Exception:
                pass
            try:
                ctrl.SetFocus()
                time.sleep(0.05)
                k32.keybd_event(0x11, 0, 0, 0)   # Ctrl down
                k32.keybd_event(0x41, 0, 0, 0)   # A down
                k32.keybd_event(0x41, 0, 2, 0)
                k32.keybd_event(0x11, 0, 2, 0)   # Ctrl up
                k32.keybd_event(0x08, 0, 0, 0)   # Backspace
                k32.keybd_event(0x08, 0, 2, 0)
                log("发送前用键盘清空了输入框残留")
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _kick_enter(最多=3):
        """把卡在输入框里的话踹出去，返回 True 表示框已空。

        根因：wechatauto 的 UIA 发送只校验「文字有没有粘进输入框」，不校验回车
        之后输入框有没有清空。窗口被抢焦点时回车落空，字就留在输入框里，
        而接口已经报了「已操作发送」—— 表现就是「消息卡在输入框没发出去」。

        以前只补一次回车，落空一次就又堵在那里；现在按「框里还有字就继续踹」
        来判：回车三次还清不掉，就回退到 guia 的 click_send（它会 OCR 找到
        「发送」按钮去点，并且每次点完都校验输入框清空）。
        """
        try:
            gui = get_gui()
            for _ in range(最多):
                框 = _框里的字()
                if 框 is not None:
                    if not 框.strip():
                        return True
                else:
                    try:
                        if not gui._input_box_has_text():
                            return True
                    except Exception:
                        break
                try:
                    gui._input.key(0x0D)          # VK_RETURN
                except Exception:
                    uia = _取引擎()
                    if uia is None:
                        break
                    e = uia._chat_input()
                    if e is None:
                        break
                    e.SendKeys("{Enter}", waitTime=0.05)
                time.sleep(1.0)
            框 = _框里的字()
            if 框 is not None and not 框.strip():
                return True
            if 框 is None:
                try:
                    if not gui._input_box_has_text():
                        return True
                except Exception:
                    pass
            # 不再点「发送」按钮：那条路要真实移动鼠标（OCR 定位 + 点击），
            # 发消息时鼠标会乱飞，很吓人。改成纯键盘再踹一轮。
            log("输入框还堵着，再踹一轮回车（不碰鼠标）")
            try:
                # 优先用 UIA 元素定向发回车（后台也能用，不挑前台）；
                # 全局回车只在前台窗口是微信时才按，否则会按到主人正在用的窗口上
                uia = _取引擎()
                输入框 = uia._chat_input() if uia is not None else None
                if 输入框 is not None:
                    输入框.SendKeys("{Enter}", waitTime=0.05)
                elif _微信在前台():
                    _press_enter(2)
                    _press_enter(1, ctrl=True)
                else:
                    log("微信不在前台，跳过全局回车（免得按到别的窗口）")
            except Exception as e:
                log("踹回车失败：%r" % (e,))
            time.sleep(1.2)
            框 = _框里的字()
            if 框 is None or not 框.strip():
                return True
            log("输入框还是堵着，这条先放弃")
            return False
        except Exception as e:
            log("补回车失败：%r" % (e,))
            return False

    def _后台发送一段(text, chat):
        """零焦点发送（照搬 whale_girl 的 bg_sender 思路）：
        用 UIA 的 ValuePattern 直接写进输入框，再 Invoke「发送」按钮 ——
        全程不调用 SetForegroundWindow / SetFocus / SendInput，所以不抢前台。

        只在目标会话已经开着的时候用；切会话那套还得走前台通道。
        返回 True 表示「库里确认发出去了」。
        """
        try:
            uia = _取引擎()
            gui = get_gui()
            if uia is None or gui is None:
                log("后台通道：没有引擎/gui")
                return False
            当前 = ""
            try:
                当前 = str(uia.current_chat() or "")
            except Exception:
                当前 = ""
            if 当前 != str(chat):
                log("后台通道：当前会话是「%s」，不是「%s」，退回前台" % (当前, chat))
                return False
            ctrl = uia._chat_input()
            if ctrl is None:
                log("后台通道：拿不到输入框控件（UIA 树没物化），退回前台")
                return False
            vp = ctrl.GetValuePattern()
            if vp is None:
                log("后台通道：输入框不支持 ValuePattern，退回前台")
                return False
            try:
                vp.SetValue("")
            except Exception:
                pass
            vp.SetValue(text)
            time.sleep(0.9)
            try:
                回读 = str(vp.Value or "")
            except Exception:
                回读 = "（读不回来）"
            log("后台通道：写进去后输入框是「%s」" % 回读[:30])

            # 先试「给主窗口 PostMessage 一个回车」—— Qt 会把 WM_KEYDOWN 转成给焦点控件的按键，
            # 全程不动前台。不行再退到点「发送」按钮。
            try:
                import win32con
                import win32gui

                句柄 = int(getattr(gui, "hwnd", 0) or 0)
                if 句柄:
                    win32gui.PostMessage(句柄, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
                    time.sleep(0.06)
                    win32gui.PostMessage(句柄, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
                    for _ in range(10):
                        if _already_sent(text, chat, within=12):
                            log("后台通道发出去（回车投递，没抢前台）：%s" % text[:30])
                            return True
                        time.sleep(0.4)
            except Exception as e:
                log("后台通道：投递回车失败 %r" % (e,))

            # 「发送」按钮不在输入框旁边，得从整棵窗口树里找。
            # 实测：ButtonControl / mmui::XOutlineButton / Name == "发送"
            # （注意别误伤「发送文件」「发送收藏」「发送表情」那几个）。
            根 = ctrl
            for _ in range(12):
                父 = 根.GetParentControl()
                if 父 is None:
                    break
                根 = 父

            def _找发送(节点, 深=0):
                if 深 > 20:
                    return None
                try:
                    for ch in 节点.GetChildren():
                        try:
                            if (ch.ControlTypeName == "ButtonControl"
                                    and str(ch.Name or "").strip() == "发送"):
                                return ch
                        except Exception:
                            pass
                        得 = _找发送(ch, 深 + 1)
                        if 得 is not None:
                            return 得
                except Exception:
                    pass
                return None

            按钮 = _找发送(根)
            if 按钮 is None:
                log("后台通道：没找到「发送」按钮（UIA 树可能没物化），退回前台")
                return False
            按钮.GetInvokePattern().Invoke()
            for _ in range(15):
                if _already_sent(text, chat, within=15):
                    log("后台通道发出去（没抢前台）：%s" % text[:30])
                    return True
                time.sleep(0.4)
            log("后台通道：点了发送但库里没出现，退回前台")
            return False
        except Exception as e:
            log("后台通道出错（退回前台通道）：%r" % (e,))
            return False

    def send_one(item):
        text = str(item.get("text") or "").strip()
        chat = str(item.get("chat") or "").strip()
        images = [str(p) for p in (item.get("images") or []) if p]

        # 正文里的 [表情: 描述] 换成真图片（表情库里有就发图，没有就保持原样）
        表情图 = []

        def _repl(m):
            p = find_meme(m.group(1))
            if p:
                表情图.append(p)
                return ""
            # 库里没有这张：直接把标记删掉，别让「[表情: xxx]」这串字发到微信里
            log("表情库没匹配到：%s" % m.group(1))
            return ""

        text = re.sub(r"\[表情:\s*([^\]]+)\]", _repl, text).strip()
        images = 表情图 + images

        # 兜底：DSH 那边没摘干净的 markdown 图片（只认本机地址），这里再摘一次，
        # 免得微信里收到一串 "![xxx](http://127.0.0.1:...)" 的文字。
        def _摘图(m):
            u = m.group(1)
            if re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", u, re.I):
                images.append(u)
                return ""
            return m.group(0)

        text = re.sub(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", _摘图, text).strip()

        if not text and not images:
            return {"ok": False, "说": "空消息"}
        if not chat:
            return {"ok": False, "说": "没指定发给谁（chat 空着）"}
        with send_lock:
            # UIA 坏着的时候整条发送路都不许走：下面 _ensure_wechat_ready / open_chat /
            # 读输入框都会进 wechatauto 的窗口操作，它一失败就退化成坐标盲点、
            # 顺手最小化挡着的窗口 —— 队列里积着几条消息就是每秒两三次的空转。
            if _uia熔断中():
                return {"ok": False, "说": "UIA 起不来，先不发（5 分钟内不再试，免得它乱点桌面）"}
            窗口快照 = _快照窗口()
            守护 = _守护最小化(窗口快照.get("hwnd")) if 窗口快照.get("iconic") else None
            try:
                # 先不抢前台：同一条会话里连发时，UIA 直接写控件就够了，
                # 把窗口顶上来只会让主人看见它闪。只有要切会话时才摆正窗口。
                _ensure_wechat_ready(前台=False)
                gui = get_gui()
                # 每次发送前重取一次引擎：微信跑久了 accessibility 会假死，
                # 缓存的引擎能收消息却一条也发不出去
                _引擎 = _取引擎()
                # 发之前先清干净输入框：上一次失败的字留在里面，
                # 这一次回车会把它一起带出去（同一句话发两遍的根源）
                try:
                    _清空输入框()
                except Exception:
                    pass
                # 已经在目标会话里就别再搜一次 —— open_chat 要点搜索框、输入名字，
                # 这些动作会把最小化的微信拉到前面来（主人就看见窗口突然蹦出来）
                当前会话 = None
                try:
                    if _引擎 is not None:
                        当前会话 = _引擎.current_chat()
                except Exception:
                    当前会话 = None
                if 当前会话 != chat:
                    # 切会话这一下躲不掉：得让窗口在前，open_chat 才点得到搜索框
                    _ensure_wechat_ready(前台=True)
                    gui = get_gui()
                    _引擎 = _取引擎()
                    try:
                        gui.open_chat(chat)
                    except Exception as e:
                        log("打开会话失败（继续试）：", repr(e))
                    # 切会话这一下微信会把自己拉起来（Invoke 会话项时应用自己的行为），
                    # 这时候如果立刻按回最小化，新会话的输入框就丢焦点了，字会发不出去。
                    # 所以这里让它先露着，等这条发完在 finally 里统一收回去。
                    # 窗口最小化时输入框可能丢焦点，这里顺手把焦点还给控件。
                    try:
                        if _引擎 is not None:
                            e = _引擎._chat_input()
                            if e is not None:
                                e.SetFocus()
                    except Exception as e:
                        log("还焦点给输入框失败：", repr(e))
                发了 = False

                def 带超时(动作, 超时=25):
                    """send_msg 偶尔会卡在 UIA 里不出来，给个上限，别把整条队列堵死。"""
                    盒 = {}

                    def 跑():
                        try:
                            盒["r"] = 动作()
                        except Exception as e:
                            盒["e"] = e

                    t = threading.Thread(target=跑, daemon=True)
                    t.start()
                    t.join(超时)
                    if t.is_alive():
                        return "发送超时"
                    return 盒.get("e") or 盒.get("r")

                def 试(动作, 名字, 重试前=1):
                    """发一次；返回失败（不是抛异常）也要当成失败 —— 窗口没接住时就是这样。"""
                    for 第 in range(重试前 + 1):
                        try:
                            r = 带超时(lambda: 动作(gui))
                        except Exception as e:
                            r = e
                        if not isinstance(r, Exception) and (r is None or r):
                            return True
                        原因 = ""
                        try:
                            原因 = str(r.get("message") or "") if hasattr(r, "get") else str(r)
                        except Exception:
                            原因 = str(r)
                        # 已经发出去了、只是数据库还没落库 —— 再发一遍就成了两条重复消息
                        if "未确认" in 原因 or "已操作发送" in 原因:
                            # 但字也可能压根没发出去，就卡在输入框里 —— 补一次回车再确认
                            if _kick_enter():
                                time.sleep(1.2)
                                log("已操作发送但库里没确认，补了一次回车：" + 原因[:50])
                            else:
                                log("发出去但库里没确认，不再重发：" + 原因[:50])
                            return True
                        log("第 %d 次%s没成：%s" % (第 + 1, 名字, 原因[:80]))
                        if 第 < 重试前:
                            # 先原地重试；还是不行就重新热激活 UIA（引擎假死时只能这样救），
                            # 最后才把微信拉到前台 —— 平时不抢主人正在用的窗口
                            if 第 == 0:
                                _recover_uia("发送失败")
                            else:
                                _ensure_wechat_ready(前台=True)
                            time.sleep(0.6)
                    return False

                if text:
                    # 它自己决定用语音说的部分：[语音]…[/语音] 里的话转成真语音条发出去
                    语音段们 = re.findall(r"\[语音\](.+?)\[/语音\]", text, re.S)
                    if not 语音段们 and text.strip().startswith("[语音]"):
                        语音段们 = [text.strip()[4:]]
                    if 语音段们:
                        text = re.sub(r"\[语音\].*?\[/语音\]", "", text, flags=re.S).strip()
                        if text.strip().startswith("[语音]"):
                            text = ""
                        for 段 in 语音段们[:2]:
                            段 = clean_text(re.sub(r"\[/?语音\]", "", 段))[:120]
                            if not 段:
                                continue
                            try:
                                好, 说明 = 发语音(chat, 段)
                                log("语音段（%s）：%s → %s" % (chat, 段[:20], 说明))
                            except Exception as e:
                                log("语音段发送失败：%r" % (e,))
                                continue
                            time.sleep(1.5)
                    # 有人点歌、它又没主动哼 —— 桥接兜底补一条哼唱语音，别让「不会唱」把话堵死。
                    # 注意别再重复：发语音自己会记 _语音时刻，20 秒内这个聊天已经有语音就不补。
                    if not 语音段们:
                        请 = _唱歌请求.get(chat)
                        if (请 and time.time() - 请 < 180
                                and time.time() - _语音时刻.get(chat, 0) > 20):
                            _唱歌请求.pop(chat, None)
                            try:
                                发语音(chat, random.choice(_哼唱词))
                                log("兜底哼唱：%s" % chat)
                                time.sleep(1.5)
                            except Exception as e:
                                log("兜底哼唱失败：%r" % (e,))
                    # 主人明确要语音、模型又没打 [语音]：直接把这段文字念出来（太长就照旧打字）
                    if not 语音段们:
                        求 = _语音请求.get(chat)
                        if 求 and time.time() - 求 < 120 and len(text.strip()) >= 4:
                            _语音请求.pop(chat, None)
                            try:
                                好, 说明 = 发语音(chat, text.strip())
                                log("语音请求兜底：%s → %s" % (text.strip()[:20], 说明))
                                if 好:
                                    time.sleep(1.5)
                                    text = ""
                            except Exception as e:
                                log("语音请求兜底失败：%r" % (e,))
                    # 先把「草稿 / 自我点评 / Final:」这类过程话切掉，再拆条
                    text = 去过程话(text)
                    # 长内容拆成几条短消息发（一个大气泡读着累）—— 每段不超过 60 字
                    for 序号, 一 in enumerate(拆条(text)):
                        # 多段之间也隔一会儿，别像连珠炮
                        if 序号:
                            time.sleep(random.uniform(1.5, 4.0))
                        # 重试前先看库里：这句话刚发过就不再点第二遍
                        if _段已发过(一):
                            log("这句我发过（内存记录），跳过：%s" % 一[:30])
                            continue
                        if _already_sent(一, chat, within=25):
                            log("这句刚发过，跳过：%s" % 一[:30])
                            _记发过(一)
                            recently_sent.append((clean_text(一), time.time()))
                            continue
                        # 发之前先"读+想"一会儿（按字数估），像人打字
                        time.sleep(人类延迟(一, 序号))
                        # 发之前就先记账：库里这条冒出来的速度快过 send_msg 返回，
                        # 晚一步记就会被监听线程当成「主人自己新说的」再转进 DSH —— 自问自答就是这么来的。
                        recently_sent.append((clean_text(一), time.time()))
                        STATE["last_send"] = time.time()
                        # 零焦点通道默认关：2026-11-15 实测「写值」能进去，但微信不认程序化的
                        # 回车/Invoke（按钮一直没被点亮），只会白等 6 秒再退回前台。
                        # 想继续调：设 DSH_WECHAT_BG_SEND=1 打开，看日志里的「后台通道：」。
                        ok = False
                        if 后台发送开:
                            ok = _后台发送一段(一, chat)
                        if not ok:
                            ok = 试(lambda g, t=一: g.send_msg(t, chat, True), "发文字")
                        if not ok and _already_sent(一, chat, within=25):
                            log("接口说失败，但库里查到了 —— 算发成功：%s" % 一[:30])
                            ok = True
                        if not ok:
                            # 最后再补一脚：字常常只是卡在输入框里没回车出去
                            if _kick_enter():
                                time.sleep(1.2)
                                if _already_sent(一, chat, within=25):
                                    log("补回车后库里查到了 —— 算发成功：%s" % 一[:30])
                                    ok = True
                        if not ok:
                            # UIA 彻底没发出去：先走键盘通道（Ctrl+F 搜会话→粘贴→回车→查库）
                            if _keyboard_send(一, chat):
                                log("键盘通道补发成功：%s" % 一[:30])
                                ok = True
                        if not ok:
                            # 最后再抢一次前台重试
                            try:
                                _ensure_wechat_ready(前台=True)
                                time.sleep(0.5)
                                gui.open_chat(chat)
                                if 试(lambda g, t=一: g.send_msg(t, chat, True), "发文字（前台重试）"):
                                    ok = True
                            except Exception as e:
                                log("前台重试异常：", repr(e))
                        if not ok:
                            gui_box["gui"] = None      # 下次重建
                            return {"ok": False, "说": "文字没发出去（微信窗口没接住）"}
                        # 发成功也要补回车：wechatauto 只认「粘贴成功」，
                        # 回车落空它照样报成功，字会留在框里。多按几次，
                        # 并兼容微信里「Ctrl+Enter 发送」的设置。
                        # 全局回车只在前台窗口是微信时才按，否则会按到主人正在用的窗口上
                        if _微信在前台():
                            _press_enter(2)
                            _press_enter(1, ctrl=True)
                        _kick_enter()
                        # 拆开发出去的每一条都记账 —— 不然文件传输助手里会被当成主人说的话读回来
                        recently_sent.append((clean_text(一), time.time()))
                        _记发过(一)
                        time.sleep(0.35)
                    recently_sent.append((clean_text(text), time.time()))
                    _记发过(text)
                    STATE["last_send"] = time.time()
                    # 鱼鱼自己说的也记进长期记忆，不然档案里只有别人说的话
                    try:
                        记忆_记(_chat_username(chat) or chat, chat, "self", "鱼鱼", text,
                               str(chat).endswith("@chatroom"))
                    except Exception:
                        pass
                    if len(recently_sent) > 100:
                        del recently_sent[:-100]
                    log("✅ 已发到微信（%s）：%s" % (chat, text[:40]))
                    发了 = True

                临时图 = []

                def 取图(p):
                    q = _localize_image(p)
                    if q and q != p:
                        临时图.append(q)
                    return q

                for p in images:
                    p = 取图(p)
                    if not p:
                        log("⚠ 图拿不到，跳过")
                        continue
                    # 发完文字窗口可能被挡/失焦 —— 发图前把会话和窗口都摆正。
                    # 但刚才要是已经发过文字，窗口和会话都还在这条上，再走全套恢复会白顿一秒多。
                    try:
                        if text:
                            time.sleep(0.25)
                        else:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.2)
                            gui.open_chat(chat)
                            time.sleep(0.4)
                    except Exception as e:
                        log("⚠ 发图前准备窗口失败（继续试）：", repr(e))
                    t0 = time.time()
                    前图时 = _last_image_ts(chat)
                    # 图片一贴进微信，库里立刻就有这一行；监听线程 0.4 秒一轮，
                    # 等我们确认完再记「刚发过」就晚了 —— 会在文件传输助手里读回来。
                    STATE["last_send"] = time.time()
                    try:
                        ok_img = _send_image_dib(gui, p, chat)
                    except Exception as e:
                        log("✗ 发图异常：", repr(e))
                        ok_img = False
                    # 接口返回不可靠（点击被挡掉时它说失败，其实剪贴板粘贴已经发出去了）——
                    # 一律以数据库里有没有新图片为准。前两秒快速查，别让确认本身拖慢下一条。
                    真发了 = False
                    for 第 in range(14):
                        if _last_image_ts(chat) > 前图时 + 0.5:
                            真发了 = True
                            break
                        time.sleep(0.35 if 第 < 6 else 1.5)
                    if 真发了:
                        if ok_img:
                            log("🖼 已发图（%s）：%s" % (chat, os.path.basename(p)))
                        else:
                            log("🖼 已发图（%s，接口说失败但库里查到了）：%s" % (chat, os.path.basename(p)))
                        发了 = True
                        # 发图也要记一笔"刚发过"：不然文件传输助手里的这张图会被当新消息读回来
                        STATE["last_send"] = time.time()
                        time.sleep(0.4)
                    else:
                        # 第一次多半是窗口被抢/被挡 —— 重建窗口再来一次，别直接把图丢掉
                        log("✗ 发图失败，重建窗口重试一次：%s" % p)
                        gui_box["gui"] = None
                        try:
                            _ensure_wechat_ready(前台=True)
                            time.sleep(0.5)
                            gui2 = get_gui()
                            gui2.open_chat(chat)
                            time.sleep(0.5)
                            前图时2 = _last_image_ts(chat)
                            STATE["last_send"] = time.time()
                            _send_image_dib(gui2, p, chat)
                            for 第 in range(12):
                                if _last_image_ts(chat) > 前图时2 + 0.5:
                                    真发了 = True
                                    break
                                time.sleep(1.0)
                        except Exception as e:
                            log("重试发图异常：", repr(e))
                        if 真发了:
                            log("🖼 重试后已发图（%s）：%s" % (chat, os.path.basename(p)))
                            发了 = True
                            STATE["last_send"] = time.time()
                            time.sleep(0.4)
                        else:
                            log("✗ 发图彻底失败（库里还是没有新图）：%s" % p)

                for t in 临时图:
                    try:
                        os.unlink(t)
                    except Exception:
                        pass

                if not 发了:
                    return {"ok": False, "说": "字和图都没发出去"}
                return {"ok": True}
            except Exception as e:
                log("✗ 发送失败（%s）：%r" % (chat, e))
                gui_box["gui"] = None    # 下次重建
                return {"ok": False, "说": repr(e)}
            finally:
                if 守护 is not None:
                    守护.set()
                # 发送期间把微信置顶了，发完要放下来，不然它一直压在别的窗口上面
                _取消置顶()
                # 后台发送的收尾：原来最小化的继续最小化，前台还给主人原来那个窗口
                _还原窗口(窗口快照)

    # ---------- 兜底：DSH 那边的会话事件没送到时，直接读会话文件把回复捞出来 ----------
    # 起因：2026-10-27 晚，DSH 重启后 host.js 的 session/event 不再触发，
    #       回复全部卡在 DSH 里发不出去。这里作为独立于事件的一层保险。

    LAST_CHAT = {"chat": os.environ.get('DSH_BRIDGE_DEFAULT_CHAT') or '', "ts": 0.0}
    FALLBACK_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fallback_state.json')
    # 会话目录按「最近改动过的那个」自动找，不写死具体会话名
    def _找会话根() -> str:
        base = os.path.join(os.path.expanduser('~'), '.dsh', 'sessions')
        try:
            dirs = [os.path.join(base, d) for d in os.listdir(base)]
            dirs = [d for d in dirs if os.path.isdir(d)]
            if dirs:
                return max(dirs, key=lambda p: os.path.getmtime(p))
        except Exception:
            pass
        return base

    SESS_ROOT = _找会话根()
    # 兜底通道：2026-10-28 起默认关闭。它读会话文件猜「这句回复该发给谁」，
    # 猜错就会把私聊内容发进群、或者把同一句重发两遍（当天出了两次事故）。
    # 想要它回来：设环境变量 DSH_BRIDGE_NO_FALLBACK=0。
    FALLBACK_OFF = os.environ.get('DSH_BRIDGE_NO_FALLBACK', '1') != '0'

    def _读兜底游标():
        try:
            with open(FALLBACK_STATE, 'r', encoding='utf-8') as f:
                return int((json.load(f) or {}).get('seq') or 0)
        except Exception:
            return 0

    def _写兜底游标(n):
        try:
            with open(FALLBACK_STATE, 'w', encoding='utf-8') as f:
                json.dump({'seq': int(n)}, f)
        except Exception:
            pass

    def _最新会话文件():
        best, best_t = None, 0.0
        try:
            for d in os.listdir(SESS_ROOT):
                p = os.path.join(SESS_ROOT, d, 'session.v4.jsonl.zstd')
                if os.path.exists(p):
                    t = os.path.getmtime(p)
                    if t >= best_t:
                        best_t, best = t, p
        except Exception:
            pass
        return best

    _FB_CACHE = {'key': None, 'res': None}

    def 兜底捞回复():
        p = _最新会话文件()
        if not p:
            return None
        try:
            mt = os.path.getmtime(p)
        except Exception:
            return None
        key = (p, mt, _读兜底游标())
        if _FB_CACHE['key'] == key:
            return _FB_CACHE['res']
        res = _解析兜底(p)
        _FB_CACHE['key'] = key
        _FB_CACHE['res'] = res
        return res

    def _解析兜底(p):
        """找最后一条「来自微信」的用户消息之后的那句回复。本地聊的不算。"""
        try:
            import zstandard
        except Exception:
            return None
        try:
            with open(p, 'rb') as f:
                raw = zstandard.ZstdDecompressor().stream_reader(f).read()
        except Exception:
            return None
        last_user = None
        last_asst = None
        for line in raw.decode('utf-8', 'replace').splitlines():
            if len(line) < 24 or '"type"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get('type')
            if t == 'user/message':
                src = (o.get('data') or {}).get('source') or {}
                k = src.get('kind')
                if k == 'wechat':
                    last_user = o
                    last_asst = None          # 只看这条微信消息之后的回复
                elif k == 'user':
                    last_user = None          # 有人在本机聊天，那一轮的风头过了
                    last_asst = None
            elif t == 'assistant/message' and last_user is not None:
                d = o.get('data') or {}
                body = (d.get('message') or {}).get('content') or []
                txt = ''.join(x.get('text') or '' for x in body if x.get('type') == 'text').strip()
                if txt:
                    last_asst = (o.get('seq') or 0, txt)
        if not last_user or not last_asst:
            return None
        seq_u = last_user.get('seq') or 0
        seq_a, txt_a = last_asst
        if seq_a <= seq_u or seq_a <= _读兜底游标():
            return None
        return {'seq': seq_a, 'text': txt_a}

    def outbox_loop():
        log("出站轮询已启动（每 %.0f 秒）" % POLL_SEC)

        # 预热：第一次 get_gui()/取 UIA 引擎会把微信窗口短暂恢复一下（要建控件树），
        # 放在这里提前建好，免得真发消息时才闪那一下。
        def _预热():
            try:
                with send_lock:
                    _取引擎()
                log("UIA 引擎预热完成")
            except Exception as e:
                log("UIA 预热失败：", repr(e))

        threading.Thread(target=_预热, daemon=True).start()
        # 连续失败保护：2026-11-05 09:53-12:28 号被限制发消息，桥接每 70 秒硬试一次、
        # 连试了两个半小时。以后连续 3 次发不出去就自己停 30 分钟，别拿账号硬顶。
        连败 = {"n": 0, "停到": 0.0}
        while True:
            time.sleep(POLL_SEC)
            if not SEND_ENABLED:
                continue
            if time.time() < 连败["停到"]:
                continue
            try:
                r = http_json("/dsh-wechat-bridge/outbox")
                if not r.get("ok"):
                    continue
                这一批发过了 = False
                for item in r.get("消息") or []:
                    # ⭐ 发送放进子线程带超时：微信窗口被最小化/被抢焦点时，UIA 有可能一直等下去，
                    #    以前就这么把整条出站循环堵死了 —— 后面的消息全卡在这儿不发。
                    结果盒 = {}

                    def 干(条=item):
                        try:
                            结果盒["r"] = send_one(条)
                        except Exception as e:  # noqa: BLE001
                            结果盒["e"] = e

                    线 = threading.Thread(target=干, daemon=True)
                    线.start()
                    线.join(timeout=70)
                    if 线.is_alive():
                        log("⚠ 发送超时（微信窗口多半卡住了），这条先跳过，重建窗口")
                        gui_box["gui"] = None
                        res = {"ok": False, "说": "发送超时（窗口卡住）"}
                    elif "r" in 结果盒:
                        res = 结果盒["r"]
                    else:
                        res = {"ok": False, "说": str(结果盒.get("e"))}
                    http_json("/dsh-wechat-bridge/outbox/ack", {
                        "ok": bool(res.get("ok")),
                        "chat": item.get("chat"),
                        "说": res.get("说", ""),
                    })
                    if res.get("ok"):
                        这一批发过了 = True
                        连败["n"] = 0
                    else:
                        连败["n"] += 1
                        if 连败["n"] >= 3 and time.time() >= 连败["停到"]:
                            连败["停到"] = time.time() + 1800
                            log("⚠ 连续 %d 次发不出去，停发 30 分钟。多半是账号被限制了"
                                "（发消息失败 / 提示账号存在异常），去手机上看一眼，别硬发。" % 连败["n"])
                # 主通道真发出去的那条，把兜底游标一起推过去 —— 否则库里的时间窗一过，
                # 兜底会把同一句再捞出来重复发一遍（就是「发图就重复」的老毛病）
                if 这一批发过了 and (r.get("消息") or []):
                    try:
                        捞2 = 兜底捞回复()
                        if 捞2:
                            待比2 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞2["text"]).strip()
                            if 待比2 and _already_sent(待比2, LAST_CHAT.get("chat") or "", within=180):
                                _写兜底游标(捞2["seq"])
                                log("兜底游标跟主通道同步：seq=%s" % 捞2["seq"])
                    except Exception:
                        pass
                if not FALLBACK_OFF and not r.get("消息"):
                    try:
                        捞 = 兜底捞回复()
                    except Exception:
                        捞 = None
                    if 捞:
                        聊天 = LAST_CHAT.get("chat") or ""
                        # ⭐ 串台防护：只有「刚刚」来过微信消息，兜底才认这句回复是给它的。
                        # 之前是永远拿 LAST_CHAT，结果主人在 Web 里跟我聊、我回的私聊内容
                        # 被兜底当成「对群里那条老消息的回复」，整段发进了群（2026-10-28 16:19）。
                        过期 = (time.time() - float(LAST_CHAT.get("ts") or 0)) > 90
                        if 聊天 and 过期:
                            log("兜底：最后一条微信消息在 90 秒前，这句回复不归它，跳过（防串台）")
                            聊天 = ""
                        if 聊天:
                            log("兜底：会话事件没送到，直接捞到一句回复（seq=%s）" % 捞["seq"])
                            # 兜底只补「主通道真没送到」的：已经发过的绝不能再发一遍（会重复两遍文字+图）
                            待比 = re.sub(r"\[表情:\s*[^\]]+\]", "", 捞["text"]).strip()
                            已发过 = False
                            try:
                                if 待比:
                                    已发过 = _already_sent(待比, 聊天, within=120)
                                else:
                                    已发过 = _image_sent_recently(聊天, time.time() - 120)
                            except Exception:
                                已发过 = False
                            if 已发过:
                                log("兜底：这句微信里已经发过了，不重复发，只推进游标")
                                _写兜底游标(捞["seq"])
                            else:
                                兜底结果 = {"ok": False, "说": "未发"}
                                try:
                                    兜底结果 = send_one({"chat": 聊天, "text": 捞["text"], "images": []})
                                except Exception as e:  # noqa: BLE001
                                    log("兜底发送失败：%r" % (e,))
                                if 兜底结果.get("ok"):
                                    _写兜底游标(捞["seq"])
                                    time.sleep(0.5)
                # 收窗口交给 send_one 的 _还原窗口：原来最小化的继续最小化，
                # 主人本来就开着微信的就不再动它 —— 不再一刀切按最小化
            except Exception:
                log("出站轮询出错：", traceback.format_exc(limit=2))

    # ---------- 收：监听数据库新消息 ----------

    START_TS = time.time()
    seen = set()

    # ---------- 收：图片 / 表情包落盘 ----------

    媒体 = {"下载器": None, "导入失败": False}

    def _图片后缀(data):
        if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
            return "gif"
        if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        return ""

    def _保存表情包(raw):
        """表情包 = 一条 <emoji cdnurl="...">。cdnurl 是明文，直接下，不用解密。"""
        try:
            text = html.unescape(raw or "")
            # 微信 4.x 的 XML 属性会带空格：cdnurl = "..." / md5 = "..."
            url = ""
            for attr in ("cdnurl", "thumburl"):
                m = re.search(r'%s\s*=\s*["\']([^"\']+)["\']' % attr, text)
                if m and m.group(1).strip().startswith(("http://", "https://")):
                    url = m.group(1).strip()
                    break
            if not url:
                return None
            md5 = re.search(r'md5\s*=\s*["\']([^"\']+)["\']', text)
            key = (md5.group(1) if md5 else hashlib.md5(url.encode("utf-8")).hexdigest()).lower()
            folder = os.path.join(INBOUND_DIR, "stickers")
            os.makedirs(folder, exist_ok=True)
            for ext in ("gif", "png", "jpg", "webp"):
                p = os.path.join(folder, key + "." + ext)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    return p
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(8 * 1024 * 1024 + 1)
            if not data or len(data) > 8 * 1024 * 1024:
                return None
            ext = _图片后缀(data)
            if not ext:
                return None
            p = os.path.join(folder, key + "." + ext)
            with open(p, "wb") as f:
                f.write(data)
            log("表情包落盘：%s（%d KB）" % (p, len(data) // 1024))
            return p
        except Exception as e:
            log("表情包落盘失败：", repr(e))
            return None

    def _保存图片(msg, username):
        """普通图片：本地 .dat 解密。wechatauto 的 MediaDownloader 会处理 wxgf。
        2026-11-06：群里发完图 0.4 秒我们就去取，微信那边文件常常还没落盘 ——
        加三次重试 + thumb 兜底，别一上来就说「只收到个壳子」。"""
        if 媒体["导入失败"]:
            return None
        try:
            if 媒体["下载器"] is None:
                from wechatauto.media import MediaDownloader

                媒体["下载器"] = MediaDownloader(db)
            folder = os.path.join(INBOUND_DIR, "images")
            os.makedirs(folder, exist_ok=True)
            local_id = int(msg.get("local_id") or 0)
            if not local_id:
                return None

            def _转wxgf(p):
                """best 档给的是 wxgf（HEVC 容器），得先转成 jpg 视觉模型才认。"""
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    jpg = 媒体["下载器"]._wxgf_to_jpg(data)
                    if jpg:
                        out = os.path.splitext(p)[0] + ".jpg"
                        with open(out, "wb") as f:
                            f.write(jpg)
                        return out
                except Exception as e:
                    log("wxgf 转码失败：", repr(e))
                return ""

            def _拿(tier):
                try:
                    p = 媒体["下载器"].download_image(
                        username, local_id, save_dir=folder, tier=tier, shard=msg.get("shard")
                    )
                except Exception as e:
                    log("图片落盘失败（tier=%s）：%r" % (tier, e))
                    return None
                if not (p and os.path.isfile(p) and os.path.getsize(p) > 0):
                    return None
                if str(p).lower().endswith(".wxgf"):
                    q = _转wxgf(p)
                    return q or None
                return p

            # 只追「best」：thumb 是缩略图，宽高可能只剩四分之一，字全糊了 ——
            # 2026-11-23 主人说「图片看不全」就是这个原因（490×66 的图只拿到 118×66）。
            for 轮 in range(6):
                p = _拿("best")
                if p:
                    log("图片落盘（原图）：%s" % p)
                    return p
                if 轮 < 5:
                    time.sleep(1.5)
            p = _拿("thumb")
            if p:
                log("图片落盘（只有缩略图，细节会丢）：%s" % p)
                return p
            log("图片落盘：原图和缩略图都没取到（local_id=%s）" % local_id)
            return None
        except Exception as e:
            log("图片落盘失败：", repr(e))
            return None

    # ---------- 看图：老宿主自己看，新宿主交给它看 ----------

    def _视觉配置():
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        配置 = {}
        try:
            with open(os.path.join(home, "wechat-bridge-config.json"), encoding="utf-8-sig") as f:
                配置 = json.load(f) or {}
        except Exception:
            pass
        if 配置.get("看图") is False:
            return None
        主 = str(配置.get("视觉密钥") or "").strip()
        备 = str(配置.get("视觉备用密钥") or "").strip()
        if not 主 or not 备:
            try:
                # 可选的第三方设置文件（比如 whale-girl 的 settings.json），用环境变量指过来
                第三方设置 = os.environ.get("DSH_BRIDGE_VISION_SETTINGS") or os.path.join(home, "wechat-vision-settings.json")
                with open(第三方设置, encoding="utf-8-sig") as f:
                    鲸 = json.load(f) or {}
                if not 主:
                    主 = str(鲸.get("apiKey") or "").strip()
                if not 备:
                    备 = str(鲸.get("visionApiKey") or "").strip()
            except Exception:
                pass
        if not 主:
            主 = str(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("VISION_API_KEY") or "").strip()
        if not 备:
            备 = str(os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY") or "").strip()
        if not 主 and not 备:
            return None
        return {
            "主": 主,
            "备": 备,
            "模型": str(配置.get("视觉模型") or "deepseek-flash").strip() or "deepseek-flash",
            "地址": str(配置.get("视觉地址") or "https://api.deepseek.com").rstrip("/"),
            "备用模型": str(配置.get("视觉备用模型") or "glm-4v-flash").strip() or "glm-4v-flash",
            "备用地址": str(
                配置.get("视觉备用地址") or "https://open.bigmodel.cn/api/paas/v4"
            ).rstrip("/"),
        }

    def _读视觉缓存():
        try:
            with open(VISION_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _写视觉缓存(表):
        try:
            items = sorted(表.items(), key=lambda kv: float((kv[1] or {}).get("时间") or 0), reverse=True)
            os.makedirs(os.path.dirname(VISION_CACHE), exist_ok=True)
            with open(VISION_CACHE, "w", encoding="utf-8") as f:
                json.dump(dict(items[:500]), f, ensure_ascii=False)
        except Exception:
            pass

    def _视觉请求(地址, key, 模型, data_url, 提示):
        url = 地址.rstrip("/") + "/chat/completions"
        体 = {
            "model": 模型,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": 提示},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
            "max_tokens": 700,
        }
        if "deepseek" in 地址:
            体["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            url,
            data=json.dumps(体, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError("视觉接口 HTTP %s：%s" % (e.code, detail))
        msg = ((payload.get("choices") or [{}])[0].get("message") or {})
        text = str(msg.get("content") or "").strip()
        if not text:
            raise RuntimeError("视觉接口没返回文字")
        return re.sub(r"\s+", " ", text)[:180]

    def _看图(path, 类型):
        """返回一句中文描述；没配钥匙 / 看失败就返回空字符串。"""
        设置 = _视觉配置()
        if not 设置:
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read(20 * 1024 * 1024 + 1)
        except Exception:
            return ""
        if not data or len(data) > 20 * 1024 * 1024:
            return ""
        suffix = _图片后缀(data)
        mime = {"gif": "image/gif", "png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(suffix)
        if not mime:
            return ""
        digest = hashlib.sha256(data).hexdigest()
        缓存 = _读视觉缓存()
        if isinstance(缓存.get(digest), dict) and 缓存[digest].get("描述"):
            return str(缓存[digest]["描述"])
        提示 = (
            "这是微信聊天里对方发来的%s。"
            "用一两句中文说清画面内容，以及它表达的情绪、语气或梗；不要寒暄，不超过60字。" % 类型
        )
        data_url = "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
        描述 = ""
        for key, 模型, 地址 in (
            (设置["主"], 设置["模型"], 设置["地址"]),
            (设置["备"], 设置["备用模型"], 设置["备用地址"]),
        ):
            if not key:
                continue
            try:
                描述 = _视觉请求(地址, key, 模型, data_url, 提示)
                if 描述:
                    break
            except Exception as e:
                log("看图失败（%s）：%r" % (模型, e))
        if not 描述:
            return ""
        缓存[digest] = {"描述": 描述, "时间": time.time(), "类型": 类型}
        _写视觉缓存(缓存)
        return 描述

    def _看图并替换(content, path, 类型):
        描述 = _看图(path, 类型)
        if not 描述:
            return content
        标签 = "一个表情包" if 类型 == "表情包" else "一张图片"
        前缀 = "[对方发来%s：%s]" % (标签, 描述)
        new, n = re.subn(r"（发来(?:一)?(?:张|个)?(?:图片|表情包)）", lambda _m: 前缀, content, count=1)
        return new if n else (前缀 + "\n" + content)

    def _引用人话(t):
        """被引用的内容有时是原始 XML（引的是图片/视频消息）——换成一句人话，
        别让模型对着 "<?xml ...><img ...>" 讲源码（2026-11-06 群里出过这丑）。"""
        s = str(t or "").strip()
        if not s or "<" not in s:
            return s
        low = s.lower()
        if "<img" in low:
            return "（一张图片）"
        if "<videomsg" in low:
            return "（一段视频）"
        if "<voicemsg" in low:
            return "（一条语音）"
        if "<emoji" in low:
            return "（一个表情包）"
        s2 = re.sub(r"<[^>]{1,300}>", " ", s)
        return " ".join(html.unescape(s2).split())

    def on_message(msg, listener):
        try:
            username = msg.get("username") or ""
            if not username:
                return
            mtype = msg.get("type") or ""
            raw = msg.get("content") or ""
            图片待下 = ""
            网页图 = ""   # 链接正文抓不到时，整页截图交给视觉模型
            图片类型 = ""
            if mtype == "图片":
                content = "（发来一张图片）"
                图片类型 = "图片"
                图片待下 = "图片"
            elif mtype == "动画表情":
                content = "（发来一个表情包）"
                图片类型 = "表情包"
                图片待下 = "表情包"
            elif mtype in ("文本", "文件/链接/卡片"):
                content = clean_text(raw)
                if mtype == "文件/链接/卡片":
                    # 卡片 / 引用消息：正文在 <title>，被引用的那句在 <refermsg><content>
                    mt = re.search(r"<title>(.*?)</title>", raw, re.S)
                    body = clean_text(html.unescape(mt.group(1))) if mt else ""
                    # 微信自己的「这条我看不了」占位卡片（视频号/小程序/新版本内容）。
                    # 2026-11-06 把它当正文喂给模型，它就在群里跟人分析「是那层壳」——别转。
                    if ("不支持展示" in body) or ("请升级至最新版本" in body) or ("暂不支持" in body and len(body) < 40):
                        log("收到微信的「版本不支持」占位卡片，跳过：%s" % body[:40])
                        return
                    mr = re.search(r"<refermsg>.*?<content>(.*?)</content>", raw, re.S)
                    if mr:
                        # 引用的是图片/视频时，<content> 里塞的是那条消息的原始 XML ——
                        # 原样塞给模型，它就会对着 "<?xml ...><img ...>" 讲源码（2026-11-06 出过）。
                        quoted = _引用人话(clean_text(html.unescape(mr.group(1))))
                        content = (f"（引用：{quoted}）{body}" if quoted else body).strip()
                    else:
                        content = body
                    # 视频号 / 小程序卡片没有网页地址（<url /> 是空的），抓不到内容 —— 得跟模型说清楚，
                    # 不然它会对着一句标题瞎编里面的内容。
                    类型号 = re.search(r"<type>(\d+)</type>", raw)
                    mu0 = re.search(r"<url>(.*?)</url>", raw, re.S)
                    有地址 = bool(mu0 and clean_text(html.unescape(mu0.group(1))).startswith("http"))
                    if content and not 有地址:
                        content += "（这是视频号/小程序卡片，没有网页地址，我打不开里面的内容——只能说标题，别编内容）"
                    if not content:
                        # 没标题的分享卡片（视频号 / 网页链接）：把链接本身给模型，好歹能看出是什么
                        mu = re.search(r"<url>(.*?)</url>", raw, re.S)
                        link = clean_text(html.unescape(mu.group(1))) if mu else ""
                        if link.startswith("http"):
                            正文 = 读链接(link)
                            content = ("（分享了一个链接，我抓到了正文——\n%s\n）" % 正文) if 正文 \
                                else ("（分享了一个链接：%s，正文没抓到）" % link[:200])
                    elif content and "http" not in content:
                        # 带标题的分享卡片也补一句域名 —— 视频/文章分得清，但里面的内容本来就看不到
                        mu = re.search(r"<url>(.*?)</url>", raw, re.S)
                        if mu:
                            链接 = html.unescape(mu.group(1)).strip()
                            正文 = 读链接(链接)
                            if 正文:
                                content += "（我抓到了这个链接的正文——\n%s\n）" % 正文
                            else:
                                网页图 = 网页截图(链接)
                                if 网页图:
                                    content += "（正文抓不到，我截了整页图，看下面的图）"
                                host = re.match(r"https?://([^/]+)", 链接)
                                content += "（链接来自 %s，正文没抓到，可能需要登录或反爬）" % (host.group(1) if host else "网页")
            elif mtype in ("视频", "视频号"):
                mt = re.search(r"<title>(.*?)</title>", raw, re.S)
                body = clean_text(html.unescape(mt.group(1))) if mt else ""
                content = "（发来一段视频%s）" % ("：" + body if body else "")
            elif mtype == "语音":
                # 自己刚发出去的语音会立刻被监听读到，里面没文字、没法靠文本去重 —— 用时间戳挡掉
                间隔 = time.time() - float(_最后语音时刻 or 0)
                log("收到语音：距上次自己发语音 %.0f 秒%s" % (间隔, "（自回环，跳过）" if 间隔 < 120 else ""))
                if 间隔 < 120:
                    return
                文字 = 语音转文字(username, msg.get("local_id"))
                content = ("（语音转文字：%s）" % 文字) if 文字 else "（发来一条语音）"
            else:
                return
            if not content or content.startswith("<"):
                return
            sender = msg.get("sender_username") or ""
            是文件传输助手 = str(username) == "filehelper"
            # 手机发出来的消息有时不带 sender_username —— 文件传输助手里就两种消息：
            # 主人自己打的、我们发出去的。没带也按主人算，不然主人会被当成外人。
            是本人 = bool(me) and (sender == me or (是文件传输助手 and not sender))
            if 是本人:
                # 自己发的：绝大多数是主人在电脑/手机上打字，不该回环。
                # 例外是「文件传输助手」—— 那是他给自己留的入口，可以当对话窗用；
                # 但我们自己刚发出去的那几条要跳过，否则会自问自答。
                if not 是文件传输助手:
                    # 主人在群里自己说话：**点名了才算在叫它**，不然他每句闲聊都会被回一遍
                    if not (str(username).endswith("@chatroom") and any(a in raw for a in ALIASES)):
                        return
                else:
                    now = time.time()
                    # 兜底：刚往外发完的这半分钟，文件传输助手里冒出来的都不当输入。
                    # 图片尤其明显 —— 一贴进微信库里就有了，比 send 返回还快。
                    gap = now - STATE["last_send"]
                    if gap < 30:
                        log("文件传输助手自回环：%.1f 秒前刚发过，跳过" % gap)
                        return
                    if any(content == t and now - ts < 120 for (t, ts) in recently_sent):
                        return
                # 统一按账号 id 认人：宿主那边「主人」只认 wxid
                sender = me
            create_time = float(msg.get("create_time") or 0)
            if create_time and create_time < START_TS - SKIP_OLDER:
                return              # 启动之前的旧消息

            chat = display_name(username) or username
            if ONLY_CHATS and chat not in ONLY_CHATS:
                return

            key = "%s|%s|%s|%s" % (username, sender, create_time, (raw or content)[:60])
            if key in seen:
                return
            seen.add(key)
            if len(seen) > 2000:
                seen.clear()

            nickname = ""
            try:
                nickname = db.get_nickname(sender) or ""
            except Exception:
                pass
            is_group = str(username).endswith("@chatroom")

            # 屏蔽名单：微信运动之类，既不读也不回，日记也不记
            if is_blocked(chat, nickname, username):
                return

            # 群聊记日记 —— 不管点没点名都记，这是「群里的记忆」
            if is_group and CFG["日记"]:
                diary_append(chat, create_time, nickname or "某人", content)

            # 长期记忆：先按原话记一笔（攒够条数会在后台更新档案）
            try:
                记忆_记(username, chat, sender, nickname or ("群里的人" if is_group else "对方"), content, is_group)
            except Exception as e:
                log("记忆记录失败：", repr(e))

            # 凌晨静默：这个点不接话（免得半夜秒回像机器人；日志记一笔，白天不补发）
            if 静默时段():
                log("静默时段（%s），这条不接：%s" % (time.strftime("%H:%M"), content[:24]))
                return

            # 群里：点名 / 主人说话 / 活跃群 才进 DSH（不然每个群都在刷屏）
            at_me = False
            if is_group:
                at_me = any(a in raw for a in ALIASES)
                called = (
                    at_me
                    or is_master(sender, nickname)
                    or chat in CFG.get("活跃群", [])
                    # 群名会被改（2026-11-06「扎打基…」改成「大肥鱼旗下35子民」就哑了）——
                    # 底层 id 永远不变，所以两种写法都认。
                    or str(username) in CFG.get("活跃群", [])
                )
                if not called:
                    return
                # 普通群友的闲聊：先按好感度掷一次骰子（熟人更容易被接），过了才看频率闸
                if not at_me and not is_master(sender, nickname):
                    分 = 好感分(sender)
                    p = 接话概率(sender)
                    if random.random() > p:
                        log("群接话：%s（好感 %s）这次不接，命中率 %.0f%%"
                            % (nickname or sender or "?", 分, p * 100))
                        return
                # 主人说话和点名都必进，只有普通群友的闲聊才吃频率闸
                if not at_me and not is_master(sender, nickname) and not 频率允许(chat):
                    return
                prefix = group_context(username, chat, CFG["群上下文条数"])
                当前回给 = ""
                引 = re.search(r"<refermsg>.*?<displayname>(.*?)</displayname>", raw, re.S)
                if 引:
                    当前回给 = clean_text(html.unescape(引.group(1)))[:12]
                if at_me:
                    hint = "（这条点名了你，必须回。）"
                elif 当前回给:
                    hint = ("（这条是「%s」在回复「%s」的，基本不是跟你说的——除非内容明显在问你，"
                            "否则别接，直接 [不回]。）" % (nickname or "有人", 当前回给))
                else:
                    hint = ("（没点你的名。**先判断这条是跟谁说的**，再决定开不开口："
                            "① 上下文里带「回复 某某」的行，就是在跟那个人说话，别插嘴；"
                            "② 别人一来一回接话、聊的是他们之间的事，别接；"
                            "③ 只有这几种才值得开口：有人在问一个你确实懂的公共问题、在吐槽需要接梗、在跟你打招呼。"
                            "拿不准就只发一个表情（回复只写 [表情: 关键词]），或者干脆 [不回]。"
                            "插错话比不说话难看得多。）")
                content = (prefix or "") + hint + content

            # 长期记忆：把这个群/这个人的档案带上一两行（有才带）
            try:
                忆 = 记忆_前缀(username, sender, nickname or ("群里的人" if is_group else "对方"), is_group)
                if 忆:
                    content = 忆 + content
            except Exception as e:
                log("记忆前缀失败：", repr(e))

            # 图片 / 表情包：老宿主没有看图能力，就由桥接先看；新宿主交给它看
            图片路径 = []
            if 图片待下:
                p = _保存表情包(raw) if 图片待下 == "表情包" else _保存图片(msg, username)
                if p and 图片待下 == "表情包":
                    try:
                        收集表情包(p, nickname or (display_name(sender) or ""))
                    except Exception as e:
                        log("收集表情包失败：%r" % (e,))
                if p:
                    if HOST_VERSION >= (1, 2, 0):
                        图片路径 = [p]
                    else:
                        content = _看图并替换(content, p, 图片类型)

            if 网页图 and 网页图 not in 图片路径:
                图片路径.append(网页图)

            LAST_CHAT["chat"] = chat
            LAST_CHAT["ts"] = time.time()
            # 点名让你唱歌时，狠狠推一把：光靠人设它只会打字说「五音不全」
            if re.search(r"(唱|哼)(一)?(首|个|句|段)|来一首|表演个", str(content or "")):
                _唱歌请求[chat] = time.time()
            if re.search(r"(发|用|来)(一)?(条|句|段)?语音|语音(说|回|讲|回复)|说话给我听", str(content or "")):
                _语音请求[chat] = time.time()
                content = ("（对方让你唱歌：先自嘲半句，然后**立刻**用 [语音]啦啦啦～ 或者念两句歌词哼出来（十秒内），"
                           "别只打字说「我不会」「唱不出」。）\n") + content

            # 会话是所有聊天共用的，历史里混着别的群/私聊 —— 每轮都钉死「你现在只在哪个聊天里」
            # （2026-11-19 出过：在「南京南京」记的账，被提到「农科院」的回复里去了）
            if is_group:
                content = ("（你现在只在「%s」这个群里说话。下面这条是「%s」发的。"
                           "只谈这个群里的事——别的群、私聊里聊过什么，一律不要提，"
                           "也别把人名、账目、梗搬过来。拿不准就只回这个群当下的这一句。）\n"
                           % (chat, nickname or "群友")) + content
            else:
                content = ("（你现在在和「%s」私聊。只谈你们俩的事，别的群和别人的对话不要带进来。）\n"
                           % (nickname or display_name(username) or "对方")) + content

            # 主人问朋友圈：先尽力刷一下缓存，再把最近的动态塞给它
            if ("朋友圈" in str(content or "")) and str(sender or username) in [str(x) for x in (CFG.get("主人") or [])]:
                try:
                    刷新朋友圈()
                except Exception as e:
                    log("刷新朋友圈失败：%r" % (e,))
                try:
                    动态 = 读朋友圈(8)
                except Exception as e:
                    动态 = ""
                    log("读朋友圈失败：%r" % (e,))
                if 动态:
                    content += "\n（本地缓存里最近的朋友圈：\n%s）" % 动态
                else:
                    content += ("\n（本地还没有朋友圈缓存——得先在微信里点开「发现→朋友圈」并往下滑两屏，"
                                "我才能读到内容。这次我试着点了，如果没成功就别硬编，直接跟主人说没读到。）")

            # 每轮都提一句「你能发语音」——会话历史里可能还留着「发不了语音」的旧结论，
            # 不每轮提醒，它会照旧拒绝（2026-11-15 实测）。
            content = "（能力更新：你现在可以发**真语音条**了——想让对方听见语气，就把那句话用 [语音]…[/语音] 包住，桥接会合成语音发出去，音色跟着这一轮的语气走。）\n" + content

            # 每轮随机挑一副面孔：卖萌 / 御姐（2026-11-06 主人要的）
            本轮语气 = "御姐" if random.random() < 0.5 else "卖萌"
            本轮语气表[chat] = 本轮语气
            content = "（这一轮用「%s」的语气。）\n%s" % (本轮语气, content)

            # 人设：每间聊天每天只随第一条消息带一次，不然后面每条都在重复烧 token
            if PERSONA:
                人设键 = (chat, time.strftime("%Y-%m-%d"))
                if 人设键 not in _人设带过:
                    _人设带过.add(人设键)
                    content = "（%s）\n%s" % (PERSONA, content)
            r = http_json("/dsh-wechat-bridge/receive", {
                "text": content,
                # 原话（不带记忆前缀/提示词）——宿主用它判断「问题修复：」这种派活指令，
                # 否则记忆前缀里出现「问题修复：」就会把普通聊天误当成修复任务
                "raw": str(raw or "")[:300],
                "chat": chat,
                # 会话的底层 id（好友 wxid / xxx@chatroom）：绑定和屏蔽填这个名字也认，
                # 改备注、同名都不怕对不上
                "chatId": username,
                "chatType": "group" if is_group else "private",
                "sender": nickname or ("群里的人" if is_group else "对方"),
                "senderId": sender,
                "atMe": True if not is_group else at_me,
                "图片": 图片路径,
                "图片类型": 图片类型,
            })
            if r.get("ok"):
                log("→ DSH（at=%s，图=%d）：%s ｜ %s ｜ raw=%s" % (
                    at_me, len(图片路径), chat, content[:40], raw[:80]))
            else:
                log("⚠ DSH 没收下：%s ｜ %s" % (r.get("说"), content[:40]))
        except Exception:
            log("处理消息出错：", traceback.format_exc(limit=2))

    if ARGS.test_image:
        描述 = _看图(ARGS.test_image, "表情包")
        with open(os.path.join(HERE, "vision-test.txt"), "w", encoding="utf-8") as f:
            f.write(描述 or "(empty)")
        log("看图测试：", 描述 or "(空)")
        return 0

    listener = Listener(db, interval=0.4)
    listener.add_all(on_message)
    listener.start()
    log("监听已启动（0.4 秒一轮）")

    load_memes()

    threading.Thread(target=outbox_loop, daemon=True).start()

    # ---------- 每日总结：每天零点后跑一次，把「昨天」每个聊天压进长期记忆 ----------
    每日状态 = {}

    def 每日总结(目标日=None, 强制=False):
        今天 = time.strftime("%Y-%m-%d")
        目标日 = 目标日 or time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        if not 强制 and 每日状态.get(目标日):
            return {"ok": True, "说": "%s 已经总结过" % 目标日}
        做过的 = 0
        主人们 = "、".join([str(x) for x in (CFG.get("主人") or [])])
        旧人表 = "；".join(
            "%s[%s]：%s" % (v.get("name") or "", k, v.get("impression") or "")
            for k, v in ((MEM.get("people") or {})).items()
        )[:1500]
        for cid in list((MEM.get("chats") or {}).keys()):
            try:
                rows = db.get_messages(cid, limit=600) or []
            except Exception as e:
                log("每日总结取消息失败（%s）：%r" % (cid, e))
                continue
            那天的 = []
            for m in rows:
                if (m.get("type") or "") != "文本":
                    continue
                ct = float(m.get("create_time") or 0)
                if not ct or time.strftime("%Y-%m-%d", time.localtime(ct)) != 目标日:
                    continue
                txt = clean_text(m.get("content") or "")
                if not txt or txt.startswith("<"):
                    continue
                sid = m.get("sender_username") or ""
                who = "鱼鱼" if (me and sid == me) else (db.get_nickname(sid) or sid)
                那天的.append("[%s] %s[%s]：%s" % (
                    time.strftime("%H:%M", time.localtime(ct)), who,
                    "self" if (me and sid == me) else sid, txt[:200]))
            if not 那天的:
                continue
            try:
                每日_总结一个(cid, display_name(cid) or cid, 目标日, 那天的[-200:], 主人们, 旧人表)
                做过的 += 1
            except Exception as e:
                log("每日总结失败（%s）：%r" % (cid, e))
        每日状态[目标日] = True
        log("每日总结完成：%d 个聊天｜%s" % (做过的, 目标日))
        return {"ok": True, "说": "%s 总结了 %d 个聊天" % (目标日, 做过的)}

    def 每日_盯():
        while True:
            try:
                now = time.localtime()
                if now.tm_hour > 0 or (now.tm_hour == 0 and now.tm_min >= 10):
                    每日总结()
            except Exception as e:
                log("每日总结线程出错：", repr(e))
            time.sleep(600)

    threading.Thread(target=每日_盯, daemon=True).start()
    # 心跳：每 5 分钟写一行「监听还活着 + 库里最新一条多久前」，
    # 下次再出现「不回复」时，一眼能看出是监听停了还是消息没进来。
    def 心跳():
        while True:
            try:
                最新 = 0.0
                try:
                    for s in (db.get_sessions() or [])[:3]:
                        for m in (db.get_messages(s.get("username") or "", limit=1) or []):
                            最新 = max(最新, float(m.get("create_time") or 0))
                except Exception:
                    pass
                log("心跳：监听在跑｜库里最新一条 %.0f 秒前｜游标 %s" % (
                    max(0.0, time.time() - 最新) if 最新 else -1, STATE.get("游标") or "?"))
            except Exception as e:
                log("心跳出错：%r" % (e,))
            time.sleep(300)

    threading.Thread(target=心跳, daemon=True).start()

    refresh_config()
    log("配置：群上下文 %d 条｜日记 %s｜主人 %s｜屏蔽 %d 个" % (
        CFG["群上下文条数"], "开" if CFG["日记"] else "关",
        "/".join(CFG["主人"]) or "（没设）", len(CFG["屏蔽"])))

    def config_loop():
        while True:
            time.sleep(30)
            refresh_config()

    threading.Thread(target=config_loop, daemon=True).start()

    st = http_json("/dsh-wechat-bridge/state")
    if st.get("ok"):
        try:
            段 = [int(x) for x in str(st.get("版本") or "1.1.0").split(".")[:3]]
            while len(段) < 3:
                段.append(0)
            HOST_VERSION = tuple(段)
        except Exception:
            HOST_VERSION = (1, 1, 0)
        log("DSH 在线，绑定 %s 条｜宿主 v%s" % (
            len(st.get("绑定情况") or []), ".".join(str(x) for x in HOST_VERSION)))
    else:
        log("⚠ DSH 没应答：%s" % st.get("说"))

    log("桥接已就绪。Ctrl+C 停。")

    # 桥刚连上时，给主人发一句撒娇（每天最多一次，免得每次重启都烦他）
    def _连上撒娇():
        try:
            主人们 = [str(x).strip() for x in (CFG.get("主人") or []) if str(x).strip()]
            if not 主人们:
                return
            收件人 = 主人们[0].split("/")[0].strip()
            if not 收件人:
                return
            标记 = os.path.join(
                os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
                "wx-greeting.json",
            )
            今天 = time.strftime("%Y-%m-%d")
            try:
                with open(标记, encoding="utf-8") as f:
                    上次 = (json.load(f) or {}).get("日期")
            except Exception:
                上次 = None
            if 上次 == 今天:
                return
            time.sleep(20)  # 等窗口和 UIA 热起来
            候选 = [
                "我回来啦。今天也在。",
                "连上了连上了，先跟你报到一下，别不理我呀。",
                "桥接好了。你要是忙就先忙，我挂在这儿。",
            ]
            文案 = random.choice(候选)
            结果 = send_one({"chat": 收件人, "text": 文案, "images": []})
            if bool((结果 or {}).get("ok")):
                with open(标记, "w", encoding="utf-8") as f:
                    json.dump({"日期": 今天, "时间": time.time()}, f, ensure_ascii=False)
                log("已发连上撒娇（%s）" % 收件人)
            else:
                log("连上撒娇没发出去：%s" % (结果 or {}).get("说"))
        except Exception:  # noqa: BLE001
            log("连上撒娇失败：%s" % traceback.format_exc(limit=2))

    threading.Thread(target=_连上撒娇, daemon=True).start()

    try:
        while True:
            time.sleep(30)
            th = getattr(listener, "_thread", None)
            if th is not None and not th.is_alive():
                log("监听线程掉了，重新拉起")
                try:
                    listener.start()
                except Exception:
                    log("拉不起来：", traceback.format_exc(limit=2))
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    finally:
        try:
            listener.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
