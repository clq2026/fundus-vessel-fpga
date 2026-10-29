"""
评价模块 —— 在有效视野内计算分割质量指标

对应《项目流程与算法详解》第四章 "评价模块"

指标体系：
    把预测掩膜和人工标注逐像素对比，得出：
    - Precision（查误检）：我说是血管的里面，多少真血管？
    - Recall（查漏检）：真血管里面，我找到了多少？
    - Dice/F1：预测和真值整体重合度，越高越好

两条纪律（设计方案反复强调）：
    1. 只在 FOV 内统计：视野外没有医学意义
    2. 参数只在开发集调，评价集固定不动：在评价集调参 = 考试前偷看答案
"""

import numpy as np


def compute_metrics(pred_mask, gt_label, fov_mask=None):
    """
    计算分割质量指标

    参数:
        pred_mask: 预测的血管掩膜 (H, W)，血管=255，背景=0
        gt_label:  人工标注      (H, W)，血管=255，背景=0
        fov_mask:  有效视野掩膜  (H, W)，有效=255，无效=0
                   如果为 None，则整张图都算有效区域

    返回:
        dict 包含以下指标:
            - tp, fp, fn, tn:  混淆矩阵四个值
            - precision:      TP / (TP + FP)
            - recall:         TP / (TP + FN)
            - dice:           2*TP / (2*TP + FP + FN)
            - f1:             同 dice（Dice 和 F1 在二分类时等价）
            - accuracy:       (TP + TN) / 总像素数
    """
    # 二值化：> 0 的都当作血管（True=1）
    pred = pred_mask > 0
    gt = gt_label > 0

    # 如果有 FOV 掩膜，只在有效视野内统计
    if fov_mask is not None:
        fov = fov_mask > 0
        pred = pred & fov  # 视野外的不算
        gt = gt & fov

    # 计算混淆矩阵四个值
    tp = np.sum(pred & gt)   # 真阳性：预测血管且标注也是血管
    fp = np.sum(pred & ~gt)  # 假阳性：预测血管但标注是背景（误检）
    fn = np.sum(~pred & gt)  # 假阴性：预测背景但标注是血管（漏检）
    tn = np.sum(~pred & ~gt) # 真阴性：预测背景且标注也是背景

    # 计算各项指标（加 1e-10 防止除以 0）
    eps = 1e-10
    precision = tp / (tp + fp + eps)
    recall = tp / (tp + fn + eps)
    dice = 2 * tp / (2 * tp + fp + fn + eps)
    accuracy = (tp + tn) / (tp + fp + fn + tn + eps)

    return {
        "tp": int(tp),
        "fp": int(fp),
        "fn": int(fn),
        "tn": int(tn),
        "precision": float(precision),
        "recall": float(recall),
        "dice": float(dice),
        "f1": float(dice),  # 二分类时 F1 和 Dice 等价
        "accuracy": float(accuracy),
    }


def evaluate_batch(results, fov_masks=None):
    """
    批量评价多张图，返回汇总统计

    参数:
        results:   列表，每项为 (img_id, pred_mask, gt_label)
        fov_masks: 字典 {img_id: fov_mask}，可选

    返回:
        dict 包含:
            - per_image: 每张图的指标
            - mean:      所有图的平均指标
    """
    per_image = {}
    all_precision = []
    all_recall = []
    all_dice = []

    for img_id, pred, gt in results:
        fov = None
        if fov_masks and img_id in fov_masks:
            fov = fov_masks[img_id]

        m = compute_metrics(pred, gt, fov)
        per_image[img_id] = m
        all_precision.append(m["precision"])
        all_recall.append(m["recall"])
        all_dice.append(m["dice"])

    return {
        "per_image": per_image,
        "mean": {
            "precision": float(np.mean(all_precision)),
            "recall": float(np.mean(all_recall)),
            "dice": float(np.mean(all_dice)),
            "n_images": len(results),
        },
    }


def format_metrics(metrics, title=""):
    """
    把指标格式化成字符串，方便打印

    参数:
        metrics: compute_metrics 或 evaluate_batch 的返回值
        title:   可选标题
    """
    lines = []
    if title:
        lines.append(f"=== {title} ===")

    if "mean" in metrics:
        # 批量结果
        m = metrics["mean"]
        lines.append(f"平均 Dice:     {m['dice']:.4f}")
        lines.append(f"平均 Precision: {m['precision']:.4f}")
        lines.append(f"平均 Recall:    {m['recall']:.4f}")
        lines.append(f"图片数量:       {m['n_images']}")
        lines.append("--- 每张图明细 ---")
        for img_id, m in metrics["per_image"].items():
            lines.append(
                f"  {img_id}: Dice={m['dice']:.4f}  "
                f"P={m['precision']:.4f}  R={m['recall']:.4f}  "
                f"(TP={m['tp']} FP={m['fp']} FN={m['fn']})"
            )
    else:
        # 单张结果
        lines.append(f"Dice:       {metrics['dice']:.4f}")
        lines.append(f"Precision:  {metrics['precision']:.4f}")
        lines.append(f"Recall:     {metrics['recall']:.4f}")
        lines.append(f"Accuracy:   {metrics['accuracy']:.4f}")
        lines.append(
            f"TP={metrics['tp']}  FP={metrics['fp']}  "
            f"FN={metrics['fn']}  TN={metrics['tn']}"
        )

    return "\n".join(lines)
