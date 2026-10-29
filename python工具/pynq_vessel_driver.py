"""
眼底血管分割加速器 —— PYNQ-Z2 板端驱动
=========================================

算法：V2 定稿版（中值滤波 3x3 + 黑帽形态学 11x11），全部在 PL 里由 Vitis HLS IP 完成。
平台：PYNQ-Z2（Zynq-7020）+ AXI DMA（Simple 模式）+ 自定义 HLS IP。

已实测验证：整帧 605x700 = 423500 字节，FPGA 输出与软件参考**逐像素完全一致（0 误差）**。

------------------------------------------------------------
★ 启动顺序铁律（写错会"一个字节都不动"，且看不出任何报错）
------------------------------------------------------------
    ① DMA 两个通道 DMACR.RS = 1      （先让通道 Run）
    ② HLS IP 写 ap_start = 1
    ③ 写 MM2S_SA / S2MM_DA           （源、目的物理地址）
    ④ 写 MM2S_LENGTH / S2MM_LENGTH   ← 这一步才真正触发传输
    ⑤ 轮询 DMA SR 的 bit1（Idle）= 1

    LENGTH 必须在 RS=1 之后写。通道还没 Run 时写 LENGTH，触发脉冲会被直接丢弃。

★ 完成判据：DMA 状态寄存器的 **bit1 Idle**（`SR & 0x02`）。
    不要用 bit0 Halted，也不要用 bit9 IOC_Irq（那是中断用的，轮询会永远等不到）。

------------------------------------------------------------
板端用法（Jupyter notebook）
------------------------------------------------------------
    import sys
    sys.path.insert(0, '/home/xilinx/jupyter_notebooks/vessel_s11')
    from pynq_vessel_driver import VesselAccel
    import numpy as np

    acc = VesselAccel('/home/xilinx/jupyter_notebooks/vessel_s11/vessel_sys_wrapper.bit')
    inp = np.fromfile('im0082_in.bin', dtype=np.uint8)     # 423500 字节
    out = acc.run(inp)                                     # 返回 np.uint8 / 423500
    print('本帧耗时 %.3f ms' % acc.last_ms)

    # 与参考结果逐像素核对
    ref = np.fromfile('im0082_ref.bin', dtype=np.uint8)
    d = np.abs(out.astype(np.int16) - ref.astype(np.int16))
    print('不一致像素 =', int((d != 0).sum()), ' 最大差 =', int(d.max()))

    # 测平均耗时（先预热一次）
    acc.run(inp)
    ts = [acc.run(inp) or acc.last_ms for _ in range(5)]
    print('平均 %.3f ms' % (sum(ts) / len(ts)))
"""

import os
import shutil
import time

import numpy as np
from pynq import Overlay, allocate, MMIO

# ---------------- 板级常量（与本项目 Block Design 一致，勿随意改） ----------------
IMG_H, IMG_W = 605, 700
FRAME_BYTES = IMG_H * IMG_W            # 423500 字节

DMA_BASE = 0x41E00000                  # axi_dma_0 的控制口
VESSEL_BASE = 0x40000000               # vessel_top_0 的控制口

# MM2S（发，PS->PL）/ S2MM（收，PL->PS）的寄存器偏移
MM2S_CR, MM2S_SR, MM2S_SA, MM2S_LEN = 0x00, 0x04, 0x18, 0x28
S2MM_CR, S2MM_SR, S2MM_DA, S2MM_LEN = 0x30, 0x34, 0x48, 0x58

CR_RS = 0x0001                         # Run/Stop 位
CR_IOC_IRQ_EN = 0x1000                 # 完成中断使能（本项目不接中断线，开着无害）
CR_RESET = 0x0004                      # 软复位（写后自清）

_SR_BITS = ['Halted', 'Idle', 'SGIncld', 'DMAIntErr', 'DMASlvErr', 'DMADecErr',
            'SGIntErr', 'SGSlvErr', 'SGDecErr', 'IOC_Irq', 'Dly_Irq', 'Err_Irq']


def decode_sr(sr):
    """把 DMA 状态寄存器的值翻译成位名，例：0x1002 -> 'Idle,IOC_Irq'。"""
    return ','.join(_SR_BITS[i] for i in range(12) if sr & (1 << i)) or 'all-zero'


def decode_ap_ctrl(v):
    """把 HLS IP 的 ap_ctrl 翻译成位名。0xE = 一帧完成的最健康状态。"""
    names = []
    if v & 0x02:
        names.append('ap_done')
    if v & 0x04:
        names.append('ap_idle')
    if v & 0x08:
        names.append('ap_ready')
    return ','.join(names) or 'busy(ap_idle=0)'


class VesselAccel:
    """眼底血管分割加速器驱动（单帧模式）。"""

    _load_seq = 0                      # 计数器：让每次加载的临时文件名都不同

    def __init__(self, bit_path, dma_base=DMA_BASE, ip_base=VESSEL_BASE, timeout=2.0):
        """
        Parameters
        ----------
        bit_path : str
            .bit 文件路径（同目录下应有同名的 .hwh）。
        timeout : float
            单帧等待超时（秒），默认 2 秒；正常一帧只需几毫秒。
        """
        self.timeout = timeout
        self.bit_path = bit_path
        self.overlay = self._load_overlay(bit_path)
        self.dma = MMIO(dma_base, 0x1000)
        self.ip = MMIO(ip_base, 0x1000)
        # 复用缓冲区：DMA 只认物理地址，必须用 allocate 拿物理连续内存
        self._tx = allocate(shape=(FRAME_BYTES,), dtype=np.uint8)
        self._rx = allocate(shape=(FRAME_BYTES,), dtype=np.uint8)
        self.last_ms = None

    # ------------------------------------------------------------------ 加载
    @classmethod
    def _load_overlay(cls, bit_path):
        """把 bit/hwh 复制成新文件名再加载。

        必须这样做：PYNQ 的 Overlay 有「文件名和当前已加载的相同就跳过下载」的短路逻辑，
        而本项目的工作方式是"原地删除旧文件 + 同名重传"，文件名完全一样
        → PYNQ 会以为已经加载过，PL 里跑的仍然是旧硬件。
        """
        cls._load_seq += 1
        root, ext = os.path.splitext(bit_path)
        run_bit = '%s_run%d%s' % (root, cls._load_seq, ext)
        run_hwh = '%s_run%d.hwh' % (root, cls._load_seq)
        shutil.copy(bit_path, run_bit)
        src_hwh = root + '.hwh'
        if os.path.exists(src_hwh):
            shutil.copy(src_hwh, run_hwh)      # PYNQ 要求 .bit 与 .hwh 前缀同名
        return Overlay(run_bit)

    # ------------------------------------------------------------------ 复位
    def _reset_channels(self):
        for cr in (MM2S_CR, S2MM_CR):
            self.dma.write(cr, CR_RESET)
            t0 = time.perf_counter()
            while (self.dma.read(cr) & CR_RESET) and (time.perf_counter() - t0) < 1.0:
                pass

    # ------------------------------------------------------------------ 主流程
    def run(self, frame):
        """把一整帧送进 PL 处理并取回结果。

        Parameters
        ----------
        frame : np.ndarray
            uint8 数组，长度必须是 FRAME_BYTES（423500）。

        Returns
        -------
        np.ndarray
            uint8 结果数组（长度 423500）。同时把本帧耗时写入 self.last_ms。
        """
        frame = np.ascontiguousarray(frame, dtype=np.uint8).reshape(-1)
        if frame.size != FRAME_BYTES:
            raise ValueError('帧长度应为 %d 字节，实际 %d' % (FRAME_BYTES, frame.size))

        dma, ip = self.dma, self.ip
        self._reset_channels()

        np.copyto(self._tx, frame)
        self._tx.flush()                   # 把 CPU 写的数据刷到 DDR（DMA 读的是 DDR）
        self._rx.flush()

        # ① 先让两个通道 Run（必须早于写 LENGTH）
        dma.write(MM2S_CR, CR_RS | CR_IOC_IRQ_EN)
        dma.write(S2MM_CR, CR_RS | CR_IOC_IRQ_EN)
        # ② 启动 HLS IP
        ip.write(0x00, 0x01)
        # ③ 写地址
        dma.write(MM2S_SA, self._tx.physical_address)
        dma.write(S2MM_DA, self._rx.physical_address)

        # ④ 写长度 —— 这一步才触发传输，计时从这里开始
        t0 = time.perf_counter()
        dma.write(MM2S_LEN, FRAME_BYTES)
        dma.write(S2MM_LEN, FRAME_BYTES)

        # ⑤ 等两个通道都进入 Idle（bit1）
        while True:
            s = dma.read(MM2S_SR)
            r = dma.read(S2MM_SR)
            if (s & 0x02) and (r & 0x02):
                break
            if time.perf_counter() - t0 > self.timeout:
                break
        self.last_ms = (time.perf_counter() - t0) * 1000

        self._rx.invalidate()              # 丢掉 CPU cache 里的旧数据
        return np.array(self._rx)

    # ------------------------------------------------------------------ 诊断
    def status(self):
        """打印当前 DMA 与 IP 的状态，出问题时用来定位。"""
        s = self.dma.read(MM2S_SR)
        r = self.dma.read(S2MM_SR)
        return {
            'MM2S_SR': '0x%08X (%s)' % (s, decode_sr(s)),
            'S2MM_SR': '0x%08X (%s)' % (r, decode_sr(r)),
            'MM2S_LEN_remaining': self.dma.read(MM2S_LEN),
            'S2MM_LEN_remaining': self.dma.read(S2MM_LEN),
            'ap_ctrl': '0x%X (%s)' % (self.ip.read(0x00), decode_ap_ctrl(self.ip.read(0x00))),
            'last_ms': self.last_ms,
        }

    def benchmark(self, frame, times=5, warmup=1):
        """测单帧平均耗时（毫秒）。

        首次运行含分配/缺页抖动，所以默认先热身 1 次再统计。
        """
        for _ in range(warmup):
            self.run(frame)
        ts = []
        for _ in range(times):
            self.run(frame)
            ts.append(self.last_ms)
        return {'avg_ms': sum(ts) / len(ts), 'min_ms': min(ts), 'max_ms': max(ts), 'runs': ts}
