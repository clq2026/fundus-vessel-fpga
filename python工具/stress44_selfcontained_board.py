"""
44-1 自包含压测 v2（板端 notebook 单元）

【相比 v1 的唯一关键修正】
  判定「通过」的标准从「没抛异常」改为「**与软件逐像素一致**」。
  v1 的教训：一次被中止的传输会永久污染 HLS IP 内部流式状态（见技能 pynq-axi-dma-driver 坑 11），
  此后每帧都"跑得动、时序正常、但数据全错"。只看异常会把这个状态误判为健康，
  于是跳过重载步骤、把整轮压测跑在坏状态上（实测：脚本因此漏掉了重载）。

【流程】
  [1] 清掉命名空间里全部旧 Overlay/MMIO/Buffer -> gc
  [2] 唯一名重下 bit + 新建 MMIO/Buffer（重载与重建绑定，见坑 10）
  [3] 单帧校验：hw vs sw 必须 0 差异；不是 0 就停，不做无意义的压测
  [4] 连跑 200 帧：耗时统计 + 逐帧 vs 参考帧 + 逐帧 vs 上一帧（区分"固定偏移"与"随机乱码"）
  [5] 跑完再单帧复核一次，确认 IP 在整轮连跑后仍然正确

【用法】板端 notebook 新建 cell，整段粘贴运行。正文零反斜杠。
"""

import datetime
import gc
import os
import shutil
import time

import cv2
import numpy as np
from pynq import MMIO, Overlay, allocate

D = "/home/xilinx/jupyter_notebooks/vessel_s11"
H, W = 605, 700
N = H * W


def hw_core(gray, tmo=2.0):
    np.copyto(buf_in, gray.reshape(-1))
    buf_in.flush()
    buf_out.flush()
    for cr in (0x00, 0x30):
        dma.write(cr, 0x4)
        t = time.perf_counter()
        while (dma.read(cr) & 0x4) and time.perf_counter() - t < 1.0:
            pass
    dma.write(0x00, 0x1001)
    dma.write(0x30, 0x1001)
    ves.write(0x00, 0x01)
    dma.write(0x18, buf_in.physical_address)
    dma.write(0x48, buf_out.physical_address)
    dma.write(0x28, N)
    dma.write(0x58, N)
    t = time.perf_counter()
    while True:
        if (dma.read(0x04) & 0x02) and (dma.read(0x34) & 0x02):
            break
        if time.perf_counter() - t > tmo:
            raise RuntimeError("硬件超时")
    buf_out.invalidate()
    return np.array(buf_out).reshape(H, W).copy()


def sw_core(gray):
    med = cv2.medianBlur(gray, 3)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    dil = cv2.dilate(med, k, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    clo = cv2.erode(dil, k, borderType=cv2.BORDER_CONSTANT, borderValue=255)
    return cv2.subtract(clo, med)


print("=" * 68)
print("44-1 压测 v2：重载 -> 数据校验 -> 200 帧连跑 -> 复核")
print("=" * 68)
print("时间:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

# ==================== [1] 清旧对象 ====================
HWK = ("Overlay", "MMIO", "ContiguousArray", "Buffer")
gone = [k for k, v in list(globals().items()) if type(v).__name__ in HWK]
for k in gone:
    globals().pop(k, None)
gc.collect()
print("[1] 已清掉旧硬件对象 %d 个: %s" % (len(gone), gone))

# ==================== [2] 重下 bit + 重建全部 ====================
tag = datetime.datetime.now().strftime("%H%M%S")
bp = os.path.join(D, "stress_%s.bit" % tag)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.bit"), bp)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.hwh"), os.path.join(D, "stress_%s.hwh" % tag))
ol = Overlay(bp)
dma = MMIO(0x41E00000, 0x1000)
ves = MMIO(0x40000000, 0x1000)
buf_in = allocate(shape=(N,), dtype=np.uint8)
buf_out = allocate(shape=(N,), dtype=np.uint8)
print("[2] 重下 bit:", os.path.basename(bp))
print("    IP:", list(ol.ip_dict.keys()))
print("    buf_in phys=0x%08X   buf_out phys=0x%08X"
      % (buf_in.physical_address, buf_out.physical_address))
print("    ap_ctrl=0x%X  MM2S_SR=0x%08X  S2MM_SR=0x%08X"
      % (ves.read(0x00), dma.read(0x04), dma.read(0x34)))

# ==================== [3] 输入 + 单帧数据校验 ====================
rgb2 = np.frombuffer(open(os.path.join(D, "im0002_rgb.bin"), "rb").read(), np.uint8).reshape(H, W, 3)
cl = cv2.createCLAHE(2.0, (8, 8)).apply(rgb2[:, :, 1])
sw = sw_core(cl)
print()
print("[3] 单帧数据校验（输入 im0002）...")
ok = False
try:
    t0 = time.perf_counter()
    got = hw_core(cl)
    ms0 = (time.perf_counter() - t0) * 1000.0
    d0 = np.abs(got.astype(np.int16) - sw.astype(np.int16))
    n0 = int((d0 != 0).sum())
    print("    %.3f ms   与软件不一致=%d/%d  最大差=%d" % (ms0, n0, d0.size, int(d0.max())))
    ok = (n0 == 0)
    if ok:
        print("    [OK] 重载后首帧与软件逐像素一致")
    else:
        print("    [BAD] 重载后数据仍不对 -> 不该在此状态继续压测")
except Exception as e:
    print("    失败:", repr(e))
    print("    MM2S_SR=0x%08X  S2MM_SR=0x%08X  ap_ctrl=0x%X"
          % (dma.read(0x04), dma.read(0x34), ves.read(0x00)))

if not ok:
    print()
    print(">> 停止。请把上面【全部输出】发我（重点看是否连重载都不对了）。")
else:
    # ==================== [4] 200 帧连跑 ====================
    print()
    print("[4] 连跑 200 帧 ...")
    M = 200
    ref = hw_core(cl).copy()
    ts = []
    nx_ref = 0
    nx_prev = 0
    prev = ref
    fail = None
    for i in range(M):
        t0 = time.perf_counter()
        try:
            out = hw_core(cl)
        except Exception as e:
            fail = (i, repr(e))
            break
        ts.append((time.perf_counter() - t0) * 1000.0)
        if not np.array_equal(out, ref):
            nx_ref += 1
        if not np.array_equal(out, prev):
            nx_prev += 1
        prev = out

    print()
    if not ts:
        print("[FAIL] 第一帧就失败:", fail)
    else:
        a = np.asarray(ts)
        k20 = min(20, len(a))
        print("完成帧数          = %d / %d" % (len(a), M))
        print("单帧耗时 ms       : min %.3f  max %.3f  mean %.3f  median %.3f  std %.3f"
              % (a.min(), a.max(), a.mean(), np.median(a), a.std()))
        print("抖动 (max-min)    : %.3f ms  (%.2f%%)"
              % (a.max() - a.min(), 100 * (a.max() - a.min()) / a.mean()))
        print("等效帧率          : %.1f fps" % (1000.0 / a.mean()))
        print("首 20 帧均值      : %.3f ms" % a[:k20].mean())
        print("末 20 帧均值      : %.3f ms" % a[-k20:].mean())
        print("漂移 (末-首)      : %+.3f ms  (%+.1f%%)"
              % (a[-k20:].mean() - a[:k20].mean(),
                 100 * (a[-k20:].mean() - a[:k20].mean()) / a[:k20].mean()))
        print("前 5 帧: %s" % ["%.3f" % x for x in ts[:5]])
        print("后 5 帧: %s" % ["%.3f" % x for x in ts[-5:]])
        print("与参考帧不一致帧数 = %d   (0 = 每帧输出恒定)" % nx_ref)
        print("与上一帧不一致次数 = %d   (0 = 帧间也恒定)" % nx_prev)
    if fail is not None:
        print("!! 第 %d 帧中断: %s" % fail)
        print("   MM2S_SR=0x%08X  S2MM_SR=0x%08X  ap_ctrl=0x%X"
              % (dma.read(0x04), dma.read(0x34), ves.read(0x00)))

    # ==================== [5] 跑完复核 ====================
    print()
    print("[5] 连跑结束后再复核一帧 ...")
    try:
        end = hw_core(cl)
        de = np.abs(end.astype(np.int16) - sw.astype(np.int16))
        ne = int((de != 0).sum())
        print("    与软件不一致=%d/%d  最大差=%d" % (ne, de.size, int(de.max())))
        if ne == 0:
            print("    [OK] 整轮连跑后硬件输出依然正确")
        else:
            print("    [BAD] 连跑过程中被污染了")
    except Exception as e:
        print("    失败:", repr(e))
    print("    结束状态 MM2S_SR=0x%08X  S2MM_SR=0x%08X  ap_ctrl=0x%X"
          % (dma.read(0x04), dma.read(0x34), ves.read(0x00)))

    print()
    if fail is None and nx_ref == 0:
        print("[PASS] %d 帧连续传输：无挂死，输出恒定且与软件逐像素一致" % len(ts))
    else:
        print("[INFO] 有异常，见上面各行")
