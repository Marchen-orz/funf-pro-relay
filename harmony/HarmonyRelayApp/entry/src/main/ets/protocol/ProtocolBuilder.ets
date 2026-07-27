/**
 * 协议帧构建器 —— 与 server/protocol.py 同构
 * Pro 已探测：吮吸(07/08)✓ 震动(01/02)✓ 电流(03/04)
 */

// 功能 -> (mode1, mode2) 通道对
const fnModes: Record<string, [number, number]> = {
  "suction":   [0x07, 0x08],
  "vibration": [0x01, 0x02],
  "electric":  [0x03, 0x04]
};

function rndSeq(): number {
  return Math.floor(Math.random() * 256);
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : (v > hi ? hi : v);
}

/**
 * 构建单功能控制帧 (12 字节)
 * 格式: [SEQ] [01] [00] [02] [00] [MODE1] [11] [INTENSITY] [00] [MODE2] [11] [01]
 */
export function buildControlFrame(fnName: string, intensity: number): Uint8Array | null {
  const modes = fnModes[fnName];
  if (modes === undefined) return null;
  const [m1, m2] = modes;
  return new Uint8Array([
    rndSeq(), 0x01, 0x00, 0x02, 0x00,
    m1, 0x11, clamp(intensity, 0, 100), 0x00,
    m2, 0x11, 0x01
  ]);
}

/** 构建停止帧：给每个功能发强度=0 */
export function buildStopFrames(): Uint8Array[] {
  const frames: Uint8Array[] = [];
  for (const fn of Object.keys(fnModes)) {
    const f = buildControlFrame(fn, 0);
    if (f) frames.push(f);
  }
  return frames;
}

/** 初始化帧 */
export function buildInitFrame(): Uint8Array {
  return new Uint8Array([
    rndSeq(), 0x01, 0x00, 0x01, 0x00,
    0xC8, 0x11, 0x01
  ]);
}

/** 帧转 hex 字符串 */
export function frameToHex(f: Uint8Array): string {
  return Array.from(f).map(b => b.toString(16).padStart(2, '0')).join('');
}

export function hexToBytes(hex: string): Uint8Array {
  const clean = hex.replace(/\s/g, '');
  const len = clean.length / 2;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    bytes[i] = parseInt(clean.substring(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

export function getFnModes(): Record<string, [number, number]> {
  return fnModes;
}

// --------- 协议探测 ---------
export const probePairs: Array<[number, number, string]> = [
  [0x01, 0x02, "旧震动"],
  [0x03, 0x04, "旧电流"],
  [0x05, 0x06, "候选A"],
  [0x07, 0x08, "旧吮吸(已知)"],
  [0x09, 0x0A, "候选B"],
  [0x0B, 0x0C, "候选C"],
  [0x0D, 0x0E, "候选D"],
  [0x0F, 0x10, "候选E"],
];

export function buildProbeFrame(m1: number, m2: number, intensity: number): Uint8Array {
  return new Uint8Array([
    rndSeq(), 0x01, 0x00, 0x02, 0x00,
    m1, 0x11, clamp(intensity, 0, 100), 0x00,
    m2, 0x11, 0x01
  ]);
}
