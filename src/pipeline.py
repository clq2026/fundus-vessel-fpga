"""
算法模块 —— 眼底血管分割流水线

对应《项目流程与算法详解》第三章 "流水线逐步详解"

核心设计：统一接口 process(img, params) -> (mask, intermediates, timing)
    - params:     参数字典，控制每一步的开关和参数值
    - intermediates: 中间结果字典，保存每一步的输出图像，方便查看和调试
    - timing:     耗时字典，记录每一步耗时多少毫秒

V1 流程：绿通道 -> 中值去噪 -> 黑帽增强 -> 固定阈值 -> 叠加
V2 增量：FOV 掩膜 + CLAHE + Otsu + 小连通域过滤（开关在 params 里控制）
V3 增量：四方向线检测（开关在 params 里控制）

将来 FPGA 版本也实现同一个 process() 函数，换后端只需换一行导入。
"""

import time
import numpy as np
import cv2


# ============================================================
# 默认参数：V1 基础版的参数设置
# 改参数不用改代码，只需在调用时传入不同的 params 字典
# ============================================================
def default_params():
    """
    返回默认参数字典，每个参数都有注释说明含义

    以后 V2/V3 只是在这里加新的键值对，不改 V1 的逻辑
    """
    return {
        # --- 第 2 步：绿通道 ---
        # 没有参数，直接取 BGR 的第 1 通道

        # --- 第 3 步：中值滤波去噪 ---
        "denoise_kernel": 3,  # 中值滤波窗口大小（3 = 3x3 窗口）

        # --- 第 4 步：黑帽增强 ---
        "morph_kernel_size": 15,     # 结构元素大小（要大于血管宽度）
        "morph_kernel_shape": "ellipse",  # 形状："ellipse" 或 "rect"

        # --- 第 5 步：固定阈值 ---
        "threshold": 60,  # 像素值 > 60 认为是血管（黑帽响应图上的阈值）

        # --- 开关 ---
        "use_clahe": False,         # V2：是否启用 CLAHE 对比度增强
        "clahe_clip_limit": 2.0,    # V2：CLAHE 限幅参数
        "clahe_tile_grid": (8, 8), # V2：CLAHE 分块大小

        "use_otsu": False,          # V2：是否用 Otsu 自动阈值（True 时忽略上面的 threshold）
        "use_fov_mask": False,      # V2：是否使用 FOV 掩膜
        "min_blob_area": 0,         # V2：连通域过滤，删小于此面积的块（0=不过滤）

        # --- V3：四方向线检测 ---
        "use_direction_filter": False,  # V3：总开关
        "direction_kernel_len": 9,      # V3：线检测核长度（实验得出 9 比 15 好）
        "direction_fusion": "weighted", # V3：融合方式（实验得出 weighted 比 max 好）
        "direction_weight": 0.3,        # V3：方向响应权重，黑帽占 0.7、方向占 0.3
    }


# ============================================================
# V1 流水线各步骤实现
# ============================================================

def _green_channel(img, intermediates, timing):
    """
    第 2 步：绿色通道提取

    为什么用绿通道：论文验证过，眼底图中血管和背景在绿色通道上对比度最大
    红通道里血管几乎看不出来，蓝通道动态范围太小

    OpenCV 存储顺序是 BGR，所以绿色通道是第 1 通道（从 0 开始数）
    """
    t0 = time.perf_counter()
    green = img[:, :, 1]  # 取绿色通道，变成灰度图
    elapsed = (time.perf_counter() - t0) * 1000  # 转为毫秒

    intermediates["green"] = green.copy()
    timing["green_channel"] = elapsed
    return green


def _median_denoise(green, params, intermediates, timing):
    """
    第 3 步：中值滤波去噪

    原理：每个像素取它 3x3 邻域内 9 个数的中位数作为新值
    好处：去椒盐噪点但不模糊血管边缘（均值滤波会模糊，中值不会）
    参数 3x3 来自论文，也是将来 HLS 模块的规格
    """
    k = params.get("denoise_kernel", 3)
    t0 = time.perf_counter()
    denoised = cv2.medianBlur(green, k)
    elapsed = (time.perf_counter() - t0) * 1000

    intermediates["denoised"] = denoised.copy()
    timing["median_denoise"] = elapsed
    return denoised


def _clahe(denoised, params, intermediates, timing):
    """
    V2 步骤：CLAHE 对比度增强

    原理：把图切成 8x8 小块，每块单独做直方图均衡，且限制放大倍数
    位置：放在形态学增强之前（论文顺序）
    仅当 params["use_clahe"] = True 时执行
    """
    t0 = time.perf_counter()
    clip = params.get("clahe_clip_limit", 2.0)
    grid = params.get("clahe_tile_grid", (8, 8))
    clahe_obj = cv2.createCLAHE(clipLimit=clip, tileGridSize=grid)
    enhanced = clahe_obj.apply(denoised)
    elapsed = (time.perf_counter() - t0) * 1000

    intermediates["clahe"] = enhanced.copy()
    timing["clahe"] = elapsed
    return enhanced


def _black_hat(denoised, params, intermediates, timing):
    """
    第 4 步：黑帽增强（V1 的灵魂步骤）

    原理：
        - 腐蚀 = 窗口内取最小值（亮区域缩小）
        - 膨胀 = 窗口内取最大值（亮区域扩大）
        - 闭运算 = 先膨胀再腐蚀（抹掉小的暗斑点）
        - 黑帽 = 闭运算 - 原图

    血管是"比周围暗的细长结构"，闭运算时被亮背景"淹没"
    两者一减，暗的细线（血管）就凸显出来了

    结构元素大小要大于血管宽度（默认 15x15）
    """
    k = params.get("morph_kernel_size", 15)
    shape_str = params.get("morph_kernel_shape", "ellipse")

    # 创建结构元素：椭圆或矩形
    if shape_str == "ellipse":
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    else:
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))

    t0 = time.perf_counter()
    blackhat = cv2.morphologyEx(denoised, cv2.MORPH_BLACKHAT, kernel)

    # 线性归一化到 0-255（GitHub 参考项目的做法）
    # 黑帽原始响应值范围很小（如 0-72），直接卡阈值 60 几乎筛不掉东西
    # 归一化后值域扩展到 0-255，阈值 60 才有意义
    if blackhat.max() > 0:
        blackhat = cv2.normalize(
            blackhat, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX
        )

    elapsed = (time.perf_counter() - t0) * 1000

    intermediates["blackhat"] = blackhat.copy()
    timing["black_hat"] = elapsed
    return blackhat


def _direction_filter(denoised, params, intermediates, timing):
    """
    V3 步骤：四方向线检测

    目的：黑帽用大椭圆核对粗血管响应强，但细血管（1-2 像素宽）响应弱。
         线检测用"细长线形核"沿血管方向滑动，匹配度最高时响应最强，
         能把黑帽漏掉的细血管重新拉出来。

    原理（论文 B-COSFIRE 12 方向的简化版，4 方向）：
        - 构造 0°、45°、90°、135° 四个方向的线形结构元素
        - 每个方向做一次黑帽（闭运算 - 原图）
        - 四个方向逐像素取最大值 = 方向响应图
        - 和 V2 的黑帽响应取最大（融合）

    仅当 params["use_direction_filter"] = True 时执行
    """
    length = params.get("direction_kernel_len", 15)
    fusion = params.get("direction_fusion", "max")

    t0 = time.perf_counter()

    # --- 构造四个方向的线形结构元素 ---
    # 0°（水平）：长条形，宽 1
    k0 = cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1))
    # 90°（垂直）
    k90 = cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
    # 45°（主对角线）：自定义方阵，主对角线为 1
    k45 = np.zeros((length, length), dtype=np.uint8)
    np.fill_diagonal(k45, 1)
    # 135°（反对角线）
    k135 = np.fliplr(k45)

    kernels = [k0, k45, k90, k135]

    # --- 每个方向做一次黑帽，归一化后存入列表 ---
    responses = []
    for k in kernels:
        bh = cv2.morphologyEx(denoised, cv2.MORPH_BLACKHAT, k)
        if bh.max() > 0:
            bh = cv2.normalize(bh, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
        responses.append(bh)

    # --- 四个方向逐像素取最大值 ---
    direction_response = np.max(responses, axis=0).astype(np.uint8)

    elapsed = (time.perf_counter() - t0) * 1000
    timing["direction_filter"] = elapsed

    # 保存中间结果：四个方向的响应图 + 融合后的方向响应图
    direction_names = ["dir_0", "dir_45", "dir_90", "dir_135"]
    for name, resp in zip(direction_names, responses):
        intermediates[name] = resp.copy()
    intermediates["direction_response"] = direction_response.copy()

    return direction_response


def _fuse_responses(blackhat, direction_response, params, intermediates, timing):
    """
    V3 步骤：融合黑帽响应和方向响应

    融合方式（params["direction_fusion"]）：
        "max"      : 逐像素取最大值（黑帽保底 + 线检测补充细血管）
        "sum"      : 相加后截断到 255（更激进，可能放大噪声）
        "weighted" : 加权求和：(1-w)*黑帽 + w*方向，w=direction_weight
                     黑帽权重高（更可靠），方向只做补充
    """
    fusion = params.get("direction_fusion", "max")
    w = params.get("direction_weight", 0.3)

    t0 = time.perf_counter()
    if fusion == "sum":
        fused = cv2.add(blackhat, direction_response)
    elif fusion == "weighted":
        fused = cv2.addWeighted(blackhat, 1 - w, direction_response, w, 0)
    else:  # max
        fused = np.maximum(blackhat, direction_response).astype(np.uint8)

    elapsed = (time.perf_counter() - t0) * 1000
    timing["fuse"] = elapsed
    intermediates["fused"] = fused.copy()
    return fused


def _thresholding(blackhat, params, intermediates, timing):
    """
    第 5 步：阈值分割

    两种模式：
    - 固定阈值（V1）：像素 > threshold -> 白色(血管)，否则 -> 黑色
    - Otsu 自动阈值（V2）：统计直方图，自动找最优阈值

    Otsu 依赖整帧统计 -> 这就是设计方案说"Otsu 放软件端"的原因
    """
    t0 = time.perf_counter()

    if params.get("use_otsu", False):
        # Otsu 模式：自动找阈值
        thresh_val, mask = cv2.threshold(
            blackhat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        timing["otsu_threshold"] = thresh_val  # 记录自动找到的阈值
    else:
        # 固定阈值模式
        thresh_val = params.get("threshold", 60)
        _, mask = cv2.threshold(blackhat, thresh_val, 255, cv2.THRESH_BINARY)

    elapsed = (time.perf_counter() - t0) * 1000

    intermediates["threshold_value"] = thresh_val
    intermediates["mask_raw"] = mask.copy()
    timing["thresholding"] = elapsed
    return mask


def _remove_small_blobs(mask, params, intermediates, timing):
    """
    V2 步骤：小连通域过滤

    原理：把相邻白色像素连成"块"（连通域），面积太小(<30像素)的删掉
    真血管是细长连续的大块，删小的不会误伤
    论文口径：删除 < 30 像素的小块
    仅当 params["min_blob_area"] > 0 时执行
    """
    min_area = params.get("min_blob_area", 0)
    if min_area <= 0:
        return mask  # 不过滤

    t0 = time.perf_counter()

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8
    )

    filtered = np.zeros_like(mask)
    for i in range(1, num_labels):  # 从 1 开始，跳过 0（背景）
        area = stats[i, cv2.CC_STAT_AREA]
        if area >= min_area:
            filtered[labels == i] = 255

    elapsed = (time.perf_counter() - t0) * 1000
    timing["remove_blobs"] = elapsed
    intermediates["mask_raw"] = intermediates.get("mask_raw", mask.copy())
    intermediates["mask_filtered"] = filtered.copy()
    return filtered


def generate_fov_mask(img):
    """
    根据论文方法自动生成 FOV 有效视野掩膜（STARE 等无自带 FOV 的数据集用）

    论文描述：
        "对 CIELab 亮度通道做阈值分割得到圆形视野区域，
         掩膜边界用 8 邻域均值膨胀 1 像素、重复 50 次来平滑边界"

    参数:
        img: 输入 BGR 彩色图
    返回:
        fov_mask: (H, W)，有效视野=255，视野外=0
    """
    h, w = img.shape[:2]

    # 1. 转到 CIELab，取亮度通道 L
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    L = lab[:, :, 0]

    # 2. 阈值分割：亮度高于某个值的区域 = 有效视野
    #    圆形视野内部亮度高（眼底组织反光），外围纯黑背景亮度低
    #    用 Otsu 自动找阈值比固定阈值更稳
    _, binary = cv2.threshold(L, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 3. 找最大连通域（就是圆形视野本体），删掉周围零星的小白点
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )
    if num_labels > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]  # 跳过背景
        largest_label = np.argmax(areas) + 1
        fov = (labels == largest_label).astype(np.uint8) * 255
    else:
        fov = binary

    # 4. 边界平滑：论文原文是"8 邻域均值膨胀 1 像素 × 50 次"。
    #    本实现改用 3×3 闭运算（膨胀 + 腐蚀）来平滑边界。
    #
    #    【性能优化 2026-10-05】形态学闭运算是**幂等**的：phi(phi(X)) ≡ phi(X)，
    #    所以重复 N 次与只做 1 次，结果逐像素完全相同。
    #    已在全部 20 张 STARE 图上验证：1 次 vs 50 次差异 = 0 像素、2 次 vs 50 次 = 0 像素
    #    （脚本：python工具/verify_fov_fast.py）。
    #    → 原来 50 次循环里有 49 次是纯冗余，这里只做 1 次，输出完全不变。
    #    注：论文原式是"膨胀 × 50"（膨胀不幂等，必须迭代）；本实现在换成闭运算的那一刻，
    #    这个循环就已经退化了 —— 属于实现替换时留下的冗余。
    fov = cv2.morphologyEx(
        fov, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    )

    # 确保是二值
    _, fov = cv2.threshold(fov, 127, 255, cv2.THRESH_BINARY)
    return fov


# ============================================================
# 主函数：统一接口
# ============================================================

def process(img, params=None, fov_mask=None):
    """
    眼底血管分割主处理函数（统一接口）

    参数:
        img:       输入 BGR 彩色图 (H, W, 3)
        params:    参数字典，用 default_params() 时 V1 开关全关
        fov_mask:  可选，外部传入的 FOV 有效视野掩膜（DRIVE 自带）。
                   如果传 None 且 params["use_fov_mask"]=True，
                   则用 generate_fov_mask() 按论文方法自动生成（STARE 用）

    返回:
        mask:          血管二值掩膜 (H, W)，血管=255，背景=0
        intermediates: 中间结果字典
        timing:        耗时字典
        fov_mask_used: 实际使用的 FOV 掩膜（可能是传入的，也可能是自生成的；没启用就是 None）

    接口不变，将来 FPGA 版本也实现这个签名。
    """
    if params is None:
        params = default_params()

    intermediates = {}
    timing = {}

    # --- FOV 掩膜准备（如果需要） ---
    if params.get("use_fov_mask", False):
        if fov_mask is None:
            # STARE：按论文方法自生成
            t0 = time.perf_counter()
            fov_mask = generate_fov_mask(img)
            timing["gen_fov"] = (time.perf_counter() - t0) * 1000
        intermediates["fov_mask"] = fov_mask.copy()
    else:
        fov_mask = None

    # --- 绿通道 ---
    green = _green_channel(img, intermediates, timing)

    # --- V2 CLAHE ---
    if params.get("use_clahe", False):
        enhanced = _clahe(green, params, intermediates, timing)
    else:
        enhanced = green
        intermediates["clahe"] = None

    # --- 中值去噪 ---
    denoised = _median_denoise(enhanced, params, intermediates, timing)

    # --- 黑帽 ---
    blackhat = _black_hat(denoised, params, intermediates, timing)

    # --- V3 四方向线检测 + 与黑帽融合 ---
    if params.get("use_direction_filter", False):
        direction_response = _direction_filter(denoised, params, intermediates, timing)
        response_for_threshold = _fuse_responses(
            blackhat, direction_response, params, intermediates, timing
        )
    else:
        response_for_threshold = blackhat
        intermediates["direction_response"] = None
        intermediates["fused"] = None

    # --- 阈值分割（Otsu 或固定） ---
    mask = _thresholding(response_for_threshold, params, intermediates, timing)

    # --- 删小连通域 ---
    if params.get("min_blob_area", 0) > 0:
        mask = _remove_small_blobs(mask, params, intermediates, timing)

    # --- FOV 清零 ---
    if fov_mask is not None:
        mask = cv2.bitwise_and(mask, fov_mask)

    intermediates["mask_final"] = mask.copy()
    intermediates["fov_mask_used"] = fov_mask

    return mask, intermediates, timing, fov_mask


# ============================================================
# 辅助函数：彩色叠加
# ============================================================

def overlay_vessels(img, mask, color=(0, 0, 255), alpha=0.5):
    """
    把血管掩膜叠加到原图上（血管染成指定颜色）

    参数:
        img:   原图 BGR
        mask:  血管掩膜（血管=255）
        color: 叠加颜色，BGR 格式（默认红色 (0,0,255)）
        alpha: 透明度 0~1（1=完全覆盖，0.5=半透明）

    返回:
        overlay: 叠加后的彩色图

    对应《项目流程与算法详解》第三章第 6 步：彩色叠加
    """
    overlay = img.copy()
    # 在血管位置涂色
    vessel_pixels = mask > 0
    # 混合：overlay = img * (1-alpha) + color * alpha
    overlay[vessel_pixels] = (
        np.array(img[vessel_pixels], dtype=np.float32) * (1 - alpha)
        + np.array(color, dtype=np.float32) * alpha
    ).astype(np.uint8)
    return overlay
