"""
指标落盘脚本（只读）—— 从 results/ 已有掩膜重算评价指标并汇总落盘

【为什么不需要重跑流水线】
    pipeline.process() 当时就已经把「算指标要用的三样东西」全部存盘了：
      1. 预测掩膜   results/<DS>_V<N>/masks/<id>_mask.png
                    （= 当时喂给 compute_metrics 的 mask，与 intermediates/<id>/mask_final.png 同源）
      2. 实际视野   results/<DS>_V<N>/intermediates/<id>/fov_mask_used.png
                    （= main.py 里的 fov_used；只有启用 FOV 的版本才有这个文件）
      3. 人工标注   数据集目录里现成（STARE 标注-ah / DRIVE training/1st_manual）
    所以本脚本只「读盘 + 重算」，**不调用 process()，不修改也不删除 results/ 下任何已有文件**。

【本脚本的写操作】仅新增以下 3 个文件（若已存在则覆盖自己）：
    results/metrics_summary.csv          每张图明细（长表，utf-8-sig，Excel 可直接打开）
    results/metrics_summary_grouped.csv  汇总表（全量 + 共同子集）
    results/metrics_summary.md           人读版：汇总表 + 共同子集对照 + 明细

【为什么要出「共同子集」表】
    STARE_V1 / DRIVE_V1 只跑了开发集 10 张；V2、V3 跑了全部 20 张（开发集 + 评价集）。
    三版图集不同，指标不能直接横比。共同子集 = 各版本都跑过的那批图，用它才能同口径对比。

用法：
    python python工具/dump_metrics.py
"""

import csv
import os
import sys
import time
import numpy as np
import cv2

# ---------- 路径与常量（与 main.py 保持一致的可调项） ----------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.evaluate import compute_metrics          # 复用评价模块，不重写逻辑
from src.data_io import load_stare_label, load_drive_label
from src.data_io import imread_unicode            # 中文路径读图

RESULTS_DIR = os.path.join(ROOT, "results")
STARE_DIR = os.path.join(ROOT, "STARE 官方项目数据")
DRIVE_DIR = os.path.join(ROOT, "DRIVE数据集")

ANNOTATOR = "ah"          # STARE 标注者，与 main.py 一致
DRIVE_SPLIT = "training"  # DRIVE 用 training 集（有标注），与 main.py 一致

# 需要扫描的 数据集 × 版本 组合
COMBOS = [
    ("STARE", 1), ("STARE", 2), ("STARE", 3),
    ("DRIVE", 1), ("DRIVE", 2), ("DRIVE", 3),
]

CSV_DETAIL = os.path.join(RESULTS_DIR, "metrics_summary.csv")
CSV_GROUPED = os.path.join(RESULTS_DIR, "metrics_summary_grouped.csv")
MD_OUT = os.path.join(RESULTS_DIR, "metrics_summary.md")


# ------------------------------------------------------------
# 读图 / 读标注
# ------------------------------------------------------------
def read_gray(path):
    """按中文路径安全地读成灰度图；文件不存在返回 None。"""
    if not os.path.exists(path):
        return None
    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    return img


def load_gt(dataset, img_id):
    """读人工标注（复用 data_io，保证与当初跑流水线时完全同一套读取逻辑）。"""
    if dataset == "STARE":
        return load_stare_label(STARE_DIR, img_id, ANNOTATOR)
    return load_drive_label(DRIVE_DIR, DRIVE_SPLIT, img_id)


def list_ids(mask_dir):
    """列出某个版本实际跑过的图 ID（以盘上掩膜文件为准，不靠猜 split）。"""
    if not os.path.isdir(mask_dir):
        return []
    names = [f for f in os.listdir(mask_dir) if f.endswith("_mask.png")]
    return sorted(f[: -len("_mask.png")] for f in names)


# ------------------------------------------------------------
# 主流程：逐图重算指标
# ------------------------------------------------------------
def collect():
    rows = []
    problems = []

    for dataset, ver in COMBOS:
        tag = f"{dataset}_V{ver}"
        mask_dir = os.path.join(RESULTS_DIR, tag, "masks")
        ids = list_ids(mask_dir)
        if not ids:
            problems.append(f"[跳过] {tag}: 没找到掩膜文件（{mask_dir}）")
            continue

        print(f"\n--- {tag} ---  共 {len(ids)} 张")
        for img_id in ids:
            pred = read_gray(os.path.join(mask_dir, f"{img_id}_mask.png"))
            fov = read_gray(
                os.path.join(RESULTS_DIR, tag, "intermediates", img_id, "fov_mask_used.png")
            )

            try:
                gt = load_gt(dataset, img_id)
            except Exception as e:  # 标注缺失/路径异常
                problems.append(f"[失败] {tag}/{img_id}: 读标注出错 {e}")
                continue

            # ---- 一致性检查：尺寸必须完全一致，否则这次比较没有意义 ----
            if pred is None:
                problems.append(f"[失败] {tag}/{img_id}: 掩膜读取失败")
                continue
            if pred.shape != gt.shape:
                problems.append(
                    f"[失败] {tag}/{img_id}: 尺寸不一致 pred{pred.shape} vs gt{gt.shape}"
                )
                continue
            if fov is not None and fov.shape != pred.shape:
                problems.append(
                    f"[失败] {tag}/{img_id}: FOV 尺寸不一致 fov{fov.shape} vs pred{pred.shape}"
                )
                continue

            m = compute_metrics(pred_mask=pred, gt_label=gt, fov_mask=fov)
            total = pred.size
            pred_pct = 100.0 * float(np.count_nonzero(pred > 0)) / total
            gt_pct = 100.0 * float(np.count_nonzero(gt > 0)) / total

            # ---- sanity check：血管占比异常时提示（多半是读错文件/读错通道） ----
            if pred_pct < 0.5 or pred_pct > 40.0:
                problems.append(
                    f"[可疑] {tag}/{img_id}: 预测血管占比 {pred_pct:.2f}% 异常，请人工看一眼掩膜"
                )

            rows.append({
                "dataset": dataset,
                "version": f"V{ver}",
                "tag": tag,
                "img_id": img_id,
                "fov_used": "yes" if fov is not None else "no",
                "tp": m["tp"], "fp": m["fp"], "fn": m["fn"], "tn": m["tn"],
                "precision": m["precision"],
                "recall": m["recall"],
                "dice": m["dice"],
                "accuracy": m["accuracy"],
                "pred_vessel_pct": pred_pct,
                "gt_vessel_pct": gt_pct,
            })

        got = [r for r in rows if r["tag"] == tag]
        if got:
            print(f"  Dice 平均(宏) = {np.mean([r['dice'] for r in got]):.4f}"
                  f"   Precision = {np.mean([r['precision'] for r in got]):.4f}"
                  f"   Recall = {np.mean([r['recall'] for r in got]):.4f}")

    return rows, problems


# ------------------------------------------------------------
# 汇总计算
# ------------------------------------------------------------
def summarize(subset):
    """给一批行算汇总。宏平均 = 每张图指标再平均（与 evaluate_batch 口径一致）；
    微/合并 = 先把所有图的 TP/FP/FN 加起来再算（对像素更公平）。"""
    if not subset:
        return None
    tp = sum(r["tp"] for r in subset)
    fp = sum(r["fp"] for r in subset)
    fn = sum(r["fn"] for r in subset)
    eps = 1e-10
    return {
        "n": len(subset),
        "dice_macro": float(np.mean([r["dice"] for r in subset])),
        "precision_macro": float(np.mean([r["precision"] for r in subset])),
        "recall_macro": float(np.mean([r["recall"] for r in subset])),
        "dice_micro": 2 * tp / (2 * tp + fp + fn + eps),
        "precision_micro": tp / (tp + fp + eps),
        "recall_micro": tp / (tp + fn + eps),
        "tp": tp, "fp": fp, "fn": fn,
    }


def common_ids(rows):
    """对每个数据集，求各版本都跑过的图 ID（交集）。"""
    out = {}
    for dataset in ("STARE", "DRIVE"):
        versions = sorted({r["version"] for r in rows if r["dataset"] == dataset})
        if len(versions) < 2:
            continue
        sets = [set(r["img_id"] for r in rows
                    if r["dataset"] == dataset and r["version"] == v) for v in versions]
        inter = set.intersection(*sets) if sets else set()
        if inter:
            out[dataset] = sorted(inter)
    return out


# ------------------------------------------------------------
# 输出
# ------------------------------------------------------------
DETAIL_FIELDS = ["dataset", "version", "tag", "img_id", "fov_used",
                 "tp", "fp", "fn", "tn", "precision", "recall", "dice",
                 "accuracy", "pred_vessel_pct", "gt_vessel_pct"]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})


def fmt_row(name, s):
    return (f"| {name} | {s['n']} | {s['dice_macro']:.4f} | {s['dice_micro']:.4f} | "
            f"{s['precision_macro']:.4f} | {s['recall_macro']:.4f} |")


def main():
    t0 = time.perf_counter()
    print("=" * 68)
    print("指标落盘（只读重算）—— 不重跑流水线，不修改 results/ 已有文件")
    print("=" * 68)

    rows, problems = collect()
    if not rows:
        print("\n没有算出任何指标，请检查 results/ 目录结构。")
        return 1

    order = [f"{d}_V{v}" for d, v in COMBOS]
    rows.sort(key=lambda r: (order.index(r["tag"]) if r["tag"] in order else 99, r["img_id"]))

    common = common_ids(rows)

    # ---- 1) 明细 CSV ----
    write_csv(CSV_DETAIL, rows, DETAIL_FIELDS)

    # ---- 2) 汇总（全量 + 共同子集）----
    grouped = []
    for tag in order:
        sub = [r for r in rows if r["tag"] == tag]
        s = summarize(sub)
        if s:
            dataset = tag.split("_")[0]
            grouped.append({"scope": "all", "dataset": dataset, "tag": tag, **s})
    for dataset, ids in common.items():
        for ver in ("V1", "V2", "V3"):
            sub = [r for r in rows if r["dataset"] == dataset
                   and r["version"] == ver and r["img_id"] in ids]
            s = summarize(sub)
            if s:
                grouped.append({"scope": "common_subset", "dataset": dataset,
                                "tag": f"{dataset}_{ver}", **s})
    gfields = ["scope", "dataset", "tag", "n", "dice_macro", "dice_micro",
               "precision_macro", "recall_macro", "precision_micro",
               "recall_micro", "tp", "fp", "fn"]
    write_csv(CSV_GROUPED, grouped, gfields)

    # ---- 3) Markdown 人读版 ----
    L = []
    L.append("# 评价指标汇总（由 `python工具/dump_metrics.py` 从 results/ 已有掩膜重算）")
    L.append("")
    L.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　"
             f"| 图源：`results/<数据集>_V<版本>/masks/`　"
             f"| STARE 标注者：`{ANNOTATOR}`　| DRIVE：`{SPLIT_TXT}`")
    L.append(">")
    L.append("> **口径说明**：指标只在有效视野（FOV）内统计；"
             "**宏平均** = 每张图先算指标再取平均（与原 `evaluate_batch` 一致）；"
             "**微/合并** = 把所有图的 TP/FP/FN 累加后再算（对像素更公平）。"
             "本脚本只读盘重算，未重跑流水线。")
    L.append("")

    L.append("## 表 1　全量汇总（按各版本实际跑过的图）")
    L.append("")
    L.append("| 数据集_版本 | 图数 | Dice(宏平均) | Dice(微/合并) | Precision(宏) | Recall(宏) |")
    L.append("|---|---|---|---|---|---|")
    for tag in order:
        sub = [r for r in rows if r["tag"] == tag]
        s = summarize(sub)
        if s:
            L.append(fmt_row(tag, s))
    L.append("")
    L.append("> ⚠ **注意**：`*_V1` 只跑了**开发集 10 张**，`*_V2` / `*_V3` 跑了**全部 20 张**"
             "（开发集 + 评价集）。**三版图集不同，本表的数字不能直接横比**，"
             "要横比请用下面的表 2。")
    L.append("")

    L.append("## 表 2　共同子集对照（同口径，可横比 —— 答辩请用这张）")
    L.append("")
    if common:
        for dataset, ids in common.items():
            L.append(f"**{dataset}** 共同子集：{len(ids)} 张　`{' '.join(ids)}`")
            L.append("")
        L.append("| 数据集_版本 | 图数 | Dice(宏平均) | Dice(微/合并) | Precision(宏) | Recall(宏) |")
        L.append("|---|---|---|---|---|---|")
        for dataset, ids in common.items():
            for ver in ("V1", "V2", "V3"):
                sub = [r for r in rows if r["dataset"] == dataset
                       and r["version"] == ver and r["img_id"] in ids]
                s = summarize(sub)
                if s:
                    L.append(fmt_row(f"{dataset}_{ver}", s))
    else:
        L.append("（未找到可用的共同子集）")
    L.append("")

    L.append("## 表 3　每张图明细")
    L.append("")
    L.append("| 数据集_版本 | 图像 | FOV | TP | FP | FN | Dice | Precision | Recall | 预测血管占比 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['tag']} | {r['img_id']} | {r['fov_used']} | {r['tp']} | {r['fp']} | "
                 f"{r['fn']} | {r['dice']:.4f} | {r['precision']:.4f} | "
                 f"{r['recall']:.4f} | {r['pred_vessel_pct']:.2f}% |")
    L.append("")

    if problems:
        L.append("## 自检提示（需人工确认）")
        L.append("")
        for p in problems:
            L.append(f"- {p}")
        L.append("")

    with open(MD_OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    # ---- 控制台汇总 ----
    print("\n" + "=" * 68)
    print("表 2：共同子集对照（同口径，可横比）")
    print("=" * 68)
    if common:
        for dataset, ids in common.items():
            print(f"\n{dataset}  共同子集 {len(ids)} 张: {' '.join(ids)}")
            print(f"  {'版本':<16}{'图数':>4}{'Dice宏':>10}{'Dice微':>10}{'P宏':>9}{'R宏':>9}")
            for ver in ("V1", "V2", "V3"):
                sub = [r for r in rows if r["dataset"] == dataset
                       and r["version"] == ver and r["img_id"] in ids]
                s = summarize(sub)
                if s:
                    print(f"  {dataset + '_' + ver:<16}{s['n']:>4}{s['dice_macro']:>10.4f}"
                          f"{s['dice_micro']:>10.4f}{s['precision_macro']:>9.4f}"
                          f"{s['recall_macro']:>9.4f}")
    else:
        print("（未找到可用的共同子集）")

    if problems:
        print("\n" + "=" * 68)
        print(f"自检提示（{len(problems)} 条，需人工确认）")
        print("=" * 68)
        for p in problems:
            print("  " + p)

    print("\n" + "=" * 68)
    print(f"完成，耗时 {time.perf_counter() - t0:.2f} 秒。共 {len(rows)} 张图。")
    print(f"  明细 -> {CSV_DETAIL}")
    print(f"  汇总 -> {CSV_GROUPED}")
    print(f"  报告 -> {MD_OUT}")
    print("=" * 68)
    return 0


# Markdown 里要显示 DRIVE 切分说明
SPLIT_TXT = f"{DRIVE_SPLIT}（1st_manual）"

if __name__ == "__main__":
    sys.exit(main())
