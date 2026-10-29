# -*- coding: utf-8 -*-
"""
FOV 边界平滑优化验证（只读，不写任何结果文件）

目的
----
generate_fov_mask() 里对 FOV 做了「50 次 3x3 闭运算」做边界平滑。
形态学闭运算满足幂等性  phi(phi(X)) == phi(X)，
所以 50 次与 1 次应当逐像素完全相同。

本脚本在全部 20 张 STARE 血管标注图上验证：
  N=0（不平滑） vs N=50   -> 闭运算到底改变了多少像素
  N=1          vs N=50   -> 预期 0（幂等）
  N=2          vs N=50   -> 预期 0

同时给出各段耗时，用于估算优化收益。

用法
----
    python python工具/verify_fov_fast.py
"""
import os
import sys
import time

import numpy as np
import cv2

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data_io import STARE_VESSEL_IDS, load_stare_image  # noqa: E402
from src.pipeline import generate_fov_mask  # noqa: E402

DATA_DIR = os.path.join(PROJECT_ROOT, "STARE 官方项目数据")
K3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

N_FULL = 50          # 原代码里的迭代次数
N_SNAP = [0, 1, 2]   # 需要对比的快照


def fov_pre(img):
    """FOV 前处理三段：Lab / Otsu / 最大连通域。返回 (fov, 各段耗时 ms)"""
    t0 = time.perf_counter()
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0]
    t_lab = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    _, binary = cv2.threshold(L, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    t_otsu = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )
    if num_labels > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        largest = int(np.argmax(areas)) + 1
        fov = (labels == largest).astype(np.uint8) * 255
    else:
        fov = binary
    t_cc = (time.perf_counter() - t0) * 1000.0
    return fov, (t_lab, t_otsu, t_cc)


def fov_close(fov, n):
    """连续做 n 次 3x3 闭运算，返回 (结果, 耗时 ms)"""
    t0 = time.perf_counter()
    for _ in range(n):
        fov = cv2.morphologyEx(fov, cv2.MORPH_CLOSE, K3)
    return fov, (time.perf_counter() - t0) * 1000.0


def main():
    print("=" * 74)
    print("FOV 边界平滑：幂等性验证（全部 20 张 STARE 图）")
    print("=" * 74)
    print(f"数据目录: {DATA_DIR}")
    print("形态学闭运算幂等性: phi(phi(X)) == phi(X)  ->  预期 diff = 0")
    print()

    t_lab_all = t_otsu_all = t_cc_all = 0.0
    t50_all = 0.0
    t_snap_all = {n: 0.0 for n in N_SNAP}
    t_one_sum = 0.0

    print(f"{'图像':<9}{'基准vs50':>10}{'1次vs50':>10}{'2次vs50':>10}{'改后vs50':>10}"
          f"{'1次耗时ms':>12}{'50次耗时ms':>12}")
    print("-" * 86)

    worst = {}
    ok_all = True

    for img_id in STARE_VESSEL_IDS:
        img = load_stare_image(DATA_DIR, img_id)
        base, (t_lab, t_otsu, t_cc) = fov_pre(img)
        t_lab_all += t_lab
        t_otsu_all += t_otsu
        t_cc_all += t_cc

        snaps = {}
        for n in N_SNAP:
            res, dt = fov_close(base.copy(), n)
            snaps[n] = res
            t_snap_all[n] += dt
            if n == 1:
                t_one_sum += dt

        full, dt50 = fov_close(base.copy(), N_FULL)
        t50_all += dt50

        # 直接调用 src/pipeline.py 里改好后的 generate_fov_mask()，
        # 与「50 次闭运算」的老行为逐像素对照 —— 这是改动的最终验收
        patched = generate_fov_mask(img)
        d_patched = int(np.count_nonzero(patched != full))
        if d_patched != 0:
            ok_all = False
            ys, xs = np.nonzero(patched != full)
            worst[img_id] = list(zip(ys[:20].tolist(), xs[:20].tolist()))

        diffs = {}
        for n in N_SNAP:
            d = int(np.count_nonzero(snaps[n] != full))
            diffs[n] = d
            if n >= 1 and d != 0:
                ok_all = False
                ys, xs = np.nonzero(snaps[n] != full)
                worst[img_id] = list(zip(ys[:20].tolist(), xs[:20].tolist()))

        print(f"{img_id:<9}{diffs[0]:>10}{diffs[1]:>10}{diffs[2]:>10}{d_patched:>10}"
              f"{t_snap_all[1] / (STARE_VESSEL_IDS.index(img_id) + 1):>12.1f}"
              f"{(t50_all / (STARE_VESSEL_IDS.index(img_id) + 1)):>12.1f}")

    n_img = len(STARE_VESSEL_IDS)
    print("-" * 86)
    stage_pre = t_lab_all + t_otsu_all + t_cc_all
    total_old = stage_pre + t50_all
    total_new = stage_pre + t_snap_all[1]

    print()
    print("【耗时汇总 / 20 张】")
    print(f"  Lab 转色        : {t_lab_all:9.1f} ms  (单张 {t_lab_all / n_img:6.2f} ms)")
    print(f"  Otsu 阈值       : {t_otsu_all:9.1f} ms  (单张 {t_otsu_all / n_img:6.2f} ms)")
    print(f"  最大连通域      : {t_cc_all:9.1f} ms  (单张 {t_cc_all / n_img:6.2f} ms)")
    print(f"  闭运算 x50      : {t50_all:9.1f} ms  (单张 {t50_all / n_img:6.2f} ms)")
    print(f"  闭运算 x1       : {t_snap_all[1]:9.1f} ms  (单张 {t_snap_all[1] / n_img:6.2f} ms)")
    print()
    print(f"  优化前 FOV 单张 : {total_old / n_img:8.2f} ms")
    print(f"  优化后 FOV 单张 : {total_new / n_img:8.2f} ms")
    print(f"  单张节省        : {(total_old - total_new) / n_img:8.2f} ms"
          f"   加速比 {total_old / max(total_new, 1e-9):.1f}x")
    print()
    print("【结论】")
    if ok_all:
        print("  [PASS] 全部 20 张图：1 次闭运算 == 2 次 == 50 次，逐像素 0 差异。")
        print("  [PASS] src/pipeline.py 改后 generate_fov_mask() == 旧的 50 次循环，0 差异。")
        print("         -> 纯性能优化，输出完全不变；Dice 等指标无需重算。")
    else:
        print("  [FAIL] 存在差异图像（说明闭运算在此实现下非严格幂等），详列如下：")
        for k, v in worst.items():
            print(f"         {k}: 前 20 个差异坐标 {v}")
    print("=" * 74)


if __name__ == "__main__":
    main()
