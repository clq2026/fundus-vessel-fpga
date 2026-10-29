# -*- coding: utf-8 -*-
"""
眼底血管分割 FPGA 加速 —— 网页交互界面（零依赖保底版）
========================================================
用途：在 PYNQ-Z2 上起一个网页服务（端口 7860），浏览器打开后可：
  ① 选择板上内置眼底图（im0082 / im0002）或上传任意图片；
  ② 一键运行「纯软件 / 硬件加速」两种后端；
  ③ 页面显示分割掩膜、红色叠加图，以及分段耗时与加速比（方案 V4 验收点）。
依赖：无新增。只用标准库 + 板上已有的 numpy / cv2 / pynq。
用法：板端 Jupyter 新建 cell 运行：
      %run demo_ui_board.py
    （服务在后台线程，cell 立即返回；关闭 notebook 或重启板子即停）
注意：脚本自己做干净重载（唯一名重下 bit + 重建 MMIO/Buffer），直接运行即可。
"""

import os
import sys
import gc
import shutil
import time
import json
import base64
import datetime
import threading
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import cv2
from pynq import MMIO, Overlay, allocate

D = "/home/xilinx/jupyter_notebooks/vessel_s11"
H, W = 605, 700
N = H * W
CLIP, GRID = 2.0, (8, 8)
K3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
K11 = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
PORT = 7860

# 自动定位模块（fundus_autoloc.py 必须和本脚本放在同一个目录 /home/xilinx/jupyter_notebooks/vessel_s11/）
# 导入失败不致命：退化成「整图缩放」的老行为，但会在页面上明确提示。
if D not in sys.path:
    sys.path.insert(0, D)
try:
    import fundus_autoloc as fa
    AUTOLOC_OK = True
except Exception as _e:
    fa = None
    AUTOLOC_OK = False
    print("[!] 未找到 fundus_autoloc.py（%s）" % _e)
    print("    上传照片将退化成整图缩放，眼模实拍图会失效。请把该模块上传到 %s" % D)

print("=" * 66)
print("眼底血管分割 FPGA 加速 —— 网页交互界面（零依赖版）")
print("=" * 66)

# ==================== [0] 干净重载 ====================
HWK = ("Overlay", "MMIO", "ContiguousArray", "Buffer")
gone = [k for k, v in list(globals().items()) if type(v).__name__ in HWK]
for k in gone:
    globals().pop(k, None)
gc.collect()

tag = datetime.datetime.now().strftime("%H%M%S")
bp = os.path.join(D, "ui_%s.bit" % tag)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.bit"), bp)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.hwh"), os.path.join(D, "ui_%s.hwh" % tag))
ol = Overlay(bp)
dma = MMIO(0x41E00000, 0x1000)
ves = MMIO(0x40000000, 0x1000)
buf_in = allocate(shape=(N,), dtype=np.uint8)
buf_out = allocate(shape=(N,), dtype=np.uint8)
print("[0] 干净重载完成:", os.path.basename(bp))
print("    IP:", list(ol.ip_dict.keys()))

HW_LOCK = threading.Lock()


# ==================== [1] 算子（与 demo_s11_board.py 逐行一致） ====================
def reset_ch():
    for cr in (0x00, 0x30):
        dma.write(cr, 0x4)
        t = time.perf_counter()
        while (dma.read(cr) & 0x4) and time.perf_counter() - t < 1.0:
            pass


def kick_ax():
    dma.write(0x00, 0x1001)
    dma.write(0x30, 0x1001)
    ves.write(0x00, 0x01)
    dma.write(0x18, buf_in.physical_address)
    dma.write(0x48, buf_out.physical_address)


def wait_idle(tmo=2.0):
    t0 = time.perf_counter()
    while True:
        if (dma.read(0x04) & 0x02) and (dma.read(0x34) & 0x02):
            return
        if time.perf_counter() - t0 > tmo:
            raise RuntimeError("硬件超时")


def hw_run(gray):
    """完整单帧（延迟口径 A）：含输入搬运与结果取回，外部计时。"""
    np.copyto(buf_in, gray.reshape(-1))
    buf_in.flush()
    buf_out.flush()
    reset_ch()
    kick_ax()
    dma.write(0x28, N)
    dma.write(0x58, N)
    wait_idle()
    buf_out.invalidate()
    return np.array(buf_out).reshape(H, W).copy()


def hw_run_preloaded(return_img=True):
    """吞吐口径 B：输入已就位，只计时「写 LENGTH -> DMA Idle」的传输段。"""
    reset_ch()
    kick_ax()
    t0 = time.perf_counter()
    dma.write(0x28, N)
    dma.write(0x58, N)
    wait_idle()
    ms = (time.perf_counter() - t0) * 1000.0
    if not return_img:
        return None, ms
    buf_out.invalidate()
    return np.array(buf_out).reshape(H, W).copy(), ms


def hw_throughput(gray, reps=5):
    """吞吐口径 B 的多次取中位版（与答辩 PPT 的 37.2x 同口径）。"""
    np.copyto(buf_in, gray.reshape(-1))
    buf_in.flush()
    hw_run_preloaded(False)  # 预热一次，不计入
    ts = []
    img = None
    for _ in range(reps):
        img, ms = hw_run_preloaded(True)
        ts.append(ms)
    return float(np.median(ts)), img


def sw_core(gray):
    med = cv2.medianBlur(gray, 3)
    dil = cv2.dilate(med, K11, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    clo = cv2.erode(dil, K11, borderType=cv2.BORDER_CONSTANT, borderValue=255)
    return cv2.subtract(clo, med)


def gen_fov(rgb):
    lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB)
    _, binary = cv2.threshold(lab[:, :, 0], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        fov = (labels == int(np.argmax(areas)) + 1).astype(np.uint8) * 255
    else:
        fov = binary
    fov = cv2.morphologyEx(fov, cv2.MORPH_CLOSE, K3)
    _, fov = cv2.threshold(fov, 127, 255, cv2.THRESH_BINARY)
    return fov


# ==================== [2] 数据与流水线 ====================
BUILTIN = {}
for nm in ("im0082_rgb.bin", "im0002_rgb.bin"):
    p = os.path.join(D, nm)
    if os.path.exists(p):
        BUILTIN[nm.replace("_rgb.bin", "")] = p
if not BUILTIN:
    raise RuntimeError("板上没有找到 im0082_rgb.bin / im0002_rgb.bin")

FOV_CACHE = {}
CLAHE = cv2.createCLAHE(CLIP, GRID)


def load_rgb(name):
    return np.frombuffer(open(BUILTIN[name], "rb").read(), np.uint8).reshape(H, W, 3)


# 定位阶段的降采样目标边长。480 是速度与精度的平衡点，理由见 rgb_from_bytes 里的实测。
LOC_SIDE = 480.0


def rgb_from_bytes(data, autoloc=True):
    """上传图 -> 700x605 的 RGB。返回 (rgb, fov_override, circle, info)。

    autoloc=True（默认，眼模实拍必须开）
        先自动找到眼底圆再裁剪，并直接用这个圆当 FOV。
        不这么做的话，整张照片（白眼球 + 桌面）会被压进 700x605，
        gen_fov 会取面积最大的连通域 —— 白球面可见面积是眼底圆的 2 倍 ——
        于是 FOV 被选成背景，掩膜被 bitwise_and 一刀切光（实测 Dice = 0）。

    autoloc=False
        老的整图缩放，只适合本来就是 700x605 左右的数字图。
    """
    _tdec = time.perf_counter()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    t_dec = (time.perf_counter() - _tdec) * 1000.0
    if img is None:
        raise RuntimeError("无法解码上传的图片")
    if autoloc and AUTOLOC_OK:
        # ★实测（2026-10-24）：定位的 morphologyEx 曾用不可分离的椭圆核
        # （核宽 = 0.03*min(H,W)，1200 档就是 27x27），且 locate_fundus 跑两遍。
        # PC 上靠 SIMD 只要 118 ms，ARM Cortex-A9 上没有这套优化，实测要 1~2 分钟。
        # 现在：480 档 + RECT 核 + 只跑一遍 + bbox 内取点，PC 15 ms -> 9 ms。
        # 几何精度不受影响：换算回原图坐标只差 1 px 级，而最终只是按这个圆做一次
        # 相似变换裁剪；Dice 逐位不变（0.5150 / 上限 0.6139）。
        h, w = img.shape[:2]
        ks = min(1.0, LOC_SIDE / float(max(h, w)))
        _t = time.perf_counter()
        small = cv2.resize(img, None, fx=ks, fy=ks,
                           interpolation=cv2.INTER_AREA) if ks < 1.0 else img
        t_rs = (time.perf_counter() - _t) * 1000.0
        # 打印页在每个格子外面画了一圈 2 px 的黑色标定圆，位置正好落在这个 FOV 边界上。
        # 它是一条细暗线，底帽算子必然把它当血管检出，于是掩膜外沿出现一个完整白环
        # （实测绿通道在半径 324 处从 52 骤降到 34，再往外 326 又跳到 117＝白纸）。
        # 把 FOV 收到 0.98（半径 324→317.5）正好把这一圈切掉；
        # 代价是丢失半径 318~324 之间的真血管约 1.3k 像素，Dice 0.515→0.507，可忽略。
        _t = time.perf_counter()
        out = fa.auto_crop_and_fov(small, shrink=0.98, verbose=True)
        t_loc = (time.perf_counter() - _t) * 1000.0
        if out is not None:
            crop, fov_auto, circ, info = out
            t_cb = 0.0
            if ks < 1.0:                       # 几何量还原到原图坐标系，并按原图裁剪
                circ = (circ[0] / ks, circ[1] / ks, circ[2] / ks)
                _t = time.perf_counter()
                crop = fa.crop_fundus(img, circ)
                t_cb = (time.perf_counter() - _t) * 1000.0
            if info is None:
                info = {}
            info["ph_ms"] = dict(decode=t_dec, resize=t_rs, locate=t_loc, crop_big=t_cb)
            print("    [上传] 定位成功 圆心(%.0f,%.0f) 半径%.0f 轴比%.2f 原图%dx%d 降采样%.2f"
                  % (circ[0], circ[1], circ[2], info.get('ecc', 0.0), w, h, ks))
            print("       上传细分: 解码 %.0f + 降采样 %.0f + 定位本体 %.0f + 大图裁剪 %.0f ms"
                  % (t_dec, t_rs, t_loc, t_cb))
            return cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), fov_auto, circ, info
        print("    [上传] 定位失败，退回整图缩放（掩膜大概率为空）")
    return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (W, H)), None, None, None


def run_pipeline(rgb, use_hw, fov):
    t = {}
    t0 = time.perf_counter(); green = rgb[:, :, 1]; t["绿通道"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); cl = CLAHE.apply(green); t["CLAHE"] = (time.perf_counter() - t0) * 1000.0
    if use_hw:
        with HW_LOCK:
            t0 = time.perf_counter(); raw = hw_run(cl); t["核心"] = (time.perf_counter() - t0) * 1000.0
    else:
        t0 = time.perf_counter(); raw = sw_core(cl); t["核心"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    norm = raw if int(raw.max()) == 0 else cv2.normalize(raw, None, 0, 255, cv2.NORM_MINMAX)
    t["归一化"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); _, m = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    t["Otsu"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= 30
    if keep.size:
        keep[0] = False
    m = (keep[labels]).astype(np.uint8) * 255
    t["删小块"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); m = cv2.bitwise_and(m, fov); t["FOV清零"] = (time.perf_counter() - t0) * 1000.0
    return m, t


def png_b64_rgb(rgb_img):
    ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb_img, cv2.COLOR_RGB2BGR))
    return base64.b64encode(buf.tobytes()).decode("ascii")


def jpg_b64_rgb(rgb_img, q=92):
    """叠加图用 JPEG 回传：700x605 彩图 PNG 约 600 KB，JPEG(q92) 约 60 KB。

    两个好处：ARM 上 JPEG 编码更快；手机经热点下载 600 KB 要好几秒。
    叠加图是给人看的，q92 肉眼无损。掩膜是二值图仍走 PNG（只有十几 KB）。
    """
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb_img, cv2.COLOR_RGB2BGR),
                           [int(cv2.IMWRITE_JPEG_QUALITY), q])
    return base64.b64encode(buf.tobytes()).decode("ascii")


def make_overlay(rgb, mask):
    ov = rgb.copy()
    sel = mask > 0
    ov[sel] = (0.45 * rgb[sel].astype(np.float32) + 0.55 * np.array([255, 0, 0], np.float32)).astype(np.uint8)
    return ov


# ==================== [2b] 预热（消除首次调用的冷启动开销） ====================
# OpenCV 的大函数首次在板端 ARM 上调用会做一次性初始化（实测约 2.8 s），
# 硬件通路首次调用也含 DMA 描述符/cache 冷启动。这里统一先空跑一次，
# 保证网页上「第一次点开始处理」看到的就是稳态耗时。
_warm_name = sorted(BUILTIN.keys())[0]
_warm_rgb = load_rgb(_warm_name)
_warm_cl = CLAHE.apply(_warm_rgb[:, :, 1])
_t0 = time.perf_counter()
gen_fov(_warm_rgb)
sw_core(_warm_cl)
hw_run(_warm_cl)
hw_throughput(_warm_cl, reps=2)
print("[0b] 预热完成（%.0f ms），首次请求即为稳态耗时" % ((time.perf_counter() - _t0) * 1000.0))


# ==================== [2c] 原语自测（只在启动跑一次，用来揪出 ARM 上的地雷函数） ====================
# 背景：定位在 PC 上只要 9 ms（480 档），板端却要 8371 ms —— 900 倍，远超正常的
# ARM/x86 差距（一般 15~20 倍）。说明是某个具体函数在 Cortex-A9 上退化，而不是
# 「整体都慢」。逐项测一遍就能直接看出来是哪一个，不必靠猜。
def bench_prims():
    rows = []

    def T(name, fn, n=3):
        """测一项。★第一遍就超过 500 ms 的项不再重复 —— 板端若有 4 s 的地雷，
        重复 3 次会把启动拖到十几秒，没必要。"""
        try:
            t = time.perf_counter()
            fn()                       # 第一遍同时充当预热（排除首次初始化开销）
            t1 = (time.perf_counter() - t) * 1000.0
            if t1 > 500.0:
                rows.append((name, t1))
                return
            ts = [t1]
            for _ in range(n - 1):
                t = time.perf_counter()
                fn()
                ts.append((time.perf_counter() - t) * 1000.0)
            rows.append((name, float(np.median(ts))))
        except Exception as e:             # 单项失败不影响其它项
            rows.append((name, -1.0))
            print("     %-30s 失败: %s" % (name, e))

    # 480x360 的合成「眼底图」：深灰底 + 偏红圆盘，保证形态学/连通域有真实工作量
    small = np.full((360, 480, 3), 40, np.uint8)
    cv2.circle(small, (232, 170), 117, (60, 90, 200), -1)     # BGR，圆盘偏红
    small = cv2.GaussianBlur(small, (5, 5), 0)
    big = cv2.resize(small, (1600, 1200), interpolation=cv2.INTER_LINEAR)
    m0 = (small[:, :, 2].astype(np.int16) - small[:, :, 0].astype(np.int16) > 12).astype(np.uint8)
    k11r = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    k11e = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    k3r = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    # 4 万点的点集（200x200），对标 solid 填充后的规模
    gp = np.full((200, 200), 255, np.uint8)
    pts = np.column_stack(np.nonzero(gp)[::-1]).astype(np.float32)
    ok, jpg = cv2.imencode(".jpg", big, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    jb = jpg.tobytes()
    Mp = np.array([[0.435, 0.0, 100.0], [0.0, 0.435, 50.0]], np.float32)

    T("imdecode 1600x1200 JPEG",
      lambda: cv2.imdecode(np.frombuffer(jb, np.uint8), cv2.IMREAD_COLOR))
    T("resize 1600x1200->480x360 AREA",
      lambda: cv2.resize(big, (480, 360), interpolation=cv2.INTER_AREA))
    if AUTOLOC_OK:
        T("R-B 色度 480x360", lambda: fa.rb_map(small))
    T("morph CLOSE 11x11 RECT",
      lambda: cv2.morphologyEx(m0, cv2.MORPH_CLOSE, k11r))
    T("morph CLOSE 11x11 ELLIPSE",
      lambda: cv2.morphologyEx(m0, cv2.MORPH_CLOSE, k11e))
    T("morph OPEN 3x3 RECT", lambda: cv2.morphologyEx(m0, cv2.MORPH_OPEN, k3r))
    T("connectedComponentsWithStats",
      lambda: cv2.connectedComponentsWithStats(m0, 8))
    T("nonzero 4万点", lambda: np.nonzero(gp))
    T("argwhere 4万点", lambda: np.argwhere(gp > 0))
    T("convexHull 4万点", lambda: cv2.convexHull(pts))
    T("minEnclosingCircle 4万点", lambda: cv2.minEnclosingCircle(pts))
    T("percentile 4万点 x2",
      lambda: (np.percentile(pts[:, 0], [2, 98]), np.percentile(pts[:, 1], [2, 98])))
    T("fillConvexPoly 240x240",
      lambda: cv2.fillConvexPoly(np.zeros((240, 240), np.uint8),
                                 np.array([[5, 5], [230, 8], [235, 232], [8, 228]], np.int32), 255))
    T("warpAffine 1600x1200->700x605 CUBIC",
      lambda: cv2.warpAffine(big, Mp, (700, 605), flags=cv2.INTER_CUBIC))

    wmax = max(len(r[0]) for r in rows)
    print("[0c] 原语自测（本板稳态耗时，用来定位慢点）：")
    print("     PC(x86) 实测参考: imdecode 3 / resize 2 / R-B 0 / CLOSE-RECT 0.1 /"
          " CLOSE-ELLIPSE 0.3 / OPEN 0 / CC 1.3 / nonzero 0.2 / argwhere 0.4 /"
          " hull 3.1 / minCircle 0.4 / pct 0.3 / fill 0 / warp 0.7 ms")
    for nm, ms in rows:
        print("     %-*s %9.1f ms" % (wmax, nm, ms))


try:
    bench_prims()
except Exception as _e:
    print("[0c] 原语自测跳过：", _e)


# ==================== [3] 网页 ====================
PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>眼底血管分割 FPGA 加速</title>
<style>
body { font-family: "Microsoft YaHei", sans-serif; background: #f4f6f8; margin: 0; }
.hd { background: #1a3c6e; color: #fff; padding: 14px 24px; font-size: 20px; }
.hd span { font-size: 13px; opacity: .85; margin-left: 12px; display: block; font-size: 12px; }
.wrap { display: flex; gap: 20px; padding: 20px; max-width: 1400px; margin: 0 auto; flex-wrap: wrap; }
.card { background: #fff; border-radius: 10px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,.12); }
.colL { width: 320px; flex: none; }
.colR { flex: 1; }
label { display: block; margin: 12px 0 4px; font-size: 14px; color: #333; }
select, input[type=file] { width: 100%; padding: 7px; font-size: 14px; }
button { margin-top: 16px; width: 100%; padding: 11px; font-size: 16px; color: #fff;
         background: #1a73e8; border: 0; border-radius: 6px; cursor: pointer; }
button:disabled { background: #9ab3d6; cursor: wait; }
img.res { width: 100%; border-radius: 6px; border: 1px solid #ddd; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.cap { font-size: 13px; color: #555; text-align: center; margin: 4px 0 10px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
td, th { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: right; }
th { background: #f0f3f7; text-align: center; }
td:first-child, th:first-child { text-align: left; }
.big { font-size: 22px; font-weight: bold; color: #c00; }
.ok { color: #0a7a2f; }
#msg { margin-top: 10px; font-size: 13px; color: #666; min-height: 18px; }
</style>
</head>
<body>
<div class="hd">眼底血管分割 FPGA 加速演示<span>PYNQ-Z2 (XC7Z020) · PL 100 MHz / ARM Cortex-A9 667 MHz</span></div>
<div class="wrap">
  <div class="card colL">
    <b>输入</b>
    <label>选择图像</label>
    <select id="imgsel">
      <option value="__NAME0__">__OPT0__</option>
      <option value="__NAME1__">__OPT1__</option>
      <option value="upload">上传任意图片…</option>
    </select>
    <!-- capture="environment" 让手机点一下直接开后置摄像头，不用在菜单里再选一次；
         桌面浏览器会忽略这个属性，电脑上照常弹文件选择框 -->
    <input type="file" id="file" accept="image/*" capture="environment" style="display:none">
    <label id="labAuto" style="display:none; background:#fff8e1; padding:8px; border-radius:5px">
      <input type="checkbox" id="autoloc" checked>
      自动定位眼底圆（<b>拍摄眼模的照片必须勾选</b>；关闭则整图缩放，掩膜会失效）
    </label>
    <label id="labFollow" style="background:#e8f0fe; padding:8px; border-radius:5px">
      <input type="checkbox" id="follow">
      <b>自动跟随最新结果</b>（每 1.5 秒刷新：手机拍照上传后，本页自动更新）
    </label>
    <label id="labNote">说明：同一张图会分别用「纯软件」与「硬件加速」各跑一遍，页面直接给出对比。</label>
    <button id="go" onclick="run()">开始处理</button>
    <div id="msg"></div>
  </div>
  <div class="card colR">
    <div class="grid">
      <div><div class="cap">红色叠加结果（硬件路径）</div><img id="ov" class="res" src="preview?name=__NAME0__"></div>
      <div><div class="cap">血管掩膜（硬件路径）</div><img id="mk" class="res" src=""></div>
    </div>
    <table id="tb" style="margin-top:10px">
      <tr><th>步骤</th><th>纯软件 ms</th><th>混合硬件 ms</th></tr>
    </table>
    <div id="sum" style="margin-top:10px; font-size:15px"></div>
  </div>
</div>
<script>
function toB64(f, maxSide) {
  // 先在手机端把照片缩到 maxSide，再上传。
  // 手机原图 4000x3000 有 4~8 MB，base64 后约 10 MB：板子只有 512 MB 内存，
  // 解码 + 缩放 + 定位时会内存吃紧，实测会直接把连接掐断（浏览器报 Failed to fetch）。
  // 算法端本来就会把照片降到 1200 再定位、裁剪成 700x605，所以 1600 足够，画质无损。
  return new Promise(function (resolve, reject) {
    var img = new Image();
    var url = URL.createObjectURL(f);
    img.onload = function () {
      try {
        var s = Math.min(1.0, maxSide / Math.max(img.width, img.height));
        var w = Math.round(img.width * s), h = Math.round(img.height * s);
        var c = document.createElement("canvas");
        c.width = w; c.height = h;
        c.getContext("2d").drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(url);
        resolve({ b64: c.toDataURL("image/jpeg", 0.92).split(",")[1],
                  info: w + "x" + h + "（原图 " + img.width + "x" + img.height + "）" });
      } catch (e) { reject(e); }
    };
    img.onerror = function () { URL.revokeObjectURL(url); reject(new Error("无法解码这张图片")); };
    img.src = url;
  });
}
function toB64Raw(f) {
  return new Promise(function (resolve) {
    var r = new FileReader();
    r.onload = function () { resolve({ b64: r.result.split(",")[1], info: "原图未压缩" }); };
    r.readAsDataURL(f);
  });
}
// 渲染结果。ovSrc / mkSrc 分开传：手机端拿到的是 base64，电脑观看页拿到的是图片 URL。
function render(d, ovSrc, mkSrc, tail) {
  document.getElementById("ov").src = ovSrc;
  document.getElementById("mk").src = mkSrc;
  var tb = document.getElementById("tb");
  tb.innerHTML = "<tr><th>步骤</th><th>纯软件 ms</th><th>混合硬件 ms</th></tr>";
  var keys = ["绿通道", "CLAHE", "核心", "归一化", "Otsu", "删小块", "FOV清零"];
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var row = tb.insertRow(-1);
    row.insertCell(0).textContent = k;
    row.insertCell(1).textContent = d.t_sw[k].toFixed(2);
    row.insertCell(2).textContent = d.t_hw[k].toFixed(2);
  }
  var tim = "";
  if (d.server_ms !== undefined) {
    tim = "<br><span style='font-size:13px;color:#666'>服务端 " + d.server_ms.toFixed(0) +
          " ms（上传处理·含解码/定位/裁剪 " + d.loc_ms.toFixed(0) + " / 图片编码 " + d.enc_ms.toFixed(0) +
          "） ｜ 上传端准备 " + (d.client_ms || 0).toFixed(0) + " ms</span>";
  }
  document.getElementById("sum").innerHTML =
    "端到端合计：软件 " + d.e2e_sw.toFixed(1) + " ms → 硬件 " + d.e2e_hw.toFixed(1) +
    " ms，加速 <span class='big'>" + d.speedup.toFixed(2) + "×</span>" +
    " ｜ 核心算子 " + d.core_sw.toFixed(1) + " → " + d.core_hw.toFixed(1) +
    " ms（<span class='big'>" + d.core_speedup.toFixed(1) + "×</span>）" +
    " ｜ 两种结果一致性：<span class='ok'>" + d.mask_bad + " 像素不一致</span>" +
    " ｜ 血管像素 " + d.mask_pixels +
    "<br>核心算子（吞吐口径·预置数据只计时传输段）：" + d.core_sw.toFixed(1) + " → " +
    d.core_hw_thr.toFixed(3) + " ms（<span class='big'>" + d.core_speedup_thr.toFixed(1) + "×</span>）" +
    " ｜ 等效帧率 " + d.fps_thr.toFixed(1) + " fps" + tim +
    "<br><span style='font-size:13px;color:#666'>两种口径差别：延迟口径含 ARM 搬数据（约多 " +
    (d.core_hw - d.core_hw_thr).toFixed(1) + " ms），吞吐口径只算 IP 传输段 —— PPT 报的 37~38× 即吞吐口径。</span>";
  document.getElementById("msg").innerHTML = tail;
}

function tailText(d) {
  var loc = "";
  if (d.located) {
    loc = " ｜ 自动定位：半径 " + d.loc_r.toFixed(0) + " px";
  } else if (d.src && d.src.indexOf("上传") === 0) {
    loc = " ｜ <span style='color:#c00'>未定位到眼底圆</span>";
  }
  return "完成。输入: " + d.name + "（" + d.src + "）" + loc;
}

// 观看模式（电脑大屏）：不上传，只轮询板子上最近一次结果。
var VIEW = location.search.indexOf("view=1") >= 0;
var lastTs = 0;
async function poll(force) {
  var msg = document.getElementById("msg");
  try {
    var r = await fetch("/latest");
    var d = await r.json();
    if (d.empty) { msg.textContent = "自动跟随中：板上还没有结果…"; return; }
    if (!force && d.ts === lastTs) { return; }
    lastTs = d.ts;
    render(d, "/latest_ov?ts=" + d.ts, "/latest_mk?ts=" + d.ts, tailText(d));
  } catch (e) {
    msg.textContent = "轮询失败: " + e;
  }
}

async function run() {
  var btn = document.getElementById("go");
  var msg = document.getElementById("msg");
  var t0 = performance.now();
  var sel = document.getElementById("imgsel").value;
  var payload = {};
  if (sel === "upload") {
    var f = document.getElementById("file").files[0];
    if (!f) { msg.textContent = "请先选择要上传的图片文件。"; return; }
    var got;
    try {
      got = await toB64(f, 1600);
    } catch (e) {
      msg.textContent = "手机端压缩失败（" + e.message + "），改为直传原图…";
      got = await toB64Raw(f);
    }
    payload.data_b64 = got.b64;
    payload.name = f.name;
    payload.autoloc = document.getElementById("autoloc").checked;
    payload.upload_info = got.info;
  } else {
    payload.name = sel;
  }
  payload.client_ms = performance.now() - t0;   // 手机端解码 + 压缩耗时
  btn.disabled = true;
  var mb = payload.data_b64 ? (payload.data_b64.length * 0.75 / 1048576) : 0;
  msg.textContent = "处理中（上传 " + mb.toFixed(2) + " MB，两种后端各跑一遍）…";
  try {
    var r = await fetch("/process", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
    var d = await r.json();
    if (!d.ok) { msg.textContent = "出错: " + d.error; btn.disabled = false; return; }
    render(d, "data:image/jpeg;base64," + d.overlay_b64,
              "data:image/png;base64," + d.mask_b64, tailText(d));
  } catch (e) {
    msg.textContent = "请求失败: " + e;
  }
  btn.disabled = false;
}
document.getElementById("imgsel").onchange = function () {
  var f = document.getElementById("file");
  var sel = document.getElementById("imgsel").value;
  var isUp = (sel === "upload");
  f.style.display = isUp ? "block" : "none";
  document.getElementById("labAuto").style.display = isUp ? "block" : "none";
  if (!isUp) { document.getElementById("ov").src = "preview?name=" + sel; }
};
// 「自动跟随」开关：勾上时每 1.5 s 拉一次板上的最新结果。
// 电脑自己上传处理时不用勾；手机拍、电脑看时勾上（或用 ?view=1 打开，默认勾上）。
// 注意：这里刻意不隐藏任何控件 —— 电脑端「直接上传图片并处理」的功能必须保留。
var followT = null;
function syncFollow() {
  var on = document.getElementById("follow").checked;
  if (on && followT === null) {
    document.getElementById("msg").textContent = "自动跟随中：等待新结果…";
    poll(true);
    followT = setInterval(function () { poll(false); }, 1500);
  } else if (!on && followT !== null) {
    clearInterval(followT);
    followT = null;
  }
}
document.getElementById("follow").onchange = syncFollow;
if (VIEW) {
  document.getElementById("follow").checked = true;
  syncFollow();
}
</script>
</body>
</html>
"""

# 标签按「图名」绑定，绝不用排序位置（sorted 会把 im0002 排在 im0082 前面）
_DIFF = {"im0082": "基准图", "im0002": "困难图"}
_NAMES = sorted(BUILTIN.keys())
PAGE = PAGE.replace("__NAME0__", _NAMES[0])
PAGE = PAGE.replace("__NAME1__", _NAMES[1] if len(_NAMES) > 1 else _NAMES[0])
for _i, _n in enumerate((_NAMES[0], _NAMES[1] if len(_NAMES) > 1 else _NAMES[0])):
    PAGE = PAGE.replace("__OPT%d__" % _i, "%s（板上内置 · %s）" % (_n, _DIFF.get(_n, "内置图")))


class Handler(BaseHTTPRequestHandler):
    # 最近一次处理结果。手机上传 -> 板子算完 -> 电脑打开 ?view=1 轮询这个缓存，
    # 于是「手机当相机、电脑当大屏」不需要任何第三方 App 或文件中转。
    LAST = {"ts": 0.0, "resp": None}

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bytes(self, b, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/preview":
            q = urllib.parse.parse_qs(u.query)
            nm = (q.get("name") or [None])[0]
            if nm in BUILTIN:
                rgb = load_rgb(nm)
                ok, buf = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                self._bytes(buf.tobytes(), "image/png")
                return
        # 电脑端观看页（?view=1）轮询用：先取元信息，ts 变了再取图，避免反复传大图
        if u.path == "/latest":
            r = Handler.LAST["resp"]
            if r is None:
                self._json({"ok": True, "empty": True, "ts": 0.0}, 200)
                return
            meta = {k: v for k, v in r.items() if k not in ("overlay_b64", "mask_b64")}
            meta["ok"] = True
            meta["empty"] = False
            meta["ts"] = Handler.LAST["ts"]
            self._json(meta, 200)
            return
        if u.path == "/latest_ov":
            r = Handler.LAST["resp"]
            if r is None:
                self._json({"ok": False, "error": "还没有结果"}, 200)
                return
            self._bytes(base64.b64decode(r["overlay_b64"]), "image/jpeg")
            return
        if u.path == "/latest_mk":
            r = Handler.LAST["resp"]
            if r is None:
                self._json({"ok": False, "error": "还没有结果"}, 200)
                return
            self._bytes(base64.b64decode(r["mask_b64"]), "image/png")
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/process":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            t_req = time.perf_counter()
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            nm = req.get("name") or "上传图"
            b64 = req.get("data_b64")
            circ, loc = None, None
            loc_ms = 0.0
            if b64:
                _t = time.perf_counter()
                rgb, fov_ov, circ, loc = rgb_from_bytes(
                    base64.b64decode(b64), autoloc=bool(req.get("autoloc", True)))
                loc_ms = (time.perf_counter() - _t) * 1000.0
                src = "上传·已定位眼底圆" if fov_ov is not None else "上传·整图缩放"
                if not AUTOLOC_OK:
                    src = "上传·整图缩放(缺 fundus_autoloc.py)"
            else:
                if nm not in BUILTIN:
                    raise RuntimeError("未知图像: %s" % nm)
                rgb = load_rgb(nm)
                src = "板上内置"
                fov_ov = None
            if fov_ov is not None:
                fov = fov_ov
                fov_ms = 0.0                      # 圆形 FOV 由定位结果直接给出，不额外耗时
            else:
                key = base64.b64encode(rgb.tobytes()).decode("ascii")[:64]
                if key not in FOV_CACHE:
                    t0 = time.perf_counter()
                    FOV_CACHE[key] = gen_fov(rgb)
                    fov_ms = (time.perf_counter() - t0) * 1000.0
                else:
                    fov_ms = 0.0
                fov = FOV_CACHE[key]
            m_sw, t_sw = run_pipeline(rgb, False, fov)
            m_hw, t_hw = run_pipeline(rgb, True, fov)
            e2e_sw = float(sum(t_sw.values()))
            e2e_hw = float(sum(t_hw.values()))
            # 吞吐口径（与答辩 PPT 的 37.x 完全同口径）：预置数据、只计时传输段
            cl_now = CLAHE.apply(rgb[:, :, 1])
            _t = time.perf_counter()
            with HW_LOCK:
                core_thr, _thr_img = hw_throughput(cl_now, reps=5)
            thr_ms = (time.perf_counter() - _t) * 1000.0
            # 叠加图走 JPEG（PNG 约 600 KB，JPEG 约 60 KB，ARM 编码也更快）
            _t = time.perf_counter()
            ov_b64 = jpg_b64_rgb(make_overlay(rgb, m_hw))
            mk_b64 = png_b64_rgb(cv2.cvtColor(m_hw, cv2.COLOR_GRAY2RGB))
            enc_ms = (time.perf_counter() - _t) * 1000.0
            resp = {
                "ok": True,
                "name": nm,
                "src": src,
                "t_sw": t_sw,
                "t_hw": t_hw,
                "e2e_sw": e2e_sw,
                "e2e_hw": e2e_hw,
                "speedup": e2e_sw / e2e_hw,
                "core_sw": t_sw["核心"],
                "core_hw": t_hw["核心"],
                "core_speedup": t_sw["核心"] / t_hw["核心"],
                "core_hw_thr": core_thr,
                "core_speedup_thr": t_sw["核心"] / core_thr,
                "fps_thr": 1000.0 / core_thr,
                "mask_bad": int(np.count_nonzero(m_sw != m_hw)),
                "mask_pixels": int(np.count_nonzero(m_hw)),
                "fov_ms": fov_ms,
                "located": circ is not None,
                "loc_r": (float(circ[2]) if circ else 0.0),
                "loc_ar": (float(loc.get('ecc', 0.0)) if loc else 0.0),
                "overlay_b64": ov_b64,
                "mask_b64": mk_b64,
            }
            server_ms = (time.perf_counter() - t_req) * 1000.0
            client_ms = float(req.get("client_ms") or 0.0)
            resp["server_ms"] = server_ms
            resp["loc_ms"] = loc_ms
            resp["enc_ms"] = enc_ms
            resp["client_ms"] = client_ms
            Handler.LAST["ts"] = time.time()
            Handler.LAST["resp"] = resp
            print("    [请求] %s | 手机端 %.0f ms | 服务端合计 %.0f ms"
                  % (nm, client_ms, server_ms))
            print("      细分: 上传处理 %.0f（含解码/定位/裁剪，见上一行）+ FOV %.0f + 软件 %.0f"
                  " + 硬件 %.0f + 吞吐测量 %.0f + 图片编码 %.0f"
                  % (loc_ms, fov_ms, e2e_sw, e2e_hw, thr_ms, enc_ms))
            self._json(resp)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 200)


# ==================== [4] 启动 ====================
def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.2.1", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.2.99"


# 重复执行本脚本时，先尽力关掉本进程里上一次启动的服务实例，释放端口
_old = globals().get("srv")
if _old is not None:
    try:
        _old.shutdown()
        _old.server_close()
        print("[4] 已关闭上一次启动的服务实例（端口释放）")
    except Exception:
        pass

# 端口被占用时自动顺延（7860, 7861, ... 最多试 10 个）
srv = None
for _p in range(PORT, PORT + 10):
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", _p), Handler)
        PORT = _p
        break
    except OSError:
        continue
if srv is None:
    raise RuntimeError("7860~7869 端口全被占用，请先停掉占用端口的服务")

th = threading.Thread(target=srv.serve_forever, daemon=True)
th.start()
print()
if PORT != 7860:
    print("[4] 注意：7860 已被占用，本服务自动改用 %d 端口" % PORT)
print("[4] 网页服务已启动（后台线程，notebook 可继续用）")
print("    浏览器打开:  http://%s:%d" % (get_ip(), PORT))
print("    停止方法: 重启 notebook 内核，或运行 srv.shutdown()")
print("=" * 66)
