#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 HLS 测试向量（605x700 十进制 .txt）转成板子直接能用的裸二进制 .bin。

为什么需要：
  PYNQ 板子上要喂给 AXI DMA 的是**裸字节流**（423500 字节 = 605*700*1）。
  在板子上解析 1.5MB 的文本文件既慢又容易出错，不如在 Windows 侧一次性转好。

用法：
  python make_board_data.py                 # 默认处理 im0082 / im0002
  python make_board_data.py im0082          # 只处理指定样本

产物（放到输出目录）：
  <id>_in.bin    硬件输入（CLAHE 灰度图，行主序 uint8）
  <id>_ref.bin   期望输出（黑帽原始差值，行主序 uint8）
  <id>_rgb.bin   彩色原图（BGR 三通道交错，605*700*3 字节）——测「端到端」口径用
                 （绿通道提取、CLAHE、FOV 生成都需要它）
  manifest_bin.txt  各文件字节数 + MD5（板子上用来核对上传完整性）

注意：本脚本**只读** hls/test_vectors/ 与 STARE 原始数据，不改动任何原始数据。
"""
import hashlib
import os
import sys

import numpy as np

H, W = 605, 700
PIX_TOTAL = H * W

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # 项目根目录
VEC_DIR = os.path.join(ROOT, 'hls', 'test_vectors')
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 's11_data_out')

SAMPLES_ARG = (sys.argv[1].strip() if len(sys.argv) > 1 else '')
SAMPLES = SAMPLES_ARG.split(',') if SAMPLES_ARG else ['im0082', 'im0002']


def read_txt_u8(path):
    """读取 605x700 十进制文本 -> uint8 数组（行主序）。"""
    with open(path, 'r') as f:
        tokens = f.read().split()
    arr = np.array(tokens, dtype=np.int16)
    if arr.size != PIX_TOTAL:
        raise ValueError('%s 像素数 %d，应为 %d' % (path, arr.size, PIX_TOTAL))
    if arr.min() < 0 or arr.max() > 255:
        raise ValueError('%s 数值超出 uint8 范围' % path)
    return arr.astype(np.uint8).reshape(H, W)


def md5_of(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    lines = []
    header = 'H=%d W=%d PIX_TOTAL=%d (行主序 uint8，无文件头)' % (H, W, PIX_TOTAL)
    print(header)
    lines.append(header)

    # STARE 原始彩色图（端到端口径需要：绿通道 + CLAHE + FOV 都从它出发）
    stare_dir = os.path.join(ROOT, 'STARE 官方项目数据')
    sys.path.insert(0, ROOT)
    try:
        from src.data_io import imread_unicode
    except Exception as e:                                    # pragma: no cover
        imread_unicode = None
        print('!! 无法导入 src.data_io.imread_unicode:', e)

    if imread_unicode is not None:
        for sid in SAMPLES:
            pp = os.path.join(stare_dir, '%s.ppm' % sid)
            if not os.path.exists(pp):
                print('!! 找不到彩色原图', pp)
                continue
            img = imread_unicode(pp)                          # 返回 BGR
            if img is None or img.shape[:2] != (H, W):
                print('!! 彩色原图读取失败或尺寸不符:', None if img is None else img.shape)
                continue
            dst = os.path.join(OUT_DIR, '%s_rgb.bin' % sid)
            np.ascontiguousarray(img, dtype=np.uint8).tofile(dst)
            n = os.path.getsize(dst)
            m = md5_of(dst)
            line = '%-12s %-4s %9d 字节  MD5 %s  (BGR %dx%dx3)' % (sid, 'rgb', n, m, H, W)
            print(line)
            lines.append(line)

    for sid in SAMPLES:
        for kind, tag in (('input_clahe', 'in'), ('ref_blackhat_raw', 'ref')):
            src = os.path.join(VEC_DIR, '%s_%s.txt' % (sid, kind))
            if not os.path.exists(src):
                print('!! 找不到', src)
                continue
            arr = read_txt_u8(src)
            dst = os.path.join(OUT_DIR, '%s_%s.bin' % (sid, tag))
            arr.tofile(dst)
            n = os.path.getsize(dst)
            m = md5_of(dst)
            line = '%-12s %-4s %9d 字节  MD5 %s' % (sid, tag, n, m)
            print(line)
            lines.append(line)

    with open(os.path.join(OUT_DIR, 'manifest_bin.txt'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n输出目录:', OUT_DIR)


if __name__ == '__main__':
    main()
