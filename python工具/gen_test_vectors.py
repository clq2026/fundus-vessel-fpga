"""
生成 HLS C 仿真测试向量（对应冻结后的硬件算法规格 v1.0）

硬件流水线边界：
  软件端：原图 → 绿通道 → CLAHE(clip=2.0, 8x8)
  ─── DMA 送入 FPGA ───
  硬件端：中值 3x3(边界复制) → 黑帽 11x11 矩形核(输出原始差值，不做归一化)
  ─── DMA 送回 ───
  软件端：MINMAX 归一化 → Otsu → 删小块 → FOV 清零

每张图输出 3 个文本文件（605 行 × 700 个十进制数，空格分隔）：
  <id>_input_clahe.txt       硬件输入（CLAHE 后的灰度图）
  <id>_ref_median.txt        参考：中值 3x3 输出（C 仿真第 1 级对比）
  <id>_ref_blackhat_raw.txt  参考：黑帽原始差值（C 仿真最终输出对比）
"""
import os
import sys
import numpy as np
import cv2

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data_io import imread_unicode
from src.pipeline import _green_channel, _clahe, default_params

STARE_DIR = os.path.join(PROJECT_ROOT, "STARE 官方项目数据")
OUT_DIR = os.path.join(PROJECT_ROOT, "hls", "test_vectors")

# 选两张：im0082（效果好的典型）、im0002（困难样本，边界/噪声情况更多）
TEST_IDS = ["im0082", "im0002"]

H, W = 605, 700          # STARE 固定分辨率（硬件综合时写死）
MEDIAN_K = 3
BH_K = 11                # 冻结：矩形 11x11


def save_txt(mat, path):
    """按行存：每行 W 个十进制数，空格分隔"""
    np.savetxt(path, mat, fmt="%d", delimiter=" ")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    for img_id in TEST_IDS:
        img_path = os.path.join(STARE_DIR, f"{img_id}.ppm")
        img = imread_unicode(img_path)
        assert img is not None, f"读不到 {img_path}"
        assert img.shape[:2] == (H, W), f"分辨率不符: {img.shape}"

        params = default_params()
        inter, timing = {}, {}

        # --- 软件端预处理：绿通道 → CLAHE（这就是硬件输入） ---
        green = _green_channel(img, inter, timing)
        clahe_out = _clahe(green, params, inter, timing)

        # --- 硬件第 1 级：中值 3x3，OpenCV 默认边界复制 BORDER_REPLICATE ---
        median_out = cv2.medianBlur(clahe_out, MEDIAN_K)

        # --- 硬件第 2 级：黑帽 11x11 矩形核 = 闭运算 - 原图 ---
        # 严格复现 OpenCV 形态学边界语义：
        #   膨胀：边界补最小值 0；腐蚀：边界补最大值 255
        # 这样边界处的窗口只有真实像素参与运算，硬件用边界钳位实现
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (BH_K, BH_K))
        dil = cv2.dilate(median_out, kernel,
                         borderType=cv2.BORDER_CONSTANT, borderValue=0)
        clo = cv2.erode(dil, kernel,
                        borderType=cv2.BORDER_CONSTANT, borderValue=255)
        # 饱和减法（差值非负）
        blackhat_raw = cv2.subtract(clo, median_out)

        # --- 存文本测试向量 ---
        save_txt(clahe_out, os.path.join(OUT_DIR, f"{img_id}_input_clahe.txt"))
        save_txt(median_out, os.path.join(OUT_DIR, f"{img_id}_ref_median.txt"))
        save_txt(blackhat_raw, os.path.join(OUT_DIR, f"{img_id}_ref_blackhat_raw.txt"))

        # 同时存 PNG 方便肉眼检查
        cv2.imencode(".png", clahe_out)[1].tofile(
            os.path.join(OUT_DIR, f"{img_id}_input_clahe.png"))
        cv2.imencode(".png", median_out)[1].tofile(
            os.path.join(OUT_DIR, f"{img_id}_ref_median.png"))
        bh_vis = cv2.normalize(blackhat_raw, None, 0, 255, cv2.NORM_MINMAX)
        cv2.imencode(".png", bh_vis)[1].tofile(
            os.path.join(OUT_DIR, f"{img_id}_ref_blackhat_raw.png"))

        print(f"[完成] {img_id}  输入范围[{clahe_out.min()},{clahe_out.max()}]  "
              f"黑帽原始范围[{blackhat_raw.min()},{blackhat_raw.max()}]")

    # 清单文件，供写 C testbench 时对照
    manifest = f"""HLS 测试向量清单（规格 v1.0）
图像尺寸: {H} 行 x {W} 列（STARE），像素 uint8
文件格式: 每个 .txt 共 {H} 行，每行 {W} 个十进制数(0-255)，空格分隔，行主序

文件命名:
  <id>_input_clahe.txt       硬件输入：绿通道+CLAHE 后的灰度图
  <id>_ref_median.txt        期望输出 1：中值 3x3（边界复制）
  <id>_ref_blackhat_raw.txt  期望输出 2：黑帽 11x11 矩形核原始差值（未归一化）

测试图:
  im0082 = 分割效果好的典型样本
  im0002 = 困难样本（对比度低、误检多），检验边界与鲁棒性

验收: C 仿真输出与 ref 文件逐像素一致（允许误差 0）
"""
    with open(os.path.join(OUT_DIR, "manifest.txt"), "w", encoding="utf-8") as f:
        f.write(manifest)

    print(f"\n测试向量已生成到: {OUT_DIR}")


if __name__ == "__main__":
    main()
