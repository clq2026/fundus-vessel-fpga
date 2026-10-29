# ============================================================
# 43-B  im0002 困难图回归 —— PYNQ 板端一键 cell
#
# 目的：在「最难的一张图」上再验一次
#       「硬件核心 vs 软件核心」是否仍然逐像素完全一致，
#       证明此前 im0082 上的 0 差异不是侥幸。
#
# 为什么挑 im0002：
#   results/metrics_summary.csv 里 STARE V2 按 Dice 升序，im0002 = 0.5564 垫底
#   （最容易的 im0163 = 0.8267；板上已有的 im0082 = 0.7923）。
#   所以它是「Dice 最低的那张」，拿它回归最有说服力。
#
# 用法：把本文件整段粘进 notebook 的一个新 cell 里运行。
#       前提：命名空间里已有 cell #16 定义的 gen_fov / e2e。
#             若提示缺少定义，先运行一次 cell #16 再回来。
#
# 说明：本脚本只读板上数据、只跑计算，不写任何文件、不改任何 notebook。
#
# 已完成核对（2026-10-05）：
#   - 板上 im0002_rgb.bin 字节数 1270500 = 605*700*3
#   - MD5 = 1c8fcab7df7b76de5e4b8032950d57ae（RGB 序）
#   - e2e(use_hw, blob=blobs_vec) 返回 **4** 个值：(m, raw, t, 耗时ms)
# ============================================================
import glob
import time

import numpy as np

print("=" * 58)
print("43-B  im0002 困难图回归：硬件 vs 软件 逐像素一致性")
print("=" * 58)

missing = [k for k in ("gen_fov", "e2e") if k not in globals()]
if missing:
    print("!! 命名空间缺少定义:", missing)
    print("   -> 先运行一次 cell #16（加载全部函数定义与 Overlay），再重跑本 cell")
else:
    cand = sorted(glob.glob("/home/xilinx/**/im0002_rgb.bin", recursive=True))
    print("im0002_rgb.bin 候选:", cand)

    if not cand:
        print("!! 没找到 im0002_rgb.bin，请先上传到 vessel_s11/ 目录")
    else:
        path = cand[-1]
        buf = open(path, "rb").read()
        print("用图:", path, "  字节:", len(buf))

        rgb = np.frombuffer(buf, np.uint8).reshape(605, 700, 3)
        print("rgb:", rgb.shape, rgb.dtype)

        t0 = time.perf_counter()
        fov = gen_fov(rgb)
        t_fov = (time.perf_counter() - t0) * 1000.0
        print("im0002 FOV 生成 = %.2f ms" % t_fov)

        print("预热中 ...")
        e2e(False)
        e2e(True)

        # e2e 返回 4 个值：(掩膜, 核心原始输出, 分段耗时字典, 整段墙钟 ms)
        m_sw, r_sw, t_sw, e_sw = e2e(False)
        m_hw, r_hw, t_hw, e_hw = e2e(True)

        n = int(m_sw.size)
        d_mask = int(np.count_nonzero(m_sw != m_hw))
        d_raw = int(np.count_nonzero(r_sw != r_hw))
        max_d = int(np.abs(r_sw.astype(np.int16) - r_hw.astype(np.int16)).max())
        c_sw = t_sw["core"]
        c_hw = t_hw["core"]

        print()
        print("核心耗时:  sw %.2f ms  |  hw %.2f ms  |  %.1fx"
              % (c_sw, c_hw, c_sw / max(c_hw, 1e-9)))
        print("端到端:    sw %.2f ms  |  hw %.2f ms  |  %.2fx"
              % (e_sw, e_hw, e_sw / max(e_hw, 1e-9)))
        print("掩膜不一致像素 = %d / %d" % (d_mask, n))
        print("黑帽不一致像素 = %d / %d   最大差 = %d" % (d_raw, n, max_d))
        if d_mask == 0 and d_raw == 0:
            print("[PASS] im0002 硬件与软件逐像素一致")
        else:
            print("[FAIL] 有差异！把上面全部输出发我")
