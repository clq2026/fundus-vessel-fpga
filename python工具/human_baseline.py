# -*- coding: utf-8 -*-
"""
人类专家基线（Human Baseline）—— STARE 双专家标注一致性

用途
----
计算 STARE 数据集两位专家标注（ah 与 vk）之间的 Dice 一致性，作为"人类上限"参考。
答辩时若被问"你的精度和人类比如何"，可用本地实测数字回答，而不必引用文献。

只读脚本：只读数据集标注，不读不写 results/ 下的任何已有产物。
产物：results/human_baseline.md、results/human_baseline.csv

用法
----
python "python工具/human_baseline.py"
"""
import os
import sys
import csv
import glob
import statistics

import numpy as np
import cv2

# ---------- 路径配置 ----------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "STARE 官方项目数据")
AH_DIR = os.path.join(BASE, "标注-ah")
VK_DIR = os.path.join(BASE, "标注-vk")
OUT_MD = os.path.join(ROOT, "results", "human_baseline.md")
OUT_CSV = os.path.join(ROOT, "results", "human_baseline.csv")

# 本项目的数据切分：前 10 张开发集调参，后 10 张评价集固定不动
DEV_IDS = {"im0001", "im0002", "im0003", "im0004", "im0005",
           "im0044", "im0077", "im0081", "im0082", "im0139"}


def imread_unicode(path):
    """cv2.imread 在 Windows 下无法处理非 ASCII 路径，改用字节流解码。"""
    with open(path, "rb") as f:
        buf = np.frombuffer(f.read(), np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise IOError("无法解码图像: %s" % path)
    return img


def dice_of(a_bool, b_bool):
    """两个布尔掩膜的 Dice 系数。"""
    tp = int(np.logical_and(a_bool, b_bool).sum())
    fp = int(np.logical_and(b_bool, ~a_bool).sum())
    fn = int(np.logical_and(~b_bool, a_bool).sum())
    denom = 2 * tp + fp + fn
    return (2.0 * tp / denom) if denom else float("nan"), tp, fp, fn


def main():
    if not os.path.isdir(AH_DIR) or not os.path.isdir(VK_DIR):
        print("[错误] 未找到双专家标注目录：")
        print("       %s" % AH_DIR)
        print("       %s" % VK_DIR)
        return 1

    files = sorted(glob.glob(os.path.join(AH_DIR, "*.ppm")))
    if not files:
        print("[错误] %s 下没有 .ppm 标注文件" % AH_DIR)
        return 1

    rows = []
    for p in files:
        img_id = os.path.basename(p).split(".")[0]
        pa = os.path.join(AH_DIR, "%s.ah.ppm" % img_id)
        pv = os.path.join(VK_DIR, "%s.vk.ppm" % img_id)
        if not (os.path.isfile(pa) and os.path.isfile(pv)):
            print("[跳过] %s：缺少 ah 或 vk 标注" % img_id)
            continue
        A = imread_unicode(pa) > 127
        V = imread_unicode(pv) > 127
        if A.shape != V.shape:
            print("[跳过] %s：尺寸不一致 %s vs %s" % (img_id, A.shape, V.shape))
            continue
        d, tp, fp, fn = dice_of(A, V)
        rows.append({
            "img_id": img_id,
            "split": "dev" if img_id in DEV_IDS else "eval",
            "dice": d,
            "ah_pct": A.mean() * 100.0,
            "vk_pct": V.mean() * 100.0,
            "tp": tp, "fp": fp, "fn": fn,
        })

    if not rows:
        print("[错误] 没有任何有效配对")
        return 1

    def mean(vals):
        return statistics.mean(vals) if vals else float("nan")

    all_d = [r["dice"] for r in rows]
    dev_d = [r["dice"] for r in rows if r["split"] == "dev"]
    ev_d = [r["dice"] for r in rows if r["split"] == "eval"]

    # ---------- 落盘 CSV ----------
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["img_id", "split", "dice", "ah_pct", "vk_pct", "tp", "fp", "fn"])
        w.writeheader()
        for r in rows:
            w.writerow({"img_id": r["img_id"], "split": r["split"],
                        "dice": "%.6f" % r["dice"],
                        "ah_pct": "%.4f" % r["ah_pct"], "vk_pct": "%.4f" % r["vk_pct"],
                        "tp": r["tp"], "fp": r["fp"], "fn": r["fn"]})

    # ---------- 落盘 Markdown ----------
    lines = []
    lines.append("# STARE 人类专家基线（双专家一致性）")
    lines.append("")
    lines.append("> 由 `python工具/human_baseline.py` 从 STARE 官方双专家标注只读重算生成。")
    lines.append("> 用途：为算法指标提供「人类上限」参考——两位专家之间的分歧本身就构成了该数据集的天花板。")
    lines.append("")
    lines.append("**标注者**：`ah`（本项目作为 GT 使用） vs `vk`（独立第二位专家）")
    lines.append("")
    lines.append("## 表 1　汇总")
    lines.append("")
    lines.append("| 子集 | 图数 | Dice（宏平均） |")
    lines.append("|---|---|---|")
    lines.append("| 开发集（前 10 张） | %d | %.4f |" % (len(dev_d), mean(dev_d)))
    lines.append("| 评价集（后 10 张） | %d | %.4f |" % (len(ev_d), mean(ev_d)))
    lines.append("| 全 20 张 | %d | %.4f |" % (len(all_d), mean(all_d)))
    lines.append("")
    lines.append("## 表 2　逐图明细")
    lines.append("")
    lines.append("| 图像 | 子集 | Dice(ah vs vk) | ah 血管占比 | vk 血管占比 |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        lines.append("| %s | %s | %.4f | %.2f%% | %.2f%% |"
                     % (r["img_id"], r["split"], r["dice"], r["ah_pct"], r["vk_pct"]))
    lines.append("")
    vk_hi = sum(1 for r in rows if r["vk_pct"] > r["ah_pct"])
    lines.append("## 说明")
    lines.append("")
    lines.append("- %d/%d 张图中 `vk` 标注的血管面积**大于** `ah`，说明 `vk` 倾向于标出更多细小血管；"
                 % (vk_hi, len(rows)))
    lines.append("  因此用 `ah` 作 GT 去衡量 `vk`，也会得到明显小于 1 的 Dice。")
    lines.append("- 该结果应作为**任何算法在该数据集上的实用上限参考**：超过它的部分，多属于对某一位专家个人习惯的拟合。")
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ---------- 屏幕输出 ----------
    print("=" * 56)
    print("STARE 人类专家一致性（ah vs vk）")
    print("=" * 56)
    print("  开发集 10 张 : Dice = %.4f" % mean(dev_d))
    print("  评价集 10 张 : Dice = %.4f" % mean(ev_d))
    print("  全 20 张     : Dice = %.4f" % mean(all_d))
    print()
    print("  逐图 Dice:")
    for r in rows:
        print("    %-8s [%s] %.4f   ah=%.2f%%  vk=%.2f%%"
              % (r["img_id"], r["split"], r["dice"], r["ah_pct"], r["vk_pct"]))
    print()
    print("已写出：%s" % OUT_MD)
    print("已写出：%s" % OUT_CSV)
    return 0


if __name__ == "__main__":
    sys.exit(main())
