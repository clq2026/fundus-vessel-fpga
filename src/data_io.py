"""
数据模块 —— 读取眼底图像、人工标注和有效视野掩膜

对应《项目流程与算法详解》第三章 "流水线逐步详解" 的第 1 步：图像读取

本模块只负责"读文件"，不做任何图像处理。
处理逻辑全部在 pipeline.py 里，这里只把文件变成矩阵。
"""

import os
import numpy as np
import cv2

# ============================================================
# STARE 数据集中有血管标注的 20 张图编号
# 来源：STARE 官方网站 vessel segmentation 子集
# ============================================================
STARE_VESSEL_IDS = [
    "im0001", "im0002", "im0003", "im0004", "im0005",
    "im0044", "im0077", "im0081", "im0082", "im0139",
    "im0162", "im0163", "im0235", "im0236", "im0239",
    "im0240", "im0255", "im0291", "im0319", "im0324",
]

# 用于开发调参的图（10 张，含正常和困难样本）
# 另外 10 张作为评价集，固定不动 —— 对应设计方案"参数在开发集确定，评价集固定"
STARE_DEV_IDS = STARE_VESSEL_IDS[:10]   # 开发集
STARE_EVAL_IDS = STARE_VESSEL_IDS[10:]  # 评价集


def imread_unicode(path):
    """
    读取含中文路径的图像（OpenCV 的 imread 不支持中文路径）

    原理：先以二进制方式读入文件，再用 imdecode 解码
    这样无论路径里有没有中文都能正常工作

    参数:
        path: 图像文件路径
    返回:
        img: BGR 格式的图像矩阵 (H, W, 3)，读失败返回 None
    """
    data = np.fromfile(path, dtype=np.uint8)  # 以二进制读取文件
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)  # 解码为 BGR 彩色图
    return img


def imwrite_unicode(path, img):
    """
    保存图像到含中文路径（OpenCV 的 imwrite 不支持中文路径）

    参数:
        path: 保存路径
        img:  图像矩阵
    """
    ext = os.path.splitext(path)[1]  # 获取扩展名（如 .png）
    result, encoded = cv2.imencode(ext, img)
    if result:
        encoded.tofile(path)  # 以二进制写入文件


def load_stare_image(data_dir, img_id):
    """
    读取一张 STARE 原图

    参数:
        data_dir: STARE 数据根目录（如 ".../STARE 官方项目数据"）
        img_id:   图像编号（如 "im0001"）
    返回:
        img: BGR 彩色图矩阵 (605, 700, 3)
    """
    path = os.path.join(data_dir, f"{img_id}.ppm")
    img = imread_unicode(path)
    if img is None:
        raise FileNotFoundError(f"无法读取图像: {path}")
    return img


def load_stare_label(data_dir, img_id, annotator="ah"):
    """
    读取一张 STARE 的人工标注（血管=255，背景=0）

    参数:
        data_dir:   STARE 数据根目录
        img_id:     图像编号
        annotator:  标注者，"ah" 或 "vk"
    返回:
        label: 二值图矩阵 (605, 700)，值为 0 或 255
    """
    sub_dir = f"标注-{annotator}"
    path = os.path.join(data_dir, sub_dir, f"{img_id}.{annotator}.ppm")
    img = imread_unicode(path)
    if img is None:
        raise FileNotFoundError(f"无法读取标注: {path}")
    # 标注图可能是彩色或灰度，统一转为灰度再二值化
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    # 确保是二值图：>127 的为血管(255)，其余为背景(0)
    _, label = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    return label


def load_stare(data_dir, img_id, annotator="ah"):
    """
    一次性读取一张 STARE 的原图和标注

    参数:
        data_dir:   STARE 数据根目录
        img_id:     图像编号
        annotator:  标注者
    返回:
        img:   原图 BGR (605, 700, 3)
        label: 标注二值图 (605, 700)
    """
    img = load_stare_image(data_dir, img_id)
    label = load_stare_label(data_dir, img_id, annotator)
    return img, label


def get_stare_batch(data_dir, img_ids, annotator="ah"):
    """
    批量读取多张 STARE 数据

    参数:
        data_dir:   STARE 数据根目录
        img_ids:    图像编号列表
        annotator:  标注者
    返回:
        列表，每项为 (img_id, img, label, fov_mask)  — fov_mask=None 表示无 FOV
    """
    results = []
    for img_id in img_ids:
        try:
            img, label = load_stare(data_dir, img_id, annotator)
            # STARE 没有自带 FOV 掩膜，返回 None（V2 时会自生成）
            results.append((img_id, img, label, None))
            print(f"  [读取成功] {img_id}  原图{img.shape}  标注{label.shape}")
        except FileNotFoundError as e:
            print(f"  [读取失败] {e}")
    return results


# ============================================================
# DRIVE 数据集加载
# DRIVE training 20 张有标注 + FOV，test 20 张无标注只有 FOV
# ============================================================
DRIVE_TRAIN_IDS = [f"{i:02d}" for i in range(21, 41)]  # 21~40 training

# training 内部切分：前 10 张开发，后 10 张评价
DRIVE_DEV_IDS = DRIVE_TRAIN_IDS[:10]
DRIVE_EVAL_IDS = DRIVE_TRAIN_IDS[10:]


def load_drive_image(data_dir, split, img_id):
    """
    读取一张 DRIVE 原图

    路径规则：DRIVE数据集/{split}/images/{img_id}_{split}.tif
    例：21 → training/images/21_training.tif
    """
    path = os.path.join(data_dir, split, "images", f"{img_id}_{split}.tif")
    img = imread_unicode(path)
    if img is None:
        raise FileNotFoundError(f"无法读取图像: {path}")
    return img


def load_drive_label(data_dir, split, img_id):
    """
    读取一张 DRIVE 人工标注（血管=255，背景=0）

    路径规则：DRIVE数据集/{split}/1st_manual/{img_id}_manual1.gif
    例：21 → training/1st_manual/21_manual1.gif
    """
    path = os.path.join(data_dir, split, "1st_manual", f"{img_id}_manual1.gif")
    img = imread_unicode(path)
    if img is None:
        raise FileNotFoundError(f"无法读取标注: {path}")
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    _, label = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    return label


def load_drive_fov(data_dir, split, img_id):
    """
    读取一张 DRIVE 的 FOV 有效视野掩膜

    路径规则：DRIVE数据集/{split}/mask/{img_id}_{split}_mask.gif
    例：21 → training/mask/21_training_mask.gif

    DRIVE 自带 FOV 掩膜，值 255=有效视野，0=视野外
    """
    path = os.path.join(data_dir, split, "mask", f"{img_id}_{split}_mask.gif")
    img = imread_unicode(path)
    if img is None:
        raise FileNotFoundError(f"无法读取 FOV 掩膜: {path}")
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    _, mask = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    return mask


def get_drive_batch(data_dir, img_ids, split="training", load_label=True, load_fov=True):
    """
    批量读取多张 DRIVE 数据

    参数:
        data_dir:   DRIVE 数据根目录（如 ".../DRIVE数据集"）
        img_ids:    图像编号列表（如 ["21", "22", ...]）
        split:      "training" 或 "test"
        load_label: 是否加载标注（test 集无标注，设为 False）
        load_fov:   是否加载 FOV 掩膜
    返回:
        列表，每项为 (img_id, img, label_or_None, fov_mask_or_None)
    """
    results = []
    for img_id in img_ids:
        try:
            img = load_drive_image(data_dir, split, img_id)

            label = None
            if load_label:
                label = load_drive_label(data_dir, split, img_id)

            fov_mask = None
            if load_fov:
                fov_mask = load_drive_fov(data_dir, split, img_id)

            results.append((img_id, img, label, fov_mask))
            parts = f"原图{img.shape}"
            if label is not None:
                parts += f"  标注{label.shape}"
            if fov_mask is not None:
                parts += f"  FOV{fov_mask.shape}"
            print(f"  [读取成功] {img_id}  {parts}")
        except FileNotFoundError as e:
            print(f"  [读取失败] {e}")
    return results
