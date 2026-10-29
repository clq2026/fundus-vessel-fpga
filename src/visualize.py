"""
展示模块 —— 保存中间结果、生成对比图

对应《项目流程与算法详解》第五章 "展示模块"

V1 阶段的功能：
    1. 把流水线每一步的中间结果单独保存为 PNG，方便验收
    2. 生成四图对比（原图 / 增强图 / 分割图 / 叠加图）

V4 阶段会在此基础上增加交互界面（Gradio），那是后面的事
"""

import os
import numpy as np
import cv2
from .data_io import imwrite_unicode


def save_intermediates(img_id, intermediates, out_dir):
    """
    把流水线每一步的中间结果保存为 PNG 图片

    参数:
        img_id:       图像编号（用于命名文件）
        intermediates: pipeline.py process() 返回的中间结果字典
        out_dir:      输出目录
    """
    img_dir = os.path.join(out_dir, img_id)
    os.makedirs(img_dir, exist_ok=True)

    # 保存每个中间步骤
    for step_name, img in intermediates.items():
        if img is None:
            continue  # 跳过未启用的步骤

        if isinstance(img, (int, float)):
            # threshold_value 是数值不是图像，跳过
            continue

        path = os.path.join(img_dir, f"{step_name}.png")
        imwrite_unicode(path, img)

    print(f"  [中间结果已保存] {img_dir}/")


def make_comparison_figure(img_id, original, mask, overlay, out_dir):
    """
    生成四图对比大图：原图 | 黑帽增强 | 分割结果 | 叠加结果

    参数:
        img_id:    图像编号
        original:  原图 BGR
        mask:      血管掩膜
        overlay:   叠加图
        out_dir:    输出目录
    """
    # 拿黑帽图作为"增强图"（如果有的话在 intermediates 里）
    # 这里简化为直接用 mask 的伪彩色作为增强图
    enhanced_color = cv2.applyColorMap(mask, cv2.COLORMAP_JET)

    # 确保所有图尺寸一致
    h, w = original.shape[:2]
    images = [
        ("原图", original),
        ("增强图(伪彩)", enhanced_color),
        ("分割结果", cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)),
        ("叠加结果", overlay),
    ]

    # 横向拼接四张图
    panels = []
    for label, img in images:
        if img.shape[:2] != (h, w):
            img = cv2.resize(img, (w, h))
        # 加标题条
        bar = np.zeros((30, w, 3), dtype=np.uint8)
        cv2.putText(bar, label, (10, 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (255, 255, 255), 1)
        panel = np.vstack([bar, img])
        panels.append(panel)

    figure = np.hstack(panels)
    path = os.path.join(out_dir, f"{img_id}_对比.png")
    imwrite_unicode(path, figure)
    print(f"  [对比图已保存] {path}")

    return figure


def print_timing(img_id, timing):
    """
    打印每一步的耗时

    参数:
        img_id:  图像编号
        timing:  pipeline.py process() 返回的耗时字典
    """
    total = sum(timing.values())
    print(f"\n  {img_id} 耗时统计:")
    print(f"  {'步骤':<20s} {'耗时(ms)':>10s}")
    print(f"  {'-'*32}")
    for step, ms in sorted(timing.items()):
        print(f"  {step:<20s} {ms:>10.2f}")
    print(f"  {'-'*32}")
    print(f"  {'总计':<20s} {total:>10.2f}")
