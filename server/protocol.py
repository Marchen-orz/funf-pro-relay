"""
协议帧构建器
读 protocol_config.json，把上层语义指令（set_suction(50, mode=2)）转成设备能识别的字节序列。
上层只需调用 build_control_frame / build_init_frame / build_stop_frames。
真机抓包后只改 protocol_config.json，本文件不动。
"""
import json
import os
import random

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "protocol_config.json")


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()


def _parse_byte(tok, ctx):
    """把模板里的Token解析成整数。
    - "0x07" -> 7
    - "[SEQ]" -> 随机字节
    - "[MODE1]" -> ctx["mode1"]
    - "[INTENSITY]" -> ctx["intensity"]
    - "[MODE2]" -> ctx["mode2"]
    - "37" -> 37
    """
    if not isinstance(tok, str):
        return int(tok)
    t = tok.strip()
    if t == "[SEQ]" and CFG["frame"].get("seq_random", True):
        return random.randint(0, 255)
    if t == "[SEQ]":
        return 0
    if t == "[MODE1]":
        return ctx["mode1"]
    if t == "[MODE2]":
        return ctx["mode2"]
    if t == "[INTENSITY]":
        return ctx["intensity"]
    if t.startswith("0x") or t.startswith("0X"):
        return int(t, 16)
    return int(t, 10)


def _resolve_mode_pair(func_name):
    """从 config 取某功能的 (mode1, mode2) 整数对。null -> None（表示该功能暂未抓包，跳过）。"""
    f = CFG["functions"].get(func_name)
    if not f:
        return (None, None)
    m1 = int(f["mode1"], 16) if isinstance(f["mode1"], str) else f["mode1"]
    m2 = int(f["mode2"], 16) if isinstance(f["mode2"], str) else f["mode2"]
    return (m1, m2)


def _clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def build_frame_from_template(template, ctx):
    return bytes(_parse_byte(tok, ctx) for tok in template)


def build_init_frame():
    """连接后必发的初始化帧。"""
    seq = CFG["init_sequence"]
    if not seq.get("enabled", True):
        return None
    return build_frame_from_template(seq["frame"], {})


def build_control_frame(func_name, intensity, mode=1):
    """构建单个功能的控制帧。

    返回 bytes，或在功能未抓包（mode1/mode2 为 null）时返回 None（调用方应跳过）。
    """
    m1, m2 = _resolve_mode_pair(func_name)
    if m1 is None or m2 is None:
        return None  # 该功能未抓包，跳过下发
    irange = CFG["frame"]["intensity_range"]
    mrange = CFG["frame"]["mode_range"]
    ctx = {
        "mode1": m1,
        "mode2": m2,
        "intensity": _clamp(intensity, irange[0], irange[1]),
    }
    if CFG["functions"][func_name].get("supports_mode", True):
        _ = _clamp(mode, mrange[0], mrange[1])
    return build_frame_from_template(CFG["frame"]["template"], ctx)


def build_stop_frames():
    """停止所有功能。按 stop_strategy：逐功能发强度=0的帧，返回一个 bytes 列表。"""
    frames = []
    for fn, f in CFG["functions"].items():
        m1, m2 = _resolve_mode_pair(fn)
        if m1 is None or m2 is None:
            continue
        ctx = {"mode1": m1, "mode2": m2, "intensity": 0}
        frames.append(build_frame_from_template(CFG["frame"]["template"], ctx))
    return frames


def build_all_frames(state):
    """根据一个 state dict {suction,vibration,electric, *_mode} 构建所有功能的帧列表。
    逐功能一对通道发包，严格遵守 per_function_pair。
    """
    frames = []
    for fn in ("suction", "vibration", "electric"):
        inten = state.get(fn, 0)
        mode = state.get(f"{fn}_mode", 1)
        f = build_control_frame(fn, inten, mode)
        if f is not None:
            frames.append(f)
    return frames


def frames_to_hex(frames):
    return [f.hex() for f in frames]


def get_config():
    return CFG


def reload_config(path=CONFIG_PATH):
    global CFG
    CFG = load_config(path)
    return CFG
