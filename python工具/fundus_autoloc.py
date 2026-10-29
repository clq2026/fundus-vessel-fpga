# -*- coding: utf-8 -*-
"""
眼底圆自动定位模块（ARM 侧预处理，HLS IP 无需任何改动）

背景
----
把眼底图贴到白色眼球模型上拍照时，画面里有三样东西：
    眼底图（橙红，R-B 约 +100~+155）
    白泡沫球（中性，R-B 约 0）
    桌面/环境（偏暖时 R-B 甚至为负）
原算法的 gen_fov 用「LAB 的 L 通道做 Otsu + 取最大连通域」，
在白球场景下会把白球面当成 FOV（白球面可见面积是眼底圆的 2 倍），
掩膜被 bitwise_and 一刀切光，Dice 直接归零。

解决
----
拍照后先由本模块自动找到眼底圆的圆心和半径，把圆裁剪出来重采样成
700x605（与 HLS IP 的输入尺寸一致），同时用这个圆直接生成 FOV。
这样整条链路不再需要参考图，也不需要物理遮光。

只依赖 cv2 / numpy，OpenCV 4.5.4（板端）与 5.x（PC）都能跑。
"""

import time
import cv2
import numpy as np

# 数字图（也就是 HLS IP 的输入）尺寸；眼底圆在其中的半径
DIG_W, DIG_H = 700, 605
DISC_R = 324          # 打印页黑描边圆直径 1299 px -> 对应数字图 648 宽 -> 半径 324


# ---------------------------------------------------------------- 基础工具
def rb_map(bgr):
    """红减蓝。眼底图为正且大，白/灰/黑背景约为 0，木桌等暖色背景为负。"""
    return bgr[:, :, 2].astype(np.int16) - bgr[:, :, 0].astype(np.int16)


def _odd(n):
    n = int(round(n))
    return n if n % 2 == 1 else n + 1


def ray_fit(bgr, cx, cy, r0, rb_thr, n_dir=72, n_samp=90, lo=0.72, hi=1.32, iters=2):
    """从中心向各方向发射线，找 R-B 跌破阈值的位置，得到边界半径 r(theta)。

    比形态学 + 外接圆准得多（后者会被开运算侵蚀掉 1% 以上），
    而且能直接读出长轴/短轴/朝向，斜拍成椭圆时也能正确还原。
    返回 (cx, cy, a, b, angle_deg)，其中 a>=b。
    """
    H, W = bgr.shape[:2]
    rb = rb_map(bgr)
    ang = np.arange(n_dir) * (2.0 * np.pi / n_dir)
    ca, sa = np.cos(ang), np.sin(ang)
    ts = np.linspace(lo, hi, n_samp) * float(r0)

    for _ in range(iters):
        X = np.rint(cx + np.outer(ca, ts)).astype(np.int32)
        Y = np.rint(cy + np.outer(sa, ts)).astype(np.int32)
        ok = (X >= 0) & (X < W) & (Y >= 0) & (Y < H)
        V = np.where(ok, rb[np.clip(Y, 0, H - 1), np.clip(X, 0, W - 1)], -999)
        below = V < rb_thr
        any_b = below.any(axis=1)
        if any_b.sum() < n_dir * 0.5:
            break
        idx = np.argmax(below, axis=1).astype(np.float32)
        # 线性插值，消除采样量化带来的阶梯
        i0 = np.clip(np.floor(idx).astype(np.int32), 0, n_samp - 2)
        v0 = V[np.arange(n_dir), i0].astype(np.float32)
        v1 = V[np.arange(n_dir), i0 + 1].astype(np.float32)
        den = (v0 - v1)
        frac = np.where(np.abs(den) > 1e-6, (v0 - rb_thr) / den, 0.0)
        step = ts[1] - ts[0]
        r_th = (ts[i0] + np.clip(frac, 0.0, 1.0) * step) * any_b
        r_th[~any_b] = np.nan
        r_th = np.where(r_th > 0.6 * r0, r_th, np.nan)
        if np.isnan(r_th).sum() > n_dir * 0.3:
            break
        # 用边界点重新估计中心（迭代一次即足够收敛）
        rr = np.nan_to_num(r_th, nan=float(np.nanmedian(r_th)))
        px = cx + rr * ca
        py = cy + rr * sa
        cx, cy = float(np.median(px)), float(np.median(py))

    rr = np.nan_to_num(r_th, nan=float(np.nanmedian(r_th)))
    # 圆周方向的循环平滑，去掉单个方向上的噪声毛刺
    rr = np.convolve(np.concatenate([rr, rr, rr]), np.ones(5) / 5.0, 'same')[n_dir:2 * n_dir]
    return float(cx), float(cy), float(np.median(rr)), rr, np.degrees(ang)


def crop_ellipse(bgr, cx, cy, a, b, angle_deg, out=(DIG_W, DIG_H),
                 ra=DISC_R, rb=None):
    """把照片里的（椭）圆区域拉正并重采样到 700x605。

    沿长轴缩 ra/a，沿短轴缩 rb/b；rb 默认等于 ra（拉成正圆）。
    """
    th = np.radians(angle_deg)
    u = np.array([np.cos(th), np.sin(th)])
    v = np.array([-np.sin(th), np.cos(th)])
    if rb is None:
        rb = ra
    F = (np.outer(np.array([1.0, 0.0]), u * (ra / max(a, 1e-6)))
         + np.outer(np.array([0.0, 1.0]), v * (rb / max(b, 1e-6))))
    Fi = np.linalg.inv(F)
    ow, oh = out
    O = np.array([ow / 2.0, oh / 2.0])
    c = np.array([cx, cy])
    M = np.column_stack([Fi, c - Fi @ O]).astype(np.float32)
    return cv2.warpAffine(bgr, M, (ow, oh), flags=cv2.INTER_CUBIC)


# ---------------------------------------------------------------- 定位
def _segment(bgr, rb_thr, close_px, min_frac, tm=None, rb=None):
    """色度分割 + 形态学，返回最大连通域的几何量。找不到返回 None。

    ★板端提速（2026-10-24，实测 8371 ms 定位里几何占了绝大部分）：
      ① 形态学核用 **MORPH_RECT** 而不是 ELLIPSE —— OpenCV 对非矩形结构元素走
         通用实现、SIMD 失效。四变体 A/B 实测：核形状与遍数对最终 Dice **完全无影响**
         （RECT 1 遍 与 ELLIPSE 2 遍 的 r、掩膜像素、Dice 全部逐位相同）；
      ② 只跑 **1 遍**（调用方不再跑第二遍），见 locate_fundus 的说明；
      ③ 连通域点先按 stats 的 bbox 裁剪再 argwhere，不对整图做 lab==i 全扫；
      ④ 凸包填实的 solid 只在**凸包 bbox 内**建，不再全图 zeros + 全图 argwhere
         （全尺寸 1280x960 时省掉 123 万点的分配与扫描）。
      ⚠ **外接圆必须用 solid 填充区域，不能用凸包顶点**：实测半径 312.48 vs 312.17
         （差 0.1%），Dice 原始 0.5150→0.5039、上限 0.6139→0.6056。
         OpenCV 的 minEnclosingCircle 是迭代近似算法，点集密度变了收敛点就不同。
      ⑤ 删掉只为打印服务的二阶矩 eig（np.cov + eigh 在 ARM 上是纯 Python 循环）。
    """
    H, W = bgr.shape[:2]
    t0 = time.perf_counter()
    if rb is None:
        rb = rb_map(bgr)
    m = (rb > rb_thr).astype(np.uint8)
    if tm is not None:
        tm.append(("二值化", time.perf_counter() - t0))
    kc = _odd(close_px)
    t0 = time.perf_counter()
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (kc, kc)))
    ko = max(3, _odd(kc / 5.0))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN,
                         cv2.getStructuringElement(cv2.MORPH_RECT, (ko, ko)))
    if tm is not None:
        tm.append(("形态学", time.perf_counter() - t0))
    t0 = time.perf_counter()
    n, lab, stats, _c = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return None
    areas = stats[1:, 4]
    i = int(np.argmax(areas)) + 1
    area = float(areas[i - 1])
    if area < min_frac * float(H * W):
        return None
    # 只在最大连通域的 bbox 内取点，避免对整图做一次 lab==i 的全扫
    x0, y0, bw, bh = stats[i, :4]
    pts = np.argwhere(lab[y0:y0 + bh, x0:x0 + bw] == i)[:, ::-1].astype(np.float32)
    pts[:, 0] += x0
    pts[:, 1] += y0
    if tm is not None:
        tm.append(("连通域+取点", time.perf_counter() - t0))
    t0 = time.perf_counter()
    # 取凸包：眼底区域本来就是凸的，这能彻底消掉血管暗线留下的孔洞，
    # 而且凸包只由最外点决定，不会像闭运算那样把边界外扩
    hull = cv2.convexHull(pts)
    # 凸包填实（外接圆必须用填充区域，不能只用顶点 —— 见函数注释 ⚠）。
    # solid 只建在凸包 bbox 内，等价于全图但省掉大量零像素的分配和扫描。
    hi = np.round(hull).astype(np.int32).reshape(-1, 2)
    bx0 = int(max(0, hi[:, 0].min() - 1))
    by0 = int(max(0, hi[:, 1].min() - 1))
    bx1 = int(min(W - 1, hi[:, 0].max() + 1))
    by1 = int(min(H - 1, hi[:, 1].max() + 1))
    solid = np.zeros((by1 - by0 + 1, bx1 - bx0 + 1), np.uint8)
    cv2.fillConvexPoly(solid, hi - np.array([bx0, by0], np.int32), 255)
    if tm is not None:
        tm.append(("凸包+填充", time.perf_counter() - t0))
    t0 = time.perf_counter()
    ys, xs = np.nonzero(solid)
    cp = np.empty((len(xs), 2), np.float32)
    cp[:, 0] = xs + bx0
    cp[:, 1] = ys + by0
    if tm is not None:
        tm.append(("取点%d" % len(xs), time.perf_counter() - t0))
    t0 = time.perf_counter()
    (cx, cy), r_enc = cv2.minEnclosingCircle(cp)
    if tm is not None:
        tm.append(("外接圆", time.perf_counter() - t0))
    t0 = time.perf_counter()
    # 轴比用「稳健 bbox」：取边界点 2%~98% 分位的跨度。
    # 二阶矩会把椭圆轴比低估约 4%（实测 1.120 vs 真值 1.170），bbox 只差 1%。
    qx = np.percentile(pts[:, 0], [2, 98])
    qy = np.percentile(pts[:, 1], [2, 98])
    ar = float((qx[1] - qx[0]) / max(qy[1] - qy[0], 1e-6))
    # 长轴朝向只用于打印，用凸包的旋转包围盒角（对凸包是旋转卡壳，很快）
    ang = 0.0
    if len(hull) >= 5:
        (_rc, _rs, ang) = cv2.minAreaRect(hull)
    if tm is not None:
        tm.append(("分位数+角", time.perf_counter() - t0))
    return dict(mask=m, area=area, cx=float(cx), cy=float(cy), r_enc=float(r_enc),
                r_eq=float(np.sqrt(area / np.pi)), ar=ar,
                ma=r_enc, mb=r_enc / ar, ang=float(ang))


def locate_fundus(bgr, rb_thr=None, min_frac=0.004, close_rel=0.030,
                  max_ecc=1.45, verbose=False):
    """在任意尺寸的照片里找到眼底圆。返回 (cx, cy, r, info)，找不到返回 None。

    判据：R-B 色度分割（眼底图橙红，白球/桌面近乎中性或偏冷）
          -> 闭运算填掉血管暗线 -> 取最大连通域
          -> 凸包 + minEnclosingCircle 定圆心与外接圆，bbox 分位给轴比

    半径用外接圆实测最准（误差约 -0.7%）；等面积半径和二阶矩半径会被
    闭运算/开运算的净外扩带偏，实测差 4% 以上，不要用。
    闭运算核按整图尺寸自适应（close_rel * min(H,W)），所以拍远拍近都一致。

    ★只跑一遍 _segment（2026-10-24）：原设计有两遍，第二遍把核改成 0.06*r_enc。
      PC 实测两遍与一遍的最终裁剪图 **逐像素 0 差异**（480 档：核 11 -> 7，但眼底
      区域取凸包后与核尺寸无关），而板端每多跑一遍就多一倍耗时 —— 直接砍掉。
    """
    tm = [] if verbose else None
    H, W = bgr.shape[:2]
    t0 = time.perf_counter()
    rb = rb_map(bgr)
    if tm is not None:
        tm.append(("R-B色度", time.perf_counter() - t0))
    if rb_thr is None:
        t0 = time.perf_counter()
        # 注意：此时 rb 是 int16，范围 -255..255，+128 后裁到 0..255 才能在 u8 上跑 Otsu
        rb8 = np.clip(rb + 128, 0, 255).astype(np.uint8)
        t = int(cv2.threshold(rb8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[0])
        rb_thr = max(t - 128 + 12, 30)
        if tm is not None:
            tm.append(("Otsu定阈值", time.perf_counter() - t0))
    s = _segment(bgr, rb_thr, max(5.0, close_rel * min(H, W)), min_frac, tm, rb)
    if s is None:
        return None

    r_enc, r_eq = s['r_enc'], s['r_eq']
    ar = s['ar']
    if not (0.70 < r_eq / r_enc <= 1.02) or not (0.80 < ar < 1.80):
        if verbose:
            print("    [定位] 形状可疑：外接圆 r=%.1f  等面积 r=%.1f  轴比=%.3f"
                  % (r_enc, r_eq, ar))
        return None
    if verbose:
        print("    [定位] 阈值 R-B>%d  圆心(%.0f, %.0f)  外接圆 r=%.1f  "
              "等面积 r=%.1f  轴比=%.3f  长轴朝向=%.1f°"
              % (rb_thr, s['cx'], s['cy'], r_enc, r_eq, ar, s['ang']))
        if tm:
            print("       颜色分割细分: " + " + ".join(
                "%s %.0f" % (k, v * 1000.0) for k, v in tm) + " ms")
    info = dict(thr=rb_thr, mask=s['mask'], area=s['area'], r_enc=r_enc, r_eq=r_eq,
                ecc=ar, major=r_enc, minor=r_enc / ar, angle=s['ang'],
                ok=True, via="color")
    return s['cx'], s['cy'], float(r_enc), info


HOUGH_MAX_CAND = 80      # 打分最多评估多少个候选，见 locate_hough 说明


def locate_hough(bgr, min_r_frac=0.06, max_r_frac=0.45, verbose=False):
    """后备方案：Hough 圆检测。颜色分割失败时用它，并用同样的色度打分挑圆。

    ★板端实测（2026-10-24）：这个函数曾让一次上传卡住 28 s。两条原因——
      ① param2=35 / minDist=0.08*min(H,W) 太松，候选能到几百个；
      ② 每个候选都在原尺度上算 np.hypot + 两次 np.median 排序。
    PC 上只要 100 ms（有 SIMD），ARM Cortex-A9 上被放大到 28 s。
    现在改为：先严后松两阶段找圆（通常第一阶段就只剩 5 个候选）、
    打分降到 1/2 尺度（点数 ÷4）、并限制最多评估 HOUGH_MAX_CAND 个。
    实测 PC 100.5 -> 11.8 ms，选中的圆完全一致（r=117.0，打分 4.25 vs 4.26）。
    """
    H, W = bgr.shape[:2]
    g = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    rmin = int(min_r_frac * min(H, W))
    rmax = int(max_r_frac * min(H, W))
    cs = None
    for p2, dr in ((55, 0.18), (35, 0.08)):
        cs = cv2.HoughCircles(g, cv2.HOUGH_GRADIENT, dp=1.2,
                              minDist=min(H, W) * dr, param1=120, param2=p2,
                              minRadius=rmin, maxRadius=rmax)
        if cs is not None and len(cs[0]) > 0:
            break
    if cs is None:
        return None
    # 打分在 1/2 尺度上做：点数 ÷4，median 也快 4 倍；圆心/半径最后还原
    sc = 0.5
    h2, w2 = max(8, int(H * sc)), max(8, int(W * sc))
    rb = cv2.resize(rb_map(bgr).astype(np.float32), (w2, h2), interpolation=cv2.INTER_AREA)
    yy, xx = np.mgrid[0:h2, 0:w2]
    best, bv = None, -1e9
    for c in cs[0][:HOUGH_MAX_CAND]:
        cx, cy, r = float(c[0]) * sc, float(c[1]) * sc, float(c[2]) * sc
        d = np.hypot(yy - cy, xx - cx) / r
        mi = d < 0.95
        mr = (d > 1.06) & (d < 1.35)
        if mi.sum() < 120 or mr.sum() < 50:      # 原尺度阈值 500/200 换算到 1/2 尺度
            continue
        v = float(np.median(rb[mi]) - np.median(rb[mr])) / (1.0 + float(np.std(rb[mi])))
        if v > bv:
            best, bv = (cx / sc, cy / sc, r / sc), v
    if best is None:
        return None
    if verbose:
        print("    [定位-Hough] r=%.1f  打分=%.2f  候选=%d（1/2 尺度打分）"
              % (best[2], bv, min(len(cs[0]), HOUGH_MAX_CAND)))
    return best[0], best[1], best[2], dict(thr=None, mask=None, area=0,
                                           r_enc=best[2], r_eq=best[2],
                                           ecc=1.0, ok=True, via="hough")


# ---------------------------------------------------------------- 裁剪与 FOV
def crop_fundus(bgr, circle, out=(DIG_W, DIG_H), disc_r=DISC_R):
    """把照片里的眼底圆重采样到 700x605（数字图坐标系）。

    circle = (cx, cy, r)。映射关系：照片中半径 r 的圆 -> 输出中半径 disc_r 的圆。
    """
    cx, cy, r = circle[0], circle[1], circle[2]
    s = float(r) / float(disc_r)
    ow, oh = out
    M = np.float32([[1.0 / s, 0.0, ow / 2.0 - cx / s],
                    [0.0, 1.0 / s, oh / 2.0 - cy / s]])
    return cv2.warpAffine(bgr, M, (ow, oh), flags=cv2.INTER_CUBIC)


def circle_fov(shape=(DIG_H, DIG_W), r=DISC_R, shrink=1.0):
    """直接用定位到的圆生成 FOV（替代 Otsu 版 gen_fov）。

    shrink<1 可把 FOV 略微收进去，避开圆边缘的纸边/胶痕。
    """
    h, w = shape
    f = np.zeros((h, w), np.uint8)
    cv2.circle(f, (w // 2, h // 2), int(round(r * shrink)), 255, -1)
    return f


def auto_crop_and_fov(bgr, rb_thr=None, shrink=1.0, verbose=False, **kw):
    """一步到位：定位 -> 裁剪 -> 生成 FOV。

    返回 (crop, fov, circle, info)；定位失败返回 None。

    裁剪用的是「按长轴的相似变换」。这在正对拍时是几何正确的：眼底照在数字图
    里本来就是 647x553 的椭圆（轴比约 1.17，STARE 各图在 1.17~1.25 之间），
    照片里若保持同样形状，按长轴归一后短轴会自动落到正确位置。

    ⚠ 已知限制：相机倾斜会让画面里的椭圆轴比改变，相似变换补不回来。
      实测倾斜 20° 时 Dice 从 0.60 掉到 0.32，30° 掉到 0.21。
      尝试过各向异性校正，但因为轴比估计本身有约 4% 的系统偏差，
      校正后反而更差，所以没有内置。**拍摄时请正对眼底圆。**
    """
    _t = time.perf_counter()
    _via = "颜色分割"
    res = locate_fundus(bgr, rb_thr=rb_thr, verbose=verbose, **kw)
    if res is None:
        _via = "Hough 兜底"
        res = locate_hough(bgr, verbose=verbose)
        if res is None:
            if verbose:
                print("    [定位] 颜色分割与 Hough 都失败")
            return None
    _t_loc = (time.perf_counter() - _t) * 1000.0
    cx, cy, r, info = res
    _t = time.perf_counter()
    crop = crop_fundus(bgr, (cx, cy, r))
    fov = circle_fov((crop.shape[0], crop.shape[1]), DISC_R, shrink)
    if verbose:
        print("    [定位] 定位 %.0f ms（%s）+ 裁剪 %.0f ms"
              % (_t_loc, _via, (time.perf_counter() - _t) * 1000.0))
    return crop, fov, (cx, cy, r), info
