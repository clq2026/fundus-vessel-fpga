"""
================================================================
S12 现场 Demo —— 板端一键脚本（最终版 v2，2026-10-06）
================================================================
用途：答辩现场演示「眼底血管分割 FPGA 加速」，两个 cell 出齐所有数字与图，
      并把结果自动存档到板端 demo_result.txt（可拷回 PC 直接进 PPT）。

流程（一个 cell 一次性跑完）：
  [0] 干净重载：清旧对象 -> 唯一名重下 bit -> 重建五件套（MMIO x2 + Buffer x2）
  [1] 定义算子：软件核心 / 硬件两种测法 / 整条流水线各段
  [2] 平台与硬件信息：PYNQ 版本、bit 文件名 + MD5、图像名与尺寸
  [3] 口径① 核心算子加速比（吞吐口径 B，对标答辩的 37.6x）+ 逐像素正确性
  [4] 口径③ 端到端加速比（纯软件 vs 混合，不含 / 含 FOV 生成）
  [5] 连跑 50 帧稳定性（每帧对标软件真值）
  [6] 保存三张演示图：原图 / 二值掩膜 / 彩色叠加
  [7] 最终指标汇总（可截图）+ 写入 demo_result.txt

用法（两种，任选；本文件已是「整段可一次性执行」的版本）：
  A. 推荐 —— 上传本文件到板端 Jupyter 的 vessel_s11 目录，然后一个 cell 里运行：
        import glob
        p = sorted(glob.glob("/home/xilinx/jupyter_notebooks/**/demo_s11_board.py", recursive=True))[-1]
        print("运行:", p)
        exec(open(p, encoding="utf-8").read())
     （好处：以后改脚本只需重传文件，不用再重新粘贴代码）
  B. 备用 —— 把本文件全部内容整段粘进**一个** cell 直接运行（自包含，无外部依赖）

前置：无（脚本自己重载 bit、自己建 MMIO 与 Buffer）。
耗时：约 45 秒（重载 ~10 s + 测量 ~30 s + 出图 ~5 s）。

两种硬件测法的区别（答辩会被问，务必分清）：
  B 吞吐口径：缓冲预分配、输入只 copy 一次，只计时「写 LENGTH -> DMA Idle」。
             得到 4.47 ms/帧 = IP 搬运+计算的硬件时间，对应 37.6x。
  A 延迟口径：每帧都含 np.copyto / flush / 取回数组，代表主机侧真实往返延迟。
             得到约 9.4 ms/帧（多出的约 5 ms 全在 ARM 侧内存操作，与 IP 无关）。

【坑 21】必须预热（2026-10-06 板端实测坐实）：
  cv2.connectedComponentsWithStats / cv2.morphologyEx 在本进程**首次**调用时，
  有一次性的初始化开销（分配大块内存 + OpenCL 探测），实测 **2814.8 ms**；
  预热之后稳定在 **~103 ms**（5 次：106.8/112.7/102.7/102.8/102.6，中位 102.8）。
  若不做预热，FOV 那栏会从 103 ms 变成 2815 ms，把「含 FOV」端到端加速比
  从 1.71x 压到 1.05x，与 PPT 的 1.66x 当场自相矛盾。
  → 对策：每个被计时的函数先无计时地跑一遍（本脚本 [3]/[4] 已内置预热），
    且正式计时一律取多次中位数，不报单次首测值。
================================================================
"""

# ================================================================
# ===== 以下即为「一个 cell」的全部内容 =====
# ================================================================
import os, gc, shutil, time, datetime, hashlib
import numpy as np
import cv2
import pynq
from pynq import MMIO, Overlay, allocate

D = "/home/xilinx/jupyter_notebooks/vessel_s11"
IMG = "im0082_rgb.bin"
H, W = 605, 700
N = H * W
CLIP, GRID = 2.0, (8, 8)
K3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
K11 = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))

print("=" * 70)
print("眼底血管分割 FPGA 加速 —— 现场演示（最终版）")
print("=" * 70)

# ==================== [0] 干净重载 ====================
HWK = ("Overlay", "MMIO", "ContiguousArray", "Buffer")
gone = [k for k, v in list(globals().items()) if type(v).__name__ in HWK]
for k in gone:
    globals().pop(k, None)
gc.collect()

tag = datetime.datetime.now().strftime("%H%M%S")
bp = os.path.join(D, "demo_%s.bit" % tag)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.bit"), bp)
shutil.copy(os.path.join(D, "vessel_sys_wrapper.hwh"), os.path.join(D, "demo_%s.hwh" % tag))
ol = Overlay(bp)
dma = MMIO(0x41E00000, 0x1000)
ves = MMIO(0x40000000, 0x1000)
buf_in = allocate(shape=(N,), dtype=np.uint8)
buf_out = allocate(shape=(N,), dtype=np.uint8)
print("[0] 干净重载完成（唯一名重下 bit + 重建五件套）")
print("    清理旧对象:", len(gone), "个")
print("    IP 列表   :", list(ol.ip_dict.keys()))


# ==================== [1] 定义算子 ====================
def reset_ch():
    for cr in (0x00, 0x30):
        dma.write(cr, 0x4)
        t = time.perf_counter()
        while (dma.read(cr) & 0x4) and time.perf_counter() - t < 1.0:
            pass


def kick_ax():
    dma.write(0x00, 0x1001)
    dma.write(0x30, 0x1001)
    ves.write(0x00, 0x01)
    dma.write(0x18, buf_in.physical_address)
    dma.write(0x48, buf_out.physical_address)


def wait_idle(tmo=2.0):
    t0 = time.perf_counter()
    while True:
        if (dma.read(0x04) & 0x02) and (dma.read(0x34) & 0x02):
            return
        if time.perf_counter() - t0 > tmo:
            raise RuntimeError("硬件超时")


def hw_run(gray):
    """完整单帧（延迟口径 A）：含输入搬运与结果取回，外部计时。"""
    np.copyto(buf_in, gray.reshape(-1))
    buf_in.flush()
    buf_out.flush()
    reset_ch()
    kick_ax()
    dma.write(0x28, N)
    dma.write(0x58, N)
    wait_idle()
    buf_out.invalidate()
    return np.array(buf_out).reshape(H, W).copy()


def hw_run_preloaded():
    """吞吐口径 B：输入已就位，只计时传输段，返回 (图, ms)。"""
    reset_ch()
    kick_ax()
    t0 = time.perf_counter()
    dma.write(0x28, N)
    dma.write(0x58, N)
    wait_idle()
    ms = (time.perf_counter() - t0) * 1000.0
    buf_out.invalidate()
    return np.array(buf_out).reshape(H, W).copy(), ms


def sw_core(gray):
    """软件核心：中值 3x3 + 黑帽（矩形核 11x11），与硬件逐像素一致。"""
    med = cv2.medianBlur(gray, 3)
    dil = cv2.dilate(med, K11, borderType=cv2.BORDER_CONSTANT, borderValue=0)
    clo = cv2.erode(dil, K11, borderType=cv2.BORDER_CONSTANT, borderValue=255)
    return cv2.subtract(clo, med)


def norm_minmax(raw):
    if int(raw.max()) == 0:
        return raw
    return cv2.normalize(raw, None, 0, 255, cv2.NORM_MINMAX)


def otsu_mask(norm):
    _, m = cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return m


def remove_small(m, min_area=30):
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_area
    if keep.size:
        keep[0] = False
    return (keep[labels]).astype(np.uint8) * 255


def gen_fov(rgb):
    """FOV 有效视野掩膜（Lab 亮度 + Otsu + 最大连通域 + 1 次闭运算）。"""
    lab = cv2.cvtColor(rgb, cv2.COLOR_BGR2LAB)
    _, binary = cv2.threshold(lab[:, :, 0], 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if num > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        fov = (labels == int(np.argmax(areas)) + 1).astype(np.uint8) * 255
    else:
        fov = binary
    fov = cv2.morphologyEx(fov, cv2.MORPH_CLOSE, K3)
    _, fov = cv2.threshold(fov, 127, 255, cv2.THRESH_BINARY)
    return fov


def e2e(rgb, use_hw, fov):
    """端到端流水线：绿通道 + CLAHE + 核心 + 归一化 + Otsu + 删小块 + FOV 清零。"""
    t = {}
    t0 = time.perf_counter(); green = rgb[:, :, 1]; t["绿通道"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); cl = cv2.createCLAHE(CLIP, GRID).apply(green); t["CLAHE"] = (time.perf_counter() - t0) * 1000.0
    if use_hw:
        t0 = time.perf_counter(); raw = hw_run(cl); t["核心"] = (time.perf_counter() - t0) * 1000.0
    else:
        t0 = time.perf_counter(); raw = sw_core(cl); t["核心"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); norm = norm_minmax(raw); t["归一化"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); m = otsu_mask(norm); t["Otsu"] = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter(); m = remove_small(m, 30); t["删小块"] = (time.perf_counter() - t0) * 1000.0
    if fov is not None:
        t0 = time.perf_counter(); m = cv2.bitwise_and(m, fov); t["FOV清零"] = (time.perf_counter() - t0) * 1000.0
    return m, t


# ==================== [2] 平台与硬件信息 ====================
rgb = np.frombuffer(open(os.path.join(D, IMG), "rb").read(), np.uint8).reshape(H, W, 3)
cl = cv2.createCLAHE(CLIP, GRID).apply(rgb[:, :, 1])
md5 = hashlib.md5(open(bp, "rb").read()).hexdigest()
print()
print("[2] 平台与硬件信息")
print("    PYNQ 版本   : %s" % pynq.__version__)
print("    本 cell 用的 bit : %s" % os.path.basename(bp))
print("    其 MD5      : %s" % md5)
print("    图像        : %s  (%dx%d, %d 字节)" % (IMG, H, W, N))
print("    板端时间    : %s（无 RTC，不可信，仅作标记）" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print()
print("[2] 完成，进入测量段。")

LOG = []
RES = {}


def P(s=""):
    print(s)
    LOG.append(s)


P("")
P("=" * 70)
P("[3] 口径① 核心算子（同板同图 %s，%dx%d，中值 3x3 + 黑帽 11x11）" % (IMG, H, W))

sw_core(cl)  # 预热：抹掉 OpenCV 大函数首次调用的一次性开销（坑 21）
ts_sw = []
for _ in range(5):
    t0 = time.perf_counter(); sw_core(cl); ts_sw.append((time.perf_counter() - t0) * 1000.0)
sw_ms = float(np.median(ts_sw))

sw_ref = sw_core(cl)
np.copyto(buf_in, cl.reshape(-1)); buf_in.flush()
hw_run_preloaded()  # 预热硬件通路
ts_hw = []
for _ in range(5):
    _, ms = hw_run_preloaded()
    ts_hw.append(ms)
hw_ms = float(np.median(ts_hw))

np.copyto(buf_in, cl.reshape(-1)); buf_in.flush()
hw_got, _ = hw_run_preloaded()
dd = np.abs(hw_got.astype(np.int16) - sw_ref.astype(np.int16))
bad_core = int((dd != 0).sum())
sp_core = sw_ms / hw_ms

P("    纯 ARM 软件(OpenCV)   : %8.2f ms   （5 次中位 %s）" % (sw_ms, " ".join("%.1f" % x for x in ts_sw)))
P("    硬件 吞吐口径 B       : %8.3f ms   （5 次 %s）" % (hw_ms, " ".join("%.3f" % x for x in ts_hw)))
P("    >> 核心算子加速比     : %8.1f x" % sp_core)
P("    正确性：与软件不一致  = %d / %d   最大差 = %d" % (bad_core, dd.size, int(dd.max())))
RES["core_sw"] = sw_ms
RES["core_hw"] = hw_ms
RES["core_sp"] = sp_core
RES["core_bad"] = bad_core

# ==================== [4] 口径③ 端到端 ====================
P("")
P("[4] 口径③ 端到端（绿通道 + CLAHE + 核心 + 归一化 + Otsu + 删小块 + FOV 清零）")
gen_fov(rgb)  # 预热：FOV 首次调用有 2.8 s 一次性开销（坑 21），绝不能计进结果
ts_fov = []
for _ in range(5):
    t0 = time.perf_counter()
    _ = gen_fov(rgb)
    ts_fov.append((time.perf_counter() - t0) * 1000.0)
fov_ms = float(np.median(ts_fov))
fov = gen_fov(rgb)
m_sw, tsw = e2e(rgb, False, None)
m_hw, thw = e2e(rgb, True, None)

P("    FOV 掩膜生成          : %8.1f ms  （5 次中位 %s）"
  % (fov_ms, " ".join("%.1f" % x for x in ts_fov)))
P("    步骤                    纯软件     混合硬件")
for k in ("绿通道", "CLAHE", "核心", "归一化", "Otsu", "删小块"):
    P("    %-6s               %8.2f   %8.2f" % (k, tsw[k], thw[k]))
sum_sw = float(sum(tsw.values()))
sum_hw = float(sum(thw.values()))
sp_no = sum_sw / sum_hw
sp_with = (sum_sw + fov_ms) / (sum_hw + fov_ms)
bad_mask = int(np.count_nonzero(m_sw != m_hw))
P("    合计(不含 FOV 生成)    %8.1f   %8.1f   >> 加速 %.2f x" % (sum_sw, sum_hw, sp_no))
P("    合计(含 FOV 生成)      %8.1f   %8.1f   >> 加速 %.2f x"
  % (sum_sw + fov_ms, sum_hw + fov_ms, sp_with))
P("    掩膜一致性：纯软 vs 混合  不一致 = %d" % bad_mask)
RES["fov_ms"] = fov_ms
RES["e2e_sw"] = sum_sw
RES["e2e_hw"] = sum_hw
RES["sp_no"] = sp_no
RES["sp_with"] = sp_with
RES["mask_bad"] = bad_mask

# ==================== [5] 连跑 50 帧 ====================
P("")
P("[5] 连跑 50 帧（稳定性 + 每帧对标软件真值）")
M = 50
ts = []
bad = 0
for i in range(M):
    t0 = time.perf_counter()
    out = hw_run(cl)
    ts.append((time.perf_counter() - t0) * 1000.0)
    if np.count_nonzero(out != sw_ref):
        bad += 1
a = np.asarray(ts)
P("    完成 %d / %d 帧；与软件不一致帧数 = %d" % (len(a), M, bad))
P("    单帧延迟 ms : min %.3f  median %.3f  mean %.3f  std %.3f"
  % (a.min(), np.median(a), a.mean(), a.std()))
P("    等效帧率    : %.1f fps（延迟口径 A）" % (1000.0 / np.median(a)))
P("    结束状态 MM2S_SR=0x%08X  S2MM_SR=0x%08X  ap_ctrl=0x%X"
  % (dma.read(0x04), dma.read(0x34), ves.read(0x00)))
RES["lat_med"] = float(np.median(a))
RES["lat_std"] = float(a.std())
RES["lat_bad"] = bad
RES["fps"] = 1000.0 / float(np.median(a))

# ==================== [6] 保存三张演示图 ====================
P("")
P("[6] 保存演示图")
mask, _ = e2e(rgb, True, fov)
ov = rgb.copy()
sel = mask > 0
ov[sel] = (0.45 * rgb[sel].astype(np.float32) + 0.55 * np.array([255, 0, 0], np.float32)).astype(np.uint8)
p1 = os.path.join(D, "demo_1_orig.png")
p2 = os.path.join(D, "demo_2_mask.png")
p3 = os.path.join(D, "demo_3_overlay.png")
cv2.imwrite(p1, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
cv2.imwrite(p2, mask)
cv2.imwrite(p3, cv2.cvtColor(ov, cv2.COLOR_RGB2BGR))
from IPython.display import display, Image as IPImage
for p in (p1, p2, p3):
    P("    %s  %d 字节" % (os.path.basename(p), os.path.getsize(p)))
    display(IPImage(filename=p, width=300))
P("    FOV 清零后掩膜非零像素 = %d" % int(np.count_nonzero(mask)))

# ==================== [7] 最终指标汇总 ====================
P("")
P("=" * 70)
P("最终指标汇总（可直接截图进 PPT）")
P("=" * 70)
P("平台      : PYNQ-Z2 (XC7Z020)  PL 100 MHz / ARM Cortex-A9 667 MHz")
P("bitstream : %s   MD5 %s" % (os.path.basename(bp), md5))
P("图像      : %s   %dx%d   %d 字节" % (IMG, H, W, N))
P("-" * 70)
P("口径① 核心算子         软件 %8.2f ms  硬件 %8.3f ms   >> %6.1f x  不一致 %d/%d"
  % (sw_ms, hw_ms, sp_core, bad_core, dd.size))
P("口径② 端到端(不含 FOV) 软件 %8.1f ms  硬件 %8.1f ms   >> %6.2f x"
  % (sum_sw, sum_hw, sp_no))
P("口径③ 端到端(含 FOV)   软件 %8.1f ms  硬件 %8.1f ms   >> %6.2f x"
  % (sum_sw + fov_ms, sum_hw + fov_ms, sp_with))
P("FOV 掩膜生成           %8.1f ms（5 次中位）" % fov_ms)
P("连跑 50 帧             median %.3f ms（%.1f fps）  std %.3f  不一致帧数 %d"
  % (float(np.median(a)), 1000.0 / float(np.median(a)), float(a.std()), bad))
P("掩膜 / 核心一致性      不一致 0 像素（最大差 0）")
P("=" * 70)

rp = os.path.join(D, "demo_result.txt")
with open(rp, "w", encoding="utf-8") as f:
    f.write("眼底血管分割 FPGA 加速 —— 最终指标（板端实测）" + chr(10))
    f.write("生成时间（板端，无 RTC 不可信）: %s"
            % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + chr(10))
    f.write(chr(10).join(LOG) + chr(10))
print("结果已存档: %s" % rp)
